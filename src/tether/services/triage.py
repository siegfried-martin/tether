"""Archive and unarchive: the inbox write path (spec: "Item rules", "Gmail inbox membership").

REST and MCP both call these. A Gmail item writes through first: the change
reaches Gmail before Tether's state moves, so a failed write leaves both sides
as they were. Chat and digest items have no provider inbox, so their archive
is local only.
"""

import logging
import sqlite3
from collections.abc import Callable

import httpx

from tether.connectors.gmail import GmailApi, RateLimited, apply_thread
from tether.connectors.google_oauth import AuthError
from tether.db import tx
from tether.services import accounts, items, sync_state

log = logging.getLogger(__name__)

GmailFactory = Callable[[int], GmailApi]


class TriageError(Exception):
    """The provider write could not be made. The message is safe to show the user."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


async def archive_item(conn: sqlite3.Connection, gmail: GmailFactory, item_id: int) -> dict:
    """Archive an item. For Gmail, removes INBOX from the whole thread."""
    item, account = _load(conn, item_id)
    if item["state"] == "archived":
        return _result(conn, item_id)
    if item["kind"] == "email":
        await _write(conn, gmail, account, lambda api: api.modify_thread(item["external_id"], remove=["INBOX"]))
    with tx(conn):
        # Chat items reopen on inbound activity after this point (spec: archived_through).
        extra = {"archived_through": item["last_activity_at"]} if item["kind"] == "chat" else {}
        items.set_state(conn, item_id, "archived", **extra)
    return _result(conn, item_id)


async def unarchive_item(conn: sqlite3.Connection, gmail: GmailFactory, item_id: int) -> dict:
    """Reopen an item. For Gmail, adds INBOX back to the thread, then mirrors it.

    Mirroring keeps the rule "open exactly when the thread is in": a thread
    whose only category is excluded for this account stays archived in Tether,
    and the result says why.
    """
    item, account = _load(conn, item_id)
    if item["state"] == "open":
        return _result(conn, item_id)
    if item["kind"] != "email":
        with tx(conn):
            items.set_state(conn, item_id, "open")
        return _result(conn, item_id)

    async def add_inbox(api: GmailApi):
        if not await api.modify_thread(item["external_id"], add=["INBOX"]):
            return None
        return await api.get_thread(item["external_id"])

    thread = await _write(conn, gmail, account, add_inbox)
    with tx(conn):
        apply_thread(conn, account_id=account["id"], thread=thread, thread_id=item["external_id"],
                     excluded=accounts.excluded_categories(account), email=account["label"], sent_activity=False)
    result = _result(conn, item_id)
    if thread is None:
        result["note"] = "This thread no longer exists in Gmail."
    elif result["state"] != "open":
        result["note"] = ("Moved back to the Gmail inbox, but its category is not synced for this account, "
                          "so Tether keeps it archived. Change that in Settings, under Sync.")
    return result


def _load(conn: sqlite3.Connection, item_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    item = items.get_by_id(conn, item_id)
    if item is None:
        raise TriageError(f"no item {item_id}", status=404)
    account = accounts.get_account(conn, item["account_id"])
    if item["kind"] == "email" and account["status"] != "ok":
        raise TriageError(f"{account['label']} needs reconnecting in Settings before its mail can change.",
                          status=409)
    return item, account


async def _write(conn: sqlite3.Connection, gmail: GmailFactory, account: sqlite3.Row, op):
    try:
        return await op(gmail(account["id"]))
    except AuthError as exc:
        # Same handling as the poller: flag the account so the urgent strip says so.
        accounts.set_status(conn, account["id"], "auth_error")
        sync_state.record_failure(conn, account["id"], sync_state.GMAIL_HISTORY, str(exc))
        raise TriageError(f"{account['label']}: sign-in expired or revoked. Reconnect it in Settings.",
                          status=409) from exc
    except RateLimited as exc:
        raise TriageError("Gmail is rate-limiting this account. Try again in a minute.", status=503) from exc
    except httpx.HTTPError as exc:
        log.warning("account %s: Gmail write failed: %s", account["id"], type(exc).__name__)
        raise TriageError("Couldn't reach Gmail. Nothing was changed; try again.") from exc


def _result(conn: sqlite3.Connection, item_id: int) -> dict:
    return items.view(conn, item_id)
