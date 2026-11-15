"""Per-stream sync health (spec: "Failure handling", "Latency and staleness")."""

import sqlite3
from datetime import timedelta

from tether.db import parse_iso, utcnow

STALE_AFTER = timedelta(hours=2)

GMAIL_HISTORY = "gmail.history"
# Cursor value asking the next poll to run a full reconcile (e.g. after sync settings change).
RECONCILE = "reconcile"


def _ensure(conn: sqlite3.Connection, account_id: int, stream: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (account_id, stream) VALUES (?, ?) ON CONFLICT DO NOTHING", (account_id, stream)
    )


def get_cursor(conn: sqlite3.Connection, account_id: int, stream: str) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM sync_state WHERE account_id = ? AND stream = ?", (account_id, stream)
    ).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: sqlite3.Connection, account_id: int, stream: str, cursor: str) -> None:
    """Save progress without marking the stream healthy."""
    _ensure(conn, account_id, stream)
    conn.execute("UPDATE sync_state SET cursor = ? WHERE account_id = ? AND stream = ?", (cursor, account_id, stream))


def record_attempt(conn: sqlite3.Connection, account_id: int, stream: str) -> None:
    _ensure(conn, account_id, stream)
    conn.execute(
        "UPDATE sync_state SET last_attempt_at = ? WHERE account_id = ? AND stream = ?",
        (utcnow(), account_id, stream),
    )


def record_ok(conn: sqlite3.Connection, account_id: int, stream: str, cursor: str | None) -> None:
    _ensure(conn, account_id, stream)
    conn.execute(
        """UPDATE sync_state SET cursor = ?, last_ok_at = ?, failures = 0, last_error = NULL
           WHERE account_id = ? AND stream = ?""",
        (cursor, utcnow(), account_id, stream),
    )


def record_failure(conn: sqlite3.Connection, account_id: int, stream: str, error: str) -> None:
    _ensure(conn, account_id, stream)
    conn.execute(
        """UPDATE sync_state SET failures = failures + 1, last_error = ?
           WHERE account_id = ? AND stream = ?""",
        (error[:500], account_id, stream),
    )


def sync_status(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT a.label AS account, a.kind, a.status, s.stream, s.last_attempt_at, s.last_ok_at,
                  s.failures, s.last_error
           FROM account a LEFT JOIN sync_state s ON s.account_id = a.id
           ORDER BY a.id, s.stream"""
    ).fetchall()
    return [dict(r) for r in rows]


def source_alerts(conn: sqlite3.Connection, now: str | None = None) -> list[dict]:
    """Urgent rule 1 (so far): auth failures and sources stale past two hours."""
    now_dt = parse_iso(now or utcnow())
    alerts = []
    for row in sync_status(conn):
        if row["status"] == "auth_error":
            alerts.append({"kind": "auth_error", "account": row["account"],
                           "message": f"{row['account']}: sign-in expired or revoked. Reconnect it in Settings."})
            continue
        if row["stream"] is None or row["status"] == "disabled":
            continue
        if row["last_ok_at"] is None:
            # Never synced: alert once it has failed repeatedly, not while the first sync runs.
            if row["failures"] >= 3:
                alerts.append({"kind": "stale", "account": row["account"],
                               "message": f"{row['account']}: has not synced yet ({row['last_error']})."})
            continue
        if now_dt - parse_iso(row["last_ok_at"]) > STALE_AFTER:
            alerts.append({"kind": "stale", "account": row["account"],
                           "message": f"{row['account']}: no successful sync for over two hours."})
    # One alert per account is enough.
    seen, unique = set(), []
    for alert in alerts:
        if alert["account"] not in seen:
            seen.add(alert["account"])
            unique.append(alert)
    return unique
