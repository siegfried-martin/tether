"""Published ICS feeds, such as an Outlook calendar (spec: "Fetching", "Measured feed").

The feed is fetched whole, then expanded with recurring-ical-events, which
applies RRULE, EXDATE and RECURRENCE-ID overrides. Outlook writes Windows
time zone names (TZID:Eastern Standard Time); icalendar resolves them through
the VTIMEZONE blocks in the feed and its Windows-to-IANA map.

The feed URL is a secret: it is never logged or stored, and neither is the feed.
"""

import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from tether.connectors.events import Instance, Window, fmt

# Outlook's free/busy field; the only place it says whether an invite was accepted.
BUSY_STATUS = {"BUSY": "busy", "TENTATIVE": "tentative", "FREE": "free", "OOF": "oof", "WORKINGELSEWHERE": "busy"}


class FeedError(Exception):
    """The feed could not be fetched or parsed. The message never contains the URL."""


async def fetch(http: httpx.AsyncClient, url: str) -> bytes:
    try:
        resp = await http.get(url, timeout=60, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise FeedError(f"couldn't reach the feed ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise FeedError(f"the feed answered HTTP {resp.status_code}")
    if b"BEGIN:VCALENDAR" not in resp.content[:4096]:
        raise FeedError("the feed did not return a calendar")
    return resp.content


def feed_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse(data: bytes, window: Window) -> list[Instance]:
    """Expand the feed into instances starting inside the window. Raises FeedError if unparseable."""
    try:
        cal = icalendar.Calendar.from_ical(data)
        # A day of margin either side: all-day and zone-shifted starts are filtered exactly below.
        occurrences = recurring_ical_events.of(cal).between(
            window.first_day - timedelta(days=1), window.last_day + timedelta(days=1))
        instances = [i for i in (_instance(ev, window.tz) for ev in occurrences) if i]
    except FeedError:
        raise
    except Exception as exc:  # a malformed feed must never reach reconcile (delete guard 1)
        raise FeedError(f"couldn't parse the feed ({type(exc).__name__})") from None
    return [i for i in instances if window.contains(i.start, i.all_day)]


def _instance(ev, tz: ZoneInfo) -> Instance | None:
    if str(ev.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start_value = ev.decoded("DTSTART")
    all_day = not isinstance(start_value, datetime)
    if "DTEND" in ev:
        end_value = ev.decoded("DTEND")
    elif "DURATION" in ev:
        end_value = start_value + ev.decoded("DURATION")
    else:
        end_value = start_value + timedelta(days=1) if all_day else start_value
    if all_day and isinstance(end_value, datetime):
        end_value = end_value.date()
    start = fmt(start_value, tz)
    original = ev.decoded("RECURRENCE-ID") if "RECURRENCE-ID" in ev else start_value
    if all_day and isinstance(original, datetime):
        original = original.date()
    busy = BUSY_STATUS.get(str(ev.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper())
    if busy is None:
        busy = "free" if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT" else "busy"
    modified = ev.get("LAST-MODIFIED") or ev.get("DTSTAMP")
    return Instance(
        uid=str(ev.get("UID", "")) or f"nouid:{start}:{ev.get('SUMMARY', '')}",
        instance_start=fmt(original, tz),
        start=start,
        end=fmt(end_value, tz),
        all_day=all_day,
        title=_text(ev.get("SUMMARY")),
        location=_text(ev.get("LOCATION")) or None,
        description=_text(ev.get("DESCRIPTION")) or None,
        modified_at=fmt(modified.dt, tz) if modified is not None and isinstance(modified.dt, (datetime, date)) else None,
        busy=busy,
    )


def _text(value) -> str:
    return str(value).strip() if value is not None else ""
