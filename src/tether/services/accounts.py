"""Accounts and their encrypted credentials."""

import re
import sqlite3

from tether.crypto import Vault
from tether.db import tx, utcnow
from tether.services import sync_state

# Account colors, assigned in order of creation: blue, rust, magenta, mint, amber, then cyan,
# forest and gray for later accounts. Chosen by search to stay apart under simulated
# deuteranomaly, both as solid edges and as the pale tints across whole rows (the hard case).
# Mirrored in web/src/shared.tsx for the color picker.
PALETTE = ["#4377f0", "#b23b19", "#b0267c", "#48cba1", "#ee9e10", "#21c5e5", "#2e7a50", "#6b7280"]
WORK_KINDS = ("ics", "digest")


def next_color(conn: sqlite3.Connection, kind: str) -> str:
    """The color for a new account. The work calendar feed and work digest share one color."""
    if kind in WORK_KINDS:
        row = conn.execute(f"SELECT color FROM account WHERE kind IN {WORK_KINDS} ORDER BY id LIMIT 1").fetchone()
        if row:
            return row["color"]
    count = conn.execute("SELECT COUNT(*) FROM account").fetchone()[0]
    return PALETTE[count % len(PALETTE)]

DEFAULT_EXCLUDED_CATEGORIES = "promotions,social,forums"
GMAIL_CATEGORIES = ("personal", "updates", "promotions", "social", "forums")
MAX_SYNC_DAYS = 90


def get_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()


def list_accounts(conn: sqlite3.Connection, kind: str | None = None) -> list[sqlite3.Row]:
    if kind:
        return conn.execute("SELECT * FROM account WHERE kind = ? ORDER BY id", (kind,)).fetchall()
    return conn.execute("SELECT * FROM account ORDER BY id").fetchall()


def excluded_categories(account: sqlite3.Row) -> frozenset[str]:
    raw = account["excluded_categories"] or ""
    return frozenset(c.strip().lower() for c in raw.split(",") if c.strip())


def sync_days(account: sqlite3.Row, default: int) -> int:
    return account["initial_sync_days"] or default


def update_sync_settings(
    conn: sqlite3.Connection, account_id: int, *,
    excluded_categories: list[str] | None = None, initial_sync_days: int | None = None,
) -> bool:
    """Change what a Gmail account syncs. Returns True when something changed.

    A change requests a full reconcile, so the inbox is re-mirrored under the
    new settings: newly excluded threads are archived, newly included ones in
    the window are imported. Shrinking the window never removes items; it only
    limits how far back imports reach.
    """
    account = get_account(conn, account_id)
    if account is None or account["kind"] != "gmail":
        raise LookupError(f"no Gmail account {account_id}")
    changes = {}
    if excluded_categories is not None:
        unknown = set(excluded_categories) - set(GMAIL_CATEGORIES)
        if unknown:
            raise ValueError(f"unknown categories: {sorted(unknown)}")
        value = ",".join(c for c in GMAIL_CATEGORIES if c in excluded_categories)
        if value != (account["excluded_categories"] or ""):
            changes["excluded_categories"] = value
    if initial_sync_days is not None:
        if not 1 <= initial_sync_days <= MAX_SYNC_DAYS:
            raise ValueError(f"initial_sync_days must be 1 to {MAX_SYNC_DAYS}")
        if initial_sync_days != account["initial_sync_days"]:
            changes["initial_sync_days"] = initial_sync_days
    if not changes:
        return False
    with tx(conn):
        assignments = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE account SET {assignments} WHERE id = ?", [*changes.values(), account_id])
        sync_state.set_cursor(conn, account_id, sync_state.GMAIL_HISTORY, sync_state.RECONCILE)
    return True


COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")


def set_color(conn: sqlite3.Connection, account_id: int, color: str) -> None:
    """Set an account's color: a hex value like #2f6fdb, shown on every item row."""
    color = color.strip().lower()
    if not COLOR_RE.match(color):
        raise ValueError("color must be a hex value like #2f6fdb")
    if conn.execute("UPDATE account SET color = ? WHERE id = ?", (color, account_id)).rowcount == 0:
        raise LookupError(f"no account {account_id}")


def sync_settings_key(account: sqlite3.Row) -> tuple:
    return (account["excluded_categories"], account["initial_sync_days"])


def upsert_gmail_account(
    conn: sqlite3.Connection, vault: Vault, *, email: str, secret: dict, scopes: str
) -> int:
    """Create the account for a Google mailbox, or refresh its credential on reconnect.

    The label is the mailbox address, which keeps reconnects idempotent.
    """
    with tx(conn):
        row = conn.execute("SELECT id FROM account WHERE label = ?", (email,)).fetchone()
        if row:
            account_id = row["id"]
            conn.execute("UPDATE account SET status = 'ok' WHERE id = ?", (account_id,))
        else:
            account_id = conn.execute(
                "INSERT INTO account (kind, label, color, excluded_categories) VALUES ('gmail', ?, ?, ?)",
                (email, next_color(conn, "gmail"), DEFAULT_EXCLUDED_CATEGORIES),
            ).lastrowid
        _save_credential(conn, vault, account_id, secret, scopes)
    return account_id


def _save_credential(conn, vault: Vault, account_id: int, secret: dict, scopes: str | None) -> None:
    conn.execute(
        """
        INSERT INTO credential (account_id, ciphertext, scopes, expires_at, updated_at)
        VALUES (:account_id, :ciphertext, :scopes, :expires_at, :now)
        ON CONFLICT (account_id) DO UPDATE SET
            ciphertext = excluded.ciphertext,
            scopes = COALESCE(:scopes_update, credential.scopes),
            expires_at = excluded.expires_at,
            updated_at = excluded.updated_at
        """,
        {
            "account_id": account_id,
            "ciphertext": vault.seal(secret),
            "scopes": scopes or "",
            "scopes_update": scopes,
            "expires_at": secret.get("expires_at_iso"),
            "now": utcnow(),
        },
    )


def save_credential(conn: sqlite3.Connection, vault: Vault, account_id: int, secret: dict) -> None:
    """Replace the stored secret (e.g. after a token refresh), keeping scopes."""
    _save_credential(conn, vault, account_id, secret, None)


def load_credential(conn: sqlite3.Connection, vault: Vault, account_id: int) -> dict:
    row = conn.execute("SELECT ciphertext FROM credential WHERE account_id = ?", (account_id,)).fetchone()
    if row is None:
        raise LookupError(f"no credential for account {account_id}")
    return vault.open(row["ciphertext"])


def set_status(conn: sqlite3.Connection, account_id: int, status: str) -> None:
    conn.execute("UPDATE account SET status = ? WHERE id = ?", (status, account_id))
