"""MCP connector: dynamic registration, owner consent, PKCE token exchange, tool calls."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

from test_web import client, login, settings  # noqa: F401  (fixtures)

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
# Host must match TETHER_BASE_URL (DNS-rebinding protection on /mcp).
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "Host": "localhost:8000"}


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register(client, redirect=REDIRECT):
    return client.post("/register", json={
        "redirect_uris": [redirect], "client_name": "Claude", "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})


def authorize(client, client_id, challenge):
    return client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "st8",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": "http://localhost:8000/mcp"}, follow_redirects=False)


def connect(client, monkeypatch):
    """Run the whole connector flow; returns the token response."""
    client_id = register(client).json()["client_id"]
    verifier, challenge = pkce()
    consent_url = authorize(client, client_id, challenge).headers["location"]
    consent_path = urlparse(consent_url).path + "?" + urlparse(consent_url).query

    # Not signed in yet: consent sends you to Google, then back to consent.
    assert client.get(consent_path, follow_redirects=False).headers["location"] == "/auth/login"
    assert login(client, monkeypatch).headers["location"] == consent_path

    page = client.get(consent_path)
    assert page.status_code == 200 and "Allow Claude to use Tether?" in page.text
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    req = parse_qs(urlparse(consent_url).query)["req"][0]
    done = client.post("/auth/mcp/consent", data={"req": req, "csrf": csrf, "decision": "approve"},
                       follow_redirects=False)
    back = urlparse(done.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    query = parse_qs(back.query)
    assert query["state"] == ["st8"]

    tokens = client.post("/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier, "resource": "http://localhost:8000/mcp"})
    assert tokens.status_code == 200, tokens.text
    return client_id, query["code"][0], verifier, tokens.json()


def rpc(client, token, method, params=None):
    return client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_discovery_metadata(client):
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "http://localhost:8000/mcp"
    assert prm["authorization_servers"] == ["http://localhost:8000"]
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"] == "http://localhost:8000/register"
    assert "S256" in asm["code_challenge_methods_supported"]
    # Other well-known paths are 404, not the SPA shell.
    assert client.get("/.well-known/openid-configuration").status_code == 404


def test_mcp_requires_a_token(client):
    resp = client.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]
    assert rpc(client, "ta_forged", "tools/list").status_code == 401


def test_mcp_rejects_foreign_host(client, monkeypatch):
    _, _, _, tokens = connect(client, monkeypatch)
    resp = client.post("/mcp", headers={**MCP_HEADERS, "Host": "evil.example",
                                        "Authorization": f"Bearer {tokens['access_token']}"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 421


def test_redirects_limited_to_claude_and_loopback(client):
    assert register(client, "https://evil.example/cb").status_code == 400
    assert register(client, "http://claude.ai/cb").status_code == 400  # https only
    assert register(client, "http://localhost:51234/callback").status_code == 201


def test_full_flow_then_tool_call(client, monkeypatch):
    _, _, _, tokens = connect(client, monkeypatch)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
    init = rpc(client, tokens["access_token"], "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert init.status_code == 200, init.text
    tools = rpc(client, tokens["access_token"], "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "sync_status", "list_items", "archive_item", "unarchive_item", "get_overview", "list_tasks",
        "create_task", "update_task", "complete_task", "search_mail", "get_thread", "ingest_digest"}
    called = rpc(client, tokens["access_token"], "tools/call", {"name": "sync_status", "arguments": {}})
    assert called.status_code == 200 and "error" not in called.json()


def test_code_is_single_use(client, monkeypatch):
    client_id, code, verifier, _ = connect(client, monkeypatch)
    again = client.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                        "client_id": client_id, "code_verifier": verifier})
    assert again.status_code == 400


def test_refresh_rotates(client, monkeypatch):
    client_id, _, _, tokens = connect(client, monkeypatch)
    fresh = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                        "client_id": client_id})
    assert fresh.status_code == 200
    assert fresh.json()["refresh_token"] != tokens["refresh_token"]
    reused = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                         "client_id": client_id})
    assert reused.status_code == 400
    assert rpc(client, fresh.json()["access_token"], "tools/list").status_code == 200


def test_revoke_kills_the_grant(client, monkeypatch):
    client_id, _, _, tokens = connect(client, monkeypatch)
    assert client.post("/revoke", data={"token": tokens["refresh_token"], "client_id": client_id,
                                           "client_secret": ""}).status_code == 200
    assert rpc(client, tokens["access_token"], "tools/list").status_code == 401


def test_wrong_pkce_verifier_refused(client, monkeypatch):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    consent_url = authorize(client, client_id, challenge).headers["location"]
    login(client, monkeypatch)
    page = client.get(urlparse(consent_url).path + "?" + urlparse(consent_url).query)
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    req = parse_qs(urlparse(consent_url).query)["req"][0]
    done = client.post("/auth/mcp/consent", data={"req": req, "csrf": csrf, "decision": "approve"},
                       follow_redirects=False)
    code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
    bad = client.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                      "client_id": client_id, "code_verifier": "x" * 50})
    assert bad.status_code == 400


def test_deny_returns_access_denied(client, monkeypatch):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    consent_url = authorize(client, client_id, challenge).headers["location"]
    login(client, monkeypatch)
    page = client.get(urlparse(consent_url).path + "?" + urlparse(consent_url).query)
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    req = parse_qs(urlparse(consent_url).query)["req"][0]
    done = client.post("/auth/mcp/consent", data={"req": req, "csrf": csrf, "decision": "deny"},
                       follow_redirects=False)
    assert parse_qs(urlparse(done.headers["location"]).query)["error"] == ["access_denied"]


@pytest.mark.parametrize("csrf", ["", "wrong"])
def test_consent_needs_csrf(client, monkeypatch, csrf):
    login(client, monkeypatch)
    assert client.post("/auth/mcp/consent", data={"req": "x", "csrf": csrf or "_", "decision": "approve"}
                       ).status_code == 403


def test_inbox_tools(client, conn, fake_gmail, monkeypatch):
    from conftest import msg
    from tether.services import items
    _, _, _, tokens = connect(client, monkeypatch)
    client.app.state.gmail_clients = lambda account_id: fake_gmail
    acct = conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'me@x.com', '#000')").lastrowid
    fake_gmail.put("t1", msg("m", "t1", labels=["INBOX"]))
    item_id = items.upsert_item(conn, account_id=acct, external_id="t1", kind="email",
                                fields={"title": "Hi", "last_activity_at": "2026-11-15T10:00:00Z",
                                        "last_direction": "in"}).id

    def call(name, **args):
        body = rpc(client, tokens["access_token"], "tools/call", {"name": name, "arguments": args}).json()
        return body["result"]

    import json
    listed = call("list_items")
    assert listed["structuredContent"]["result"][0]["id"] == item_id
    archived = call("archive_item", id=item_id)
    assert json.loads(archived["content"][0]["text"])["state"] == "archived"
    assert fake_gmail.modifies == [("t1", (), ("INBOX",))]
    missing = call("archive_item", id=999)
    assert missing["isError"] and "no item 999" in missing["content"][0]["text"]


def test_task_from_search_flow(client, conn, fake_gmail, monkeypatch):
    import json
    from conftest import msg
    _, _, _, tokens = connect(client, monkeypatch)
    client.app.state.gmail_clients = lambda account_id: fake_gmail
    conn.execute("INSERT INTO account (kind, label, color, excluded_categories) VALUES ('gmail', 'me@x.com', '#000', '')")
    fake_gmail.put("t9", msg("m", "t9", labels=["CATEGORY_PERSONAL"], subject="Contract renewal"))

    def call(name, **args):
        result = rpc(client, tokens["access_token"], "tools/call", {"name": name, "arguments": args}).json()["result"]
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    [hit] = call("search_mail", query="contract")["results"]
    assert hit["in_inbox"] is False and hit["item_id"] is None
    task = call("create_task", title="Renew contract", thread={"account": hit["account"], "thread_id": hit["thread_id"]},
                due_at="2026-11-20", priority="high")
    assert task["item_title"] == "Contract renewal" and task["item_state"] == "archived"
    assert call("update_task", id=task["id"], fields={"notes": "ask legal"})["notes"] == "ask legal"
    assert call("complete_task", id=task["id"])["status"] == "done"
    overview = call("get_overview")
    assert overview["tasks"]["count"] == 0


def test_ingest_digest_tool(client, conn, monkeypatch):
    import json
    _, _, _, tokens = connect(client, monkeypatch)

    def call(name, **args):
        return rpc(client, tokens["access_token"], "tools/call", {"name": name, "arguments": args}).json()["result"]

    out = json.loads(call("ingest_digest", raw_text="ITEM From: Ann Subject: Plat Needed: Review Priority: high",
                          digest_date="2026-11-15")["content"][0]["text"])
    assert out["created"] == 1 and out["rejected"] == []
    bad = call("ingest_digest", raw_text="NO ACTION ITEMS", digest_date="soon")
    assert bad["isError"] and "digest_date" in bad["content"][0]["text"]
