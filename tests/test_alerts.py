from tether.services import sync_state


def _account(conn):
    return conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'a', '#000')").lastrowid


def test_stale_after_two_hours(conn):
    acct = _account(conn)
    sync_state.record_ok(conn, acct, "gmail.history", "1")
    conn.execute("UPDATE sync_state SET last_ok_at = '2026-11-15T08:00:00Z'")
    assert sync_state.source_alerts(conn, now="2026-11-15T09:59:00Z") == []
    [alert] = sync_state.source_alerts(conn, now="2026-11-15T10:01:00Z")
    assert alert["kind"] == "stale"


def test_never_synced_alerts_after_repeated_failures(conn):
    acct = _account(conn)
    sync_state.record_attempt(conn, acct, "gmail.history")
    assert sync_state.source_alerts(conn) == []
    for _ in range(3):
        sync_state.record_failure(conn, acct, "gmail.history", "ConnectError")
    assert sync_state.source_alerts(conn)[0]["kind"] == "stale"


def test_auth_error_is_one_alert(conn):
    acct = _account(conn)
    sync_state.record_ok(conn, acct, "gmail.history", "1")
    sync_state.record_ok(conn, acct, "other.stream", "1")
    conn.execute("UPDATE account SET status = 'auth_error'")
    assert len(sync_state.source_alerts(conn)) == 1
