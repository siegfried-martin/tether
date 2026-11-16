"""Background polling. Runs as asyncio tasks inside the single Tether process.

Each account polls in its own task, so one mailbox's long first sync or quota
backoff never delays the others.
"""

import asyncio
import logging
import sqlite3
import time
from datetime import UTC, datetime

import httpx

from tether.config import Settings
from tether.connectors.gmail import STREAM as GMAIL_STREAM
from tether.connectors.gmail import GmailClient, GmailSync, RateLimited
from tether.connectors import gcal, ics
from tether.connectors.events import Window
from tether.connectors.google_oauth import AuthError
from tether.crypto import Vault
from tether.services import accounts, calendar, sync_state

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


class CalendarPoller:
    """One task for the whole calendar hub: fetch due sources, then reassign and write the hub.

    Sources are fetched one after another, so hub assignment never runs
    concurrently with itself. Google calendars are due every
    calendar_google_minutes, ICS feeds every calendar_ics_minutes.
    """

    TICK = 60
    LIST_EVERY = 3600     # seconds between refreshes of each account's calendar list

    def __init__(self, conn: sqlite3.Connection, vault: Vault, http: httpx.AsyncClient, settings: Settings):
        self.conn, self.vault, self.http, self.settings = conn, vault, http, settings
        self._wake = asyncio.Event()
        self._force = False
        self._lists_at = 0.0
        self._dirty = True
        self._lock = asyncio.Lock()    # a cycle and a one-source refresh never overlap

    def mark_dirty(self) -> None:
        """Source events changed outside a fetch (a source turned off, a hold released)."""
        self._dirty = True

    def wake(self, *, refetch: bool = False) -> None:
        """Run a cycle now. refetch also fetches every source, due or not."""
        self._force = self._force or refetch
        self._wake.set()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.cycle()
            except Exception as exc:  # the poller must survive anything one cycle throws
                log.warning("calendar cycle failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.TICK)
            except TimeoutError:
                pass
            self._wake.clear()

    def hub(self) -> calendar.Hub | None:
        return calendar.hub_config(self.conn, self.settings.hub_account, self.settings.hub_calendar_id)

    def client(self, account_id: int) -> gcal.CalendarClient:
        return gcal.CalendarClient(self.http, self.conn, self.vault, account_id,
                                   self.settings.google_client_id, self.settings.google_client_secret)

    async def refresh_source(self, source_id: int) -> None:
        """Fetch one source now (after Tether added an event to it) so the agenda shows the change
        at once. The hub write follows in the next cycle."""
        async with self._lock:
            source = next((s for s in calendar.list_sources(self.conn) if s["id"] == source_id), None)
            if not source or not source["enabled"]:
                return
            now = datetime.now(UTC)
            window = Window.around(now.astimezone(self.settings.tz).date(), self.settings.tz)
            if await self.poll_source(source, window, now):
                calendar.assign_hub(self.conn)
        self.wake()

    async def cycle(self, now: datetime | None = None) -> None:
        async with self._lock:
            await self._cycle(now)

    async def _cycle(self, now: datetime | None = None) -> None:
        force, self._force = self._force, False
        now = now or datetime.now(UTC)
        hub = self.hub()
        window = Window.around(now.astimezone(self.settings.tz).date(), self.settings.tz)
        if force or time.monotonic() - self._lists_at > self.LIST_EVERY:
            await self.refresh_lists(hub)
        if self.settings.ics_url:
            calendar.ensure_ics_source(self.conn, self.settings.ics_label)
        for source in calendar.list_sources(self.conn):
            if source["enabled"] and (force or self._due(source, now)):
                if await self.poll_source(source, window, now):
                    self._dirty = True
        if self._dirty:
            calendar.assign_hub(self.conn)
            self._dirty = False
        await self.write(hub)

    async def refresh_lists(self, hub: calendar.Hub | None) -> None:
        self._lists_at = time.monotonic()
        for account in accounts.list_accounts(self.conn, kind="gmail"):
            if account["status"] != "ok":
                continue
            try:
                calendar.sync_calendar_list(self.conn, account["id"], await self.client(account["id"]).list_calendars(),
                                            hub)
            except Exception as exc:  # a list refresh failing only delays new calendars showing up
                log.info("account %s: calendar list refresh failed: %s", account["id"], type(exc).__name__)

    def _due(self, source: dict, now: datetime) -> bool:
        row = self.conn.execute("SELECT last_attempt_at FROM sync_state WHERE account_id = ? AND stream = ?",
                                (source["account_id"], calendar.stream(source["id"]))).fetchone()
        if row is None or row["last_attempt_at"] is None:
            return True
        minutes = self.settings.calendar_ics_minutes if source["kind"] == "ics" \
            else self.settings.calendar_google_minutes
        last = datetime.fromisoformat(row["last_attempt_at"].replace("Z", "+00:00"))
        return (now - last).total_seconds() >= minutes * 60 - 5

    async def poll_source(self, source: dict, window: Window, now: datetime) -> bool:
        """Fetch one source and diff it. True when source events changed. A failed or unparseable
        fetch records the failure and changes nothing (delete guard 1)."""
        account_id, name = source["account_id"], calendar.stream(source["id"])
        sync_state.record_attempt(self.conn, account_id, name)
        try:
            if source["kind"] == "ics":
                if not self.settings.ics_url:
                    return False
                data = await ics.fetch(self.http, self.settings.ics_url)
                # The window rolls daily, so an unchanged feed is still re-expanded once a day.
                cursor = f"{ics.feed_hash(data)}:{window.first_day}"
                if cursor == sync_state.get_cursor(self.conn, account_id, name):
                    sync_state.record_ok(self.conn, account_id, name, cursor)
                    return False
                instances = await asyncio.to_thread(ics.parse, data, window)
            else:
                if accounts.get_account(self.conn, account_id)["status"] != "ok":
                    return False
                cursor = None
                raw = await self.client(account_id).list_events(source["external_ref"], window.start, window.end)
                instances = [i for i in (gcal.to_instance(e, self.settings.tz) for e in raw) if i]
            stats = calendar.apply_fetch(self.conn, source["id"], instances, window=window, now=now)
            sync_state.record_ok(self.conn, account_id, name, cursor)
            return any(stats.values())
        except AuthError as exc:
            log.warning("account %s: calendar auth error", account_id)
            accounts.set_status(self.conn, account_id, "auth_error")
            sync_state.record_failure(self.conn, account_id, name, str(exc))
        except Exception as exc:  # one source failing must not stop the others
            log.warning("calendar source %s: fetch failed: %s", source["id"], type(exc).__name__)
            sync_state.record_failure(self.conn, account_id, name, calendar.describe_error(exc))
        return False

    async def write(self, hub: calendar.Hub | None) -> None:
        if hub is None:
            await calendar.write_hub(self.conn, None, None)
            return
        if accounts.get_account(self.conn, hub.account_id)["status"] != "ok":
            return
        # A different hub calendar than last time: everything must be written there afresh.
        if sync_state.get_cursor(self.conn, hub.account_id, calendar.HUB_STREAM) not in (None, hub.calendar_id):
            self.conn.execute("UPDATE hub_event SET content_hash = NULL")
        pending = calendar.pending_writes(self.conn)
        if not (pending["upserts"] or pending["deletes"]):
            return
        sync_state.record_attempt(self.conn, hub.account_id, calendar.HUB_STREAM)
        try:
            stats = await calendar.write_hub(self.conn, self.client(hub.account_id), hub)
            sync_state.record_ok(self.conn, hub.account_id, calendar.HUB_STREAM, hub.calendar_id)
            if any(stats.values()):
                log.info("hub calendar: %s", stats)
        except AuthError as exc:
            accounts.set_status(self.conn, hub.account_id, "auth_error")
            sync_state.record_failure(self.conn, hub.account_id, calendar.HUB_STREAM, str(exc))
        except Exception as exc:  # quota or HTTP errors: the rest stay pending for the next cycle
            log.warning("hub calendar: write failed: %s", calendar.describe_error(exc))
            sync_state.record_failure(self.conn, hub.account_id, calendar.HUB_STREAM, calendar.describe_error(exc))
