"""The calendar hub (spec: "Calendar hub").

Every source event maps to exactly one hub event, and any poll can be rerun
without creating duplicates. The steps of one cycle:

1. apply_fetch: diff one source's fetched window into source_event, under
   the delete guards (never after a failed fetch, never past events, hold a
   mass delete).
2. assign_hub: group source events (same UID and instance, or same start,
   end and normalized title), give each group exactly one hub event, keeping
   existing assignments (splits, merges, new groups), and merge content.
3. write_hub: push changed hub events to the hub Google calendar under IDs
   derived from their keys, and delete hub events left with no members.

The agenda reads hub_event rows, so it works before a hub calendar exists.
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from tether.connectors import gcal
from tether.connectors.events import BUSY_LEVELS, Instance, Window, is_past
from tether.db import iso, parse_iso, tx, utcnow
from tether.services import accounts

log = logging.getLogger(__name__)

HOLD_MIN_EVENTS = 5          # a fetch that would delete more than this many future events ...
HOLD_MIN_SHARE = 0.25        # ... and more than this share of the source's future events is held
ICS_REF = "feed"             # calendar_source.external_ref of the ICS feed (its URL stays in the environment)
JOIN_URL = re.compile(r"https://(?:teams\.microsoft\.com/l/meetup-join|teams\.live\.com/meet|meet\.google\.com|"
                      r"[\w.-]*zoom\.us/(?:j|my|w))/[^\s<>\"')\]]+")
AGENDA_DESCRIPTION_CAP = 1500


class CalendarError(Exception):
    """A calendar request that can't be done. The message is safe to show the user."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Hub:
    """Where hub events are written: one calendar on one Google account."""
    account_id: int
    calendar_id: str


def hub_config(conn: sqlite3.Connection, account_label: str | None, calendar_id: str | None) -> Hub | None:
    if not account_label or not calendar_id:
        return None
    row = conn.execute("SELECT id FROM account WHERE label = ? AND kind = 'gmail'", (account_label,)).fetchone()
    return Hub(row["id"], calendar_id) if row else None


def stream(source_id: int) -> str:
    return f"calendar:{source_id}"


HUB_STREAM = "calendar.hub"


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.kind, s.external_ref, s.name, s.enabled, s.access_role, s.is_primary,
                  a.id AS account_id, a.label AS account, a.color AS account_color, a.status AS account_status,
                  (SELECT COUNT(*) FROM source_event e WHERE e.source_id = s.id) AS events,
                  (SELECT COUNT(*) FROM source_event e WHERE e.source_id = s.id AND e.missing_since IS NOT NULL)
                    AS held
           FROM calendar_source s JOIN account a ON a.id = s.account_id
           ORDER BY a.id, s.enabled DESC, s.name"""
    ).fetchall()
    return [{**dict(r), "enabled": bool(r["enabled"]), "is_primary": bool(r["is_primary"])} for r in rows]


def get_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT s.*, a.label AS account, a.kind AS account_kind FROM calendar_source s
           JOIN account a ON a.id = s.account_id WHERE s.id = ?""", (source_id,)).fetchone()
    if row is None:
        raise CalendarError(f"no calendar source {source_id}", status=404)
    return row


def sync_calendar_list(conn: sqlite3.Connection, account_id: int, calendars: list[dict], hub: Hub | None) -> None:
    """Record a Google account's calendars as sources. New ones start enabled only if they are the
    account's own primary calendar; the hub calendar is never a source."""
    with tx(conn):
        for cal in calendars:
            is_hub = hub is not None and cal["id"] == hub.calendar_id
            existing = conn.execute("SELECT id, name, enabled FROM calendar_source WHERE account_id = ? "
                                    "AND external_ref = ?", (account_id, cal["id"])).fetchone()
            name = cal.get("summaryOverride") or cal.get("summary") or cal["id"]
            role, primary = cal.get("accessRole"), int(bool(cal.get("primary")))
            if existing is None:
                enabled = bool(primary) and role == "owner" and not is_hub
                conn.execute("INSERT INTO calendar_source (account_id, kind, external_ref, name, enabled, access_role, "
                             "is_primary) VALUES (?, 'google', ?, ?, ?, ?, ?)",
                             (account_id, cal["id"], name, int(enabled), role, primary))
            else:
                conn.execute("UPDATE calendar_source SET name = ?, enabled = enabled AND ?, access_role = ?, "
                             "is_primary = ? WHERE id = ?", (name, int(not is_hub), role, primary, existing["id"]))


def ensure_ics_source(conn: sqlite3.Connection, label: str) -> int:
    """The ICS feed's account and source, created on first use. Its URL stays in the environment."""
    with tx(conn):
        row = conn.execute("SELECT id FROM account WHERE kind = 'ics' ORDER BY id LIMIT 1").fetchone()
        if row:
            account_id = row["id"]
            conn.execute("UPDATE account SET label = ? WHERE id = ? AND label != ? AND NOT EXISTS "
                         "(SELECT 1 FROM account WHERE label = ?)", (label, account_id, label, label))
        else:
            account_id = conn.execute("INSERT INTO account (kind, label, color) VALUES ('ics', ?, ?)",
                                      (label, accounts.next_color(conn, "ics"))).lastrowid
        source = conn.execute("SELECT id FROM calendar_source WHERE account_id = ? AND external_ref = ?",
                              (account_id, ICS_REF)).fetchone()
        if source:
            conn.execute("UPDATE calendar_source SET name = ? WHERE id = ?", (label, source["id"]))
            return source["id"]
        return conn.execute("INSERT INTO calendar_source (account_id, kind, external_ref, name, enabled) "
                            "VALUES (?, 'ics', ?, ?, 1)", (account_id, ICS_REF, label)).lastrowid


def set_source_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool, hub: Hub | None) -> dict:
    """Turn a source on or off. Off removes its events; the next hub pass removes the hub copies
    that no other source holds."""
    source = get_source(conn, source_id)
    if enabled and hub and source["kind"] == "google" and source["external_ref"] == hub.calendar_id \
            and source["account_id"] == hub.account_id:
        raise CalendarError("That is the hub calendar, which can't also be a source.")
    with tx(conn):
        conn.execute("UPDATE calendar_source SET enabled = ? WHERE id = ?", (int(enabled), source_id))
        if not enabled:
            conn.execute("DELETE FROM source_event WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM sync_state WHERE account_id = ? AND stream = ?",
                         (source["account_id"], stream(source_id)))
    return next(s for s in list_sources(conn) if s["id"] == source_id)


# --------------------------------------------------------------------------
# 1. Diff one source's fetch
# --------------------------------------------------------------------------


def _content_hash(i: Instance) -> str:
    return _hash([i.start, i.end, i.all_day, i.title, i.location, i.description, i.busy])


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def apply_fetch(conn: sqlite3.Connection, source_id: int, instances: list[Instance], *, window: Window,
                now: datetime) -> dict:
    """Diff a successful window fetch into source_event. Call only after a fetch that succeeded and parsed.

    Absent future events are removed, unless that would remove more than 5
    events and more than 25% of the source's future events, or a hold is
    already in force: then they are held (missing_since set) and an alert
    raised. Absent past events are left alone. Events behind the window are
    pruned without touching their hub event.
    """
    stats = {"added": 0, "changed": 0, "removed": 0, "held": 0, "pruned": 0}
    fetched: dict[str, Instance] = {}
    for inst in instances:
        fetched.setdefault(inst.key, inst)
    stamp = iso(now)
    with tx(conn):
        rows = conn.execute("SELECT * FROM source_event WHERE source_id = ?", (source_id,)).fetchall()
        existing = {f"{r['uid']}|{r['instance_start']}": r for r in rows}
        for key, inst in fetched.items():
            row = existing.get(key)
            digest = _content_hash(inst)
            if row is None:
                conn.execute(
                    """INSERT INTO source_event (source_id, uid, instance_start, start, "end", all_day, title,
                           location, description, modified_at, content_hash, busy, external_id, series_id,
                           organizer_self, guests)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, inst.uid, inst.instance_start, inst.start, inst.end, int(inst.all_day), inst.title,
                     inst.location, inst.description, inst.modified_at, digest, inst.busy, *_google_ids(inst)))
                stats["added"] += 1
                continue
            elif row["content_hash"] != digest or row["missing_since"] is not None:
                conn.execute(
                    """UPDATE source_event SET start = ?, "end" = ?, all_day = ?, title = ?, location = ?,
                           description = ?, modified_at = ?, content_hash = ?, busy = ?, missing_since = NULL
                       WHERE id = ?""",
                    (inst.start, inst.end, int(inst.all_day), inst.title, inst.location, inst.description,
                     inst.modified_at, digest, inst.busy, row["id"]))
                stats["changed"] += 1
                if row["content_hash"] != digest and inst.modified_at:
                    lag = (now - parse_iso(inst.modified_at)).total_seconds() / 60
                    if 0 <= lag < 24 * 60:
                        log.info("source %s: change seen %.0f min after it was made", source_id, lag)
            ids = _google_ids(inst)
            if tuple(row[c] for c in GOOGLE_ID_COLUMNS) != ids:
                # Bookkeeping for deletes only: the hub content is unchanged, so not counted as a change.
                conn.execute("UPDATE source_event SET external_id = ?, series_id = ?, organizer_self = ?, "
                             "guests = ? WHERE id = ?", (*ids, row["id"]))

        absent = [r for k, r in existing.items() if k not in fetched]
        aged = [r for r in absent if window.starts_before(r["start"], bool(r["all_day"]))]
        _delete_source_rows(conn, [r["id"] for r in aged])
        stats["pruned"] = len(aged)

        aged_ids = {r["id"] for r in aged}
        future_absent = [r for r in absent if r["id"] not in aged_ids
                         and not is_past(r["start"], bool(r["all_day"]), now, window.tz)]
        future_total = sum(1 for r in rows if r["id"] not in aged_ids
                           and not is_past(r["start"], bool(r["all_day"]), now, window.tz))
        # A hold is in force while any earlier-held event is still absent.
        held_now = any(r["missing_since"] for r in absent if r["id"] not in aged_ids)
        mass = len(future_absent) > HOLD_MIN_EVENTS and len(future_absent) > HOLD_MIN_SHARE * future_total
        if future_absent and (held_now or mass):
            new = [r["id"] for r in future_absent if r["missing_since"] is None]
            conn.executemany("UPDATE source_event SET missing_since = ? WHERE id = ?", [(stamp, i) for i in new])
            stats["held"] = len(future_absent)
            if new:
                log.warning("source %s: holding %d deletes for review", source_id, len(new))
        else:
            _delete_source_rows(conn, [r["id"] for r in future_absent])
            stats["removed"] = len(future_absent)
    return stats


GOOGLE_ID_COLUMNS = ("external_id", "series_id", "organizer_self", "guests")


def _google_ids(inst: Instance) -> tuple:
    return inst.external_id, inst.series_id, int(inst.organizer_self), inst.guests


def _delete_source_rows(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("DELETE FROM source_event WHERE id = ?", [(i,) for i in ids])


# --------------------------------------------------------------------------
# Holds
# --------------------------------------------------------------------------


def holds(conn: sqlite3.Connection) -> list[dict]:
    """Held deletes per source, with each held event's title and start (for get_overview and Settings)."""
    rows = conn.execute(
        """SELECT s.id AS source_id, s.name, a.label AS account, e.title, e.start, e.all_day
           FROM source_event e JOIN calendar_source s ON s.id = e.source_id JOIN account a ON a.id = s.account_id
           WHERE e.missing_since IS NOT NULL ORDER BY s.id, e.start"""
    ).fetchall()
    grouped: dict[int, dict] = {}
    for r in rows:
        entry = grouped.setdefault(r["source_id"], {"source_id": r["source_id"], "source": r["name"] or r["account"],
                                                    "events": []})
        entry["events"].append({"title": r["title"], "start": r["start"], "all_day": bool(r["all_day"])})
    return list(grouped.values())


def hold_alerts(conn: sqlite3.Connection) -> list[dict]:
    return [{"kind": "calendar_hold", "account": h["source"], "source_id": h["source_id"],
             "message": f"{h['source']}: {len(h['events'])} events vanished from the feed at once. Tether is "
                        "holding their removal until you review it in Settings, under Calendar."}
            for h in holds(conn)]


def release_hold(conn: sqlite3.Connection, source_id: int, action: str) -> dict:
    """Apply: remove the held events; the next hub pass deletes the hub events they leave empty.
    Discard: remove them and stop managing those hub events, leaving them in Google as they are."""
    if action not in ("apply", "discard"):
        raise CalendarError("action must be apply or discard")
    get_source(conn, source_id)
    with tx(conn):
        held = conn.execute("SELECT id, hub_event_id FROM source_event WHERE source_id = ? "
                            "AND missing_since IS NOT NULL", (source_id,)).fetchall()
        if not held:
            raise CalendarError("That source has no held deletes.", status=409)
        _delete_source_rows(conn, [r["id"] for r in held])
        dropped = 0
        if action == "discard":
            for hub_id in {r["hub_event_id"] for r in held if r["hub_event_id"]}:
                if not conn.execute("SELECT 1 FROM source_event WHERE hub_event_id = ?", (hub_id,)).fetchone():
                    conn.execute("DELETE FROM hub_event WHERE id = ?", (hub_id,))
                    dropped += 1
    return {"source_id": source_id, "action": action, "released": len(held), "unmanaged_hub_events": dropped}


# --------------------------------------------------------------------------
# 2. Group, assign and merge
# --------------------------------------------------------------------------


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip().lower()


def google_event_id(dedup_key: str) -> str:
    """SHA-1 of the hub key in lowercase base32hex: an ID Google accepts from clients."""
    return base64.b32hexencode(hashlib.sha1(dedup_key.encode()).digest()).decode().rstrip("=").lower()


def _groups(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Connected sets under: same UID and instance start; or same start, end and normalized title."""
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_by: dict[tuple, int] = {}
    for n, r in enumerate(rows):
        for test in (("uid", r["uid"], r["instance_start"]),
                     ("time", r["start"], r["end"], normalize_title(r["title"]))):
            if test[0] == "time" and not normalize_title(r["title"]):
                continue  # untitled events are only matched by UID
            if test in first_by:
                union(first_by[test], n)
            else:
                first_by[test] = n
    out: dict[int, list] = defaultdict(list)
    for n, r in enumerate(rows):
        out[find(n)].append(r)
    return list(out.values())


def _member_key(r: sqlite3.Row) -> str:
    return f"{r['uid']}|{r['instance_start']}"


def assign_hub(conn: sqlite3.Connection, now: str | None = None) -> dict:
    """Give every group exactly one hub event, sticky across polls, and store each group's merged content."""
    now = now or utcnow()
    stats = {"groups": 0, "new": 0, "merged": 0, "split": 0}
    with tx(conn):
        rows = conn.execute(
            """SELECT e.*, s.name AS source_name FROM source_event e
               JOIN calendar_source s ON s.id = e.source_id WHERE s.enabled = 1
               ORDER BY e.source_id, e.uid, e.instance_start"""
        ).fetchall()
        hubs = {r["id"]: r for r in conn.execute("SELECT id, dedup_key, created_at FROM hub_event")}
        groups = _groups(rows)
        stats["groups"] = len(groups)
        assigned: dict[int, int | None] = {r["id"]: r["hub_event_id"] for r in rows}

        # 1. Splits: a hub event claimed by several groups stays with the largest
        #    (tie: the group holding the lowest member key); the others lose it.
        claims: dict[int, list[int]] = defaultdict(list)
        for g_index, group in enumerate(groups):
            for hub_id in {assigned[r["id"]] for r in group if assigned[r["id"]] in hubs}:
                claims[hub_id].append(g_index)
        for hub_id, claimants in claims.items():
            if len(claimants) < 2:
                continue
            keep = min(claimants, key=lambda g: (-len(groups[g]), min(_member_key(r) for r in groups[g])))
            for g in claimants:
                if g != keep:
                    stats["split"] += 1
                    for r in groups[g]:
                        if assigned[r["id"]] == hub_id:
                            assigned[r["id"]] = None

        taken = {h["dedup_key"] for h in hubs.values()}
        for group in groups:
            # 2. Merges: keep the oldest hub event, repoint every member.
            held = sorted({assigned[r["id"]] for r in group if assigned[r["id"]] in hubs},
                          key=lambda h: (hubs[h]["created_at"], h))
            if held:
                hub_id = held[0]
                stats["merged"] += len(held) - 1
            else:
                # 3. New group: key is the lowest member key, suffixed while another hub event holds it.
                base = min(_member_key(r) for r in group)
                key, n = base, 1
                while key in taken:
                    n += 1
                    key = f"{base}#{n}"
                taken.add(key)
                hub_id = conn.execute(
                    "INSERT INTO hub_event (dedup_key, google_event_id, created_at) VALUES (?, ?, ?)",
                    (key, google_event_id(key), now)).lastrowid
                hubs[hub_id] = {"id": hub_id, "dedup_key": key, "created_at": now}
                stats["new"] += 1
            for r in group:
                if r["hub_event_id"] != hub_id:
                    conn.execute("UPDATE source_event SET hub_event_id = ? WHERE id = ?", (hub_id, r["id"]))
            _store_merged(conn, hub_id, group)
        # Source events of disabled sources no longer exist, and the rest were all repointed above.
    return stats


def _store_merged(conn: sqlite3.Connection, hub_id: int, group: list[sqlite3.Row]) -> None:
    """Title, start and end from the most recently modified member; descriptions that differ are
    concatenated under their source's name; location is the first non-empty one."""
    ordered = sorted(group, key=lambda r: (r["source_id"], _member_key(r)))
    latest = max(ordered, key=lambda r: (r["modified_at"] or "", -ordered.index(r)))
    location = next((r["location"] for r in ordered if r["location"]), None)
    descriptions: dict[str, str] = {}
    for r in ordered:
        if r["description"] and r["description"] not in descriptions:
            descriptions[r["description"]] = r["source_name"]
    if len(descriptions) > 1:
        description = "\n\n".join(f"— {source} —\n{text}" for text, source in descriptions.items())
    else:
        description = next(iter(descriptions), None)
    busy = max((r["busy"] for r in ordered), key=lambda b: BUSY_LEVELS.index(b) if b in BUSY_LEVELS else 2)
    content = {"title": latest["title"], "start": latest["start"], "end": latest["end"],
               "all_day": bool(latest["all_day"]), "location": location, "description": description, "busy": busy}
    conn.execute(
        """UPDATE hub_event SET title = ?, start = ?, "end" = ?, all_day = ?, location = ?, description = ?,
               busy = ?, desired_hash = ? WHERE id = ?""",
        (content["title"], content["start"], content["end"], int(content["all_day"]), content["location"],
         content["description"], content["busy"], _hash(content), hub_id))


# --------------------------------------------------------------------------
# 3. Write the hub calendar
# --------------------------------------------------------------------------


def pending_writes(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT h.id, h.content_hash, h.desired_hash,
                  EXISTS (SELECT 1 FROM source_event e WHERE e.hub_event_id = h.id) AS has_members
           FROM hub_event h"""
    ).fetchall()
    return {"upserts": sum(1 for r in rows if r["has_members"] and r["content_hash"] != r["desired_hash"]),
            "deletes": sum(1 for r in rows if not r["has_members"])}


async def write_hub(conn: sqlite3.Connection, api: gcal.CalendarApi | None, hub: Hub | None) -> dict:
    """Push pending hub changes to Google. Without a hub calendar, only drops never-written empty rows.

    Each write commits on its own, so a failure part-way loses nothing: the
    rest stay pending for the next pass.
    """
    stats = {"inserted": 0, "updated": 0, "deleted": 0}
    empty = conn.execute(
        """SELECT * FROM hub_event h WHERE NOT EXISTS (SELECT 1 FROM source_event e WHERE e.hub_event_id = h.id)"""
    ).fetchall()
    for h in empty:
        if h["content_hash"] is None:
            conn.execute("DELETE FROM hub_event WHERE id = ?", (h["id"],))
        elif api and hub:
            await api.delete_event(hub.calendar_id, h["google_event_id"])
            conn.execute("DELETE FROM hub_event WHERE id = ?", (h["id"],))
            stats["deleted"] += 1
    if not (api and hub):
        return stats
    due = conn.execute(
        """SELECT * FROM hub_event h WHERE desired_hash IS NOT NULL AND content_hash IS NOT desired_hash
           AND EXISTS (SELECT 1 FROM source_event e WHERE e.hub_event_id = h.id) ORDER BY start"""
    ).fetchall()
    for h in due:
        body = gcal.event_body(h["google_event_id"], title=h["title"], start=h["start"], end=h["end"],
                               all_day=bool(h["all_day"]), location=h["location"], description=h["description"],
                               busy=h["busy"], content_hash=h["desired_hash"])
        if h["content_hash"] is None:
            # A 409 means the ID exists, perhaps cancelled: adopt it by replacing it (status confirmed).
            if await api.insert_event(hub.calendar_id, body):
                stats["inserted"] += 1
            else:
                await api.update_event(hub.calendar_id, h["google_event_id"], body)
                stats["updated"] += 1
        elif await api.update_event(hub.calendar_id, h["google_event_id"], body):
            stats["updated"] += 1
        else:
            await api.insert_event(hub.calendar_id, body)  # removed in Google by hand: put it back
            stats["inserted"] += 1
        conn.execute("UPDATE hub_event SET content_hash = ? WHERE id = ?", (h["desired_hash"], h["id"]))
    return stats


# --------------------------------------------------------------------------
# Agenda
# --------------------------------------------------------------------------


def get_agenda(conn: sqlite3.Connection, start: date, end: date, tz: ZoneInfo, *, descriptions: bool = True,
               actions: bool = False, hub: Hub | None = None) -> list[dict]:
    """Hub events that start on days start..end inclusive (owner's zone), in start order.

    Each event lists its sources, and a join link when its description or
    location carries a Teams, Meet or Zoom URL. With actions (the web app),
    each source also says whether Tether can delete the event from it.
    """
    if end < start:
        raise CalendarError("end is before start")
    if (end - start).days > 120:
        raise CalendarError("ask for at most 120 days at a time")
    lo, hi = datetime.combine(start, time(0), tz), datetime.combine(end + timedelta(days=1), time(0), tz)
    rows = conn.execute(
        """SELECT h.* FROM hub_event h
           WHERE EXISTS (SELECT 1 FROM source_event e WHERE e.hub_event_id = h.id)
             AND ((h.all_day = 1 AND h.start >= ? AND h.start < ?) OR (h.all_day = 0 AND h.start >= ? AND h.start < ?))
           ORDER BY h.start""",
        (start.isoformat(), (end + timedelta(days=1)).isoformat(), iso(lo), iso(hi)),
    ).fetchall()
    sources = defaultdict(list)
    for r in conn.execute(
            """SELECT e.hub_event_id, s.id, s.name, s.kind, s.access_role, s.external_ref, s.account_id,
                      a.color, a.status AS account_status, MAX(e.external_id) AS external_id,
                      MAX(e.series_id IS NOT NULL) AS recurring, MIN(e.organizer_self) AS organizer,
                      MAX(e.guests) AS guests
               FROM source_event e JOIN calendar_source s ON s.id = e.source_id JOIN account a ON a.id = s.account_id
               GROUP BY e.hub_event_id, s.id ORDER BY s.id"""):
        source = {"source_id": r["id"], "name": r["name"], "color": r["color"]}
        if actions:
            source.update(deletable=bool(r["external_id"]) and _writable(r, hub), recurring=bool(r["recurring"]),
                          organizer=bool(r["organizer"]), guests=r["guests"])
        sources[r["hub_event_id"]].append(source)
    out = []
    for h in rows:
        text = " ".join(filter(None, [h["location"], h["description"]]))
        join = JOIN_URL.search(text)
        event = {"id": h["id"], "title": h["title"] or "(no title)", "start": h["start"], "end": h["end"],
                 "all_day": bool(h["all_day"]), "location": h["location"], "busy": h["busy"],
                 "join_url": join.group(0) if join else None, "sources": sources[h["id"]]}
        if descriptions:
            d = h["description"] or None
            event["description"] = d[:AGENDA_DESCRIPTION_CAP] + "…" if d and len(d) > AGENDA_DESCRIPTION_CAP else d
        out.append(event)
    # All-day events first within their day.
    return sorted(out, key=lambda e: (_local_day(e, tz), not e["all_day"], e["start"]))


def parse_range(start: str | date | None, end: str | date | None, today: date,
                default_days: int = 14) -> tuple[date, date]:
    """Dates for get_agenda: start defaults to today, end to two weeks on from start."""
    def read(value, name):
        if value is None or value == "":
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            raise CalendarError(f"{name} must be a date (YYYY-MM-DD)") from None
    first = read(start, "start") or today
    last = read(end, "end") or first + timedelta(days=default_days - 1)
    return first, last


def _local_day(event: dict, tz: ZoneInfo) -> str:
    if event["all_day"]:
        return event["start"]
    return parse_iso(event["start"]).astimezone(tz).date().isoformat()


def calendar_status(conn: sqlite3.Connection, hub: Hub | None, hub_label: str | None) -> dict:
    sources = [{**s, "writable": _writable(s, hub)} for s in list_sources(conn)]
    return {"hub": {"configured": hub is not None, "account": hub_label if hub else None,
                    **pending_writes(conn)},
            "sources": sources, "holds": holds(conn)}


# --------------------------------------------------------------------------
# New events
# --------------------------------------------------------------------------

REPEATS = {
    "daily": "RRULE:FREQ=DAILY",
    "weekdays": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekly": "RRULE:FREQ=WEEKLY",      # on the start's weekday
    "monthly": "RRULE:FREQ=MONTHLY",    # on the start's day of the month
    "yearly": "RRULE:FREQ=YEARLY",
}
EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
MAX_GUESTS = 100


@dataclass
class NewEvent:
    """An event the owner creates on one of their own calendars. start and end are local wall-clock
    times ("2026-11-16T09:30") in time_zone, or dates for an all-day event, end inclusive."""
    source_id: int
    start: str
    end: str
    time_zone: str
    title: str = ""
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    guests: list[str] = field(default_factory=list)
    meet: bool = False
    busy: str = "busy"
    repeat: str | None = None
    notify: bool = True


def _writable(source: dict, hub: Hub | None) -> bool:
    is_hub = hub is not None and source["account_id"] == hub.account_id and source["external_ref"] == hub.calendar_id
    return (source["kind"] == "google" and source["access_role"] in ("owner", "writer")
            and source["account_status"] == "ok" and not is_hub)


def new_event_body(ev: NewEvent) -> dict:
    """The Google event for a NewEvent, after checking it. Raises CalendarError with a message for the form."""
    title = ev.title.strip()
    if len(title) > 1024:
        raise CalendarError("The title is too long.")
    try:
        ZoneInfo(ev.time_zone)
    except (ValueError, KeyError):
        raise CalendarError("Unknown time zone.") from None
    if ev.all_day:
        try:
            first, last = date.fromisoformat(ev.start[:10]), date.fromisoformat(ev.end[:10])
        except ValueError:
            raise CalendarError("Dates must look like 2026-11-16.") from None
        if last < first:
            raise CalendarError("The event ends before it starts.")
        # Google's end date is exclusive. The zone is where a repeat is expanded.
        start = {"date": first.isoformat(), "timeZone": ev.time_zone}
        end = {"date": (last + timedelta(days=1)).isoformat(), "timeZone": ev.time_zone}
    else:
        try:
            a, b = datetime.fromisoformat(ev.start), datetime.fromisoformat(ev.end)
        except ValueError:
            raise CalendarError("Times must look like 2026-11-16T09:30.") from None
        if a.tzinfo or b.tzinfo:
            raise CalendarError("Send wall-clock times with a time zone, not offsets.")
        if b <= a:
            raise CalendarError("The event ends before it starts.")
        if b - a > timedelta(days=31):
            raise CalendarError("An event can last at most 31 days.")
        start = {"dateTime": a.isoformat(timespec="minutes") + ":00", "timeZone": ev.time_zone}
        end = {"dateTime": b.isoformat(timespec="minutes") + ":00", "timeZone": ev.time_zone}
    if ev.busy not in ("busy", "free"):
        raise CalendarError("Show as must be busy or free.")
    if ev.repeat and ev.repeat not in REPEATS:
        raise CalendarError("Unknown repeat.")
    guests = list(dict.fromkeys(g.strip().lower() for g in ev.guests if g.strip()))
    if len(guests) > MAX_GUESTS:
        raise CalendarError(f"At most {MAX_GUESTS} guests.")
    bad = [g for g in guests if not EMAIL.match(g)]
    if bad:
        raise CalendarError(f"Not an email address: {bad[0]}")
    location, description = (ev.location or "").strip(), (ev.description or "").strip()
    if len(location) > 1024 or len(description) > 8000:
        raise CalendarError("The location or description is too long.")
    body: dict = {"summary": title, "start": start, "end": end,
                  "transparency": "transparent" if ev.busy == "free" else "opaque",
                  "reminders": {"useDefault": True}}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if guests:
        body["attendees"] = [{"email": g} for g in guests]
    if ev.repeat:
        body["recurrence"] = [REPEATS[ev.repeat]]
    if ev.meet:
        body["conferenceData"] = {"createRequest": {"requestId": secrets.token_hex(12),
                                                    "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    return body


async def create_event(conn: sqlite3.Connection, api: gcal.CalendarApi, ev: NewEvent, hub: Hub | None) -> dict:
    """Create an event on one of the owner's Google calendars."""
    source = next((s for s in list_sources(conn) if s["id"] == ev.source_id), None)
    if source is None:
        raise CalendarError(f"no calendar source {ev.source_id}", status=404)
    if not _writable(source, hub):
        raise CalendarError("Tether can't add events to that calendar.")
    body = new_event_body(ev)
    try:
        created = await api.create_event(source["external_ref"], body, conference=ev.meet,
                                         send_updates="all" if body.get("attendees") and ev.notify else "none")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        log.warning("calendar source %s: event create failed: HTTP %s", source["id"], code)
        if code == 403:
            raise CalendarError("Google refused. Reconnect this account in Settings so Tether may add events, "
                                "then try again.", status=403) from None
        raise CalendarError(f"Google couldn't create the event (HTTP {code}).", status=502) from None
    return {"id": created.get("id"), "link": created.get("htmlLink"), "meet": created.get("hangoutLink"),
            "source_id": source["id"], "shown": bool(source["enabled"])}


# --------------------------------------------------------------------------
# Deleting events
# --------------------------------------------------------------------------


async def delete_event(conn: sqlite3.Connection, client: Callable[[int], gcal.CalendarApi], hub_event_id: int, *,
                       series: bool, notify: bool, hub: Hub | None) -> dict:
    """Delete an agenda event from every Google calendar of the owner's that holds it and Tether may
    change. series deletes the whole repeating series, not just this occurrence. notify emails the
    guests a cancellation (only for events the owner organizes). Copies on calendars Tether can't change
    (an ICS feed, a shared read-only calendar) stay, and are named in "remaining".

    The source events go at once, so the agenda drops the event without waiting for a fetch; the
    next hub pass removes the hub copy. Returns "undo": what restore_events needs to bring it back,
    or None when the deletes can't be cleanly undone (guests were emailed, or the owner was a guest).
    """
    rows = conn.execute(
        """SELECT e.id, e.source_id, e.external_id, e.series_id, e.organizer_self, e.guests,
                  s.name, s.kind, s.access_role, s.external_ref, s.account_id, a.status AS account_status
           FROM source_event e JOIN calendar_source s ON s.id = e.source_id JOIN account a ON a.id = s.account_id
           WHERE e.hub_event_id = ? ORDER BY e.source_id""", (hub_event_id,)).fetchall()
    if not rows:
        raise CalendarError("That event is no longer on the calendar.", status=404)
    targets = [r for r in rows if r["external_id"] and _writable(r, hub)]
    if not targets:
        raise CalendarError("Tether can't delete this event: it's only on calendars Tether can't change.")
    done: dict[tuple[int, str], sqlite3.Row] = {}
    for r in targets:
        event_id = r["series_id"] if series and r["series_id"] else r["external_id"]
        if (r["source_id"], event_id) in done:
            continue
        emails = "all" if notify and r["organizer_self"] and r["guests"] else "none"
        try:
            await client(r["account_id"]).delete_event(r["external_ref"], event_id, send_updates=emails)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            log.warning("calendar source %s: event delete failed: HTTP %s", r["source_id"], code)
            _forget(conn, list(done.values()), series)
            if code == 403:
                raise CalendarError("Google refused. Reconnect this account in Settings so Tether may change "
                                    "events, then try again.", status=403) from None
            raise CalendarError(f"Google couldn't delete the event (HTTP {code}).", status=502) from None
        done[(r["source_id"], event_id)] = r
    _forget(conn, list(done.values()), series)
    clean = all(r["organizer_self"] and not (notify and r["guests"]) for r in done.values())
    deleted_from = {r["source_id"] for r in done.values()}
    return {"deleted": len(done), "series": series,
            "sources": sorted({r["name"] for r in done.values()}),
            "remaining": sorted({r["name"] for r in rows if r["source_id"] not in deleted_from}),
            "undo": [{"source_id": sid, "event_id": eid} for sid, eid in done] if clean else None}


def _forget(conn: sqlite3.Connection, deleted: list[sqlite3.Row], series: bool) -> None:
    """Drop the source events Google just deleted, then regroup so the agenda reflects it."""
    if not deleted:
        return
    with tx(conn):
        for r in deleted:
            if series and r["series_id"]:
                conn.execute("DELETE FROM source_event WHERE source_id = ? AND (series_id = ? OR id = ?)",
                             (r["source_id"], r["series_id"], r["id"]))
            else:
                conn.execute("DELETE FROM source_event WHERE id = ?", (r["id"],))
    assign_hub(conn)


async def restore_events(conn: sqlite3.Connection, client: Callable[[int], gcal.CalendarApi], undo: list[dict],
                         hub: Hub | None) -> list[int]:
    """Undo delete_event: Google keeps a deleted event, cancelled, and setting it confirmed brings it
    back. Returns the sources to fetch again."""
    sources = {s["id"]: s for s in list_sources(conn)}
    restored: list[int] = []
    for u in undo:
        source = sources.get(u.get("source_id"))
        if source is None or not _writable(source, hub) or not u.get("event_id"):
            raise CalendarError("Can't restore that event.")
        try:
            await client(source["account_id"]).restore_event(source["external_ref"], u["event_id"])
        except httpx.HTTPStatusError as exc:
            log.warning("calendar source %s: event restore failed: HTTP %s", source["id"], exc.response.status_code)
            raise CalendarError(f"Google couldn't restore the event (HTTP {exc.response.status_code}).",
                                status=502) from None
        restored.append(source["id"])
    return sorted(set(restored))


def describe_error(exc: Exception) -> str:
    """A short, payload-free description for sync_state."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}"[:300]
