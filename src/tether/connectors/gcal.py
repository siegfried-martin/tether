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


    async def find_by_uid(self, calendar_id: str, ical_uid: str) -> list[dict]:
        """Events with this iCalendar UID: a series' master and its changed occurrences, or one event."""
        resp = await self._get(f"{self._cal(calendar_id)}/events", {"iCalUID": ical_uid, "showDeleted": "true"})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("items", [])

    async def get_event(self, calendar_id: str, event_id: str) -> dict | None:
        resp = await self._get(f"{self._cal(calendar_id)}/events/{quote(event_id, safe='')}")
        if resp.status_code in (400, 404, 410):
            return None
        resp.raise_for_status()
        return resp.json()

    async def respond(self, calendar_id: str, event_id: str, email: str, response: str) -> dict:
        """Set this calendar's owner's answer to an invite. attendeesOmitted leaves the other guests
        as they are; sendUpdates=all has Google email the organizer, as answering in Gmail does."""
        resp = await self._request("PATCH", f"{self._cal(calendar_id)}/events/{quote(event_id, safe='')}",
                                   params={"sendUpdates": "all"},
                                   json={"attendeesOmitted": True,
                                         "attendees": [{"email": email, "responseStatus": response}]})
        resp.raise_for_status()
        return resp.json()


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
    description = _with_details(event, event.get("description") or None)
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


MAX_GUESTS_LISTED = 15
RESPONSE_NOTE = {"declined": "declined", "tentative": "maybe"}


def _with_details(event: dict, description: str | None) -> str | None:
    """The description, with what Google shows beside it: the video call (Meet or an add-on such
    as Zoom keeps it outside the description) and the guest list. Put first, as Google does,
    so they survive the agenda's cut of long descriptions."""
    lines = []
    conference = event.get("conferenceData") or {}
    points = conference.get("entryPoints") or []
    video = next((p for p in points if p.get("entryPointType") == "video" and p.get("uri")), None)
    join = video["uri"] if video else event.get("hangoutLink")
    if join and join not in (description or ""):
        name = ((conference.get("conferenceSolution") or {}).get("name")
                or ("Google Meet" if "meet.google.com" in join else "the call"))
        lines.append(f"Join {name}: {join}")
        code = video and (video.get("meetingCode") or video.get("accessCode"))
        secret = video and (video.get("passcode") or video.get("password") or video.get("pin"))
        if code or secret:
            lines.append(" · ".join(filter(None, [code and f"Meeting ID: {code}", secret and f"Passcode: {secret}"])))
        phone = next((p for p in points if p.get("entryPointType") == "phone" and (p.get("label") or p.get("uri"))),
                     None)
        if phone:
            number = phone.get("label") or phone["uri"].removeprefix("tel:")
            pin = phone.get("pin") or phone.get("accessCode")
            lines.append(f"Phone: {number}" + (f" (PIN {pin})" if pin else ""))
    guests = [a for a in event.get("attendees", []) if not a.get("self") and not a.get("resource")]
    if guests:
        def who(a: dict) -> str:
            notes = ["organizer"] if a.get("organizer") else []
            if a.get("responseStatus") in RESPONSE_NOTE:
                notes.append(RESPONSE_NOTE[a["responseStatus"]])
            name = a.get("displayName") or a.get("email") or "?"
            return f"{name} ({', '.join(notes)})" if notes else name
        ordered = sorted(guests, key=lambda a: not a.get("organizer"))   # organizer first
        listed = ", ".join(who(a) for a in ordered[:MAX_GUESTS_LISTED])
        more = len(ordered) - MAX_GUESTS_LISTED
        lines.append(f"Guests ({len(guests)}): {listed}" + (f", and {more} more" if more > 0 else ""))
    if not lines:
        return description
    details = "\n".join(lines)
    return f"{details}\n\n{description}" if description else details


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
