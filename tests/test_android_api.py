"""The Android app's API: sign-in as the built-in client, bearer tokens on /api,
connected apps, widget data, task focus and assetlinks."""

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from test_calendar import TZ, apply, inst, sources  # noqa: F401  (fixtures)
from test_mcp import connect, pkce, rpc
from test_web import client, login, settings  # noqa: F401  (fixtures)

from tether.connectors.events import Instance
from tether.services import calendar, mcp_auth, tasks, widget

APP_REDIRECT = "com.marstol.tether:/oauth2redirect"
TODAY = date(2026, 11, 15)


def app_sign_in(client, monkeypatch, *, signed_in=False):
    """The app's flow: authorize in a browser tab, approve, exchange the code. Returns the token response."""
    verifier, challenge = pkce()
    start = client.get("/authorize", params={
        "response_type": "code", "client_id": mcp_auth.APP_CLIENT_ID, "redirect_uri": APP_REDIRECT,
        "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256"}, follow_redirects=False)
    assert start.status_code == 302, start.text
    consent = urlparse(start.headers["location"])
    consent_path = f"{consent.path}?{consent.query}"
    if not signed_in:
        login(client, monkeypatch)
    page = client.get(consent_path)
    assert "Allow Tether for Android to use Tether?" in page.text and "return to <strong>the app</strong>" in page.text
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    done = client.post("/auth/mcp/consent", data={"req": parse_qs(consent.query)["req"][0], "csrf": csrf,
                                                 "decision": "approve"}, follow_redirects=False)
    back = done.headers["location"]
    assert back.startswith(APP_REDIRECT + "?")
    query = parse_qs(urlparse(back).query)
    assert query["state"] == ["s1"]
    tokens = client.post("/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": APP_REDIRECT,
        "client_id": mcp_auth.APP_CLIENT_ID, "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    return tokens.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- auth


def test_app_signs_in_and_uses_the_api_with_its_token(client, monkeypatch):
    tokens = app_sign_in(client, monkeypatch)
    client.cookies.clear()   # the widget has no browser session
    assert client.get("/api/me").status_code == 401
    me = client.get("/api/me", headers=bearer(tokens["access_token"]))
    assert me.status_code == 200 and me.json() == {"email": "owner@example.com"}
    assert client.post("/api/tasks", json={"title": "From the widget"},
                       headers=bearer(tokens["access_token"])).status_code == 201

    # Refresh works for the app client, and the old access token keeps working until it expires.
    fresh = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                        "client_id": mcp_auth.APP_CLIENT_ID})
    assert fresh.status_code == 200
    assert client.get("/api/me", headers=bearer(fresh.json()["access_token"])).status_code == 200


def test_bad_bearer_says_so(client):
    resp = client.get("/api/widget/tasks", headers=bearer("ta_nope"))
    assert resp.status_code == 401 and "invalid_token" in resp.headers["www-authenticate"]


def test_app_and_connector_tokens_stay_in_their_lanes(client, monkeypatch):
    app_tokens = app_sign_in(client, monkeypatch)
    _, _, _, mcp_tokens = connect_signed_in(client, monkeypatch)
    client.cookies.clear()
    assert rpc(client, app_tokens["access_token"], "tools/list").status_code == 401
    assert rpc(client, mcp_tokens["access_token"], "tools/list").status_code == 200
    assert client.get("/api/me", headers=bearer(mcp_tokens["access_token"])).status_code == 401


def connect_signed_in(client, monkeypatch):
    """The Claude connector flow from test_mcp, which expects to start signed out."""
    client.cookies.clear()
    return connect(client, monkeypatch)


def test_other_clients_cannot_use_the_app_redirect(client):
    resp = client.post("/register", json={
        "redirect_uris": [APP_REDIRECT], "client_name": "Imposter", "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"], "response_types": ["code"]})
    assert resp.status_code == 400


def test_connections_list_and_revoke(client, monkeypatch, conn):
    connect_signed_in(client, monkeypatch)   # Claude, signs in as owner
    app_tokens = app_sign_in(client, monkeypatch, signed_in=True)
    listed = client.get("/api/connections").json()
    assert sorted((c["kind"], c["name"]) for c in listed) == [("app", "Tether for Android"), ("mcp", "Claude")]
    app_grant = next(c["id"] for c in listed if c["kind"] == "app")

    # Only a browser session manages connections, not a token.
    saved = dict(client.cookies)
    client.cookies.clear()
    assert client.get("/api/connections", headers=bearer(app_tokens["access_token"])).status_code == 401
    client.cookies.update(saved)

    assert client.delete(f"/api/connections/{app_grant}").status_code == 204
    assert client.delete(f"/api/connections/{app_grant}").status_code == 404
    assert [c["kind"] for c in client.get("/api/connections").json()] == ["mcp"]
    client.cookies.clear()
    assert client.get("/api/me", headers=bearer(app_tokens["access_token"])).status_code == 401


def test_expired_connections_are_not_listed(client, monkeypatch, conn):
    app_sign_in(client, monkeypatch)
    conn.execute("UPDATE oauth_token SET expires_at = 0")
    assert client.get("/api/connections").json() == []


def test_assetlinks(client, settings):
    assert client.get("/.well-known/assetlinks.json").status_code == 404
    settings.android_cert_sha256 = "aa:bb:cc, DD:EE:FF"
    body = client.get("/.well-known/assetlinks.json").json()
    assert body[0]["target"] == {"namespace": "android_app", "package_name": "com.marstol.tether",
                                 "sha256_cert_fingerprints": ["AA:BB:CC", "DD:EE:FF"]}


# --------------------------------------------------------------------------- tasks widget


def test_widget_tasks(conn):
    a = tasks.create_task(conn, title="A", due_at=(TODAY - timedelta(days=1)).isoformat(), tags=["Home"])
    b = tasks.create_task(conn, title="B", due_at=TODAY.isoformat())
    sub = tasks.create_task(conn, title="B.1", parent_id=b["id"], tags=["Zion"])
    tasks.create_task(conn, title="C", due_at=(TODAY + timedelta(days=1)).isoformat())
    done = tasks.create_task(conn, title="Done")
    conn.execute("UPDATE task SET status = 'done', completed_at = '2026-11-14T00:00:00Z' WHERE id = ?", (done["id"],))
    tasks.move_task(conn, b["id"], before=a["id"])

    out = widget.widget_tasks(conn, TODAY)
    assert [(t["title"], t["due"]) for t in out["tasks"]] == [("B", "today"), ("A", "overdue"), ("C", "tomorrow")]
    assert [s["title"] for s in out["tasks"][0]["subtasks"]] == ["B.1"]
    assert out["count"] == 3 and out["tag"] is None
    assert out["tasks"][1]["tags"][0]["name"] == "Home" and out["tasks"][0]["url"] == f"/tasks?task={b['id']}"
    assert {t["name"] for t in out["tags"]} == {"Home", "Zion"}

    zion = next(t["id"] for t in out["tags"] if t["name"] == "Zion")
    tagged = widget.widget_tasks(conn, TODAY, tag_id=zion)
    assert [t["title"] for t in tagged["tasks"]] == ["B"] and tagged["tag"]["name"] == "Zion"
    assert tagged["tasks"][0]["subtasks"][0]["id"] == sub["id"]
    assert [t["title"] for t in widget.widget_tasks(conn, TODAY, limit=1)["tasks"]] == ["B"]
    with pytest.raises(tasks.TaskError):
        widget.widget_tasks(conn, TODAY, tag_id=999)


def test_widget_api_etag(client, monkeypatch):
    login(client, monkeypatch)
    client.post("/api/tasks", json={"title": "One"})
    first = client.get("/api/widget/tasks")
    assert first.status_code == 200 and first.json()["tasks"][0]["title"] == "One"
    tag = first.headers["etag"]
    assert client.get("/api/widget/tasks", headers={"If-None-Match": tag}).status_code == 304
    client.post("/api/tasks", json={"title": "Two"})
    assert client.get("/api/widget/tasks", headers={"If-None-Match": tag}).status_code == 200
    assert client.get("/api/widget/tasks?tag=42").status_code == 404
    cal = client.get("/api/widget/calendar?start=2026-11-15&days=3")
    assert cal.status_code == 200 and [d["date"] for d in cal.json()["days"]] == [
        "2026-11-15", "2026-11-16", "2026-11-17"]
    assert client.get("/api/widget/calendar?start=nope").status_code == 422


# --------------------------------------------------------------------------- calendar widget


def test_widget_calendar_spans_days(conn, sources):
    apply(conn, sources["me"], [
        inst("standup", "2026-11-16T14:00:00Z", title="Standup"),
        # 10pm to 1am New York: Monday night into Tuesday.
        inst("late", "2026-11-17T03:00:00Z", minutes=180, title="Late deploy"),
        # 11pm to midnight New York: ends exactly at midnight, so Monday only.
        inst("tomidnight", "2026-11-17T04:00:00Z", minutes=60, title="Wind down"),
        Instance(uid="trip", instance_start="2026-11-13", start="2026-11-13", end="2026-11-17", all_day=True,
                 title="Trip"),
    ])
    calendar.assign_hub(conn)
    out = widget.widget_calendar(conn, date(2026, 11, 15), 3, TZ, TODAY)
    days = {d["date"]: [(e["title"], e["continued"], e["continues"]) for e in d["events"]] for d in out["days"]}
    assert days == {
        "2026-11-15": [("Trip", True, True)],
        "2026-11-16": [("Trip", True, False), ("Standup", False, False), ("Late deploy", False, True),
                       ("Wind down", False, False)],
        "2026-11-17": [("Late deploy", True, False)],
    }
    standup = out["days"][1]["events"][1]
    assert standup["url"] == f"/calendar/day?date=2026-11-16&event={standup['id']}"
    assert standup["calendar"] == "me@x.com" and standup["color"]
    assert out["tz"] == "America/New_York"
    assert len(widget.widget_calendar(conn, TODAY, 99, TZ, TODAY)["days"]) == widget.MAX_DAYS


# --------------------------------------------------------------------------- task focus


def test_task_page_focus_finds_the_page(conn):
    made = [tasks.create_task(conn, title=f"T{n}") for n in range(5)]
    page = tasks.task_page(conn, offset=0, limit=2, focus=made[4]["id"])
    assert page["offset"] == 4 and page["found"] and [t["title"] for t in page["tasks"]] == ["T4"]
    missing = tasks.task_page(conn, offset=2, limit=2, focus=999)
    assert missing["offset"] == 2 and missing["focus"] == 999 and not missing["found"]
