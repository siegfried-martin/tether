from tether.services import items


def _account(conn):
    return conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'a', '#000')").lastrowid


FIELDS = {"title": "Hi", "sender": "Ann", "snippet": "x", "last_activity_at": "2026-11-15T10:00:00Z",
          "last_direction": "in"}


def test_upsert_is_idempotent(conn):
    acct = _account(conn)
    first = items.upsert_item(conn, account_id=acct, external_id="t1", kind="email", fields=FIELDS)
    before = dict(items.get_item(conn, acct, "t1"))
    second = items.upsert_item(conn, account_id=acct, external_id="t1", kind="email", fields=FIELDS)
    assert first.created and not second.created and first.id == second.id
    assert dict(items.get_item(conn, acct, "t1")) == before  # nothing changed, not even updated_at
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1


def test_state_none_preserves_state(conn):
    acct = _account(conn)
    items.upsert_item(conn, account_id=acct, external_id="t1", kind="email", fields=FIELDS, state="archived")
    items.upsert_item(conn, account_id=acct, external_id="t1", kind="email", fields={**FIELDS, "snippet": "y"})
    row = items.get_item(conn, acct, "t1")
    assert row["state"] == "archived" and row["snippet"] == "y"


def test_list_items_newest_first_with_account_color(conn):
    acct = _account(conn)
    items.upsert_item(conn, account_id=acct, external_id="old", kind="email",
                      fields={**FIELDS, "last_activity_at": "2026-11-01T00:00:00Z"})
    items.upsert_item(conn, account_id=acct, external_id="new", kind="email", fields=FIELDS)
    items.upsert_item(conn, account_id=acct, external_id="gone", kind="email", fields=FIELDS, state="archived")
    listed = items.list_items(conn)
    assert [i["last_activity_at"] for i in listed] == ["2026-11-15T10:00:00Z", "2026-11-01T00:00:00Z"]
    assert listed[0]["account_color"] == "#000"
