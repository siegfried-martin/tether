"""Google Calendar: read source calendars, write the hub calendar (spec: "Calendar hub").

Sources are fetched as the full window with singleEvents=true, so recurring
series arrive as single instances. The instance key is iCalUID plus the
original start time.
"""

from datetime import datetime
from typing import Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

from tether.connectors.events import Instance, fmt
from tether.connectors.google_api import GoogleClient
from tether.db import iso, parse_iso

API = "https://www.googleapis.com/calendar/v3"
SKIPPED_TYPES = {"workingLocation"}   # "Home"/"Office" markers, not events


class CalendarApi(Protocol):
    async def list_calendars(self) -> list[dict]: ...
    async def list_events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]: ...
    async def insert_event(self, calendar_id: str, body: dict) -> bool: ...
    async def update_event(self, calendar_id: str, event_id: str, body: dict) -> bool: ...
    async def restore_event(self, calendar_id: str, event_id: str) -> None:
        """Bring back an event deleted moments ago: Google keeps it, cancelled, until it is purged."""
        resp = await self._request("PATCH", f"{self._cal(calendar_id)}/events/{quote(event_id, safe='')}",
                                   params={"sendUpdates": "none"}, json={"status": "confirmed"})
        resp.raise_for_status()

    async def delete_event(self, calendar_id: str, event_id: str, *, send_updates: str | None = None) -> None: ...
    async def create_event(self, calendar_id: str, body: dict, *, conference: bool, send_updates: str) -> dict: ...
    async def restore_event(self, calendar_id: str, event_id: str) -> None: ...


class CalendarClient(GoogleClient):
    API = API
    SERVICE = "Google Calendar"
    MIN_INTERVAL = 0.2
    FORBIDDEN_IS_AUTH = False

    @staticmethod
    def _cal(calendar_id: str) -> str:
        return f"/calendars/{quote(calendar_id, safe='')}"

    async def list_calendars(self) -> list[dict]:
        found, page = [], None
        while True:
            params = {"maxResults": 250, **({"pageToken": page} if page else {})}
            resp = await self._get("/users/me/calendarList", params)
            resp.raise_for_status()
            body = resp.json()
            found += body.get("items", [])
            page = body.get("nextPageToken")
            if not page:
                return found

    async def list_events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
        found, page = [], None
        while True:
            params = {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(), "singleEvents": "true",
                      "maxResults": 2500, "showDeleted": "false", **({"pageToken": page} if page else {})}
            resp = await self._get(f"{self._cal(calendar_id)}/events", params)
            resp.raise_for_status()
            body = resp.json()
            found += body.get("items", [])
            page = body.get("nextPageToken")
            if not page:
                return found

    async def insert_event(self, calendar_id: str, body: dict) -> bool:
        """Insert with a client-chosen ID. False when that ID already exists (409), even as cancelled."""
        resp = await self._request("POST", f"{self._cal(calendar_id)}/events", json=body)
        if resp.status_code == 409:
            return False
        resp.raise_for_status()
        return True

    async def update_event(self, calendar_id: str, event_id: str, body: dict) -> bool:
        """Replace an event. Restores a cancelled one when body carries status confirmed. False on 404/410."""
        resp = await self._request("PUT", f"{self._cal(calendar_id)}/events/{event_id}", json=body)
        if resp.status_code in (404, 410):
            return False
        resp.raise_for_status()
        return True

    async def create_event(self, calendar_id: str, body: dict, *, conference: bool, send_updates: str) -> dict:
        """A new event with a Google-chosen ID (an owner's own event, not a hub copy). Returns Google's event.
        conference asks Google to attach a Meet link; send_updates is "all" or "none" (guest emails)."""
        params = {"sendUpdates": send_updates, **({"conferenceDataVersion": 1} if conference else {})}
        resp = await self._request("POST", f"{self._cal(calendar_id)}/events", params=params, json=body)
        resp.raise_for_status()
        return resp.json()

    async def delete_event(self, calendar_id: str, event_id: str, *, send_updates: str | None = None) -> None:
        """Delete; an event already gone counts as deleted. send_updates ("all" or "none") decides whether
        guests are emailed a cancellation; left out, Google's default (none) applies."""
        params = {"sendUpdates": send_updates} if send_updates else None
        resp = await self._request("DELETE", f"{self._cal(calendar_id)}/events/{quote(event_id, safe='')}",
                                   params=params)
        if resp.status_code not in (404, 410):
            resp.raise_for_status()


def to_instance(event: dict, tz: ZoneInfo) -> Instance | None:
    """One events.list item as an Instance, or None when it should not reach the hub."""
    if event.get("status") == "cancelled" or event.get("eventType") in SKIPPED_TYPES:
        return None
    me = next((a for a in event.get("attendees", []) if a.get("self")), None)
    response = me.get("responseStatus") if me else None
    if response == "declined":
        return None
    start, all_day = _time(event["start"], tz)
    end, _ = _time(event.get("end", event["start"]), tz)
    original = event.get("originalStartTime")
    if event.get("eventType") == "outOfOffice":
        busy = "oof"
    elif event.get("transparency") == "transparent":
        busy = "free"
    elif response in ("tentative", "needsAction"):
        busy = "tentative"
    else:
        busy = "busy"
    description = event.get("description") or None
    join = event.get("hangoutLink")
    if join and (not description or join not in description):
        # Meet links live outside the description; carry them so the hub copy can be joined.
        description = f"{description}\n\nJoin: {join}" if description else f"Join: {join}"
    return Instance(
        uid=event.get("iCalUID") or event["id"],
        instance_start=_time(original, tz)[0] if original else start,
        start=start, end=end, all_day=all_day,
        title=event.get("summary") or "",
        location=event.get("location") or None,
        description=description,
        modified_at=iso(parse_iso(event["updated"])) if event.get("updated") else None,
        busy=busy,
        external_id=event.get("id"),
        series_id=event.get("recurringEventId"),
        organizer_self="organizer" not in event or bool(event["organizer"].get("self")),   # Google omits false
        guests=sum(1 for a in event.get("attendees", []) if not a.get("self") and not a.get("resource")),
    )


def _time(value: dict, tz: ZoneInfo) -> tuple[str, bool]:
    if "date" in value:
        return value["date"], True
    return fmt(datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")), tz), False


def event_body(event_id: str, *, title: str, start: str, end: str, all_day: bool, location: str | None,
               description: str | None, busy: str, content_hash: str) -> dict:
    """The hub copy as a Google event. status confirmed also revives a cancelled event on re-adoption."""
    def when(value: str) -> dict:
        return {"date": value} if all_day else {"dateTime": value}
    return {
        "id": event_id,
        "summary": title,
        "location": location or "",
        "description": description or "",
        "start": when(start),
        "end": when(end),
        "status": "confirmed",
        "transparency": "transparent" if busy == "free" else "opaque",
        "extendedProperties": {"private": {"tetherHash": content_hash}},
    }
