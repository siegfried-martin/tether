"""Notifications: Web Push encryption and VAPID, which mail and reminders fall due, and sending."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from test_calendar import feed, vevent, WINDOW
from test_web import client, login, settings  # noqa: F401  (fixtures)
from tether.config import Settings
from tether.connectors import gcal, ics, webpush
from tether.connectors.events import Instance, Window
from tether.crypto import generate_key
from tether.db import iso
from tether.services import calendar, items, notify

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)   # 10:00 in New York


def device():
    """A browser's push keys: (private key, p256dh, auth)."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key, webpush.b64(webpush._public_bytes(key.public_key())), webpush.b64(b"0123456789abcdef")


# --------------------------------------------------------------------------- Web Push


def test_encrypted_payload_decrypts_with_the_device_key():
    key, p256dh, auth = device()
    body = webpush.encrypt(b'{"title":"hi"}', p256dh, auth)
    assert http_ece.decrypt(body, private_key=key, auth_secret=webpush.unb64(auth), version="aes128gcm") \
        == b'{"title":"hi"}'
    with pytest.raises(ValueError):
        webpush.encrypt(b"x" * 4000, p256dh, auth)


def test_vapid_token_is_signed_for_the_push_service():
    vapid = webpush.Vapid.from_private(webpush.Vapid.generate(), "https://tether.example")
    header = vapid.authorization("https://fcm.googleapis.com/fcm/send/abc", now=1_000_000)
    token, k = header.removeprefix("vapid t=").split(", k=")
    assert k == vapid.public_key and len(webpush.unb64(k)) == 65
    head, claims, sig = token.split(".")
    assert json.loads(webpush.unb64(claims)) == {"aud": "https://fcm.googleapis.com", "exp": 1_000_000 + 43200,
                                                 "sub": "https://tether.example"}
    raw = webpush.unb64(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    vapid.key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


# --------------------------------------------------------------------------- reminders from the sources


def test_google_reminders():
    base = {"id": "e", "start": {"dateTime": "2026-11-27T12:00:00Z"}, "end": {"dateTime": "2026-11-27T13:00:00Z"}}
    assert gcal.to_instance({**base, "reminders": {"useDefault": True}}, TZ, "10").reminders == "10"
    assert gcal.to_instance(base, TZ, None).reminders is None
    custom = {"useDefault": False, "overrides": [{"method": "email", "minutes": 1440},
                                                 {"method": "popup", "minutes": 5}, {"method": "popup", "minutes": 60}]}
    assert gcal.to_instance({**base, "reminders": custom}, TZ).reminders == "5,60"
    assert gcal.to_instance({**base, "reminders": {"useDefault": False}}, TZ).reminders == ""
    assert gcal.default_reminders({"defaultReminders": [{"method": "popup", "minutes": 30}]}) == "30"
    assert gcal.default_reminders({"defaultReminders": []}) is None


def test_ics_alarms():
    alarm = "BEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT15M\nEND:VALARM"
    data = feed(vevent("a", "20261118T090000", "20261118T100000", extra="RRULE:FREQ=DAILY;COUNT=2\n" + alarm),
                vevent("b", "20261118T110000", "20261118T120000"))
    got = {(i.uid, i.start): i.reminders for i in ics.parse(data, WINDOW)}
    assert got == {("a", "2026-11-18T14:00:00Z"): "15", ("a", "2026-11-19T14:00:00Z"): "15",
                   ("b", "2026-11-18T16:00:00Z"): None}


# --------------------------------------------------------------------------- what falls due


@pytest.fixture
def source(conn):
    acct = conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'me@x.com', '#000')").lastrowid
    return acct, conn.execute("INSERT INTO calendar_source (account_id, kind, external_ref, name, enabled) "
                              "VALUES (?, 'google', 'primary', 'Me', 1)", (acct,)).lastrowid


def subscribe(conn, endpoint="https://push.example/1", **prefs):
    _, p256dh, auth = device()
    notify.save_subscription(conn, endpoint, p256dh, auth, **prefs)
    conn.execute("UPDATE push_subscription SET created_at = ?", (iso(NOW - timedelta(days=1)),))


def events(conn, source_id, *instances):
    calendar.apply_fetch(conn, source_id, list(instances), window=Window.around(NOW.date(), TZ), now=NOW)
    calendar.assign_hub(conn)


def timed(uid, minutes_from_now, reminders=None, title="Design review"):
    start = NOW + timedelta(minutes=minutes_from_now)
    return Instance(uid, iso(start), iso(start), iso(start + timedelta(hours=1)), False, title, location="Room 4",
                    reminders=reminders)


def due(conn, at):
    return [(m.key.split(":")[-1], m.payload.get("title")) for m in notify.due_reminders(conn, at, TZ)]


def test_reminders_use_the_events_times_or_thirty_minutes(conn, source):
    subscribe(conn)
    events(conn, source[1], timed("default", 30), timed("own", 10, "10"), timed("none", 10, ""),
           timed("later", 45))
    assert sorted(due(conn, NOW)) == [("10", "Design review"), ("30", "Design review")]
    [m] = [m for m in notify.due_reminders(conn, NOW, TZ) if m.key.endswith(":30")]
    assert m.payload["body"] == "In 30 min · 10:30 – 11:30 AM · Room 4"
    assert m.payload["url"].startswith("/calendar?date=2026-11-27&event=")
    assert due(conn, NOW + timedelta(minutes=16)) == [("30", "Design review")]   # "later": 45 - 30 = 15 min on
    assert due(conn, NOW + timedelta(minutes=40)) == []                          # past the grace


def test_all_day_reminders_only_when_the_event_has_one(conn, source):
    subscribe(conn)
    events(conn, source[1], Instance("bday", "2026-11-28", "2026-11-28", "2026-11-29", True, "Birthday",
                                     reminders="900"),
           Instance("holiday", "2026-11-28", "2026-11-28", "2026-11-29", True, "Holiday"))
    nine_am = datetime(2026, 11, 27, 9, 0, tzinfo=TZ)   # 15 hours before midnight
    [m] = notify.due_reminders(conn, nine_am, TZ)
    assert m.payload["title"] == "Birthday" and m.payload["body"] == "Tomorrow · All day"


def test_no_reminders_without_a_device_that_wants_them(conn, source):
    subscribe(conn, events=False)
    events(conn, source[1], timed("a", 30))
    assert notify.due_reminders(conn, NOW, TZ) == []


def mail_item(conn, acct, ext, minutes_ago, **fields):
    return items.upsert_item(conn, account_id=acct, external_id=ext, kind="email", fields={
        "title": f"Subject {ext}", "sender": f"Sender {ext}", "last_activity_at": iso(NOW - timedelta(minutes=minutes_ago)),
        "last_direction": "in", **fields}).id


def test_new_mail(conn, source):
    acct = source[0]
    subscribe(conn)
    new = mail_item(conn, acct, "new", 2)
    mail_item(conn, acct, "old", 120)
    mail_item(conn, acct, "sent", 1, last_direction="out")
    archived = mail_item(conn, acct, "done", 1)
    items.set_state(conn, archived, "archived")
    [m] = notify.due_mail(conn, NOW)
    assert m.payload["title"] == "Sender new" and m.payload["body"] == "Subject new\nme@x.com"
    assert m.payload["url"] == f"/inbox?item={new}"
    for i in range(5):
        mail_item(conn, acct, f"burst{i}", 1)
    first, *rest = notify.due_mail(conn, NOW)
    assert first.payload["title"] == "6 new emails" and len(rest) == 6 and not any(r.payload for r in rest)


async def test_notifier_sends_once_and_drops_dead_devices(conn, source):
    sent = []

    def handler(request: httpx.Request):
        sent.append(str(request.url))
        assert request.headers["content-encoding"] == "aes128gcm"
        assert request.headers["authorization"].startswith("vapid t=")
        return httpx.Response(410 if "gone" in str(request.url) else 201)

    s = Settings(encryption_key=generate_key(), session_secret="s" * 32, google_client_id="c",
                 google_client_secret="x", owner_email="o@x.com", timezone="America/New_York",
                 vapid_private_key=webpush.Vapid.generate(), base_url="https://tether.example", _env_file=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        notifier = notify.Notifier(conn, http, s)
        subscribe(conn, "https://push.example/live")
        subscribe(conn, "https://push.example/gone", mail=False)
        events(conn, source[1], timed("a", 30))
        mail_item(conn, source[0], "m", 1)
        assert await notifier.tick(NOW) == {"mail": 1, "events": 1}
        assert sorted(sent) == ["https://push.example/gone", "https://push.example/live",
                                "https://push.example/live"]
        assert [r["endpoint"] for r in conn.execute("SELECT endpoint FROM push_subscription")] == \
            ["https://push.example/live"]
        assert await notifier.tick(NOW + timedelta(seconds=20)) == {"mail": 0, "events": 0}


def test_subscription_api(client, monkeypatch):
    login(client, monkeypatch)
    assert client.get("/api/push").json()["key"] is None   # no VAPID key in the test settings
    _, p256dh, auth = device()
    sub = {"endpoint": "https://push.example/x", "keys": {"p256dh": p256dh, "auth": auth}}
    assert client.post("/api/push/subscription", json=sub).json() == {"device": {"mail": True, "events": True}}
    assert client.post("/api/push/subscription", json={**sub, "mail": False}).json()["device"]["mail"] is False
    renewed = {**sub, "endpoint": "https://push.example/y", "replaces": "https://push.example/x"}
    assert client.post("/api/push/subscription", json=renewed).json()["device"] == {"mail": False, "events": True}
    assert client.post("/api/push/device", json={"endpoint": "https://push.example/x"}).json() == {"device": None}
    assert client.post("/api/push/unsubscribe", json={"endpoint": "https://push.example/y"}).status_code == 204
    assert client.post("/api/push/device", json={"endpoint": "https://push.example/y"}).json() == {"device": None}
    bad = {**sub, "endpoint": "http://push.example/z"}
    assert client.post("/api/push/subscription", json=bad).status_code == 422
