import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from tether.config import Settings
from tether.connectors import google_oauth
from tether.crypto import generate_key
from tether.services import accounts, items
from tether.web.app import create_app

OWNER = "owner@example.com"
CLIENT_ID = "cid.apps.googleusercontent.com"


def id_token(email, sub="sub-1", verified=True):
    claims = {"aud": CLIENT_ID, "iss": "https://accounts.google.com", "exp": time.time() + 300,
              "sub": sub, "email": email, "email_verified": verified}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


@pytest.fixture
def settings(tmp_path):
    return Settings(encryption_key=generate_key(), session_secret="s" * 32, google_client_id=CLIENT_ID,
                    google_client_secret="secret", owner_email=OWNER, static_dir=tmp_path / "none",
                    _env_file=None)


@pytest.fixture
def client(settings, conn):
    with TestClient(create_app(settings, conn=conn, start_pollers=False)) as c:
        yield c


def _oauth_round_trip(client, monkeypatch, start_path, token):
    async def fake_exchange(*args, **kwargs):
        return token
    monkeypatch.setattr(google_oauth, "exchange_code", fake_exchange)
    redirect = client.get(start_path, follow_redirects=False)
    assert redirect.status_code == 303
    state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]
    return client.get(f"/auth/google/callback?code=c&state={state}", follow_redirects=False)


def login(client, monkeypatch, email=OWNER, **kw):
    return _oauth_round_trip(client, monkeypatch, "/auth/login", {"id_token": id_token(email, **kw)})


def test_api_requires_login(client):
    assert client.get("/api/items").status_code == 401


def test_owner_can_log_in(client, monkeypatch):
    assert login(client, monkeypatch).status_code == 303
    assert client.get("/api/me").json() == {"email": OWNER}


@pytest.mark.parametrize("email,verified", [("intruder@example.com", True), (OWNER, False)])
def test_others_are_refused(client, monkeypatch, email, verified):
    assert login(client, monkeypatch, email=email, verified=verified).status_code == 403
    assert client.get("/api/me").status_code == 401


def test_owner_sub_pin(settings, conn, monkeypatch):
    settings.owner_sub = "the-real-sub"
    with TestClient(create_app(settings, conn=conn, start_pollers=False)) as c:
        assert login(c, monkeypatch, sub="other").status_code == 403
        assert login(c, monkeypatch, sub="the-real-sub").status_code == 303


def test_bad_state_rejected(client):
    assert client.get("/auth/google/callback?code=c&state=forged").status_code == 400


def test_connect_mailbox_stores_encrypted_credential(client, conn, monkeypatch):
    login(client, monkeypatch)
    # A long marker: short strings like "rt" turn up in random base64 ciphertext by chance.
    refresh = "refresh-token-plaintext-marker"
    token = {"id_token": id_token("me@gmail.com", sub="s2"), "access_token": "at", "refresh_token": refresh,
             "expires_in": 3600, "scope": f"openid email {google_oauth.GMAIL_MODIFY}"}
    resp = _oauth_round_trip(client, monkeypatch, "/accounts/google/connect", token)
    assert resp.status_code == 303 and resp.headers["location"] == "/settings"
    [acct] = client.get("/api/accounts").json()
    assert acct["label"] == "me@gmail.com" and acct["kind"] == "gmail"
    raw = conn.execute("SELECT ciphertext FROM credential").fetchone()[0]
    assert refresh.encode() not in raw, "refresh token must not be stored in clear"
    secret = accounts.load_credential(conn, client.app.state.vault, acct["id"])
    assert secret["refresh_token"] == refresh and secret["email"] == "me@gmail.com"
    # Reconnecting the same mailbox updates, never duplicates.
    _oauth_round_trip(client, monkeypatch, "/accounts/google/connect", token)
    assert len(client.get("/api/accounts").json()) == 1


def test_connect_without_gmail_scope_refused(client, monkeypatch):
    login(client, monkeypatch)
    token = {"id_token": id_token("me@gmail.com"), "access_token": "at", "refresh_token": "rt",
             "expires_in": 3600, "scope": "openid email"}
    assert _oauth_round_trip(client, monkeypatch, "/accounts/google/connect", token).status_code == 400


def test_items_and_overview(client, conn, monkeypatch):
    login(client, monkeypatch)
    acct = conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'a', '#123')").lastrowid
    items.upsert_item(conn, account_id=acct, external_id="t", kind="email",
                      fields={"title": "Hi", "last_activity_at": "2026-11-15T10:00:00Z", "last_direction": "in"})
    accounts.set_status(conn, acct, "auth_error")
    listed = client.get("/api/items").json()
    assert listed["total"] == 1
    [item] = listed["items"]
    assert item["title"] == "Hi" and item["account_color"] == "#123"
    for n in range(3):
        items.upsert_item(conn, account_id=acct, external_id=f"p{n}", kind="email",
                          fields={"title": f"P{n}", "last_activity_at": f"2026-11-14T10:0{n}:00Z", "last_direction": "in"})
    page = client.get("/api/items?limit=2&offset=2").json()
    assert page["total"] == 4 and [i["title"] for i in page["items"]] == ["P1", "P0"]
    overview = client.get("/api/overview").json()
    assert overview["inbox"]["count"] == 4
    assert overview["urgent"]["alerts"][0]["kind"] == "auth_error"


def test_sync_settings_api(client, conn, monkeypatch):
    login(client, monkeypatch)
    acct = accounts.upsert_gmail_account(conn, client.app.state.vault, email="me@gmail.com",
                                         secret={"refresh_token": "rt", "email": "me@gmail.com"}, scopes="x")
    [view] = client.get("/api/accounts").json()
    assert view["sync"]["initial_sync_days"] == 14
    assert view["sync"]["excluded_categories"] == ["forums", "promotions", "social"]
    resp = client.patch(f"/api/accounts/{acct}/sync", json={"excluded_categories": ["updates"], "initial_sync_days": 7})
    assert resp.status_code == 200 and resp.json()["changed"]
    assert resp.json()["account"]["sync"] == {**view["sync"], "excluded_categories": ["updates"], "initial_sync_days": 7}
    assert client.patch(f"/api/accounts/{acct}/sync", json={"initial_sync_days": 500}).status_code == 422
    assert client.patch("/api/accounts/999/sync", json={"initial_sync_days": 5}).status_code == 404


def test_home_page_for_visitors(settings, conn, monkeypatch, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("app shell")
    (static / "home.html").write_text("public home")
    (static / "privacy.html").write_text("privacy")
    settings.static_dir = static
    with TestClient(create_app(settings, conn=conn, start_pollers=False)) as c:
        assert c.get("/").text == "public home"                  # signed out: what Tether is
        assert c.get("/").headers["X-Tether-Page"] == "home"
        assert c.get("/inbox").text == "app shell"               # deep links still reach the app's sign-in
        assert c.get("/privacy.html").text == "privacy"
        login(c, monkeypatch)
        assert c.get("/").text == "app shell"                    # signed in: the app
        assert c.get("/about").text == "public home"
