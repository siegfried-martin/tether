"""Calendar hub: ICS and Google parsing, the fetch diff and its delete guards, grouping and sticky
hub assignment, idempotent hub writes, the agenda, and the poller end to end."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from test_web import client, login, settings  # noqa: F401  (fixtures)
from tether.connectors import gcal, ics
from tether.connectors.events import Instance, Window
from tether.services import calendar, overview

TZ = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 15)                       # a Sunday
NOW = datetime(2026, 11, 15, 17, 0, tzinfo=UTC)  # noon in New York
WINDOW = Window.around(TODAY, TZ)

EASTERN = """BEGIN:VTIMEZONE
TZID:Eastern Standard Time
BEGIN:STANDARD
DTSTART:16010101T020000
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE"""


def feed(*events: str) -> bytes:
    body = "\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:Microsoft Exchange Server 2010", EASTERN,
                      *events, "END:VCALENDAR"])
    return body.replace("\n", "\r\n").encode()


def vevent(uid, start, end=None, *, summary="Meeting", tz="Eastern Standard Time", extra=""):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}"]
    if len(start) == 8:
        lines += [f"DTSTART;VALUE=DATE:{start}", f"DTEND;VALUE=DATE:{end}"]
    else:
        lines += [f"DTSTART;TZID={tz}:{start}", f"DTEND;TZID={tz}:{end}"]
    lines += ["DTSTAMP:20261101T000000Z", "STATUS:CONFIRMED", *[x for x in extra.split("\n") if x], "END:VEVENT"]
    return "\n".join(lines)


WORK_FEED = feed(
    vevent("standup", "20261102T090000", "20261102T091500", summary="Standup",
           extra="RRULE:FREQ=WEEKLY;BYDAY=MO\nEXDATE;TZID=Eastern Standard Time:20261123T090000\n"
                 "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\nLAST-MODIFIED:20261101T120000Z"),
    vevent("standup", "20261116T100000", "20261116T101500", summary="Standup (moved)",
           extra="RECURRENCE-ID;TZID=Eastern Standard Time:20261116T090000\nX-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE"),
    vevent("offsite", "20261126", "20261127", summary="Thanksgiving", extra="X-MICROSOFT-CDO-BUSYSTATUS:FREE"),
    vevent("review", "20261117T140000", "20261117T150000", summary="Q4 review",
           extra="LOCATION:Room 4\nDESCRIPTION:Agenda\\nJoin: https://teams.microsoft.com/l/meetup-join/19%3ameeting_x/0?c=1"),
    vevent("ancient", "20250101T090000", "20250101T100000", summary="Long ago"),
    vevent("india", "20261118T193000", "20261118T203000", summary="Offshore sync", tz="India Standard Time"),
)


# --------------------------------------------------------------------------- parsing


def test_ics_expands_series_with_overrides_exdates_and_windows_zones():
    got = {(i.uid, i.instance_start): i for i in ics.parse(WORK_FEED, WINDOW)}
    standups = sorted(k[1] for k in got if k[0] == "standup")
    # Nov 9 and 16 (moved) and 30 ... in the window; Nov 23 is excluded; Nov 2 is before the window.
    assert standups[:3] == ["2026-11-09T14:00:00Z", "2026-11-16T14:00:00Z", "2026-11-30T14:00:00Z"]
    moved = got[("standup", "2026-11-16T14:00:00Z")]
    assert (moved.start, moved.title, moved.busy) == ("2026-11-16T15:00:00Z", "Standup (moved)", "tentative")
    regular = got[("standup", "2026-11-09T14:00:00Z")]
    assert regular.busy == "busy" and regular.modified_at == "2026-11-01T12:00:00Z"
    holiday = got[("offsite", "2026-11-26")]
    assert (holiday.all_day, holiday.start, holiday.end, holiday.busy) == (True, "2026-11-26", "2026-11-27", "free")
    review = got[("review", "2026-11-17T19:00:00Z")]
    assert review.location == "Room 4" and "meetup-join" in review.description
    # A Windows zone name with no VTIMEZONE block resolves through the Windows-to-IANA map.
    assert got[("india", "2026-11-18T14:00:00Z")].end == "2026-11-18T15:00:00Z"
    assert not any(k[0] == "ancient" for k in got)


def test_ics_rejects_garbage():
    with pytest.raises(ics.FeedError):
        ics.parse(b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:notadate\r\nEND:VEVENT\r\nEND:VCALENDAR", WINDOW)


def gevent(eid, start, end=None, **kw):
    timed = "T" in start
    ev = {"id": eid, "iCalUID": kw.pop("uid", f"{eid}@google.com"), "status": "confirmed", "summary": "Lunch",
          "updated": "2026-11-10T08:00:00.000Z",
          "start": {"dateTime": start} if timed else {"date": start},
          "end": {"dateTime": end or start} if timed else {"date": end}}
    ev.update(kw)
    return ev


def test_google_event_to_instance():
    plain = gcal.to_instance(gevent("a", "2026-11-17T12:00:00-05:00", "2026-11-17T13:00:00-05:00",
                                    hangoutLink="https://meet.google.com/abc-defg-hij"), TZ)
    assert (plain.start, plain.end, plain.instance_start) == ("2026-11-17T17:00:00Z", "2026-11-17T18:00:00Z",
                                                               "2026-11-17T17:00:00Z")
    assert plain.description == "Join: https://meet.google.com/abc-defg-hij"
    assert plain.modified_at == "2026-11-10T08:00:00Z"
    moved = gcal.to_instance(gevent("b_1", "2026-11-18T15:00:00Z", "2026-11-18T16:00:00Z", uid="series@x",
                                    originalStartTime={"dateTime": "2026-11-18T14:00:00Z"}), TZ)
    assert (moved.uid, moved.instance_start) == ("series@x", "2026-11-18T14:00:00Z")
    declined = gevent("c", "2026-11-18T15:00:00Z", attendees=[{"self": True, "responseStatus": "declined"}])
    assert gcal.to_instance(declined, TZ) is None
    assert gcal.to_instance(gevent("d", "2026-11-18", "2026-11-19", eventType="workingLocation"), TZ) is None
    assert gcal.to_instance(gevent("e", "2026-11-18", "2026-11-19", transparency="transparent"), TZ).busy == "free"
    invited = gevent("f", "2026-11-18T15:00:00Z", attendees=[{"self": True, "responseStatus": "needsAction"}])
    assert gcal.to_instance(invited, TZ).busy == "tentative"


# --------------------------------------------------------------------------- fetch diff


@pytest.fixture
def sources(conn):
    """A Google account with two calendars, and the ICS feed."""
    acct = conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'me@x.com', '#111111')").lastrowid
    calendar.sync_calendar_list(conn, acct, [
        {"id": "me@x.com", "summary": "me@x.com", "primary": True, "accessRole": "owner"},
        {"id": "family@group", "summary": "Family", "accessRole": "owner"},
        {"id": "hub@group", "summary": "Tether", "accessRole": "owner"},
    ], calendar.Hub(acct, "hub@group"))
    work = calendar.ensure_ics_source(conn, "Work calendar")
    by_ref = {s["external_ref"]: s for s in calendar.list_sources(conn)}
    return {"account": acct, "me": by_ref["me@x.com"]["id"], "family": by_ref["family@group"]["id"],
            "hub": by_ref["hub@group"]["id"], "work": work, "hub_config": calendar.Hub(acct, "hub@group")}


def inst(uid, start, *, minutes=60, title="Sync", **kw):
    if len(start) == 10:
        end = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
        return Instance(uid=uid, instance_start=kw.pop("instance_start", start), start=start, end=end, all_day=True,
                        title=title, **kw)
    s = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end = (s + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Instance(uid=uid, instance_start=kw.pop("instance_start", start), start=start, end=end, all_day=False,
                    title=title, **kw)


def apply(conn, source_id, instances, now=NOW):
    return calendar.apply_fetch(conn, source_id, instances, window=WINDOW, now=now)


def count(conn, source_id, held=False):
    clause = " AND missing_since IS NOT NULL" if held else ""
    return conn.execute(f"SELECT COUNT(*) FROM source_event WHERE source_id = ?{clause}", (source_id,)).fetchone()[0]


def test_new_sources_enable_only_primary_and_never_the_hub(conn, sources):
    enabled = {s["external_ref"]: s["enabled"] for s in calendar.list_sources(conn)}
    assert enabled == {"me@x.com": True, "family@group": False, "hub@group": False, "feed": True}
    with pytest.raises(calendar.CalendarError, match="hub calendar"):
        calendar.set_source_enabled(conn, sources["hub"], True, sources["hub_config"])
    assert calendar.set_source_enabled(conn, sources["family"], True, sources["hub_config"])["enabled"]


def test_fetch_diff_adds_changes_and_removes(conn, sources):
    src = sources["me"]
    first = apply(conn, src, [inst("a", "2026-11-16T15:00:00Z"), inst("b", "2026-11-17T15:00:00Z")])
    assert first["added"] == 2
    assert not any(apply(conn, src, [inst("a", "2026-11-16T15:00:00Z"), inst("b", "2026-11-17T15:00:00Z")]).values())
    moved = apply(conn, src, [inst("a", "2026-11-16T16:00:00Z", instance_start="2026-11-16T15:00:00Z")])
    assert moved["changed"] == 1 and moved["removed"] == 1
    assert count(conn, src) == 1


def test_past_events_are_never_deleted_and_aged_events_are_pruned(conn, sources):
    src = sources["me"]
    apply(conn, src, [inst("past", "2026-11-14T15:00:00Z"), inst("old", "2026-11-09T15:00:00Z")])
    stats = apply(conn, src, [], now=NOW)
    assert stats["removed"] == 0 and count(conn, src) == 2, "absent past events stay"
    later = Window.around(TODAY + timedelta(days=4), TZ)
    calendar.apply_fetch(conn, src, [], window=later, now=NOW + timedelta(days=4))
    assert count(conn, src) == 1, "the event behind the new window is pruned"


def test_mass_disappearance_is_held_until_released(conn, sources):
    src = sources["work"]
    events = [inst(f"e{n}", f"2026-11-{16 + n}T15:00:00Z") for n in range(10)]
    apply(conn, src, events)
    calendar.assign_hub(conn)
    # Seven of ten future events vanish: more than 5 and more than 25%, so all seven are held.
    stats = apply(conn, src, events[:3])
    assert stats["held"] == 7 and stats["removed"] == 0 and count(conn, src, held=True) == 7
    [hold] = calendar.holds(conn)
    assert hold["source"] == "Work calendar" and len(hold["events"]) == 7
    assert calendar.hold_alerts(conn)[0]["kind"] == "calendar_hold"
    # While held, one more vanishing joins the hold, and a returning event leaves it.
    stats = apply(conn, src, events[:2] + [events[5]])
    assert count(conn, src, held=True) == 7 and stats["held"] == 7
    calendar.release_hold(conn, src, "apply")
    assert count(conn, src) == 3 and calendar.holds(conn) == []
    with pytest.raises(calendar.CalendarError, match="no held deletes"):
        calendar.release_hold(conn, src, "apply")


def test_small_disappearance_deletes_at_once(conn, sources):
    src = sources["work"]
    events = [inst(f"e{n}", f"2026-11-{16 + n}T15:00:00Z") for n in range(10)]
    apply(conn, src, events)
    assert apply(conn, src, events[:5])["removed"] == 5, "5 is not more than 5: no hold"


def test_discard_stops_managing_the_hub_events(conn, sources):
    src = sources["work"]
    events = [inst(f"e{n}", f"2026-11-{16 + n}T15:00:00Z") for n in range(8)]
    apply(conn, src, events)
    calendar.assign_hub(conn)
    apply(conn, src, events[:1])
    result = calendar.release_hold(conn, src, "discard")
    assert result["released"] == 7 and result["unmanaged_hub_events"] == 7
    assert conn.execute("SELECT COUNT(*) FROM hub_event").fetchone()[0] == 1


# --------------------------------------------------------------------------- grouping and assignment


def hub_ids(conn):
    return {r["uid"]: r["hub_event_id"] for r in conn.execute("SELECT uid, hub_event_id FROM source_event")}


def test_same_invite_on_two_calendars_and_copies_by_time_and_title_share_one_hub_event(conn, sources):
    calendar.set_source_enabled(conn, sources["family"], True, None)
    apply(conn, sources["me"], [inst("inv", "2026-11-17T15:00:00Z", title="Dentist"),
                                inst("copy-a", "2026-11-18T15:00:00Z", title="  Book  club")])
    apply(conn, sources["family"], [inst("inv", "2026-11-17T15:00:00Z", title="Dentist"),
                                    inst("copy-b", "2026-11-18T15:00:00Z", title="book club")])
    apply(conn, sources["work"], [inst("other", "2026-11-18T15:00:00Z", title="Standup")])
    assert calendar.assign_hub(conn)["groups"] == 3
    ids = hub_ids(conn)
    assert ids["copy-a"] == ids["copy-b"] and ids["other"] != ids["copy-a"]
    assert conn.execute("SELECT COUNT(*) FROM hub_event").fetchone()[0] == 3
    before = dict(ids)
    calendar.assign_hub(conn)
    assert hub_ids(conn) == before, "rerunning assigns nothing new"


def test_leaver_gets_its_own_hub_event_and_the_group_keeps_its_own(conn, sources):
    calendar.set_source_enabled(conn, sources["family"], True, None)
    apply(conn, sources["me"], [inst("a", "2026-11-18T15:00:00Z", title="Book club")])
    apply(conn, sources["family"], [inst("b", "2026-11-18T15:00:00Z", title="Book club")])
    calendar.assign_hub(conn)
    shared = hub_ids(conn)["a"]
    # b is retitled, so it no longer matches. Tie on size: the lowest member key ("a|...") keeps the event.
    apply(conn, sources["family"], [inst("b", "2026-11-18T15:00:00Z", title="Book club (cancelled)")])
    stats = calendar.assign_hub(conn)
    assert stats["split"] == 1 and stats["new"] == 1
    assert hub_ids(conn)["a"] == shared and hub_ids(conn)["b"] != shared


def test_two_groups_joining_keep_the_oldest_hub_event(conn, sources):
    calendar.set_source_enabled(conn, sources["family"], True, None)
    apply(conn, sources["me"], [inst("a", "2026-11-18T15:00:00Z", title="Lunch")])
    calendar.assign_hub(conn, now="2026-11-15T10:00:00Z")
    apply(conn, sources["family"], [inst("b", "2026-11-18T16:00:00Z", title="Lunch")])
    calendar.assign_hub(conn, now="2026-11-15T11:00:00Z")
    oldest = hub_ids(conn)["a"]
    # b is moved to a's time, so it now matches a.
    apply(conn, sources["family"], [inst("b", "2026-11-18T15:00:00Z", title="Lunch",
                                         instance_start="2026-11-18T16:00:00Z")])
    assert calendar.assign_hub(conn)["merged"] == 1
    assert set(hub_ids(conn).values()) == {oldest}
    assert calendar.pending_writes(conn)["deletes"] == 1


def test_new_key_taken_gets_a_suffix(conn, sources):
    apply(conn, sources["me"], [inst("a", "2026-11-18T15:00:00Z", title="Lunch")])
    conn.execute("INSERT INTO hub_event (dedup_key, google_event_id, created_at) VALUES "
                 "('a|2026-11-18T15:00:00Z', 'x', '2026-01-01')")
    calendar.assign_hub(conn)
    key = conn.execute("SELECT dedup_key FROM hub_event h JOIN source_event e ON e.hub_event_id = h.id").fetchone()[0]
    assert key == "a|2026-11-18T15:00:00Z#2"


def test_merged_content(conn, sources):
    calendar.set_source_enabled(conn, sources["family"], True, None)
    apply(conn, sources["me"], [inst("inv", "2026-11-17T15:00:00Z", title="Dentist", description="Bring card",
                                     modified_at="2026-11-01T00:00:00Z", busy="tentative")])
    apply(conn, sources["family"], [inst("inv", "2026-11-17T15:00:00Z", title="Dentist (Ann)", location="Main St",
                                         description="Ann's appt", modified_at="2026-11-02T00:00:00Z")])
    calendar.assign_hub(conn)
    h = conn.execute("SELECT * FROM hub_event").fetchone()
    assert h["title"] == "Dentist (Ann)", "the most recently modified member wins"
    assert h["location"] == "Main St" and h["busy"] == "busy"
    assert h["description"] == "— me@x.com —\nBring card\n\n— Family —\nAnn's appt"


def test_google_event_ids_are_valid_and_stable():
    gid = calendar.google_event_id("abc|2026-11-18T15:00:00Z")
    assert gid == calendar.google_event_id("abc|2026-11-18T15:00:00Z")
    assert len(gid) == 32 and set(gid) <= set("0123456789abcdefghijklmnopqrstuv")


# --------------------------------------------------------------------------- writes


class FakeCalendar:
    """The hub calendar plus source calendars, in memory. Deleted events linger as cancelled."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.sources: dict[str, list[dict]] = {}
        self.calendars: list[dict] = []

    async def list_calendars(self):
        return self.calendars

    async def list_events(self, calendar_id, time_min, time_max):
        return self.sources.get(calendar_id, [])

    async def insert_event(self, calendar_id, body):
        self.calls.append(("insert", body["id"]))
        if body["id"] in self.events:
            return False
        self.events[body["id"]] = dict(body)
        return True

    async def update_event(self, calendar_id, event_id, body):
        self.calls.append(("update", event_id))
        if event_id not in self.events:
            return False
        self.events[event_id] = dict(body)
        return True

    async def delete_event(self, calendar_id, event_id):
        self.calls.append(("delete", event_id))
        if event_id in self.events:
            self.events[event_id]["status"] = "cancelled"

    def live(self):
        return {k: v for k, v in self.events.items() if v["status"] == "confirmed"}


async def test_writes_are_idempotent_and_follow_changes(conn, sources):
    api, hub = FakeCalendar(), sources["hub_config"]
    apply(conn, sources["me"], [inst("a", "2026-11-18T15:00:00Z", title="Lunch"),
                                inst("d", "2026-11-20", title="Trip")])
    calendar.assign_hub(conn)
    assert await calendar.write_hub(conn, api, hub) == {"inserted": 2, "updated": 0, "deleted": 0}
    gid = calendar.google_event_id("a|2026-11-18T15:00:00Z")
    assert api.events[gid]["start"] == {"dateTime": "2026-11-18T15:00:00Z"}
    assert api.events[calendar.google_event_id("d|2026-11-20")]["start"] == {"date": "2026-11-20"}
    api.calls.clear()
    calendar.assign_hub(conn)
    assert await calendar.write_hub(conn, api, hub) == {"inserted": 0, "updated": 0, "deleted": 0}
    assert api.calls == []
    apply(conn, sources["me"], [inst("a", "2026-11-18T15:00:00Z", title="Lunch with Ann"),
                                inst("d", "2026-11-20", title="Trip")])
    calendar.assign_hub(conn)
    await calendar.write_hub(conn, api, hub)
    assert api.events[gid]["summary"] == "Lunch with Ann" and api.calls == [("update", gid)]
    apply(conn, sources["me"], [inst("d", "2026-11-20", title="Trip")])
    calendar.assign_hub(conn)
    assert (await calendar.write_hub(conn, api, hub))["deleted"] == 1
    assert gid not in api.live()


async def test_reinsert_after_delete_readopts_the_cancelled_event(conn, sources):
    api, hub = FakeCalendar(), sources["hub_config"]
    calendar.set_source_enabled(conn, sources["family"], True, None)
    apply(conn, sources["family"], [inst("a", "2026-11-18T15:00:00Z")])
    calendar.assign_hub(conn)
    await calendar.write_hub(conn, api, hub)
    calendar.set_source_enabled(conn, sources["family"], False, None)
    calendar.assign_hub(conn)
    await calendar.write_hub(conn, api, hub)
    assert api.live() == {}
    calendar.set_source_enabled(conn, sources["family"], True, None)
    apply(conn, sources["family"], [inst("a", "2026-11-18T15:00:00Z")])
    calendar.assign_hub(conn)
    stats = await calendar.write_hub(conn, api, hub)
    assert stats == {"inserted": 0, "updated": 1, "deleted": 0}, "409 on insert, then replaced as confirmed"
    assert len(api.live()) == 1


async def test_without_a_hub_nothing_is_written(conn, sources):
    apply(conn, sources["me"], [inst("a", "2026-11-18T15:00:00Z")])
    calendar.assign_hub(conn)
    apply(conn, sources["me"], [])
    calendar.assign_hub(conn)
    await calendar.write_hub(conn, None, None)
    assert conn.execute("SELECT COUNT(*) FROM hub_event").fetchone()[0] == 0, "never-written empty rows go"


# --------------------------------------------------------------------------- agenda


def test_agenda(conn, sources):
    apply(conn, sources["work"], ics.parse(WORK_FEED, WINDOW))
    calendar.assign_hub(conn)
    day = calendar.get_agenda(conn, date(2026, 11, 17), date(2026, 11, 17), TZ)
    assert [e["title"] for e in day] == ["Q4 review"]
    assert day[0]["join_url"].startswith("https://teams.microsoft.com/l/meetup-join/")
    assert day[0]["sources"] == [{"source_id": sources["work"], "name": "Work calendar", "color": day[0]["sources"][0]["color"]}]
    thanksgiving = calendar.get_agenda(conn, date(2026, 11, 26), date(2026, 11, 26), TZ)
    assert [(e["title"], e["all_day"], e["busy"]) for e in thanksgiving] == [("Thanksgiving", True, "free")]
    with pytest.raises(calendar.CalendarError):
        calendar.get_agenda(conn, TODAY, TODAY - timedelta(days=1), TZ)
    assert calendar.parse_range(None, None, TODAY) == (TODAY, TODAY + timedelta(days=13))


def test_overview_has_todays_agenda_and_hold_alerts(conn, sources):
    apply(conn, sources["me"], [inst("now", "2026-11-15T20:00:00Z", title="Call mom")])
    calendar.assign_hub(conn)
    data = overview.get_overview(conn, TODAY, TZ)
    assert [e["title"] for e in data["calendar"]["today"]] == ["Call mom"]
    assert "description" not in data["calendar"]["today"][0]


# --------------------------------------------------------------------------- poller


async def test_poller_cycle_end_to_end(conn, sources, monkeypatch):
    from tether.config import Settings
    from tether.pollers import CalendarPoller
    api = FakeCalendar()
    api.sources["me@x.com"] = [gevent("g1", "2026-11-17T14:00:00-05:00", "2026-11-17T15:00:00-05:00",
                                      summary="Q4 review", uid="review")]
    cfg = Settings(encryption_key="x", session_secret="s", google_client_id="x", google_client_secret="x",
                   owner_email="o@x.com", timezone="America/New_York", hub_account="me@x.com",
                   hub_calendar_id="hub@group", ics_url="https://example.invalid/feed.ics", _env_file=None)
    poller = CalendarPoller(conn, None, None, cfg)
    poller.client = lambda account_id: api
    fetches = []

    async def fake_fetch(http, url):
        fetches.append(url)
        return WORK_FEED
    monkeypatch.setattr(ics, "fetch", fake_fetch)
    await poller.cycle(now=NOW)
    # The Google copy and the feed's "review" share UID and start: one hub event.
    review = calendar.get_agenda(conn, date(2026, 11, 17), date(2026, 11, 17), TZ)
    assert [e["title"] for e in review] == ["Q4 review"] and len(review[0]["sources"]) == 2
    written = len(api.live())
    assert written == conn.execute("SELECT COUNT(*) FROM hub_event").fetchone()[0] > 5
    # Nothing is due a minute later; the unchanged feed is not re-diffed even when forced.
    api.calls.clear()
    await poller.cycle(now=NOW + timedelta(minutes=1))
    assert len(fetches) == 1 and api.calls == []
    poller.wake(refetch=True)
    await poller.cycle(now=NOW + timedelta(minutes=2))
    assert len(fetches) == 2 and api.calls == []
    assert all(r["failures"] == 0 for r in conn.execute("SELECT failures FROM sync_state"))


def test_calendar_api(client, conn, monkeypatch):
    login(client, monkeypatch)
    acct = conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'me@x.com', '#111111')").lastrowid
    calendar.sync_calendar_list(conn, acct, [{"id": "me@x.com", "summary": "Me", "primary": True,
                                              "accessRole": "owner"}], None)
    [source] = client.get("/api/calendar").json()["sources"]
    assert source["enabled"] and source["name"] == "Me"
    client.app.state.calendar.wake = lambda **kw: None
    assert client.patch(f"/api/calendar/sources/{source['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.get("/api/agenda?start=2026-11-15&end=2026-11-16").json() == []
    assert client.get("/api/agenda?start=soon").status_code == 422
    assert client.post(f"/api/calendar/sources/{source['id']}/release", json={"action": "apply"}).status_code == 409
