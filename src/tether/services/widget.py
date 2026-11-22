"""Compact data for the Android home-screen widgets: Tasks, Inbox, Agenda and Day.

The widgets poll these every 15-30 minutes, so each answer is small and only
what a widget draws. Every task and event carries `url`, a path in the web app
that opens it there (the app prefixes the site's address).
"""

import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from tether.services import accounts, calendar, items, tasks

MAX_TASKS = 100
MAX_ITEMS = 50          # the web inbox's page size, so every email the widget shows is on page 1 there
SNIPPET_CAP = 160
MAX_DAYS = 14
# Events that started this many days before the first day are still looked at,
# so a multi-day event already under way shows on the days it covers.
LOOKBACK_DAYS = 7


class WidgetError(ValueError):
    """A widget asked for something that is gone. The message is safe to show."""

    def __init__(self, message: str, *, status: int = 404):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- inbox


def widget_inbox(conn: sqlite3.Connection, *, account: str | None = None, limit: int = MAX_ITEMS) -> dict:
    """Open inbox items, newest activity first, from every account or one.

    Each item's url opens it in the web inbox (filtered the same way), where it
    is on the first page because the widget shows at most one page.
    """
    limit = min(max(int(limit), 1), MAX_ITEMS)
    every = accounts.list_accounts(conn)
    chosen = next((a for a in every if a["label"] == account), None) if account else None
    if account and chosen is None:
        raise WidgetError(f"no account {account!r}")
    filt = f"&account={quote(account)}" if account else ""
    rows = items.list_items(conn, account=account, state="open", limit=limit)
    return {
        "count": items.count_items(conn, account=account, state="open"),
        "account": {"label": chosen["label"], "color": chosen["color"]} if chosen else None,
        "accounts": [{"label": a["label"], "color": a["color"]} for a in every],
        "items": [{
            "id": i["id"],
            "sender": i["sender"] or i["account"],
            "title": i["title"],
            "snippet": (i["snippet"] or "")[:SNIPPET_CAP] or None,
            "at": i["last_activity_at"],
            "out": i["last_direction"] == "out",
            "account": i["account"],
            "color": i["account_color"],
            "task": i["open_tasks"] > 0,
            "high": i["priority"] == "high",
            "url": f"/inbox?item={i['id']}{filt}",
        } for i in rows],
    }


# --------------------------------------------------------------------------- tasks


def due_state(due_at: str | None, today: date) -> str | None:
    """overdue, today, tomorrow or later, by the owner's date; None without a due date."""
    if not due_at:
        return None
    due = date.fromisoformat(due_at[:10])
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    return "tomorrow" if due == today + timedelta(days=1) else "later"


def _task(t: dict, today: date) -> dict:
    return {
        "id": t["id"],
        "title": t["title"],
        "due_at": t["due_at"],
        "due": due_state(t["due_at"], today),
        "priority": t["priority"],
        "tags": [{"name": g["name"], "color": g["color"]} for g in t["tags"]],
        "email": {"account": t["item_account"], "color": t["item_color"]} if t["item_id"] else None,
        "url": f"/tasks?task={t['id']}",
    }


def widget_tasks(conn: sqlite3.Connection, today: date, *, tag_id: int | None = None,
                 limit: int = 50) -> dict:
    """Open tasks in the owner's order, each with its open subtasks, as on the Tasks page.

    With tag_id, the same filter as the Tasks page: a task shows when it or one
    of its open subtasks has the tag.
    """
    limit = min(max(int(limit), 1), MAX_TASKS)
    tag = tasks.get_tag(conn, tag_id) if tag_id is not None else None   # TaskError 404 if it was deleted
    rows = [t for t in tasks.task_page(conn, limit=200, tag_id=tag_id)["tasks"] if t["status"] == "open"]
    open_ids = {t["id"] for t in rows}
    heads = [t for t in rows if t["parent_id"] not in open_ids]
    out = []
    for head in heads[:limit]:
        view = _task(head, today)
        view["subtasks"] = [_task(t, today) for t in rows if t["parent_id"] == head["id"]]
        out.append(view)
    return {
        "today": today.isoformat(),
        "count": len(heads),
        "tasks": out,
        "tag": {"id": tag["id"], "name": tag["name"], "color": tag["color"]} if tag else None,
        "tags": [{"id": g["id"], "name": g["name"], "color": g["color"]} for g in tasks.list_tags(conn)],
    }


# --------------------------------------------------------------------------- calendar


def _span(event: dict, tz: ZoneInfo) -> tuple[date, date]:
    """The first and last local day an event covers."""
    if event["all_day"]:
        first = date.fromisoformat(event["start"][:10])
        end = date.fromisoformat(event["end"][:10]) if event["end"] else first + timedelta(days=1)
        return first, max(first, end - timedelta(days=1))   # an all-day end is exclusive
    start = datetime.fromisoformat(event["start"]).astimezone(tz)
    end = datetime.fromisoformat(event["end"]).astimezone(tz) if event["end"] else start
    # An event ending exactly at midnight doesn't reach into the next day.
    last = (end - timedelta(microseconds=1)).date() if end > start else start.date()
    return start.date(), last


def widget_calendar(conn: sqlite3.Connection, start: date, days: int, tz: ZoneInfo, today: date) -> dict:
    """The merged calendar for `days` days from `start`, grouped by the owner's local day.

    An event shows on every day it covers; `continued` and `continues` say
    whether it started before that day or runs past it.
    """
    days = min(max(int(days), 1), MAX_DAYS)
    end = start + timedelta(days=days - 1)
    by_day: dict[date, list[dict]] = {start + timedelta(days=n): [] for n in range(days)}
    for e in calendar.get_agenda(conn, start - timedelta(days=LOOKBACK_DAYS), end, tz, descriptions=False):
        first, last = _span(e, tz)
        source = e["sources"][0] if e["sources"] else {}
        day = max(first, start)
        while day <= min(last, end):
            by_day[day].append({
                "id": e["id"],
                "title": e["title"],
                "start": e["start"],
                "end": e["end"],
                "all_day": e["all_day"],
                "location": e["location"],
                "busy": e["busy"],
                "join_url": e["join_url"],
                "color": source.get("color"),
                "calendar": source.get("name"),
                "continued": first < day,
                "continues": last > day,
                "url": f"/calendar/day?date={day.isoformat()}&event={e['id']}",
            })
            day += timedelta(days=1)
    return {
        "today": today.isoformat(),
        "tz": tz.key,
        "start": start.isoformat(),
        "days": [{"date": d.isoformat(), "events": sorted(evs, key=lambda e: (not e["all_day"], e["start"]))}
                 for d, evs in by_day.items()],
    }
