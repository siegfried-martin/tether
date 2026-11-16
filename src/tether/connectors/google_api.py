"""Shared Google REST client: token refresh, request pacing and quota backoff.

GmailClient and CalendarClient build on it. One instance serves one account.
"""

import asyncio
import logging
import sqlite3
import time

import httpx

from tether.connectors import google_oauth
from tether.connectors.google_oauth import AuthError
from tether.crypto import Vault
from tether.services import accounts

log = logging.getLogger(__name__)


class RateLimited(Exception):
    """Google kept refusing for quota reasons after retries. Transient: the next poll resumes."""


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


class GoogleClient:
    """Thin async REST client with token refresh, pacing and quota backoff.

    Requests are paced MIN_INTERVAL apart, and quota refusals are waited out
    instead of failing. Subclasses set API (the base URL) and SERVICE (for
    messages).
    """

    API = ""
    SERVICE = "Google"
    MIN_INTERVAL = 0.5
    MAX_RETRIES = 4      # quota refusals waited out per request before giving up for this poll
    # Gmail answers 403 only when access is gone. Calendar also answers 403 for one calendar
    # it may not write, which must not mark the whole account as needing a reconnect.
    FORBIDDEN_IS_AUTH = True

    def __init__(self, http: httpx.AsyncClient, conn: sqlite3.Connection, vault: Vault,
                 account_id: int, client_id: str, client_secret: str, *, sleep=asyncio.sleep,
                 min_interval: float | None = None):
        self.http, self.conn, self.vault = http, conn, vault
        if min_interval is not None:
            self.MIN_INTERVAL = min_interval
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
            resp = await self.http.request(method, f"{self.API}{path}", params=params, json=json,
                                           headers={"Authorization": f"Bearer {await self._token()}"})
            if resp.status_code == 401:
                if refreshed:
                    raise AuthError(f"{self.SERVICE} rejected a freshly refreshed token (401)")
                refreshed, self.secret["expires_at"] = True, 0  # force a refresh, then retry once
                continue
            reasons = _error_reasons(resp) if resp.status_code == 403 else set()
            if resp.status_code == 429 or reasons & QUOTA_REASONS:
                quota_waits += 1
                if quota_waits > self.MAX_RETRIES:
                    raise RateLimited(f"{self.SERVICE} quota still exhausted after {self.MAX_RETRIES} waits")
                delay = _retry_after(resp) or min(60.0, 20.0 * quota_waits)
                log.info("account %s: %s quota hit, waiting %.0fs", self.account_id, self.SERVICE, delay)
                await self._sleep(delay)
                continue
            if resp.status_code == 403 and self.FORBIDDEN_IS_AUTH:
                raise AuthError(f"{self.SERVICE} refused access: {', '.join(sorted(reasons)) or 'forbidden'}")
            return resp
