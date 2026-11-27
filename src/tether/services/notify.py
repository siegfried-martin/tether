"""Notifications: new mail and event reminders, sent by Web Push to every device that asked.

A device (a browser, or the Android app through Chrome) subscribes from Settings and
chooses mail, reminders or both. Every pass looks for:

- mail: open inbox emails whose latest message came in within the last hour and
  hasn't been notified yet, one notification per new message;
- reminders: hub events whose notification time has come, using the event's own
  notification times when it has them, else 30 minutes before (timed events only:
  an all-day event is reminded only when it says when).

Each notification is logged by key before the next pass, so nothing goes out twice,
and a restart catches up on anything that fell due in the last 15 minutes.
Notification text is encrypted end to end; logs carry counts only.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from tether.config import Settings
from tether.connectors import webpush
from tether.db import iso, parse_iso, tx, utcnow

log = logging.getLogger(__name__)

DEFAULT_REMINDER = 30          # minutes before a timed event that says nothing about notifications
GRACE = timedelta(minutes=15)  # a reminder still goes out this late (after a restart or a slow fetch)
MAIL_FRESH = timedelta(hours=1)
MAIL_SUMMARY_OVER = 4          # more new emails than this in one pass: one summary instead
LOOKAHEAD = timedelta(days=29)  # Google allows notifications up to four weeks ahead
KEEP_LOG = timedelta(days=40)


@dataclass(frozen=True)
class Message:
    key: str | None            # notification_log key; None for a summary or a test
    kind: str                  # "mail" or "events": which devices want it
    payload: dict
    ttl: int = 6 * 3600


# --------------------------------------------------------------------------- subscriptions


def _view(row: sqlite3.Row | None) -> dict | None:
    return {"mail": bool(row["mail"]), "events": bool(row["events"])} if row else None


def get_subscription(conn: sqlite3.Connection, endpoint: str) -> dict | None:
    return _view(conn.execute("SELECT * FROM push_subscription WHERE endpoint = ?", (endpoint,)).fetchone())


def save_subscription(conn: sqlite3.Connection, endpoint: str, p256dh: str, auth: str, *,
                      mail: bool | None = None, events: bool | None = None,
                      replaces: str | None = None) -> dict:
    """Add a device, or refresh its keys. Preferences left out keep their saved value (on for a new
    device). replaces: the endpoint the browser had before it renewed the subscription."""
    if not endpoint.startswith("https://"):
        raise ValueError("a push endpoint is an https URL")
    with tx(conn):
        old = conn.execute("SELECT * FROM push_subscription WHERE endpoint IN (?, ?) ORDER BY endpoint = ? DESC",
                           (endpoint, replaces or endpoint, endpoint)).fetchone()
        keep_mail = bool(old["mail"]) if old else True
        keep_events = bool(old["events"]) if old else True
        if replaces and replaces != endpoint:
            conn.execute("DELETE FROM push_subscription WHERE endpoint = ?", (replaces,))
        conn.execute(
            """INSERT INTO push_subscription (endpoint, p256dh, auth, mail, events, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth,
                   mail = excluded.mail, events = excluded.events, failures = 0""",
            (endpoint, p256dh, auth, int(keep_mail if mail is None else mail),
             int(keep_events if events is None else events), old["created_at"] if old else utcnow()))
    return get_subscription(conn, endpoint)


def delete_subscription(conn: sqlite3.Connection, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscription WHERE endpoint = ?", (endpoint,))


# --------------------------------------------------------------------------- what is due


def _sent(conn: sqlite3.Connection, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    marks = ",".join("?" * len(keys))
    return {r["key"] for r in conn.execute(f"SELECT key FROM notification_log WHERE key IN ({marks})", keys)}


def due_mail(conn: sqlite3.Connection, now: datetime) -> list[Message]:
    """New inbound messages in the inbox since the last pass (and since the first device subscribed)."""
    first = conn.execute("SELECT MIN(created_at) AS t FROM push_subscription WHERE mail = 1").fetchone()["t"]
    if first is None:
        return []
    since = max(iso(now - MAIL_FRESH), first)
    rows = conn.execute(
        """SELECT i.id, i.title, i.sender, i.last_activity_at, a.label AS account
           FROM item i JOIN account a ON a.id = i.account_id
           WHERE i.kind = 'email' AND i.state = 'open' AND i.last_direction = 'in' AND i.last_activity_at >= ?
           ORDER BY i.last_activity_at""", (since,)).fetchall()
    keyed = [(f"mail:{r['id']}:{r['last_activity_at']}", r) for r in rows]
    done = _sent(conn, [k for k, _ in keyed])
    fresh = [(k, r) for k, r in keyed if k not in done]
    if len(fresh) > MAIL_SUMMARY_OVER:
        senders = list(dict.fromkeys(r["sender"] or "?" for _, r in reversed(fresh)))
        body = ", ".join(senders[:5]) + (f" and {len(senders) - 5} more" if len(senders) > 5 else "")
        summary = Message(None, "mail", {"title": f"{len(fresh)} new emails", "body": body, "tag": "mail",
                                         "url": "/inbox"})
        return [summary, *(Message(k, "mail", {}) for k, _ in fresh)]   # empty payload: log only
    return [Message(k, "mail", {
        "title": r["sender"] or r["account"],
        "body": f"{r['title'] or '(no subject)'}\n{r['account']}",
        "tag": f"mail-{r['id']}",
        "url": f"/inbox?item={r['id']}",
        "ts": int(parse_iso(r["last_activity_at"]).timestamp() * 1000),
    }) for k, r in fresh]


def _minutes(stored: list[str | None], all_day: bool) -> set[int]:
    """The hub event's notification times from its copies' (each '' none, None unknown)."""
    known = [s for s in stored if s is not None]
    if not known:
        return set() if all_day else {DEFAULT_REMINDER}
    return {int(m) for s in known for m in s.split(",") if m}


def due_reminders(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> list[Message]:
    if not conn.execute("SELECT 1 FROM push_subscription WHERE events = 1").fetchone():
        return []
    today = now.astimezone(tz).date()
    rows = conn.execute(
        """SELECT h.id, h.title, h.start, h."end", h.all_day, h.location,
                  GROUP_CONCAT(COALESCE(e.reminders, '?'), '|') AS reminders
           FROM hub_event h JOIN source_event e ON e.hub_event_id = h.id
           JOIN calendar_source s ON s.id = e.source_id AND s.enabled = 1
           WHERE (h.all_day = 0 AND h.start > ? AND h.start <= ?) OR (h.all_day = 1 AND h.start >= ? AND h.start <= ?)
           GROUP BY h.id""",
        (iso(now - GRACE), iso(now + LOOKAHEAD), today.isoformat(), (today + LOOKAHEAD).isoformat())).fetchall()
    due = []
    for r in rows:
        all_day = bool(r["all_day"])
        start = datetime.combine(date.fromisoformat(r["start"]), time(0), tz) if all_day else parse_iso(r["start"])
        stored = [None if s == "?" else s for s in r["reminders"].split("|")]
        for minutes in sorted(_minutes(stored, all_day)):
            fire = start - timedelta(minutes=minutes)
            if fire <= now < fire + GRACE:
                due.append((f"event:{r['id']}:{r['start']}:{minutes}", r, start))
    done = _sent(conn, [k for k, _, _ in due])
    out, seen = [], set()
    for key, r, start in due:
        if key in done or r["id"] in seen:   # two notification times at once: one notification
            if key not in done:
                out.append(Message(key, "events", {}))
            continue
        seen.add(r["id"])
        out.append(Message(key, "events", {
            "title": r["title"] or "(no title)",
            "body": " · ".join(filter(None, [_when(r, start, now, tz), r["location"]])),
            "tag": f"event-{r['id']}",
            "url": f"/calendar?date={start.astimezone(tz).date().isoformat()}&event={r['id']}",
        }, ttl=max(60, int((start - now).total_seconds()) + 15 * 60)))
    return out


def _clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def _when(r: sqlite3.Row, start: datetime, now: datetime, tz: ZoneInfo) -> str:
    day = start.astimezone(tz).date()
    today = now.astimezone(tz).date()
    day_name = ("Today" if day == today else "Tomorrow" if day == today + timedelta(days=1)
                else start.astimezone(tz).strftime("%a, %b ") + str(day.day))
    if r["all_day"]:
        return f"{day_name} · All day"
    end = parse_iso(r["end"]).astimezone(tz)
    first, last = _clock(start.astimezone(tz)), _clock(end)
    if first[-3:] == last[-3:] and end.date() == start.astimezone(tz).date():
        first = first[:-3]   # "10:30 – 11:30 AM"
    span = f"{first} – {last}"
    left = (start - now).total_seconds() / 60
    if left <= 1:
        return f"Now · {span}"
    if left < 60:
        return f"In {round(left)} min · {span}"
    if day == today:
        return f"Today · {span}"
    return f"{day_name} · {span}"


# --------------------------------------------------------------------------- sending


class Notifier:
    TICK = 20

    def __init__(self, conn: sqlite3.Connection, http: httpx.AsyncClient, settings: Settings):
        self.conn, self.http, self.settings = conn, http, settings
        self.vapid = (webpush.Vapid.from_private(settings.vapid_private_key, settings.vapid_subject)
                      if settings.vapid_private_key else None)
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def run_forever(self) -> None:
        if self.vapid is None:
            log.info("notifications off: no VAPID key configured")
            return
        while True:
            try:
                await self.tick()
            except Exception as exc:  # the loop must survive anything one pass throws
                log.warning("notification pass failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.TICK)
            except TimeoutError:
                pass
            self._wake.clear()

    async def tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        messages = due_mail(self.conn, now) + due_reminders(self.conn, now, self.settings.tz)
        stats = {"mail": 0, "events": 0}
        for m in messages:
            if m.key:
                # Logged first: a device that fails now isn't retried on every pass.
                self.conn.execute("INSERT OR IGNORE INTO notification_log (key, sent_at) VALUES (?, ?)",
                                  (m.key, iso(now)))
            if m.payload:
                await self.push(m)
                stats[m.kind] += 1
        self.conn.execute("DELETE FROM notification_log WHERE sent_at < ?", (iso(now - KEEP_LOG),))
        if any(stats.values()):
            log.info("notifications: %s", stats)
        return stats

    async def push(self, message: Message, *, endpoint: str | None = None) -> int:
        """Send to every device that wants this kind (or just one). Returns how many took it."""
        if self.vapid is None:
            return 0
        column = "mail" if message.kind == "mail" else "events"
        if endpoint:
            subs = self.conn.execute("SELECT * FROM push_subscription WHERE endpoint = ?", (endpoint,)).fetchall()
        else:
            subs = self.conn.execute(f"SELECT * FROM push_subscription WHERE {column} = 1").fetchall()
        ok = 0
        for sub in subs:
            try:
                await webpush.send(self.http, self.vapid, sub["endpoint"], sub["p256dh"], sub["auth"],
                                   message.payload, ttl=message.ttl, urgency="high")
            except webpush.Gone:
                log.info("push subscription %s is gone; removed", sub["id"])
                delete_subscription(self.conn, sub["endpoint"])
            except (httpx.HTTPError, ValueError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                log.warning("push subscription %s: send failed (%s)", sub["id"], status)
                self.conn.execute("UPDATE push_subscription SET failures = failures + 1 WHERE id = ?", (sub["id"],))
            else:
                ok += 1
                self.conn.execute("UPDATE push_subscription SET last_ok_at = ?, failures = 0 WHERE id = ?",
                                  (utcnow(), sub["id"]))
        return ok

    async def test(self, endpoint: str) -> bool:
        message = Message(None, "test", {"title": "Tether", "body": "Notifications are on for this device.",
                                         "tag": "test", "url": "/settings"}, ttl=600)
        return await self.push(message, endpoint=endpoint) > 0
