"""GmailClient error classification, quota backoff, and resumable first sync."""

import time

import httpx
import pytest
from conftest import msg

from tether.connectors import google_oauth
from tether.connectors.gmail import GmailClient, GmailSync, RateLimited
from tether.connectors.google_oauth import AuthError
from tether.services import accounts, items, sync_state


def quota_403():
    return httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})


@pytest.fixture
def account(conn, vault):
    secret = {"refresh_token": "rt", "access_token": "at", "expires_at": int(time.time()) + 3600,
              "email": "me@example.com", "sub": "s"}
    return accounts.upsert_gmail_account(conn, vault, email="me@example.com", secret=secret, scopes="x")


def client_with(conn, vault, account, responses):
    calls, sleeps = [], []
    queue = list(responses)

    def handler(request):
        calls.append(request.url.path)
        return queue.pop(0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GmailClient(http, conn, vault, account, "cid", "cs", sleep=fake_sleep)
    return client, calls, sleeps


async def test_quota_403_waits_and_retries(conn, vault, account):
    client, calls, sleeps = client_with(conn, vault, account, [
        quota_403(), httpx.Response(200, json={"historyId": "7"})])
    assert (await client.get_profile())["historyId"] == "7"
    assert len(calls) == 2
    assert 20.0 in sleeps, "first quota wait is 20s"


async def test_429_honours_retry_after(conn, vault, account):
    client, _, sleeps = client_with(conn, vault, account, [
        httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"historyId": "7"})])
    await client.get_profile()
    assert 7.0 in sleeps


async def test_persistent_quota_raises_rate_limited_not_auth(conn, vault, account):
    client, calls, _ = client_with(conn, vault, account, [quota_403() for _ in range(5)])
    with pytest.raises(RateLimited):
        await client.get_profile()
    assert len(calls) == 5


async def test_permission_403_is_auth_error(conn, vault, account):
    client, _, _ = client_with(conn, vault, account, [
        httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}})])
    with pytest.raises(AuthError, match="insufficientPermissions"):
        await client.get_profile()


async def test_401_refreshes_once_then_retries(conn, vault, account, monkeypatch):
    refreshes = []

    async def fake_refresh(http, **kw):
        refreshes.append(kw["refresh_token"])
        return {"access_token": "new", "expires_in": 3600}

    monkeypatch.setattr(google_oauth, "refresh", fake_refresh)
    client, _, _ = client_with(conn, vault, account, [
        httpx.Response(401), httpx.Response(200, json={"historyId": "7"})])
    await client.get_profile()
    assert refreshes == ["rt"]
    assert accounts.load_credential(conn, vault, account)["access_token"] == "new"


async def test_requests_are_paced(conn, vault, account):
    client, _, sleeps = client_with(conn, vault, account, [httpx.Response(200, json={}) for _ in range(3)])
    for _ in range(3):
        await client.get_profile()
    assert len([s for s in sleeps if 0 < s <= GmailClient.MIN_INTERVAL]) == 2


class FlakyGmail:
    """Wraps FakeGmail; get_thread raises RateLimited after `budget` fetches."""

    def __init__(self, inner, budget):
        self.inner, self.budget = inner, budget

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def get_thread(self, thread_id):
        if self.budget == 0:
            raise RateLimited("quota")
        self.budget -= 1
        return await self.inner.get_thread(thread_id)


async def test_interrupted_initial_sync_resumes_without_refetching(conn, fake_gmail, account):
    for n in range(5):
        fake_gmail.put(f"t{n}", msg(f"m{n}", f"t{n}", labels=["INBOX"]))
    sync = lambda api: GmailSync(conn, api, account_id=account, email="me@example.com",
                                 excluded=frozenset(), initial_sync_days=14)
    with pytest.raises(RateLimited):
        await sync(FlakyGmail(fake_gmail, budget=3)).run()
    assert sync_state.get_cursor(conn, account, "gmail.history") == "init:100"
    assert len(items.item_states(conn, account)) == 3

    fake_gmail.history_id = 150  # mailbox moved on meanwhile
    fake_gmail.fetches.clear()
    await sync(fake_gmail).run()
    assert len(fake_gmail.fetches) == 2, "only the two unfetched threads"
    assert len(items.item_states(conn, account)) == 5
    # Cursor is the ID recorded at the start, so changes during the gap are replayed.
    assert sync_state.get_cursor(conn, account, "gmail.history") == "100"


def test_access_log_redacts_query_string():
    import logging
    from tether.web.app import RedactQueryStrings
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4", "GET", "/auth/google/callback?code=SECRET&state=x", "1.1", 303), None)
    RedactQueryStrings().filter(record)
    assert "SECRET" not in record.getMessage() and "/auth/google/callback?…" in record.getMessage()
