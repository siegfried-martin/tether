"""search_mail, get_thread and thread-linked tasks."""

import base64

import pytest
from conftest import FakeGmail, msg

from tether.services import accounts, items, mail


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def full_msg(mid, tid, *, labels, plain=None, html=None, attachment=None, minutes_ago=10, subject="Lunch"):
    m = msg(mid, tid, labels=labels, subject=subject, minutes_ago=minutes_ago)
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "filename": "", "body": {"data": b64(plain)},
                      "headers": [{"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'}]})
    if html is not None:
        parts.append({"mimeType": "text/html", "filename": "", "body": {"data": b64(html)}})
    if attachment:
        parts.append({"mimeType": "application/pdf", "filename": attachment,
                      "body": {"attachmentId": "a1", "size": 12345}})
    m["payload"] = {**m["payload"], "mimeType": "multipart/mixed", "parts": parts}
    return m


@pytest.fixture
def mailboxes(conn):
    """Two accounts, each with its own fake Gmail."""
    fakes = {}
    for label in ("home@x.com", "work@x.com"):
        account_id = conn.execute(
            "INSERT INTO account (kind, label, color, excluded_categories) VALUES ('gmail', ?, '#000', ?)",
            (label, "promotions,social,forums")).lastrowid
        fakes[account_id] = FakeGmail()
    return fakes


def gmail_for(mailboxes):
    return lambda account_id: mailboxes[account_id]


async def test_search_merges_accounts_newest_first(conn, mailboxes):
    home, work = mailboxes.values()
    home.put("h1", msg("m1", "h1", labels=["INBOX"], subject="Lunch Friday", minutes_ago=30))
    work.put("w1", msg("m2", "w1", labels=[], subject="Lunch order", minutes_ago=5))
    work.put("w2", msg("m3", "w2", labels=["INBOX"], subject="Budget", minutes_ago=1))
    tracked = items.upsert_item(conn, account_id=1, external_id="h1", kind="email", fields={
        "last_activity_at": "2026-11-15T10:00:00Z", "last_direction": "in"}).id
    out = await mail.search_mail(conn, gmail_for(mailboxes), "lunch")
    assert [(r["account"], r["thread_id"]) for r in out["results"]] == [("work@x.com", "w1"), ("home@x.com", "h1")]
    assert out["results"][0]["in_inbox"] is False and out["results"][1]["in_inbox"] is True
    assert out["results"][1]["item_id"] == tracked and out["results"][0]["item_id"] is None
    assert out["errors"] == []
    only = await mail.search_mail(conn, gmail_for(mailboxes), "*", account="work@x.com")
    assert {r["thread_id"] for r in only["results"]} == {"w1", "w2"}


async def test_search_reports_failing_account_and_returns_others(conn, mailboxes):
    home, work = mailboxes.values()
    home.put("h1", msg("m1", "h1", labels=["INBOX"], subject="Lunch"))

    async def broken(query, limit):
        raise RuntimeError("boom")
    work.search_thread_ids = broken
    out = await mail.search_mail(conn, gmail_for(mailboxes), "lunch")
    assert [r["thread_id"] for r in out["results"]] == ["h1"]
    assert out["errors"] == [{"account": "work@x.com", "error": "work@x.com: couldn't reach Gmail (RuntimeError)."}]
    bad = await mail.search_mail(conn, gmail_for(mailboxes), "bad:query", account="home@x.com")
    assert "rejected the search query" in bad["errors"][0]["error"]


async def test_search_skips_accounts_needing_reconnect(conn, mailboxes):
    accounts.set_status(conn, 2, "auth_error")
    mailboxes[1].put("h1", msg("m1", "h1", labels=["INBOX"], subject="Lunch"))
    out = await mail.search_mail(conn, gmail_for(mailboxes), "*")
    assert [r["account"] for r in out["results"]] == ["home@x.com"]


async def test_unknown_account(conn, mailboxes):
    with pytest.raises(mail.MailError, match="accounts: home@x.com, work@x.com"):
        await mail.search_mail(conn, gmail_for(mailboxes), "*", account="nope@x.com")


async def test_get_thread_text(conn, mailboxes):
    home = mailboxes[1]
    home.put("t", full_msg("m1", "t", labels=["INBOX"], plain="Hi,\r\nlunch at noon?", attachment="menu.pdf",
                           minutes_ago=20),
             full_msg("m2", "t", labels=["SENT"], html="<p>Sure &amp; thanks</p><ul><li>noon</li></ul>"
                      "<style>p{}</style>", minutes_ago=10),
             full_msg("d", "t", labels=["DRAFT"], plain="unsent"))
    out = await mail.get_thread(conn, gmail_for(mailboxes), "home@x.com", "t")
    assert out["subject"] == "Lunch" and out["item_id"] is None
    first, second = out["messages"]
    assert first["body"] == "Hi,\nlunch at noon?" and not first["sent_by_me"]
    assert first["attachments"] == [{"name": "menu.pdf", "size": 12345, "type": "application/pdf"}]
    assert second["body"] == "Sure & thanks\n\n- noon" and second["sent_by_me"]
    assert len(out["messages"]) == 2, "drafts are left out"


async def test_get_thread_caps_body(conn, mailboxes):
    mailboxes[1].put("t", full_msg("m1", "t", labels=["INBOX"], plain="x" * 50))
    [m] = (await mail.get_thread(conn, gmail_for(mailboxes), "home@x.com", "t", body_cap=10))["messages"]
    assert m["body"] == "x" * 10 and m["truncated"]


async def test_get_thread_missing(conn, mailboxes):
    with pytest.raises(mail.MailError) as err:
        await mail.get_thread(conn, gmail_for(mailboxes), "home@x.com", "nope")
    assert err.value.status == 404


async def test_ensure_item_mirrors_gmail_state(conn, mailboxes):
    home = mailboxes[1]
    home.put("old", msg("m1", "old", labels=["CATEGORY_PERSONAL"], subject="Old thread"))
    item_id = await mail.ensure_item(conn, gmail_for(mailboxes), "home@x.com", "old")
    item = items.get_by_id(conn, item_id)
    assert item["state"] == "archived" and item["title"] == "Old thread"
    assert await mail.ensure_item(conn, gmail_for(mailboxes), "home@x.com", "old") == item_id
    with pytest.raises(mail.MailError):
        await mail.ensure_item(conn, gmail_for(mailboxes), "home@x.com", "missing")


def test_html_to_text_skips_scripts_and_collapses_space():
    assert mail.html_to_text("<div>a&nbsp;  b</div><script>x()</script><br>c") == "a b\n\nc"
