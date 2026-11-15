"""Storage for Tether's MCP OAuth server: clients, grants and tokens (spec: "MCP auth").

Tokens are random strings handed to the client once; only their SHA-256 hashes
are stored, so a copy of the database holds no usable token.
"""

import hashlib
import json
import secrets
import sqlite3
import time

from tether.db import tx, utcnow

CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60  # sliding: every refresh issues a new one

PREFIX = {"code": "tc_", "access": "ta_", "refresh": "tr_"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def save_client(conn: sqlite3.Connection, client_id: str, info: dict) -> None:
    conn.execute(
        "INSERT INTO oauth_client (client_id, info, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT (client_id) DO UPDATE SET info = excluded.info",
        (client_id, json.dumps(info), utcnow()),
    )


def get_client(conn: sqlite3.Connection, client_id: str) -> dict | None:
    row = conn.execute("SELECT info FROM oauth_client WHERE client_id = ?", (client_id,)).fetchone()
    return json.loads(row["info"]) if row else None


def create_grant(conn: sqlite3.Connection, *, client_id: str, subject: str, scopes: list[str],
                 resource: str, code_data: dict) -> str:
    """Record an approval and return a single-use authorization code for it."""
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO oauth_grant (client_id, subject, scopes, resource, created_at) VALUES (?, ?, ?, ?, ?)",
            (client_id, subject, " ".join(scopes), resource, utcnow()),
        )
        return _issue(conn, cur.lastrowid, "code", CODE_TTL, code_data)


def _issue(conn: sqlite3.Connection, grant_id: int, kind: str, ttl: int, data: dict | None = None) -> str:
    token = PREFIX[kind] + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO oauth_token (token_hash, grant_id, kind, expires_at, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (_hash(token), grant_id, kind, int(time.time()) + ttl, json.dumps(data) if data else None, utcnow()),
    )
    return token


def lookup(conn: sqlite3.Connection, token: str, kind: str) -> dict | None:
    """A live token of this kind, joined with its grant; None if unknown, expired or revoked."""
    row = conn.execute(
        """SELECT t.expires_at, t.data, g.id AS grant_id, g.client_id, g.subject, g.scopes, g.resource
           FROM oauth_token t JOIN oauth_grant g ON g.id = t.grant_id
           WHERE t.token_hash = ? AND t.kind = ? AND g.revoked_at IS NULL""",
        (_hash(token), kind),
    ).fetchone()
    if row is None or row["expires_at"] < time.time():
        return None
    found = dict(row)
    found["scopes"] = found["scopes"].split() if found["scopes"] else []
    found["data"] = json.loads(found["data"]) if found["data"] else None
    return found


def issue_tokens(conn: sqlite3.Connection, grant_id: int, *, consume: str | None = None) -> dict:
    """Issue an access and refresh token on a grant. `consume` is the code or
    refresh token being exchanged; it is deleted in the same transaction, so it
    works exactly once."""
    with tx(conn):
        if consume is not None:
            gone = conn.execute("DELETE FROM oauth_token WHERE token_hash = ?", (_hash(consume),)).rowcount
            if not gone:
                raise LookupError("token already used")
        return {
            "access_token": _issue(conn, grant_id, "access", ACCESS_TTL),
            "refresh_token": _issue(conn, grant_id, "refresh", REFRESH_TTL),
            "expires_in": ACCESS_TTL,
        }


def revoke(conn: sqlite3.Connection, token: str) -> None:
    """Revoke the grant behind a token, which invalidates every token on it."""
    with tx(conn):
        row = conn.execute("SELECT grant_id FROM oauth_token WHERE token_hash = ?", (_hash(token),)).fetchone()
        if row:
            conn.execute("UPDATE oauth_grant SET revoked_at = ? WHERE id = ?", (utcnow(), row["grant_id"]))
            conn.execute("DELETE FROM oauth_token WHERE grant_id = ?", (row["grant_id"],))


def purge_expired(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM oauth_token WHERE expires_at < ?", (int(time.time()),)).rowcount
