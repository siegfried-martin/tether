"""The home screen as data (spec: "Home screen: what counts as urgent").

Urgent is a fixed rule list, in display order:
1. Source alerts: auth failures and stale sources.
2. Tasks overdue or due today.
3. Follow-ups past due (phase 6).
4. Open items with high priority, or due today or earlier.

Held calendar deletes are source alerts. Today's agenda comes from the hub.
"""

import sqlite3
from datetime import date
from zoneinfo import ZoneInfo

from tether.services import calendar, items, sync_state, tasks


def urgent_items(conn: sqlite3.Connection, today: date) -> list[dict]:
    rows = conn.execute(
        f"""{items.VIEW_SQL}
            WHERE i.state = 'open' AND (i.priority = 'high' OR substr(i.due_at, 1, 10) <= ?)
            ORDER BY i.due_at IS NULL, i.due_at, i.last_activity_at DESC""",
        (today.isoformat(),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_overview(conn: sqlite3.Connection, today: date, tz: ZoneInfo = ZoneInfo("UTC")) -> dict:
    counts = items.open_counts(conn)
    return {
        "today": today.isoformat(),
        "urgent": {
            "alerts": sync_state.source_alerts(conn) + calendar.hold_alerts(conn),
            "tasks": tasks.due_tasks(conn, today),
            "follow_ups": [],
            "items": urgent_items(conn, today),
        },
        "inbox": {"count": counts["total"], "by_account": counts["by_account"],
                  "preview": items.list_items(conn, state="open", limit=5)},
        "tasks": {"count": tasks.open_count(conn)},
        "calendar": {"today": calendar.get_agenda(conn, today, today, tz, descriptions=False),
                     "holds": calendar.holds(conn)},
    }
