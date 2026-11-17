"""Tasks (spec: "Task rules").

- Two levels: a task with a parent cannot itself be a parent.
- Completing a parent completes its open subtasks.
- Completing a task archives its linked item once no other open task links to
  it. That archive writes through to Gmail, like any other.
- Due dates are local calendar dates (YYYY-MM-DD) in the owner's time zone.
- Tags: any number per task, named by the owner. Setting a task's tags by name
  creates the ones that don't exist yet.
"""

import re
import sqlite3
from datetime import date

from tether.db import tx, utcnow
from tether.services import accounts, items, triage
from tether.services.triage import GmailFactory

PRIORITIES = ("high", "normal", "low")
EDITABLE = ("title", "notes", "due_at", "priority", "sort_order", "parent_id", "tags")
MAX_TAG_LENGTH = 40
MAX_TAGS_PER_TASK = 20
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


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
    return _with_tags(conn, [dict(row)])[0]


def list_tasks(conn: sqlite3.Connection, *, status: str | None = "open", parent_id: int | None = None,
               tag: str | None = None, done_limit: int = 50) -> list[dict]:
    """Tasks in display order: each top-level task followed by its subtasks.

    status="open" lists open tasks. status="done" lists the most recently
    completed, up to done_limit. None lists everything. tag (a name) keeps
    tasks with that tag.
    """
    where, params = [], []
    if tag is not None:
        tag_id = _tag_id(conn, tag)
        where.append("EXISTS (SELECT 1 FROM task_tag x WHERE x.task_id = t.id AND x.tag_id = ?)")
        params.append(tag_id)
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
        return _with_tags(conn, [dict(r) for r in rows])
    # Group subtasks under their parent: order by the parent's position, then the task's own.
    rows = conn.execute(
        f"""{VIEW_SQL} LEFT JOIN task p ON p.id = t.parent_id {clause}
            ORDER BY COALESCE(p.sort_order, t.sort_order), COALESCE(t.parent_id, t.id),
                     t.parent_id IS NOT NULL, t.sort_order, t.id""",
        params,
    ).fetchall()
    return _with_tags(conn, [dict(r) for r in rows])


def task_page(conn: sqlite3.Connection, *, offset: int = 0, limit: int = 50, tag_id: int | None = None) -> dict:
    """Every task, for the Tasks page: open tasks oldest first, each followed by its open
    subtasks, then completed tasks most recently completed first.

    Paging counts groups (a task with its subtasks), so a page never splits one.
    With tag_id, an open group is shown whole when the task or any of its open
    subtasks has the tag; a completed task shows when it has the tag itself.
    """
    rows = _with_tags(conn, [dict(r) for r in conn.execute(f"{VIEW_SQL} ORDER BY t.created_at, t.id").fetchall()])
    open_rows = [r for r in rows if r["status"] == "open"]
    open_ids = {r["id"] for r in open_rows}
    children: dict[int, list[dict]] = {}
    for r in open_rows:
        if r["parent_id"] in open_ids:
            children.setdefault(r["parent_id"], []).append(r)
    groups = [[r, *children.get(r["id"], [])] for r in open_rows if r["parent_id"] not in open_ids]
    done = sorted((r for r in rows if r["status"] == "done"),
                  key=lambda r: (r["completed_at"] or "", r["parent_id"] is None, -r["id"]), reverse=True)
    if tag_id is not None:
        def tagged(r: dict) -> bool:
            return any(t["id"] == tag_id for t in r["tags"])
        groups = [g for g in groups if any(map(tagged, g))]
        done = [r for r in done if tagged(r)]
        open_count_shown = sum(len(g) for g in groups)
    groups += [[r] for r in done]
    offset, limit = max(offset, 0), min(max(limit, 1), 200)
    return {
        "tasks": [t for g in groups[offset:offset + limit] for t in g],
        "total": len(groups),
        "open": open_count_shown if tag_id is not None else len(open_rows),
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
                notes: str | None = None, due_at: str | None = None, priority: str | None = None,
                tags: list[str] | None = None) -> dict:
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
        if tags:
            _set_tags(conn, task_id, tags)
    return get_task(conn, task_id)


def update_task(conn: sqlite3.Connection, task_id: int, fields: dict) -> dict:
    """Edit title, notes, due date, priority, order, parent or tags (a list of names, replacing the
    task's tags; new names become new tags). A None value clears the field."""
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise TaskError(f"can't edit {', '.join(sorted(unknown))}; editable: {', '.join(EDITABLE)}")
    get_task(conn, task_id)
    changes = _check_fields(fields)
    tags = changes.pop("tags", None)
    if "tags" in fields:
        changes["updated_at"] = utcnow()
    with tx(conn):
        if tags is not None or "tags" in fields:
            _set_tags(conn, task_id, tags or [])
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
        conn.execute("DELETE FROM task_tag WHERE task_id IN (SELECT id FROM task WHERE id = ? OR parent_id = ?)",
                     (task_id, task_id))
        conn.execute("DELETE FROM task WHERE parent_id = ?", (task_id,))
        conn.execute("DELETE FROM task WHERE id = ?", (task_id,))


def due_tasks(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Urgent rule 2: open tasks overdue or due today, earliest first."""
    rows = conn.execute(
        f"{VIEW_SQL} WHERE t.status = 'open' AND t.due_at IS NOT NULL AND t.due_at <= ? "
        "ORDER BY t.due_at, t.sort_order",
        (today.isoformat(),),
    ).fetchall()
    return _with_tags(conn, [dict(r) for r in rows])


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM task WHERE status = 'open' AND parent_id IS NULL").fetchone()[0]


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------


def _with_tags(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Each task with its tags, by name."""
    by_task: dict[int, list[dict]] = {}
    if rows:
        ids = [r["id"] for r in rows]
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            for t in conn.execute(
                    f"""SELECT x.task_id, g.id, g.name, g.color FROM task_tag x JOIN tag g ON g.id = x.tag_id
                        WHERE x.task_id IN ({",".join("?" * len(part))}) ORDER BY g.name COLLATE NOCASE""", part):
                by_task.setdefault(t["task_id"], []).append({"id": t["id"], "name": t["name"], "color": t["color"]})
    return [{**r, "tags": by_task.get(r["id"], [])} for r in rows]


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip().lstrip("#").strip()
    if not name:
        raise TaskError("a tag needs a name")
    if len(name) > MAX_TAG_LENGTH:
        raise TaskError(f"tag names are at most {MAX_TAG_LENGTH} characters")
    return name


def _check_color(color: str) -> str:
    if not HEX.match(color or ""):
        raise TaskError("color must look like #4377f0")
    return color.lower()


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    """Every tag, by name, with how many open and done tasks carry it."""
    rows = conn.execute(
        """SELECT g.id, g.name, g.color,
                  COUNT(t.id) FILTER (WHERE t.status = 'open') AS open_tasks,
                  COUNT(t.id) FILTER (WHERE t.status = 'done') AS done_tasks
           FROM tag g LEFT JOIN task_tag x ON x.tag_id = g.id LEFT JOIN task t ON t.id = x.task_id
           GROUP BY g.id ORDER BY g.name COLLATE NOCASE""").fetchall()
    return [dict(r) for r in rows]


def _tag_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM tag WHERE name = ?", (_clean_name(name),)).fetchone()
    if row is None:
        raise TaskError(f"no tag named {name!r}", status=404)
    return row["id"]


def _ensure_tag(conn: sqlite3.Connection, name: str, color: str | None = None) -> int:
    name = _clean_name(name)
    row = conn.execute("SELECT id FROM tag WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    if color is None:
        # Next palette color in turn, so neighbouring new tags differ.
        count = conn.execute("SELECT COUNT(*) FROM tag").fetchone()[0]
        color = accounts.PALETTE[count % len(accounts.PALETTE)]
    return conn.execute("INSERT INTO tag (name, color, created_at) VALUES (?, ?, ?)",
                        (name, _check_color(color), utcnow())).lastrowid


def create_tag(conn: sqlite3.Connection, name: str, color: str | None = None) -> dict:
    """A new tag; an existing name (ignoring case) returns that tag unchanged."""
    with tx(conn):
        tag_id = _ensure_tag(conn, name, color)
    return get_tag(conn, tag_id)


def get_tag(conn: sqlite3.Connection, tag_id: int) -> dict:
    tag = next((t for t in list_tags(conn) if t["id"] == tag_id), None)
    if tag is None:
        raise TaskError(f"no tag {tag_id}", status=404)
    return tag


def update_tag(conn: sqlite3.Connection, tag_id: int, *, name: str | None = None, color: str | None = None) -> dict:
    get_tag(conn, tag_id)
    with tx(conn):
        if name is not None:
            name = _clean_name(name)
            clash = conn.execute("SELECT id FROM tag WHERE name = ? AND id != ?", (name, tag_id)).fetchone()
            if clash:
                raise TaskError(f"there's already a tag named {name!r}", status=409)
            conn.execute("UPDATE tag SET name = ? WHERE id = ?", (name, tag_id))
        if color is not None:
            conn.execute("UPDATE tag SET color = ? WHERE id = ?", (_check_color(color), tag_id))
    return get_tag(conn, tag_id)


def delete_tag(conn: sqlite3.Connection, tag_id: int) -> None:
    """Delete a tag; its tasks stay, without it."""
    get_tag(conn, tag_id)
    with tx(conn):
        conn.execute("DELETE FROM task_tag WHERE tag_id = ?", (tag_id,))
        conn.execute("DELETE FROM tag WHERE id = ?", (tag_id,))


def _set_tags(conn: sqlite3.Connection, task_id: int, names: list[str]) -> None:
    """Replace a task's tags with these names, creating new tags as needed. Call inside tx."""
    if isinstance(names, str):
        raise TaskError("tags must be a list of names")
    ids = list(dict.fromkeys(_ensure_tag(conn, n) for n in names))
    if len(ids) > MAX_TAGS_PER_TASK:
        raise TaskError(f"a task can have at most {MAX_TAGS_PER_TASK} tags")
    conn.execute("DELETE FROM task_tag WHERE task_id = ?", (task_id,))
    conn.executemany("INSERT INTO task_tag (task_id, tag_id) VALUES (?, ?)", [(task_id, i) for i in ids])
