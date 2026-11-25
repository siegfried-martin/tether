"""Calendar invitations inside Gmail threads: what the invite says, whether it is on the
invited account's calendar, and answering it from Tether.

The invite is read live from the thread's text/calendar part, like any body: it is never
stored or logged. The answer is written to the invited account's own Google Calendar with
sendUpdates=all, so Google emails the organizer just as answering in Gmail would.
"""

import base64
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import icalendar

from tether.connectors.gcal import CalendarClient
from tether.connectors.gmail import RateLimited
from tether.connectors.google_oauth import AuthError
from tether.db import iso
from tether.services import accounts, items
from tether.services.mail import MailError, calendar_parts
from tether.services.triage import GmailFactory

log = logging.getLogger(__name__)

RESPONSES = ("accepted", "tentative", "declined")
CalendarFactory = Callable[[int], CalendarClient]


# --------------------------------------------------------------------------- finding the invite


async def _ics(api, message: dict) -> bytes | None:
    for part in calendar_parts(message.get("payload", {})):
        body = part.get("body", {})
        data = body.get("data")
        if not data and body.get("attachmentId"):
            data = await api.get_attachment(message["id"], body["attachmentId"])
        if data:
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return None


# --------------------------------------------------------------------------- parsing


def _when(value, tz: ZoneInfo) -> tuple[str, bool]:
    """ISO start/end for the web: a bare date for all-day, else a UTC instant.
    A floating time (no zone) is read in the owner's zone."""
    if isinstance(value, datetime):
        return iso(value if value.tzinfo else value.replace(tzinfo=tz)), False
    return value.isoformat(), True


def _address(value) -> tuple[str, str]:
    """(name, email) of an ORGANIZER or ATTENDEE property."""
    email = str(value).removeprefix("mailto:").removeprefix("MAILTO:")
    name = str(value.params.get("CN", "")).strip('"') if hasattr(value, "params") else ""
    return name or email, email


def parse_ics(data: bytes, tz: ZoneInfo) -> dict | None:
    """The first event of an iMIP message, or None when there isn't one."""
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception:  # a malformed invite just doesn't get a card
        return None
    event = next(iter(cal.walk("VEVENT")), None)
    if event is None or event.get("DTSTART") is None:
        return None
    start = event.decoded("DTSTART")
    if event.get("DTEND") is not None:
        end = event.decoded("DTEND")
    elif event.get("DURATION") is not None:
        end = start + event.decoded("DURATION")
    else:
        end = start + timedelta(days=1) if not isinstance(start, datetime) else start
    start_iso, all_day = _when(start, tz)
    end_iso, _ = _when(end, tz)
    attendees = event.get("ATTENDEE") or []
    if not isinstance(attendees, list):
        attendees = [attendees]
    method = str(cal.get("METHOD", "REQUEST")).upper()
    organizer = _address(event["ORGANIZER"]) if event.get("ORGANIZER") else ("", "")
    recurrence = event.decoded("RECURRENCE-ID") if event.get("RECURRENCE-ID") is not None else None
    out = {
        "method": method,
        "uid": str(event.get("UID", "")),
        "recurrence_id": recurrence,
        "title": str(event.get("SUMMARY", "")) or "(no title)",
        "start": start_iso, "end": end_iso, "all_day": all_day,
        "location": str(event.get("LOCATION", "")) or None,
        "organizer": organizer[0] or None,
        "organizer_email": organizer[1] or None,
        "recurring": event.get("RRULE") is not None or recurrence is not None,
        "guests": len(attendees),
        "cancelled": method == "CANCEL" or str(event.get("STATUS", "")).upper() == "CANCELLED",
        "reply": None,
    }
    if method == "REPLY" and attendees:
        who, _ = _address(attendees[0])
        out["reply"] = {"who": who, "response": str(attendees[0].params.get("PARTSTAT", "")).lower() or None}
    return out


# --------------------------------------------------------------------------- the calendar side


def _google_time(value: dict) -> tuple[str, bool]:
    if "date" in value:
        return value["date"], True
    return iso(datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))), False


def _same_moment(google: dict, recurrence) -> bool:
    if isinstance(recurrence, datetime):
        if "dateTime" not in google:
            return False
        moment = datetime.fromisoformat(google["dateTime"].replace("Z", "+00:00"))
        return moment == (recurrence if recurrence.tzinfo else recurrence.replace(tzinfo=UTC))
    return google.get("date") == recurrence.isoformat()


def _instance_id(master_id: str, recurrence) -> str:
    """Google's ID for one occurrence of a series that has no exception yet."""
    if isinstance(recurrence, datetime):
        moment = recurrence if recurrence.tzinfo else recurrence.replace(tzinfo=UTC)
        return f"{master_id}_{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    return f"{master_id}_{recurrence:%Y%m%d}"


async def _find(api: CalendarClient, parsed: dict) -> dict | None:
    """The invited account's copy of the event, or None when it isn't on that calendar."""
    if not parsed["uid"]:
        return None
    found = await api.find_by_uid("primary", parsed["uid"])
    if not found:
        return None
    recurrence = parsed["recurrence_id"]
    if recurrence is not None:
        exact = next((e for e in found if e.get("originalStartTime")
                      and _same_moment(e["originalStartTime"], recurrence)), None)
        if exact:
            return exact
        master = next((e for e in found if not e.get("recurringEventId")), None)
        if master:
            return await api.get_event("primary", _instance_id(master["id"], recurrence)) or master
    return next((e for e in found if not e.get("recurringEventId")), found[0])


def _view(parsed: dict, event: dict | None, account: str) -> dict:
    out = {k: v for k, v in parsed.items() if k not in ("uid", "recurrence_id", "organizer_email")}
    out.update(account=account, on_calendar=False, response=None, can_respond=False, calendar_url=None,
               organizer_email=parsed["organizer_email"])
    if event is None or event.get("status") == "cancelled":
        out["cancelled"] = out["cancelled"] or bool(event)
        return out
    me = next((a for a in event.get("attendees", []) if a.get("self")), None)
    start, all_day = _google_time(event["start"])
    end, _ = _google_time(event.get("end", event["start"]))
    out.update(
        # The calendar has the latest version (a later update may not be in this thread).
        title=event.get("summary") or out["title"], start=start, end=end, all_day=all_day,
        location=event.get("location") or out["location"],
        on_calendar=True,
        response=me.get("responseStatus") if me else None,
        can_respond=bool(me) and not (event.get("organizer") or {}).get("self") and parsed["method"] != "REPLY",
        calendar_url=event.get("htmlLink"),
        guests=max(out["guests"], sum(1 for a in event.get("attendees", []) if not a.get("resource"))),
    )
    return out


# --------------------------------------------------------------------------- entry points


async def _resolve(conn: sqlite3.Connection, gmail: GmailFactory, calendars: CalendarFactory, item_id: int,
                   tz: ZoneInfo) -> tuple[sqlite3.Row, dict, dict | None] | None:
    item = items.get_by_id(conn, item_id)
    if item is None:
        raise MailError(f"no item {item_id}", status=404)
    acct = accounts.get_account(conn, item["account_id"])
    if item["kind"] != "email" or acct is None or acct["kind"] != "gmail":
        raise MailError("only Gmail threads carry invites Tether can read", status=422)
    try:
        api = gmail(acct["id"])
        thread = await api.get_thread_full(item["external_id"])
        if thread is None:
            raise MailError(f"no thread {item['external_id']} in {acct['label']}", status=404)
        messages = sorted((m for m in thread.get("messages", []) if "DRAFT" not in m.get("labelIds", [])),
                          key=lambda m: int(m.get("internalDate", 0)), reverse=True)
        parsed = None
        for m in messages:   # the newest invite in the thread
            data = await _ics(api, m)
            if data and (parsed := parse_ics(data, tz)):
                break
        if parsed is None:
            return None
        event = await _find(calendars(acct["id"]), parsed)
    except Exception as exc:
        raise MailError(_describe(acct, exc)) from exc
    return acct, parsed, event


async def get_invite(conn: sqlite3.Connection, gmail: GmailFactory, calendars: CalendarFactory, item_id: int,
                     tz: ZoneInfo) -> dict | None:
    """The invite in an email item's thread, with the invited account's answer, or None."""
    resolved = await _resolve(conn, gmail, calendars, item_id, tz)
    if resolved is None:
        return None
    acct, parsed, event = resolved
    return _view(parsed, event, acct["label"])


async def respond(conn: sqlite3.Connection, gmail: GmailFactory, calendars: CalendarFactory, item_id: int,
                  response: str, tz: ZoneInfo) -> tuple[dict, int | None]:
    """Answer the invite as the invited account; Google emails the organizer.
    Returns the updated invite and that account's primary calendar source, to refetch."""
    if response not in RESPONSES:
        raise MailError(f"response must be one of {', '.join(RESPONSES)}", status=422)
    resolved = await _resolve(conn, gmail, calendars, item_id, tz)
    if resolved is None:
        raise MailError("this email has no calendar invite", status=422)
    acct, parsed, event = resolved
    view = _view(parsed, event, acct["label"])
    if not view["can_respond"]:
        reason = ("it isn't on this account's calendar; open it in Gmail to answer" if not view["on_calendar"]
                  else "you organize it" if view["method"] != "REPLY" else "it is a reply, not an invitation")
        raise MailError(f"Tether can't answer this invite: {reason}.", status=422)
    me = next(a for a in event.get("attendees", []) if a.get("self"))
    try:
        updated = await calendars(acct["id"]).respond("primary", event["id"], me["email"], response)
    except Exception as exc:
        raise MailError(_describe(acct, exc)) from exc
    log.info("account %s: answered an invite (%s)", acct["id"], response)
    source = conn.execute("SELECT id FROM calendar_source WHERE account_id = ? AND is_primary = 1 AND enabled = 1",
                          (acct["id"],)).fetchone()
    return _view(parsed, updated, acct["label"]), (source["id"] if source else None)


def _describe(acct: sqlite3.Row, exc: BaseException) -> str:
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, AuthError):
        return f"{acct['label']}: sign-in expired or revoked. Reconnect it in Settings."
    if isinstance(exc, RateLimited):
        return f"{acct['label']}: Google is rate-limiting this account. Try again in a minute."
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403:
        return f"{acct['label']}: Google refused the calendar change. Reconnect the account in Settings."
    log.warning("account %s: invite call failed: %s", acct["id"], type(exc).__name__)
    return f"{acct['label']}: couldn't reach Google ({type(exc).__name__})."

