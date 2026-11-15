"""SQLite access.

Concurrency model: every route handler is `async def` and every poller is an
asyncio task, so all database access happens on the event-loop thread through
one connection. Writes are serialized by construction; there is never a second
writer. This only holds with exactly one uvicorn worker (spec: one-worker rule).
"""

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path


def connect(path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    # check_same_thread stays on in production: it enforces the loop-thread-only rule above.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=check_same_thread)
    # isolation_level=None is autocommit; use tx() for transactions.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def _migrations() -> list[tuple[int, str]]:
    found = []
    for entry in resources.files("tether.migrations").iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.read_text()))
    return sorted(found)


# A migration that rebuilds a table (SQLite's 12-step ALTER) starts with this line.
# Foreign keys are switched off around it and checked before it commits.
REBUILD_MARKER = "-- migrate: rebuild"


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in _migrations():
        if version > current:
            _apply(conn, version, sql)


def _apply(conn: sqlite3.Connection, version: int, sql: str) -> None:
    rebuild = sql.lstrip().startswith(REBUILD_MARKER)
    if rebuild:
        # Must happen outside a transaction to take effect.
        conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};")
        if rebuild and conn.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError(f"migration {version} broke a foreign key")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        if rebuild:
            conn.execute("PRAGMA foreign_keys = ON")


class tx:
    """`with tx(conn): ...` runs the block in one transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def utcnow() -> str:
    return iso(datetime.now(UTC))


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
