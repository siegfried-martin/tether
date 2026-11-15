"""Archive and unarchive: write-through to Gmail, local-only for other kinds, failure handling."""

import httpx
import pytest
from conftest import msg

from test_web import client, login, settings  # noqa: F401  (fixtures)
from tether.connectors.gmail import GmailSync
from tether.connectors.google_oauth import AuthError
from tether.services import accounts, items, triage

EMAIL = "me@example.com"
EXCLUDED = frozenset({"promotions", "social", "forums"})


@pytest.fixture
def acct(conn):
    return conn.execute(
        "INSERT INTO account (kind, label, color, excluded_categories) VALUES ('gmail', ?, '#000', ?)",
        (EMAIL, "promotions,social,forums")).lastrowid


@pytest.fixture
def gmail(fake_gmail):
    return lambda account_id: fake_gmail


async def _synced(conn, fake_gmail, acct, *threads):
    for tid, labels in threads:
        fake_gmail.put(tid, msg(f"{tid}-m", tid, labels=labels))
    await GmailSync(conn, fake_gmail, account_id=acct, email=EMAIL, excluded=EXCLUDED, initial_sync_days=14).run()
    return {tid: items.get_item(conn, acct, tid)["id"] for tid, _ in threads}


async def test_archive_writes_through_then_poll_agrees(conn, fake_gmail, gmail, acct):
    ids = await _synced(conn, fake_gmail, acct, ("t1", ["INBOX"]))
    result = await triage.archive_item(conn, gmail, ids["t1"])
    assert result["state"] == "archived" and result["archived_at"]
    assert fake_gmail.modifies == [("t1", (), ("INBOX",))]
    # The next history poll sees INBOX removed and leaves the item archived.
    await GmailSync(conn, fake_gmail, account_id=acct, email=EMAIL, excluded=EXCLUDED, initial_sync_days=14).run()
    assert items.get_by_id(conn, ids["t1"])["state"] == "archived"


async def test_unarchive_adds_inbox_and_mirrors(conn, fake_gmail, gmail, acct):
    ids = await _synced(conn, fake_gmail, acct, ("t1", ["INBOX"]))
    await triage.archive_item(conn, gmail, ids["t1"])
    result = await triage.unarchive_item(conn, gmail, ids["t1"])
    assert result["state"] == "open" and result["archived_at"] is None and "note" not in result
    assert fake_gmail.modifies[-1] == ("t1", ("INBOX",), ())
    assert "INBOX" in fake_gmail.threads["t1"]["messages"][0]["labelIds"]


async def test_unarchive_excluded_category_stays_archived_with_note(conn, fake_gmail, gmail, acct):
    fake_gmail.put("p", msg("p-m", "p", labels=["CATEGORY_PROMOTIONS"]))
    items.upsert_item(conn, account_id=acct, external_id="p", kind="email", state="archived",
                      fields={"last_activity_at": "2026-11-15T10:00:00Z", "last_direction": "in"})
    item_id = items.get_item(conn, acct, "p")["id"]
    result = await triage.unarchive_item(conn, gmail, item_id)
    assert result["state"] == "archived" and "not synced" in result["note"]


async def test_already_in_target_state_makes_no_write(conn, fake_gmail, gmail, acct):
    ids = await _synced(conn, fake_gmail, acct, ("t1", ["INBOX"]))
    assert (await triage.unarchive_item(conn, gmail, ids["t1"]))["state"] == "open"
    await triage.archive_item(conn, gmail, ids["t1"])
    await triage.archive_item(conn, gmail, ids["t1"])
    assert len(fake_gmail.modifies) == 1


async def test_failed_write_changes_nothing(conn, fake_gmail, gmail, acct):
    ids = await _synced(conn, fake_gmail, acct, ("t1", ["INBOX"]))
    fake_gmail.fail_with = httpx.ConnectError("down")
    with pytest.raises(triage.TriageError) as err:
        await triage.archive_item(conn, gmail, ids["t1"])
    assert err.value.status == 502
    assert items.get_by_id(conn, ids["t1"])["state"] == "open"


async def test_auth_failure_flags_account(conn, fake_gmail, gmail, acct):
    ids = await _synced(conn, fake_gmail, acct, ("t1", ["INBOX"]))
    fake_gmail.fail_with = AuthError("revoked")
    with pytest.raises(triage.TriageError) as err:
        await triage.archive_item(conn, gmail, ids["t1"])
    assert err.value.status == 409
    assert accounts.get_account(conn, acct)["status"] == "auth_error"
    # Further writes are refused up front until the account is reconnected.
    with pytest.raises(triage.TriageError, match="reconnecting"):
        await triage.archive_item(conn, gmail, ids["t1"])


async def test_deleted_thread_archives_locally(conn, fake_gmail, gmail, acct):
    ids = await _synced(conn, fake_gmail, acct, ("t1", ["INBOX"]))
    del fake_gmail.threads["t1"]
    assert (await triage.archive_item(conn, gmail, ids["t1"]))["state"] == "archived"


async def test_chat_item_archive_is_local(conn, fake_gmail, gmail):
    chat = conn.execute("INSERT INTO account (kind, label, color) VALUES ('slack', 'work', '#111')").lastrowid
    items.upsert_item(conn, account_id=chat, external_id="D1", kind="chat",
                      fields={"last_activity_at": "2026-11-15T10:00:00Z", "last_direction": "in"})
    item_id = items.get_item(conn, chat, "D1")["id"]
    assert (await triage.archive_item(conn, gmail, item_id))["state"] == "archived"
    assert items.get_by_id(conn, item_id)["archived_through"] == "2026-11-15T10:00:00Z"
    assert (await triage.unarchive_item(conn, gmail, item_id))["state"] == "open"
    assert fake_gmail.modifies == []


async def test_unknown_item(conn, gmail):
    with pytest.raises(triage.TriageError) as err:
        await triage.archive_item(conn, gmail, 999)
    assert err.value.status == 404


def test_archive_api_and_archived_listing(client, conn, fake_gmail, monkeypatch, acct):
    login(client, monkeypatch)
    client.app.state.gmail_clients = lambda account_id: fake_gmail
    for tid, minutes in (("old", 60), ("new", 5)):
        fake_gmail.put(tid, msg(f"{tid}-m", tid, labels=["INBOX"], minutes_ago=minutes))
        items.upsert_item(conn, account_id=acct, external_id=tid, kind="email",
                          fields={"last_activity_at": f"2026-11-15T10:{minutes:02d}:00Z", "last_direction": "in"})
    old, new = (items.get_item(conn, acct, t)["id"] for t in ("old", "new"))
    assert client.post(f"/api/items/{new}/archive").json()["state"] == "archived"
    assert client.post(f"/api/items/{old}/archive").json()["state"] == "archived"
    archived = client.get("/api/items?state=archived").json()
    assert [i["id"] for i in archived] == [old, new], "most recently archived first"
    assert client.post(f"/api/items/{old}/unarchive").json()["state"] == "open"
    assert client.get("/api/overview").json()["inbox"]["by_account"] == {EMAIL: 1}
    assert client.post("/api/items/999/archive").status_code == 404


def test_account_color_api(client, conn, monkeypatch, acct):
    login(client, monkeypatch)
    assert client.patch(f"/api/accounts/{acct}", json={"color": "#AA00ff"}).json()["color"] == "#aa00ff"
    assert client.patch(f"/api/accounts/{acct}", json={"color": "red"}).status_code == 422
    assert client.patch("/api/accounts/999", json={"color": "#000000"}).status_code == 404
