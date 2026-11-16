"""Tasks (spec: "Task rules").

- Two levels: a task with a parent cannot itself be a parent.
- Completing a parent completes its open subtasks.
- Completing a task archives its linked item once no other open task links to
  it. That archive writes through to Gmail, like any other.
- Due dates are local calendar dates (YYYY-MM-DD) in the owner's time zone.
"""

import sqlite3
from datetime import date

from tether.db import tx, utcnow
from tether.services import items, triage
from tether.services.triage import GmailFactory

PRIORITIES = ("high", "normal", "low")
EDITABLE = ("title", "notes", "due_at", "priority", "sort_order", "parent_id")


class TaskError(ValueError):
    """Invalid task operation. The message is safe to show the user."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


VIEW_SQL = """
    SELECT t.id, t.parent_id, t.title, t.notes, t.status, t.due_at, t.priority, t.sort_order,
           t.item_id, t.completed_at, t.created_at, t.updated_at,
           i.title AS item_title, i.url AS item_url, i.state AS item_state, i.kind AS item_kind,
           a.label AS item_account, a.color AS item_color
    FROM task t
    LEFT JOIN item i ON i.id = t.item_id
    LEFT JOIN account a ON a.id = i.account_id
"""


def get_task(conn: sqlite3.Connection, task_id: int) -> dict:
    row = conn.execute(f"{VIEW_SQL} WHERE t.id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskError(f"no task {task_id}", status=404)
    return dict(row)


def list_tasks(conn: sqlite3.Connection, *, status: str | None = "open", parent_id: int | None = None,
               done_limit: int = 50) -> list[dict]:
    """Tasks in display order: each top-level task followed by its subtasks.

    status="open" lists open tasks. status="done" lists the most recently
    completed, up to done_limit. None lists everything.
    """
    where, params = [], []
    if status not in (None, "open", "done"):
        raise TaskError("status must be open or done")
    if status:
        where.append("t.status = ?")
        params.append(status)
    if parent_id is not None:
        where.append("t.parent_id = ?")
        params.append(parent_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    if status == "done":
        rows = conn.execute(f"{VIEW_SQL} {clause} ORDER BY t.completed_at DESC LIMIT ?",
                            [*params, done_limit]).fetchall()
        return [dict(r) for r in rows]
    # Group subtasks under their parent: order by the parent's position, then the task's own.
    rows = conn.execute(
        f"""{VIEW_SQL} LEFT JOIN task p ON p.id = t.parent_id {clause}
            ORDER BY COALESCE(p.sort_order, t.sort_order), COALESCE(t.parent_id, t.id),
                     t.parent_id IS NOT NULL, t.sort_order, t.id""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def task_page(conn: sqlite3.Connection, *, offset: int = 0, limit: int = 50) -> dict:
    """Every task, for the Tasks page: open tasks oldest first, each followed by its open
    subtasks, then completed tasks most recently completed first.

    Paging counts groups (a task with its subtasks), so a page never splits one.
    """
    rows = [dict(r) for r in conn.execute(f"{VIEW_SQL} ORDER BY t.created_at, t.id").fetchall()]
    open_rows = [r for r in rows if r["status"] == "open"]
    open_ids = {r["id"] for r in open_rows}
    children: dict[int, list[dict]] = {}
    for r in open_rows:
        if r["parent_id"] in open_ids:
            children.setdefault(r["parent_id"], []).append(r)
    groups = [[r, *children.get(r["id"], [])] for r in open_rows if r["parent_id"] not in open_ids]
    done = sorted((r for r in rows if r["status"] == "done"),
                  key=lambda r: (r["completed_at"] or "", r["parent_id"] is None, -r["id"]), reverse=True)
    groups += [[r] for r in done]
    offset, limit = max(offset, 0), min(max(limit, 1), 200)
    return {
        "tasks": [t for g in groups[offset:offset + limit] for t in g],
        "total": len(groups),
        "open": len(open_rows),
    }


def _check_parent(conn: sqlite3.Connection, parent_id: int | None, task_id: int | None = None) -> None:
    if parent_id is None:
        return
    if parent_id == task_id:
        raise TaskError("a task can't be its own parent")
    parent = conn.execute("SELECT id, parent_id FROM task WHERE id = ?", (parent_id,)).fetchone()
    if parent is None:
        raise TaskError(f"no parent task {parent_id}", status=404)
    if parent["parent_id"] is not None:
        raise TaskError("subtasks can't have subtasks: tasks are two levels deep")
    if task_id is not None:
        if conn.execute("SELECT 1 FROM task WHERE parent_id = ?", (task_id,)).fetchone():
            raise TaskError("this task has subtasks, so it can't become a subtask")


def _check_fields(fields: dict) -> dict:
    out = dict(fields)
    if "title" in out:
        out["title"] = (out["title"] or "").strip()
        if not out["title"]:
            raise TaskError("a task needs a title")
    if out.get("priority") is not None and out["priority"] not in PRIORITIES:
        raise TaskError("priority must be high, normal or low")
    if out.get("due_at") is not None:
        try:
            out["due_at"] = date.fromisoformat(str(out["due_at"])[:10]).isoformat()
        except ValueError as exc:
            raise TaskError("due_at must be a date like 2026-11-20") from exc
    if "notes" in out:
        out["notes"] = (out["notes"] or "").strip() or None
    return out


def _next_sort_order(conn: sqlite3.Connection, parent_id: int | None) -> float:
    row = conn.execute("SELECT MAX(sort_order) FROM task WHERE parent_id IS ?", (parent_id,)).fetchone()
    return (row[0] or 0) + 1


def create_task(conn: sqlite3.Connection, *, title: str, parent_id: int | None = None, item_id: int | None = None,
                notes: str | None = None, due_at: str | None = None, priority: str | None = None) -> dict:
    fields = _check_fields({"title": title, "notes": notes, "due_at": due_at, "priority": priority})
    with tx(conn):
        _check_parent(conn, parent_id)
        if item_id is not None and items.get_by_id(conn, item_id) is None:
            raise TaskError(f"no item {item_id}", status=404)
        now = utcnow()
        task_id = conn.execute(
            """INSERT INTO task (parent_id, title, notes, due_at, priority, sort_order, item_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (parent_id, fields["title"], fields["notes"], fields["due_at"], fields["priority"],
             _next_sort_order(conn, parent_id), item_id, now, now),
        ).lastrowid
    return get_task(conn, task_id)


def update_task(conn: sqlite3.Connection, task_id: int, fields: dict) -> dict:
    """Edit title, notes, due date, priority, order or parent. A None value clears the field."""
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise TaskError(f"can't edit {', '.join(sorted(unknown))}; editable: {', '.join(EDITABLE)}")
    get_task(conn, task_id)
    changes = _check_fields(fields)
    with tx(conn):
        if "parent_id" in changes:
            _check_parent(conn, changes["parent_id"], task_id)
            if "sort_order" not in changes:
                changes["sort_order"] = _next_sort_order(conn, changes["parent_id"])
        if changes:
            changes["updated_at"] = utcnow()
            assignments = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE task SET {assignments} WHERE id = ?", [*changes.values(), task_id])
    return get_task(conn, task_id)


async def complete_task(conn: sqlite3.Connection, gmail: GmailFactory, task_id: int) -> dict:
    """Complete a task and its open subtasks, then archive items nothing else is waiting on.

    The completion always stands. If archiving a linked item fails (Gmail
    down, account needs reconnecting), the result carries a warning and the
    item stays open, so nothing is lost.
    """
    task = get_task(conn, task_id)
    if task["status"] == "done":
        return {**task, "archived_items": [], "warnings": []}
    now = utcnow()
    with tx(conn):
        done_ids = [task_id] + [r["id"] for r in conn.execute(
            "SELECT id FROM task WHERE parent_id = ? AND status = 'open'", (task_id,))]
        marks = ",".join("?" * len(done_ids))
        conn.execute(f"UPDATE task SET status = 'done', completed_at = ?, updated_at = ? WHERE id IN ({marks})",
                     [now, now, *done_ids])
        linked = [r["item_id"] for r in conn.execute(
            f"SELECT DISTINCT item_id FROM task WHERE id IN ({marks}) AND item_id IS NOT NULL", done_ids)]
    archived, warnings = [], []
    for item_id in linked:
        still_needed = conn.execute(
            "SELECT 1 FROM task WHERE item_id = ? AND status = 'open'", (item_id,)).fetchone()
        item = items.get_by_id(conn, item_id)
        if still_needed or item is None or item["state"] != "open":
            continue
        try:
            await triage.archive_item(conn, gmail, item_id)
            archived.append(item_id)
        except triage.TriageError as exc:
            warnings.append(f"Task done, but its email wasn't archived: {exc}")
    return {**get_task(conn, task_id), "archived_items": archived, "warnings": warnings}


def reopen_task(conn: sqlite3.Connection, task_id: int) -> dict:
    """Reopen a completed task. Reopening a subtask reopens its parent too."""
    task = get_task(conn, task_id)
    now = utcnow()
    with tx(conn):
        ids = [task_id] + ([task["parent_id"]] if task["parent_id"] else [])
        marks = ",".join("?" * len(ids))
        conn.execute(f"""UPDATE task SET status = 'open', completed_at = NULL, updated_at = ?
                         WHERE id IN ({marks}) AND status = 'done'""", [now, *ids])
    return get_task(conn, task_id)


def delete_task(conn: sqlite3.Connection, task_id: int) -> None:
    """Delete a task and its subtasks. Linked items are untouched."""
    get_task(conn, task_id)
    with tx(conn):
        conn.execute("DELETE FROM task WHERE parent_id = ?", (task_id,))
        conn.execute("DELETE FROM task WHERE id = ?", (task_id,))


def due_tasks(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Urgent rule 2: open tasks overdue or due today, earliest first."""
    rows = conn.execute(
        f"{VIEW_SQL} WHERE t.status = 'open' AND t.due_at IS NOT NULL AND t.due_at <= ? "
        "ORDER BY t.due_at, t.sort_order",
        (today.isoformat(),),
    ).fetchall()
    return [dict(r) for r in rows]


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM task WHERE status = 'open' AND parent_id IS NULL").fetchone()[0]
