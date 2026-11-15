"""Task rules: two levels, cascading completion, the archive rule, due-today urgency."""

from datetime import date

import httpx
import pytest
from conftest import msg

from test_web import client, login, settings  # noqa: F401  (fixtures)
from tether.services import items, overview, tasks

TODAY = date(2026, 11, 15)


@pytest.fixture
def acct(conn):
    return conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'me@x.com', '#000')").lastrowid


@pytest.fixture
def gmail(fake_gmail):
    return lambda account_id: fake_gmail


def _item(conn, fake_gmail, acct, tid):
    fake_gmail.put(tid, msg(f"{tid}-m", tid, labels=["INBOX"]))
    return items.upsert_item(conn, account_id=acct, external_id=tid, kind="email", fields={
        "title": f"thread {tid}", "last_activity_at": "2026-11-15T10:00:00Z", "last_direction": "in"}).id


def test_two_levels_only(conn):
    parent = tasks.create_task(conn, title="Trip")
    child = tasks.create_task(conn, title="Book flights", parent_id=parent["id"])
    with pytest.raises(tasks.TaskError, match="two levels"):
        tasks.create_task(conn, title="Window seat", parent_id=child["id"])
    with pytest.raises(tasks.TaskError, match="has subtasks"):
        tasks.update_task(conn, parent["id"], {"parent_id": tasks.create_task(conn, title="Other")["id"]})
    with pytest.raises(tasks.TaskError, match="own parent"):
        tasks.update_task(conn, child["id"], {"parent_id": child["id"]})


def test_list_groups_subtasks_under_parent(conn):
    a = tasks.create_task(conn, title="A")
    b = tasks.create_task(conn, title="B")
    a1 = tasks.create_task(conn, title="A1", parent_id=a["id"])
    b1 = tasks.create_task(conn, title="B1", parent_id=b["id"])
    a2 = tasks.create_task(conn, title="A2", parent_id=a["id"])
    assert [t["title"] for t in tasks.list_tasks(conn)] == ["A", "A1", "A2", "B", "B1"]
    # Moving B ahead of A moves its subtasks with it.
    tasks.update_task(conn, b["id"], {"sort_order": 0})
    assert [t["id"] for t in tasks.list_tasks(conn)] == [b["id"], b1["id"], a["id"], a1["id"], a2["id"]]


def test_field_validation(conn):
    with pytest.raises(tasks.TaskError, match="title"):
        tasks.create_task(conn, title="  ")
    with pytest.raises(tasks.TaskError, match="priority"):
        tasks.create_task(conn, title="x", priority="urgent")
    with pytest.raises(tasks.TaskError, match="date"):
        tasks.create_task(conn, title="x", due_at="tomorrow")
    t = tasks.create_task(conn, title=" x ", due_at="2026-11-20T09:00:00", notes="  ")
    assert t["title"] == "x" and t["due_at"] == "2026-11-20" and t["notes"] is None
    assert tasks.update_task(conn, t["id"], {"due_at": None})["due_at"] is None
    with pytest.raises(tasks.TaskError, match="can't edit"):
        tasks.update_task(conn, t["id"], {"status": "done"})


async def test_complete_cascades_and_archives_linked_item(conn, fake_gmail, gmail, acct):
    item_id = _item(conn, fake_gmail, acct, "t1")
    parent = tasks.create_task(conn, title="Reply to Ann", item_id=item_id)
    child = tasks.create_task(conn, title="Find the invoice", parent_id=parent["id"])
    result = await tasks.complete_task(conn, gmail, parent["id"])
    assert result["status"] == "done" and result["archived_items"] == [item_id] and result["warnings"] == []
    assert tasks.get_task(conn, child["id"])["status"] == "done"
    assert items.get_by_id(conn, item_id)["state"] == "archived"
    assert fake_gmail.modifies == [("t1", (), ("INBOX",))]


async def test_item_stays_open_while_another_task_links_it(conn, fake_gmail, gmail, acct):
    item_id = _item(conn, fake_gmail, acct, "t1")
    first = tasks.create_task(conn, title="Read", item_id=item_id)
    second = tasks.create_task(conn, title="Reply", item_id=item_id)
    assert (await tasks.complete_task(conn, gmail, first["id"]))["archived_items"] == []
    assert items.get_by_id(conn, item_id)["state"] == "open"
    assert (await tasks.complete_task(conn, gmail, second["id"]))["archived_items"] == [item_id]


async def test_completion_stands_when_archive_fails(conn, fake_gmail, gmail, acct):
    item_id = _item(conn, fake_gmail, acct, "t1")
    task = tasks.create_task(conn, title="Reply", item_id=item_id)
    fake_gmail.fail_with = httpx.ConnectError("down")
    result = await tasks.complete_task(conn, gmail, task["id"])
    assert result["status"] == "done" and result["archived_items"] == []
    assert "wasn't archived" in result["warnings"][0]
    assert items.get_by_id(conn, item_id)["state"] == "open"


def test_reopen_subtask_reopens_parent(conn):
    parent = tasks.create_task(conn, title="P")
    child = tasks.create_task(conn, title="C", parent_id=parent["id"])
    conn.execute("UPDATE task SET status = 'done', completed_at = 'x'")
    assert tasks.reopen_task(conn, child["id"])["status"] == "open"
    assert tasks.get_task(conn, parent["id"])["status"] == "open"


def test_delete_removes_subtasks(conn):
    parent = tasks.create_task(conn, title="P")
    tasks.create_task(conn, title="C", parent_id=parent["id"])
    tasks.delete_task(conn, parent["id"])
    assert tasks.list_tasks(conn, status=None) == []


def test_overview_urgent_rules(conn, acct):
    tasks.create_task(conn, title="overdue", due_at="2026-11-14")
    tasks.create_task(conn, title="today", due_at="2026-11-15")
    tasks.create_task(conn, title="tomorrow", due_at="2026-11-16")
    tasks.create_task(conn, title="undated")
    items.upsert_item(conn, account_id=acct, external_id="d1", kind="digest", fields={
        "title": "Sign closing docs", "priority": "high", "last_activity_at": "2026-11-15T09:00:00Z",
        "last_direction": "in"})
    data = overview.get_overview(conn, TODAY)
    assert [t["title"] for t in data["urgent"]["tasks"]] == ["overdue", "today"]
    assert [i["title"] for i in data["urgent"]["items"]] == ["Sign closing docs"]
    assert data["tasks"]["count"] == 4 and data["today"] == "2026-11-15"


def test_tasks_api(client, conn, fake_gmail, monkeypatch, acct):
    login(client, monkeypatch)
    client.app.state.gmail_clients = lambda account_id: fake_gmail
    item_id = _item(conn, fake_gmail, acct, "t1")
    created = client.post("/api/tasks", json={"title": "Reply", "item_id": item_id, "due_at": "2026-11-20"})
    assert created.status_code == 201
    task = created.json()
    assert task["item_title"] == "thread t1" and task["item_account"] == "me@x.com"
    assert client.patch(f"/api/tasks/{task['id']}", json={"priority": "high"}).json()["priority"] == "high"
    assert client.patch(f"/api/tasks/{task['id']}", json={"due_at": None}).json()["due_at"] is None
    assert client.post("/api/tasks", json={"title": "x", "priority": "urgent"}).status_code == 422
    done = client.post(f"/api/tasks/{task['id']}/complete").json()
    assert done["status"] == "done" and done["archived_items"] == [item_id]
    assert [t["id"] for t in client.get("/api/tasks?status=done").json()] == [task["id"]]
    assert client.post(f"/api/tasks/{task['id']}/reopen").json()["status"] == "open"
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 204
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 404
