"""Background polling. Runs as asyncio tasks inside the single Tether process.

Each account polls in its own task, so one mailbox's long first sync or quota
backoff never delays the others.
"""

import asyncio
import logging
import sqlite3

import httpx

from tether.config import Settings
from tether.connectors.gmail import STREAM as GMAIL_STREAM
from tether.connectors.gmail import GmailClient, GmailSync, RateLimited
from tether.connectors.google_oauth import AuthError
from tether.crypto import Vault
from tether.services import accounts, sync_state

log = logging.getLogger(__name__)


class GmailPoller:
    def __init__(self, conn: sqlite3.Connection, vault: Vault, http: httpx.AsyncClient, settings: Settings):
        self.conn, self.vault, self.http, self.settings = conn, vault, http, settings
        self._wake = asyncio.Event()
        self._running: dict[int, asyncio.Task] = {}

    def wake(self) -> None:
        """Poll now (e.g. right after an account is connected)."""
        self._wake.set()

    async def run_forever(self) -> None:
        try:
            while True:
                self.start_due()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.settings.gmail_poll_seconds)
                except TimeoutError:
                    pass
                self._wake.clear()
        finally:
            for task in self._running.values():
                task.cancel()

    def start_due(self) -> None:
        """Start a poll for every healthy account that isn't already mid-poll."""
        for account in accounts.list_accounts(self.conn, kind="gmail"):
            task = self._running.get(account["id"])
            if account["status"] != "ok" or (task and not task.done()):
                continue
            self._running[account["id"]] = asyncio.create_task(self.poll_one(account))

    async def poll_one(self, account: sqlite3.Row) -> None:
        account_id = account["id"]
        try:
            await self._sync(account)
        finally:
            # Settings saved while this poll ran were overwritten by its final cursor: ask again.
            current = accounts.get_account(self.conn, account_id)
            if current and accounts.sync_settings_key(current) != accounts.sync_settings_key(account):
                sync_state.set_cursor(self.conn, account_id, GMAIL_STREAM, sync_state.RECONCILE)
                self.wake()

    async def _sync(self, account: sqlite3.Row) -> None:
        account_id = account["id"]
        try:
            client = GmailClient(self.http, self.conn, self.vault, account_id,
                                 self.settings.google_client_id, self.settings.google_client_secret)
            await GmailSync(
                self.conn, client, account_id=account_id, email=client.email,
                excluded=accounts.excluded_categories(account),
                initial_sync_days=accounts.sync_days(account, self.settings.initial_sync_days),
            ).run()
        except AuthError as exc:
            log.warning("account %s: auth error: %s", account_id, exc)
            accounts.set_status(self.conn, account_id, "auth_error")
            sync_state.record_failure(self.conn, account_id, GMAIL_STREAM, str(exc))
        except RateLimited as exc:
            # Transient: progress is saved, the next poll resumes.
            log.info("account %s: %s", account_id, exc)
            sync_state.record_failure(self.conn, account_id, GMAIL_STREAM, str(exc))
        except Exception as exc:  # one account failing must not stop the others
            # Exception type and message only, never payloads (logs carry no snippets or tokens).
            log.warning("account %s: sync failed: %s", account_id, type(exc).__name__)
            sync_state.record_failure(self.conn, account_id, GMAIL_STREAM, f"{type(exc).__name__}: {exc}")
