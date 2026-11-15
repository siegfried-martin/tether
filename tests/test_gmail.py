from conftest import msg

from tether.connectors.gmail import GmailSync, derive_thread, thread_is_in
from tether.services import items

EXCLUDED = frozenset({"promotions", "social", "forums"})
EMAIL = "me@example.com"


def _account(conn):
    return conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', ?, '#000')", (EMAIL,)).lastrowid


def _sync(conn, api, acct, days=14):
    return GmailSync(conn, api, account_id=acct, email=EMAIL, excluded=EXCLUDED, initial_sync_days=days)


def _item(conn, acct, tid):
    return items.get_item(conn, acct, tid)


# ------------------------------------------------------------ membership


def test_membership_by_category():
    personal = msg("m1", "t", labels=["INBOX", "CATEGORY_PERSONAL"])
    promo = msg("m2", "t", labels=["INBOX", "CATEGORY_PROMOTIONS"])
    updates = msg("m3", "t", labels=["INBOX", "CATEGORY_UPDATES"])
    uncategorized = msg("m4", "t", labels=["INBOX"])
    archived = msg("m5", "t", labels=["CATEGORY_PERSONAL"])
    assert thread_is_in([personal], EXCLUDED)
    assert not thread_is_in([promo], EXCLUDED)
    assert thread_is_in([updates], EXCLUDED)
    assert thread_is_in([uncategorized], EXCLUDED), "no category label counts as Personal"
    assert thread_is_in([promo, updates], EXCLUDED), "any qualifying INBOX message puts the thread in"
    assert not thread_is_in([archived], EXCLUDED)


def test_excluded_set_is_configurable():
    promo = msg("m", "t", labels=["INBOX", "CATEGORY_PROMOTIONS"])
    assert thread_is_in([promo], frozenset({"social"}))


def test_derive_fields():
    thread = {"id": "t1", "messages": [
        msg("m1", "t1", labels=["INBOX"], subject="Lunch?", frm='"Ann Lee" <ann@x.com>', minutes_ago=30),
        msg("m2", "t1", labels=["SENT"], snippet="Sure &amp; thanks", minutes_ago=20),
        msg("m3", "t1", labels=["DRAFT"], minutes_ago=5),
    ]}
    view = derive_thread(thread, excluded=EXCLUDED, email=EMAIL)
    assert view.fields["title"] == "Lunch?"
    assert view.fields["sender"] == "Ann Lee"
    assert view.fields["last_direction"] == "out", "drafts are ignored; latest real message is sent"
    assert view.fields["snippet"] == "Sure & thanks"
    assert view.fields["url"] == "https://mail.google.com/mail/?authuser=me%40example.com#all/t1"


def test_derive_drafts_only_is_none():
    assert derive_thread({"id": "t", "messages": [msg("d", "t", labels=["DRAFT"])]},
                         excluded=EXCLUDED, email=EMAIL) is None


# ----------------------------------------------------------- initial sync


async def test_initial_sync_window_and_filter(conn, fake_gmail):
    acct = _account(conn)
    fake_gmail.put("recent", msg("a", "recent", labels=["INBOX", "CATEGORY_PERSONAL"]))
    fake_gmail.put("old", msg("b", "old", labels=["INBOX"], minutes_ago=60 * 24 * 30))
    fake_gmail.put("promo", msg("c", "promo", labels=["INBOX", "CATEGORY_PROMOTIONS"]))
    fake_gmail.put("archived", msg("d", "archived", labels=["CATEGORY_PERSONAL"]))
    await _sync(conn, fake_gmail, acct).run()
    assert [i["title"] for i in items.list_items(conn)] == ["Hello"]
    assert _item(conn, acct, "recent")["state"] == "open"
    assert _item(conn, acct, "old") is None
    cursor = conn.execute("SELECT cursor FROM sync_state").fetchone()[0]
    assert cursor == "100"


async def test_rerun_changes_nothing(conn, fake_gmail):
    acct = _account(conn)
    fake_gmail.put("t", msg("a", "t", labels=["INBOX"]))
    await _sync(conn, fake_gmail, acct).run()
    before = [dict(r) for r in conn.execute("SELECT * FROM item")]
    await _sync(conn, fake_gmail, acct).run()
    assert [dict(r) for r in conn.execute("SELECT * FROM item")] == before


# ---------------------------------------------------------------- history


async def _initial(conn, fake_gmail):
    acct = _account(conn)
    await _sync(conn, fake_gmail, acct).run()
    return acct


async def test_new_inbound_thread_opens(conn, fake_gmail):
    acct = await _initial(conn, fake_gmail)
    m = msg("a", "t", labels=["INBOX", "CATEGORY_PERSONAL"])
    fake_gmail.put("t", m)
    fake_gmail.record(messagesAdded=[{"message": m}])
    await _sync(conn, fake_gmail, acct).run()
    assert _item(conn, acct, "t")["state"] == "open"
    assert conn.execute("SELECT cursor FROM sync_state").fetchone()[0] == "101"


async def test_sent_only_thread_created_archived_out(conn, fake_gmail):
    acct = await _initial(conn, fake_gmail)
    m = msg("a", "t", labels=["SENT"], to="Bob <bob@x.com>")
    fake_gmail.put("t", m)
    fake_gmail.record(messagesAdded=[{"message": m}])
    await _sync(conn, fake_gmail, acct).run()
    row = _item(conn, acct, "t")
    assert row["state"] == "archived" and row["last_direction"] == "out" and row["sender"] == "To: Bob"


async def test_sent_on_open_item_keeps_state(conn, fake_gmail):
    acct = await _initial(conn, fake_gmail)
    inbound = msg("a", "t", labels=["INBOX"], minutes_ago=30)
    fake_gmail.put("t", inbound)
    fake_gmail.record(messagesAdded=[{"message": inbound}])
    await _sync(conn, fake_gmail, acct).run()
    reply = msg("b", "t", labels=["SENT"], minutes_ago=1)
    fake_gmail.put("t", inbound, reply)
    fake_gmail.record(messagesAdded=[{"message": reply}])
    await _sync(conn, fake_gmail, acct).run()
    row = _item(conn, acct, "t")
    assert row["state"] == "open" and row["last_direction"] == "out"


async def test_archive_in_gmail_and_move_to_promotions(conn, fake_gmail):
    fake_gmail.put("t1", msg("a", "t1", labels=["INBOX"]))
    fake_gmail.put("t2", msg("b", "t2", labels=["INBOX", "CATEGORY_PERSONAL"]))
    acct = await _initial(conn, fake_gmail)
    fake_gmail.put("t1", msg("a", "t1", labels=[]))
    fake_gmail.record(labelsRemoved=[{"message": {"id": "a", "threadId": "t1", "labelIds": []},
                                      "labelIds": ["INBOX"]}])
    fake_gmail.put("t2", msg("b", "t2", labels=["INBOX", "CATEGORY_PROMOTIONS"]))
    fake_gmail.record(labelsAdded=[{"message": {"id": "b", "threadId": "t2",
                                                "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]},
                                    "labelIds": ["CATEGORY_PROMOTIONS"]}])
    await _sync(conn, fake_gmail, acct).run()
    assert _item(conn, acct, "t1")["state"] == "archived"
    assert _item(conn, acct, "t2")["state"] == "archived"


async def test_reply_reopens_archived_item(conn, fake_gmail):
    fake_gmail.put("t", msg("a", "t", labels=["SENT"]))
    acct = await _initial(conn, fake_gmail)
    fake_gmail.record(messagesAdded=[{"message": msg("a", "t", labels=["SENT"])}])
    await _sync(conn, fake_gmail, acct).run()
    assert _item(conn, acct, "t")["state"] == "archived"
    reply = msg("b", "t", labels=["INBOX"], minutes_ago=1)
    fake_gmail.put("t", msg("a", "t", labels=["SENT"]), reply)
    fake_gmail.record(messagesAdded=[{"message": reply}])
    await _sync(conn, fake_gmail, acct).run()
    row = _item(conn, acct, "t")
    assert row["state"] == "open" and row["last_direction"] == "in"


async def test_irrelevant_history_not_fetched(conn, fake_gmail):
    acct = await _initial(conn, fake_gmail)
    m = msg("a", "t", labels=["CATEGORY_PERSONAL"])  # filtered straight out of the inbox
    fake_gmail.put("t", m)
    fake_gmail.record(messagesAdded=[{"message": m}])
    await _sync(conn, fake_gmail, acct).run()
    assert fake_gmail.fetches == [] and _item(conn, acct, "t") is None


async def test_deleted_thread_archives_item(conn, fake_gmail):
    fake_gmail.put("t", msg("a", "t", labels=["INBOX"]))
    acct = await _initial(conn, fake_gmail)
    del fake_gmail.threads["t"]
    fake_gmail.record(messagesDeleted=[{"message": {"id": "a", "threadId": "t"}}])
    await _sync(conn, fake_gmail, acct).run()
    assert _item(conn, acct, "t")["state"] == "archived"


# -------------------------------------------------------------- reconcile


async def test_expired_history_reconciles(conn, fake_gmail):
    fake_gmail.put("stays", msg("a", "stays", labels=["INBOX"]))
    fake_gmail.put("leaves", msg("b", "leaves", labels=["INBOX"]))
    fake_gmail.put("sent", msg("c", "sent", labels=["SENT"]))
    acct = await _initial(conn, fake_gmail)
    fake_gmail.record(messagesAdded=[{"message": msg("c", "sent", labels=["SENT"])}])
    await _sync(conn, fake_gmail, acct).run()

    fake_gmail.expired = True
    fake_gmail.history_id = 500
    fake_gmail.put("leaves", msg("b", "leaves", labels=[]))
    fake_gmail.put("sent", msg("c", "sent", labels=["SENT"]), msg("r", "sent", labels=["INBOX"], minutes_ago=1))
    fake_gmail.put("new", msg("d", "new", labels=["INBOX"]))
    fake_gmail.put("old", msg("e", "old", labels=["INBOX"], minutes_ago=60 * 24 * 30))
    fake_gmail.fetches.clear()
    await _sync(conn, fake_gmail, acct).run()

    assert _item(conn, acct, "stays")["state"] == "open"
    assert _item(conn, acct, "leaves")["state"] == "archived"
    assert _item(conn, acct, "sent")["state"] == "open"
    assert _item(conn, acct, "new")["state"] == "open"
    assert _item(conn, acct, "old") is None, "older threads wait for new activity"
    assert "stays" not in fake_gmail.fetches, "unchanged open items are not refetched"
    assert conn.execute("SELECT cursor FROM sync_state").fetchone()[0] == "500"
