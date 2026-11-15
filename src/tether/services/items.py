"""Inbox items: one row per conversation (spec: "An item is a conversation")."""

import sqlite3
from dataclasses import dataclass

from tether.db import utcnow

UPSERT_FIELDS = ("title", "sender", "snippet", "needed", "url", "last_activity_at", "last_direction", "priority", "due_at")


@dataclass
class UpsertResult:
    id: int
    created: bool


def get_item(conn: sqlite3.Connection, account_id: int, external_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM item WHERE account_id = ? AND external_id = ?", (account_id, external_id)
    ).fetchone()


def upsert_item(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    external_id: str,
    kind: str,
    fields: dict,
    state: str | None = None,
) -> UpsertResult:
    """Insert or update an item on its natural key.

    `state=None` leaves an existing item's state alone (new items start open).
    Rewriting identical values is a no-op apart from updated_at, so reruns are safe.
    """
    unknown = set(fields) - set(UPSERT_FIELDS)
    if unknown:
        raise ValueError(f"unknown item fields: {sorted(unknown)}")
    now = utcnow()
    existing = get_item(conn, account_id, external_id)
    if existing is None:
        cols = ["account_id", "external_id", "kind", "state", "created_at", "updated_at", *fields]
        values = [account_id, external_id, kind, state or "open", now, now, *fields.values()]
        new_id = conn.execute(
            f"INSERT INTO item ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values
        ).lastrowid
        return UpsertResult(new_id, True)

    changes = {k: v for k, v in fields.items() if existing[k] != v}
    if state is not None and existing["state"] != state:
        changes |= _state_change(state, now)
    if changes:
        changes["updated_at"] = now
        assignments = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE item SET {assignments} WHERE id = ?", [*changes.values(), existing["id"]])
    return UpsertResult(existing["id"], False)


def _state_change(state: str, now: str) -> dict:
    """Columns to write when an item moves to `state`. archived_at feeds "recently archived"."""
    return {"state": state, "archived_at": now if state == "archived" else None}


def set_state(conn: sqlite3.Connection, item_id: int, state: str, **extra) -> None:
    """Move an item to `state`. A no-op when it is already there."""
    now = utcnow()
    changes = {**_state_change(state, now), **extra, "updated_at": now}
    assignments = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE item SET {assignments} WHERE id = ? AND state != ?", [*changes.values(), item_id, state])


def get_by_id(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM item WHERE id = ?", (item_id,)).fetchone()


def open_item_ids(conn: sqlite3.Connection, account_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT external_id, id FROM item WHERE account_id = ? AND state = 'open'", (account_id,)
    ).fetchall()
    return {r["external_id"]: r["id"] for r in rows}


def item_states(conn: sqlite3.Connection, account_id: int) -> dict[str, str]:
    rows = conn.execute("SELECT external_id, state FROM item WHERE account_id = ?", (account_id,)).fetchall()
    return {r["external_id"]: r["state"] for r in rows}


VIEW_SQL = """
    SELECT i.id, i.kind, i.title, i.sender, i.snippet, i.needed, i.url, i.last_activity_at,
           i.last_direction, i.state, i.archived_at, i.priority, i.due_at,
           a.id AS account_id, a.label AS account, a.color AS account_color,
           (SELECT COUNT(*) FROM task t WHERE t.item_id = i.id AND t.status = 'open') AS open_tasks
    FROM item i JOIN account a ON a.id = i.account_id
"""


def view(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(f"{VIEW_SQL} WHERE i.id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_items(
    conn: sqlite3.Connection, *, account: str | None = None, state: str | None = "open", limit: int = 100
) -> list[dict]:
    """Inbox items with their account's label and color.

    Open items come newest activity first. Archived items come most recently
    archived first, then items that were never open (sent-only threads).
    """
    where, params = [], []
    if state:
        where.append("i.state = ?")
        params.append(state)
    if account:
        where.append("a.label = ?")
        params.append(account)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = "i.archived_at IS NULL, i.archived_at DESC, " if state == "archived" else ""
    rows = conn.execute(
        f"{VIEW_SQL} {clause} ORDER BY {order}i.last_activity_at DESC LIMIT ?",
        [*params, min(max(limit, 1), 500)],
    ).fetchall()
    return [dict(r) for r in rows]


def open_counts(conn: sqlite3.Connection) -> dict:
    """Open items in total and per account label, for the inbox header and filter chips."""
    rows = conn.execute(
        """SELECT a.label, COUNT(i.id) AS n FROM account a
           LEFT JOIN item i ON i.account_id = a.id AND i.state = 'open'
           GROUP BY a.id ORDER BY a.id"""
    ).fetchall()
    return {"total": sum(r["n"] for r in rows), "by_account": {r["label"]: r["n"] for r in rows}}
