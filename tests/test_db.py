import sqlite3

import pytest

from tether.db import utcnow


def test_schema_has_nine_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == {"account", "credential", "item", "task", "follow_up", "calendar_source",
                     "source_event", "hub_event", "sync_state"}


def test_migrate_is_idempotent(conn):
    from tether.db import migrate
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_one_open_follow_up_per_item(conn):
    now = utcnow()
    conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'a', '#000')")
    conn.execute("""INSERT INTO item (account_id, external_id, kind, last_activity_at, last_direction,
                    created_at, updated_at) VALUES (1, 't', 'email', ?, 'out', ?, ?)""", (now, now, now))
    add = "INSERT INTO follow_up (item_id, status, source) VALUES (1, ?, 'manual')"
    conn.execute(add, ("waiting",))
    conn.execute(add, ("replied",))  # terminal rows are unlimited
    conn.execute(add, ("dismissed",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(add, ("proposed",))


def test_account_kinds(conn):
    conn.execute("INSERT INTO account (kind, label, color) VALUES ('digest', 'Work', '#000')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO account (kind, label, color) VALUES ('other', 'x', '#000')")


def test_rebuild_migration_keeps_rows_and_foreign_keys(tmp_path):
    """0003 rebuilds `account` under existing child rows; ids and references survive."""
    from tether import db as dbmod
    path = tmp_path / "t.db"
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    for version, sql in dbmod._migrations():
        if version < 3:
            dbmod._apply(c, version, sql)
    now = utcnow()
    c.execute("INSERT INTO account (id, kind, label, color, initial_sync_days) VALUES (7, 'gmail', 'a', '#000', 3)")
    c.execute("INSERT INTO sync_state (account_id, stream, cursor) VALUES (7, 's', 'x')")
    c.execute("""INSERT INTO item (account_id, external_id, kind, last_activity_at, last_direction,
                 created_at, updated_at) VALUES (7, 't', 'email', ?, 'in', ?, ?)""", (now, now, now))
    c.close()

    c = dbmod.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 3
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert dict(c.execute("SELECT id, kind, initial_sync_days FROM account").fetchone()) == \
        {"id": 7, "kind": "gmail", "initial_sync_days": 3}
    assert c.execute("SELECT count(*) FROM item WHERE account_id = 7").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO sync_state (account_id, stream) VALUES (99, 's')")
    c.close()
