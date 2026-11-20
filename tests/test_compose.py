"""Drafting and sending mail: create_draft, get_draft, delete_draft, send_draft."""

import base64
import json
from email import message_from_bytes, policy

import pytest
from conftest import FakeGmail

from tether.services import compose
from tether.services.mail import MailError


def _payload(part) -> dict:
    """A parsed MIME part as Gmail's format=full payload."""
    out = {"mimeType": part.get_content_type(), "filename": part.get_filename() or "",
           "headers": [{"name": k, "value": str(v)} for k, v in part.items()]}
    if part.is_multipart():
        out["parts"] = [_payload(p) for p in part.iter_parts()]
        out["body"] = {"size": 0}
    else:
        data = part.get_payload(decode=True) or b""
        out["body"] = {"data": base64.urlsafe_b64encode(data).decode(), "size": len(data)}
    return out


class DraftingGmail(FakeGmail):
    """FakeGmail plus drafts and sending, storing messages the way Gmail returns them."""

    def __init__(self):
        super().__init__()
        self.drafts: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.raw: dict[str, bytes] = {}
        self.n = 0

    async def get_message_meta(self, message_id, headers):
        for thread in self.threads.values():
            for m in thread["messages"]:
                if m["id"] == message_id:
                    return {**m, "threadId": thread["id"]}
        return next((m for m in self.sent if m["id"] == message_id), None)

    async def create_draft(self, raw, thread_id):
        self.n += 1
        data = base64.urlsafe_b64decode(raw)
        parsed = message_from_bytes(data, policy=policy.default)
        mid, did = f"msg{self.n}", f"r-{self.n}"
        self.raw[did] = data
        message = {"id": mid, "threadId": thread_id or f"new{self.n}", "labelIds": ["DRAFT"],
                   "payload": _payload(parsed)}
        self.drafts[did] = {"id": did, "message": message}
        return {"id": did, "message": {"id": mid, "threadId": message["threadId"], "labelIds": ["DRAFT"]}}

    async def get_draft(self, draft_id):
        return self.drafts.get(draft_id)

    async def delete_draft(self, draft_id):
        return self.drafts.pop(draft_id, None) is not None

    async def send_draft(self, draft_id):
        draft = self.drafts.pop(draft_id, None)
        if draft is None:
            return None
        message = {**draft["message"], "labelIds": ["SENT"]}
        self.sent.append(message)
        return {"id": message["id"], "threadId": message["threadId"], "labelIds": ["SENT"]}


def parent_msg():
    return {"id": "p1", "threadId": "t1", "internalDate": "1", "labelIds": ["INBOX"], "payload": {"headers": [
        {"name": "Subject", "value": "Lunch Friday"},
        {"name": "Message-ID", "value": "<root-2@mail.x>"},
        {"name": "References", "value": "<root-1@mail.x>"},
    ]}}


@pytest.fixture
def box(conn):
    conn.execute("INSERT INTO account (kind, label, color) VALUES ('gmail', 'me@x.com', '#000')")
    fake = DraftingGmail()
    fake.put("t1", parent_msg())
    return fake


def factory(fake):
    return lambda account_id: fake


async def test_new_draft_is_stored_not_sent(conn, box):
    d = await compose.create_draft(conn, factory(box), "me@x.com", to=["Ann Lee <ann@y.com>", "bob@y.com"],
                                   cc=["cat@y.com"], bcc=["me2@x.com"], subject="Plans", body="Hi Ann,\n\nSee you.")
    assert box.sent == [] and list(box.drafts) == [d["draft_id"]]
    assert d["to"] == ["Ann Lee <ann@y.com>", "bob@y.com"] and d["cc"] == ["cat@y.com"] and d["bcc"] == ["me2@x.com"]
    assert d["subject"] == "Plans" and d["body"] == "Hi Ann,\n\nSee you."
    assert d["in_reply_to"] is None and d["made_by_tether"] and not d["has_html"]
    assert "#drafts?compose=msg1" in d["url"]
    assert (await compose.get_draft(conn, factory(box), "me@x.com", d["draft_id"]))["body"] == d["body"]


async def test_reply_threads_everywhere(conn, box):
    d = await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], body="Sounds good",
                                   in_reply_to_message_id="p1")
    assert d["thread_id"] == "t1", "joins the sender's Gmail thread"
    assert d["subject"] == "Re: Lunch Friday"
    assert d["in_reply_to"] == "<root-2@mail.x>"
    assert d["references"] == "<root-1@mail.x> <root-2@mail.x>"
    # A matching subject (any Re:/Fwd: prefixes) is fine; a different one would split the thread.
    ok = await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], body="x",
                                    subject="RE: lunch  friday", in_reply_to_message_id="p1")
    assert ok["thread_id"] == "t1"
    with pytest.raises(MailError, match="keeps the thread's subject"):
        await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], body="x",
                                   subject="Something else", in_reply_to_message_id="p1")
    with pytest.raises(MailError, match="no message"):
        await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], body="x",
                                   in_reply_to_message_id="nope")


async def test_html_is_optional_alternative(conn, box):
    d = await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], subject="S",
                                   body="plain words", html_body="<p>rich <b>words</b></p>")
    assert d["has_html"] and d["body"] == "plain words"


@pytest.mark.parametrize("kwargs, match", [
    ({"to": []}, "at least one"),
    ({"to": ["not an address"]}, "isn't an email address"),
    ({"to": ["ann@y.com\r\nBcc: evil@z.com"]}, "one line"),
    ({"to": [f"a{n}@y.com" for n in range(compose.MAX_RECIPIENTS + 1)]}, "at most"),
    ({"subject": ""}, "needs a subject"),
    ({"subject": "two\nlines"}, "one line"),
    ({"body": "  "}, "needs a body"),
])
async def test_draft_validation(conn, box, kwargs, match):
    args = {"to": ["ann@y.com"], "subject": "S", "body": "b", **kwargs}
    with pytest.raises(MailError, match=match):
        await compose.create_draft(conn, factory(box), "me@x.com", **args)
    assert box.drafts == {}


async def test_send_exactly_the_stored_draft_and_log_it(conn, box):
    d = await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], bcc=["me2@x.com"],
                                   body="Sounds good", in_reply_to_message_id="p1")
    sent = await compose.send_draft(conn, factory(box), "me@x.com", d["draft_id"])
    assert sent["in_sent"] and sent["thread_id"] == "t1" and sent["message_id"] == d["message_id"]
    assert sent["to"] == ["ann@y.com"] and sent["bcc"] == ["me2@x.com"] and sent["subject"] == "Re: Lunch Friday"
    [row] = compose.send_log(conn)
    assert row["account"] == "me@x.com" and row["to_addrs"] == ["ann@y.com"] and row["bcc_addrs"] == ["me2@x.com"]
    assert row["subject"] == "Re: Lunch Friday" and row["thread_id"] == "t1" and row["sent_at"]
    assert "body" not in row and "Sounds good" not in json.dumps(row)
    with pytest.raises(MailError, match="wasn't made with create_draft"):   # gone, and can't send twice
        await compose.send_draft(conn, factory(box), "me@x.com", d["draft_id"])


async def test_only_tethers_own_drafts_can_be_sent_or_deleted(conn, box):
    box.drafts["owner-draft"] = {"id": "owner-draft", "message": {"id": "m9", "threadId": "t9", "payload": {
        "headers": [{"name": "To", "value": "x@y.com"}, {"name": "Subject", "value": "mine"}]}}}
    assert (await compose.get_draft(conn, factory(box), "me@x.com", "owner-draft"))["made_by_tether"] is False
    for action in (compose.send_draft, compose.delete_draft):
        with pytest.raises(MailError, match="only sends or deletes its own drafts") as err:
            await action(conn, factory(box), "me@x.com", "owner-draft")
        assert err.value.status == 403
    assert box.sent == [] and "owner-draft" in box.drafts


async def test_delete_draft(conn, box):
    d = await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], subject="S", body="b")
    assert (await compose.delete_draft(conn, factory(box), "me@x.com", d["draft_id"]))["deleted"]
    assert box.drafts == {}
    with pytest.raises(MailError, match="no draft"):
        await compose.get_draft(conn, factory(box), "me@x.com", d["draft_id"])


async def test_draft_deleted_in_gmail_is_not_sent(conn, box):
    d = await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], subject="S", body="b")
    box.drafts.clear()
    with pytest.raises(MailError, match="no draft") as err:
        await compose.send_draft(conn, factory(box), "me@x.com", d["draft_id"])
    assert err.value.status == 404 and box.sent == [] and compose.send_log(conn) == []


async def test_hourly_send_cap(conn, box):
    for _ in range(compose.MAX_SENDS_PER_HOUR):
        d = await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], subject="S", body="b")
        await compose.send_draft(conn, factory(box), "me@x.com", d["draft_id"])
    d = await compose.create_draft(conn, factory(box), "me@x.com", to=["ann@y.com"], subject="S", body="b")
    with pytest.raises(MailError, match="an hour") as err:
        await compose.send_draft(conn, factory(box), "me@x.com", d["draft_id"])
    assert err.value.status == 429 and len(box.sent) == compose.MAX_SENDS_PER_HOUR


async def test_unknown_account(conn, box):
    with pytest.raises(MailError, match="no Gmail account"):
        await compose.create_draft(conn, factory(box), "who@x.com", to=["ann@y.com"], subject="S", body="b")
