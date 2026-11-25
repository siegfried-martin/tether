"""Calendar invites in Gmail threads: parsing, finding the calendar copy, answering."""

import base64
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeGmail, msg

from tether.services import invite, items, mail
from tether.services.mail import MailError

TZ = ZoneInfo("America/New_York")


def b64(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")


def ics(method="REQUEST", *, uid="abc123@google.com", extra="", start="DTSTART:20261203T150000Z",
        end="DTEND:20261203T160000Z", attendee_status="NEEDS-ACTION"):
    return "\r\n".join([
        "BEGIN:VCALENDAR", "PRODID:-//Google Inc//Google Calendar 70.9054//EN", "VERSION:2.0", f"METHOD:{method}",
        "BEGIN:VEVENT", start, end, f"UID:{uid}",
        "ORGANIZER;CN=Ann Lee:mailto:ann@example.com",
        f"ATTENDEE;CN=me@x.com;PARTSTAT={attendee_status}:mailto:me@x.com",
        "ATTENDEE;CN=Ann Lee;PARTSTAT=ACCEPTED:mailto:ann@example.com",
        "SUMMARY:Design review", "LOCATION:Room 4", *([extra] if extra else []),
        "END:VEVENT", "END:VCALENDAR", ""])


def invite_msg(mid, tid, body: str | None = None, *, attachment_id=None, minutes_ago=10):
    m = msg(mid, tid, labels=["INBOX"], subject="Invitation: Design review", minutes_ago=minutes_ago)
    cal = {"mimeType": "text/calendar", "filename": "",
           "body": {"attachmentId": attachment_id, "size": 900} if attachment_id else {"data": b64(body)}}
    m["payload"] = {**m["payload"], "mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "filename": "", "body": {"data": b64("You have been invited")}}, cal]}
    return m


class InviteGmail(FakeGmail):
    def __init__(self):
        super().__init__()
        self.attachments = {}

    async def get_attachment(self, message_id, attachment_id):
        return self.attachments.get((message_id, attachment_id))


class FakeCalendar:
    def __init__(self, events=()):
        self.events = {e["id"]: e for e in events}
        self.responses = []

    async def find_by_uid(self, calendar_id, uid):
        return [e for e in self.events.values() if e.get("iCalUID") == uid]

    async def get_event(self, calendar_id, event_id):
        return self.events.get(event_id)

    async def respond(self, calendar_id, event_id, email, response):
        self.responses.append((calendar_id, event_id, email, response))
        event = self.events[event_id]
        for a in event["attendees"]:
            if a.get("self"):
                a["responseStatus"] = response
        return event


def gevent(eid="ev1", *, uid="abc123@google.com", status="needsAction", organizer_self=False, **extra):
    return {"id": eid, "iCalUID": uid, "status": "confirmed", "summary": "Design review",
            "start": {"dateTime": "2026-12-03T10:00:00-05:00"}, "end": {"dateTime": "2026-12-03T11:00:00-05:00"},
            "htmlLink": "https://calendar.google.com/event?eid=x",
            "organizer": {"email": "ann@example.com", **({"self": True} if organizer_self else {})},
            "attendees": [{"email": "me@x.com", "self": True, "responseStatus": status},
                          {"email": "ann@example.com", "organizer": True, "responseStatus": "accepted"}],
            **extra}


@pytest.fixture
def setup(conn):
    account_id = conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'me@x.com', '#000')").lastrowid
    gmail, cal = InviteGmail(), FakeCalendar([gevent()])
    item_id = items.upsert_item(conn, account_id=account_id, external_id="t1", kind="email", fields={
        "last_activity_at": "2026-11-25T10:00:00Z", "last_direction": "in"}).id
    return gmail, cal, item_id, (lambda _: gmail), (lambda _: cal)


async def test_thread_flags_invite(conn, setup):
    gmail, _, item_id, gm, _ = setup
    gmail.put("t1", invite_msg("m1", "t1", ics()))
    assert (await mail.get_item_thread(conn, gm, item_id))["invite"] is True
    gmail.put("t1", msg("m1", "t1", labels=["INBOX"]))
    assert (await mail.get_item_thread(conn, gm, item_id))["invite"] is False


async def test_invite_on_calendar(conn, setup):
    gmail, _, item_id, gm, cm = setup
    gmail.put("t1", invite_msg("m1", "t1", ics()))
    view = await invite.get_invite(conn, gm, cm, item_id, TZ)
    assert view["title"] == "Design review" and view["location"] == "Room 4"
    assert view["start"] == "2026-12-03T15:00:00Z" and view["end"] == "2026-12-03T16:00:00Z"
    assert view["organizer"] == "Ann Lee" and view["guests"] == 2 and view["method"] == "REQUEST"
    assert view["on_calendar"] and view["can_respond"] and view["response"] == "needsAction"
    assert not view["cancelled"] and not view["recurring"]


async def test_respond_patches_own_answer(conn, setup):
    gmail, cal, item_id, gm, cm = setup
    gmail.put("t1", invite_msg("m1", "t1", ics()))
    view, source = await invite.respond(conn, gm, cm, item_id, "accepted", TZ)
    assert cal.responses == [("primary", "ev1", "me@x.com", "accepted")]
    assert view["response"] == "accepted" and source is None
    with pytest.raises(MailError):
        await invite.respond(conn, gm, cm, item_id, "maybe", TZ)


async def test_invite_in_attachment(conn, setup):
    gmail, _, item_id, gm, cm = setup
    gmail.put("t1", invite_msg("m1", "t1", attachment_id="a1"))
    gmail.attachments[("m1", "a1")] = b64(ics())
    assert (await invite.get_invite(conn, gm, cm, item_id, TZ))["can_respond"]


async def test_newest_invite_wins(conn, setup):
    gmail, _, item_id, gm, cm = setup
    gmail.put("t1", invite_msg("m1", "t1", ics(), minutes_ago=60),
              invite_msg("m2", "t1", ics("CANCEL", uid="other@google.com"), minutes_ago=5))
    view = await invite.get_invite(conn, gm, cm, item_id, TZ)
    assert view["cancelled"] and not view["on_calendar"] and not view["can_respond"]


async def test_not_on_calendar_cannot_answer(conn, setup):
    gmail, cal, item_id, gm, cm = setup
    cal.events.clear()
    gmail.put("t1", invite_msg("m1", "t1", ics()))
    view = await invite.get_invite(conn, gm, cm, item_id, TZ)
    assert view["on_calendar"] is False and view["can_respond"] is False and view["title"] == "Design review"
    with pytest.raises(MailError, match="isn't on this account's calendar"):
        await invite.respond(conn, gm, cm, item_id, "declined", TZ)


async def test_organizer_and_reply(conn, setup):
    gmail, cal, item_id, gm, cm = setup
    cal.events = {"ev1": gevent(organizer_self=True)}
    gmail.put("t1", invite_msg("m1", "t1", ics()))
    assert (await invite.get_invite(conn, gm, cm, item_id, TZ))["can_respond"] is False
    gmail.put("t1", invite_msg("m1", "t1", ics("REPLY", attendee_status="ACCEPTED")))
    view = await invite.get_invite(conn, gm, cm, item_id, TZ)
    assert view["reply"] == {"who": "me@x.com", "response": "accepted"} and not view["can_respond"]


async def test_one_occurrence_of_a_series(conn, setup):
    gmail, cal, item_id, gm, cm = setup
    master = gevent("series", recurrence=["RRULE:FREQ=WEEKLY"])
    occurrence = gevent("series_20261210T150000Z", recurringEventId="series",
                        originalStartTime={"dateTime": "2026-12-10T10:00:00-05:00"})
    occurrence.pop("iCalUID")   # not an exception yet: only reachable by its ID
    cal.events = {"series": master, occurrence["id"]: occurrence}
    gmail.put("t1", invite_msg("m1", "t1", ics(extra="RECURRENCE-ID:20261210T150000Z",
                                                start="DTSTART:20261210T160000Z", end="DTEND:20261210T170000Z")))
    view, _ = await invite.respond(conn, gm, cm, item_id, "tentative", TZ)
    assert cal.responses[-1][1] == "series_20261210T150000Z" and view["recurring"]


def test_parse_all_day_and_floating():
    allday = invite.parse_ics(ics(start="DTSTART;VALUE=DATE:20261205", end="DTEND;VALUE=DATE:20261206").encode(), TZ)
    assert allday["all_day"] and allday["start"] == "2026-12-05" and allday["end"] == "2026-12-06"
    floating = invite.parse_ics(ics(start="DTSTART:20261205T090000", end="DTEND:20261205T093000").encode(), TZ)
    assert floating["start"] == "2026-12-05T14:00:00Z"
    assert invite.parse_ics(b"not a calendar", TZ) is None
