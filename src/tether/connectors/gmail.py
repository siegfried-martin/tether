"""Gmail connector: polls history.list and mirrors inbox membership into items.

Spec v1.1 rules implemented here:
- A thread is "in" when any INBOX message carries a category outside the
  account's excluded set; a message with no category counts as Personal.
- A Gmail item is open exactly when its thread is in.
- Sent activity on a thread with no item creates it archived, direction out.
- First sync records the history ID, imports INBOX threads active within
  initial_sync_days, then polls history from the recorded ID.
- An expired history ID triggers a full reconcile.
"""

import asyncio
import html
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

import httpx

from tether.connectors import google_oauth
from tether.connectors.google_oauth import AuthError
from tether.crypto import Vault
from tether.db import iso, tx
from tether.services import accounts, items, sync_state

log = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
STREAM = sync_state.GMAIL_HISTORY
# Cursor value while the first import is in progress: "init:<history id recorded at its start>".
INITIAL_PREFIX = "init:"

CATEGORY_LABELS = {
    "CATEGORY_PERSONAL": "personal",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_FORUMS": "forums",
}


class HistoryExpired(Exception):
    """history.list returned 404: the stored history ID is too old."""


class RateLimited(Exception):
    """Gmail kept refusing for quota reasons after retries. Transient: the next poll resumes."""


# 403/429 reasons that mean "slow down", not "access revoked".
QUOTA_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"})


def _error_reasons(resp: httpx.Response) -> set[str]:
    try:
        return {e.get("reason", "") for e in resp.json().get("error", {}).get("errors", [])}
    except ValueError:
        return set()


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


# --------------------------------------------------------------------------
# Pure derivation: thread JSON -> item fields
# --------------------------------------------------------------------------


@dataclass
class ThreadView:
    is_in: bool
    fields: dict


def message_category(labels: list[str]) -> str:
    for label in labels:
        if label in CATEGORY_LABELS:
            return CATEGORY_LABELS[label]
    return "personal"


def thread_is_in(messages: list[dict], excluded: frozenset[str]) -> bool:
    return any(
        "INBOX" in (labels := m.get("labelIds", [])) and message_category(labels) not in excluded
        for m in messages
    )


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _display_name(address: str) -> str:
    """'Ann Lee <ann@x.com>' -> 'Ann Lee'; bare addresses pass through."""
    address = address.strip()
    if "<" in address and not address.startswith("<"):
        return address.split("<", 1)[0].strip().strip('"') or address
    return address.strip("<>")


def thread_url(email: str, thread_id: str) -> str:
    return f"https://mail.google.com/mail/?authuser={quote(email)}#all/{thread_id}"


def derive_thread(thread: dict, *, excluded: frozenset[str], email: str) -> ThreadView | None:
    """Item fields for a thread, or None when it has no non-draft messages."""
    messages = [m for m in thread.get("messages", []) if "DRAFT" not in m.get("labelIds", [])]
    if not messages:
        return None
    messages.sort(key=lambda m: int(m.get("internalDate", 0)))
    latest = messages[-1]
    outbound = "SENT" in latest.get("labelIds", [])
    inbound = [m for m in messages if "SENT" not in m.get("labelIds", [])]
    if inbound:
        sender = _display_name(_header(inbound[-1], "From"))
    else:
        sender = "To: " + _display_name(_header(latest, "To"))
    last_activity = datetime.fromtimestamp(int(latest.get("internalDate", 0)) / 1000, UTC)
    return ThreadView(
        is_in=thread_is_in(messages, excluded),
        fields={
            "title": _header(messages[0], "Subject") or "(no subject)",
            "sender": sender,
            "snippet": html.unescape(latest.get("snippet", "")),
            "url": thread_url(email, thread["id"]),
            "last_activity_at": iso(last_activity),
            "last_direction": "out" if outbound else "in",
        },
    )


def apply_thread(
    conn: sqlite3.Connection, *, account_id: int, thread: dict | None, thread_id: str,
    excluded: frozenset[str], email: str, sent_activity: bool,
) -> None:
    """Mirror one thread's current state into its item."""
    existing = items.get_item(conn, account_id, thread_id)
    view = derive_thread(thread, excluded=excluded, email=email) if thread else None
    if view is None:
        # Thread deleted, or drafts only.
        if existing and existing["state"] != "archived":
            items.set_state(conn, existing["id"], "archived")
        return
    if existing is None and not view.is_in and not sent_activity:
        return  # not in the inbox and nothing we sent: not ours to track
    items.upsert_item(
        conn, account_id=account_id, external_id=thread_id, kind="email",
        fields=view.fields, state="open" if view.is_in else "archived",
    )


def touched_threads(history: list[dict]) -> tuple[set[str], set[str]]:
    """From history records: (thread IDs worth refetching, thread IDs with new sent messages).

    A thread is worth refetching when a change involves INBOX or SENT, or it
    already has an item; callers add the second condition.
    """
    relevant, sent = set(), set()
    for record in history:
        for entry in record.get("messagesAdded", []):
            msg = entry["message"]
            labels = msg.get("labelIds", [])
            if "SENT" in labels and "DRAFT" not in labels:
                sent.add(msg["threadId"])
            relevant.add(msg["threadId"])
        for key in ("labelsAdded", "labelsRemoved", "messagesDeleted"):
            for entry in record.get(key, []):
                relevant.add(entry["message"]["threadId"])
    return relevant, sent


def inbox_query(excluded: frozenset[str], newer_than_days: int | None = None) -> str:
    parts = ["in:inbox"]
    if newer_than_days:
        parts.append(f"newer_than:{newer_than_days}d")
    parts += [f"-category:{c}" for c in sorted(excluded)]
    return " ".join(parts)


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


class GmailApi(Protocol):
    async def get_profile(self) -> dict: ...
    async def list_thread_ids(self, query: str) -> list[str]: ...
    async def get_thread(self, thread_id: str) -> dict | None: ...
    async def list_history(self, start_history_id: str) -> tuple[list[dict], str]: ...
    async def modify_thread(self, thread_id: str, *, add: list[str] = (), remove: list[str] = ()) -> bool: ...


class GmailClient:
    """Thin async REST client with token refresh, pacing and quota backoff.

    Gmail's per-user quota is 6,000 query-cost units a minute, and a metadata
    thread fetch costs roughly 40, so a burst of ~150 fetches exhausts it. The
    client paces requests and waits out quota refusals instead of failing.
    """

    MIN_INTERVAL = 0.5   # seconds between requests: ~120 a minute, under the per-user quota
    MAX_RETRIES = 4      # quota refusals waited out per request before giving up for this poll

    def __init__(self, http: httpx.AsyncClient, conn: sqlite3.Connection, vault: Vault,
                 account_id: int, client_id: str, client_secret: str, *, sleep=asyncio.sleep):
        self.http, self.conn, self.vault = http, conn, vault
        self.account_id = account_id
        self.client_id, self.client_secret = client_id, client_secret
        self.secret = accounts.load_credential(conn, vault, account_id)
        self._sleep = sleep
        self._next_request_at = 0.0

    @property
    def email(self) -> str:
        return self.secret["email"]

    async def _token(self) -> str:
        if self.secret.get("expires_at", 0) - 60 < time.time():
            token = await google_oauth.refresh(
                self.http, client_id=self.client_id, client_secret=self.client_secret,
                refresh_token=self.secret["refresh_token"],
            )
            self.secret["access_token"] = token["access_token"]
            self.secret["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
            self.secret["expires_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.secret["expires_at"]))
            accounts.save_credential(self.conn, self.vault, self.account_id, self.secret)
        return self.secret["access_token"]

    async def _pace(self) -> None:
        wait = self._next_request_at - time.monotonic()
        if wait > 0:
            await self._sleep(wait)
        self._next_request_at = time.monotonic() + self.MIN_INTERVAL

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json: dict | None = None) -> httpx.Response:
        refreshed, quota_waits = False, 0
        while True:
            await self._pace()
            resp = await self.http.request(method, f"{API}{path}", params=params, json=json,
                                           headers={"Authorization": f"Bearer {await self._token()}"})
            if resp.status_code == 401:
                if refreshed:
                    raise AuthError("Gmail rejected a freshly refreshed token (401)")
                refreshed, self.secret["expires_at"] = True, 0  # force a refresh, then retry once
                continue
            reasons = _error_reasons(resp) if resp.status_code == 403 else set()
            if resp.status_code == 429 or reasons & QUOTA_REASONS:
                quota_waits += 1
                if quota_waits > self.MAX_RETRIES:
                    raise RateLimited(f"Gmail quota still exhausted after {self.MAX_RETRIES} waits")
                delay = _retry_after(resp) or min(60.0, 20.0 * quota_waits)
                log.info("account %s: Gmail quota hit, waiting %.0fs", self.account_id, delay)
                await self._sleep(delay)
                continue
            if resp.status_code == 403:
                raise AuthError(f"Gmail refused access: {', '.join(sorted(reasons)) or 'forbidden'}")
            return resp

    async def get_profile(self) -> dict:
        resp = await self._get("/profile")
        resp.raise_for_status()
        return resp.json()

    async def list_thread_ids(self, query: str) -> list[str]:
        ids, page = [], None
        while True:
            params = {"q": query, "maxResults": 500}
            if page:
                params["pageToken"] = page
            resp = await self._get("/threads", params)
            resp.raise_for_status()
            body = resp.json()
            ids += [t["id"] for t in body.get("threads", [])]
            page = body.get("nextPageToken")
            if not page:
                return ids

    async def get_thread(self, thread_id: str) -> dict | None:
        resp = await self._get(f"/threads/{thread_id}", {
            "format": "metadata", "metadataHeaders": ["Subject", "From", "To"],
        })
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def list_history(self, start_history_id: str) -> tuple[list[dict], str]:
        records, page, latest = [], None, start_history_id
        while True:
            params = {"startHistoryId": start_history_id, "maxResults": 500}
            if page:
                params["pageToken"] = page
            resp = await self._get("/history", params)
            if resp.status_code == 404:
                raise HistoryExpired(start_history_id)
            resp.raise_for_status()
            body = resp.json()
            records += body.get("history", [])
            latest = body.get("historyId", latest)
            page = body.get("nextPageToken")
            if not page:
                return records, latest

    async def modify_thread(self, thread_id: str, *, add: list[str] = (), remove: list[str] = ()) -> bool:
        """Add and remove labels on every message in a thread. False when the thread is gone."""
        resp = await self._request("POST", f"/threads/{thread_id}/modify",
                                   json={"addLabelIds": list(add), "removeLabelIds": list(remove)})
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True


class GmailClients:
    """Builds a GmailClient per account for one-off writes (archive, unarchive)."""

    def __init__(self, http: httpx.AsyncClient, conn: sqlite3.Connection, vault: Vault,
                 client_id: str, client_secret: str):
        self.http, self.conn, self.vault = http, conn, vault
        self.client_id, self.client_secret = client_id, client_secret

    def __call__(self, account_id: int) -> GmailClient:
        return GmailClient(self.http, self.conn, self.vault, account_id, self.client_id, self.client_secret)


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


class GmailSync:
    def __init__(self, conn: sqlite3.Connection, api: GmailApi, *, account_id: int, email: str,
                 excluded: frozenset[str], initial_sync_days: int):
        self.conn, self.api = conn, api
        self.account_id, self.email = account_id, email
        self.excluded, self.initial_sync_days = excluded, initial_sync_days

    async def run(self) -> None:
        """One poll: initial sync, incremental history, or full reconcile."""
        sync_state.record_attempt(self.conn, self.account_id, STREAM)
        cursor = sync_state.get_cursor(self.conn, self.account_id, STREAM)
        if cursor == sync_state.RECONCILE:
            cursor = await self.reconcile()
        elif cursor is None or cursor.startswith(INITIAL_PREFIX):
            cursor = await self.initial_sync(resume=cursor)
        else:
            try:
                cursor = await self.poll_history(cursor)
            except HistoryExpired:
                log.info("account %s: history ID expired, reconciling", self.account_id)
                cursor = await self.reconcile()
        sync_state.record_ok(self.conn, self.account_id, STREAM, cursor)

    async def _apply(self, thread_id: str, sent_activity: bool = False) -> None:
        thread = await self.api.get_thread(thread_id)
        with tx(self.conn):
            apply_thread(self.conn, account_id=self.account_id, thread=thread, thread_id=thread_id,
                         excluded=self.excluded, email=self.email, sent_activity=sent_activity)

    async def initial_sync(self, resume: str | None = None) -> str:
        """Import INBOX threads inside the window, resumably.

        The starting history ID is saved first (as "init:<id>"), so an
        interrupted import resumes where it stopped: threads already imported
        are skipped, and any change to them since is replayed from that ID.
        """
        if resume:
            history_id = resume.removeprefix(INITIAL_PREFIX)
        else:
            history_id = str((await self.api.get_profile())["historyId"])
            sync_state.set_cursor(self.conn, self.account_id, STREAM, INITIAL_PREFIX + history_id)
        done = items.item_states(self.conn, self.account_id).keys() if resume else set()
        for thread_id in await self.api.list_thread_ids(inbox_query(self.excluded, self.initial_sync_days)):
            if thread_id not in done:
                await self._apply(thread_id)
        return history_id

    async def poll_history(self, cursor: str) -> str:
        records, latest = await self.api.list_history(cursor)
        relevant, sent = touched_threads(records)
        known = items.item_states(self.conn, self.account_id)
        for thread_id in sorted(relevant):
            if thread_id in known or thread_id in sent or self._involves_inbox(records, thread_id):
                await self._apply(thread_id, sent_activity=thread_id in sent)
        return str(latest)

    @staticmethod
    def _involves_inbox(records: list[dict], thread_id: str) -> bool:
        for record in records:
            for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                for entry in record.get(key, []):
                    if entry["message"]["threadId"] != thread_id:
                        continue
                    labels = entry.get("labelIds", []) + entry["message"].get("labelIds", [])
                    if "INBOX" in labels:
                        return True
        return False

    async def reconcile(self) -> str:
        """Full reconcile: after an expired history ID, or when sync settings change.

        Converges when interrupted: a rerun only refetches what still differs.
        """
        history_id = (await self.api.get_profile())["historyId"]
        all_in = set(await self.api.list_thread_ids(inbox_query(self.excluded)))
        recent_in = set(await self.api.list_thread_ids(inbox_query(self.excluded, self.initial_sync_days)))
        states = items.item_states(self.conn, self.account_id)
        # Open items no longer in, and archived items back in: refetch to mirror.
        stale = {t for t, s in states.items() if (s == "open") != (t in all_in)}
        # New threads only when active inside the initial-sync window.
        new = recent_in - states.keys()
        for thread_id in sorted(stale | new):
            await self._apply(thread_id)
        return str(history_id)
