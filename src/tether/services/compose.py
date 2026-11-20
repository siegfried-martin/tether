"""Drafting and sending mail from a connected Gmail account (spec: "Mail write tools").

Guardrails:
- create_draft only stores a draft in Gmail; nothing is sent.
- send_draft sends one draft exactly as stored: only its id goes to Gmail, so nothing can be
  changed at send time. Only drafts made through create_draft can be sent or deleted from Tether,
  never ones the owner wrote in Gmail.
- No bulk sending: one draft per call, at most MAX_RECIPIENTS a message and MAX_SENDS_PER_HOUR.
- Every send is logged in mail_send_log (account, recipients, subject, time). The app log gets
  ids and counts only. Bodies are never stored or logged.
- Replies thread in every mailbox: threadId for the sender's Gmail, In-Reply-To and References
  for everyone else, and the thread's subject kept.
"""

import base64
import json
import logging
import re
import sqlite3
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from urllib.parse import quote

import httpx

from tether.connectors.gmail import RateLimited, thread_url
from tether.connectors.google_oauth import AuthError
from tether.db import parse_iso, tx, utcnow
from tether.services.mail import MailError, _account, message_body
from tether.services.triage import GmailFactory

log = logging.getLogger(__name__)

MAX_RECIPIENTS = 20
MAX_SENDS_PER_HOUR = 10
MAX_BODY = 100_000
ADDRESS = re.compile(r"^[^@\s<>(),;:\"\[\]]+@[^@\s<>(),;:\"\[\]]+\.[^@\s<>(),;:\"\[\]]+$")
REPLY_PREFIX = re.compile(r"^\s*((re|fwd?|aw|sv)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
PARENT_HEADERS = ["Message-ID", "Message-Id", "References", "Subject"]


# --------------------------------------------------------------------------- create


async def create_draft(conn: sqlite3.Connection, gmail: GmailFactory, account: str, *, to: list[str],
                       body: str, subject: str | None = None, cc: list[str] | None = None,
                       bcc: list[str] | None = None, in_reply_to_message_id: str | None = None,
                       html_body: str | None = None) -> dict:
    """Store a draft in the account's Gmail Drafts and return it as Gmail stored it. Sends nothing."""
    acct = _account(conn, account)
    to_l, cc_l, bcc_l = _addresses(to, "to"), _addresses(cc or [], "cc"), _addresses(bcc or [], "bcc")
    if not to_l:
        raise MailError("a draft needs at least one To address", status=422)
    if len(to_l) + len(cc_l) + len(bcc_l) > MAX_RECIPIENTS:
        raise MailError(f"at most {MAX_RECIPIENTS} recipients a message", status=422)
    for name, text in (("body", body), ("html_body", html_body or "")):
        if len(text) > MAX_BODY:
            raise MailError(f"{name} is over {MAX_BODY} characters", status=422)
    if not body.strip():
        raise MailError("a draft needs a body", status=422)
    api = gmail(acct["id"])

    msg = EmailMessage()
    thread_id = None
    if in_reply_to_message_id:
        parent = await _call(acct, api.get_message_meta(in_reply_to_message_id, PARENT_HEADERS))
        if parent is None:
            raise MailError(f"no message {in_reply_to_message_id} in {acct['label']}; pass a message id "
                            "from get_thread", status=404)
        headers = {h["name"].lower(): h["value"] for h in parent.get("payload", {}).get("headers", [])}
        rfc_id = headers.get("message-id", "").strip()
        if not rfc_id:
            raise MailError("that message has no Message-ID, so a reply to it can't be threaded", status=422)
        original = headers.get("subject", "")
        if subject is None or not subject.strip():
            subject = original if REPLY_PREFIX.match(original) else f"Re: {original}".strip()
        elif _bare_subject(subject) != _bare_subject(original):
            raise MailError(f"a reply keeps the thread's subject ({original!r}) so it stays in the thread; "
                            "leave subject out to use it", status=422)
        references = _unique(headers.get("references", "").split() + [rfc_id])
        msg["In-Reply-To"] = rfc_id
        msg["References"] = " ".join(references[-30:])   # the root, and the latest replies
        thread_id = parent["threadId"]
    elif not subject or not subject.strip():
        raise MailError("a new message needs a subject", status=422)
    if "\n" in subject or "\r" in subject:
        raise MailError("the subject must be one line", status=422)

    msg["To"] = ", ".join(to_l)
    if cc_l:
        msg["Cc"] = ", ".join(cc_l)
    if bcc_l:
        msg["Bcc"] = ", ".join(bcc_l)   # kept in the draft; Gmail strips it from what recipients get
    msg["Subject"] = subject.strip()
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    created = await _call(acct, api.create_draft(raw, thread_id))
    draft_id = created["id"]
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO mail_draft (account_id, draft_id, thread_id, created_at) "
                     "VALUES (?, ?, ?, ?)", (acct["id"], draft_id, created["message"].get("threadId"), utcnow()))
    log.info("account %s: draft %s created (%d recipients, reply: %s)", acct["id"], draft_id,
             len(to_l) + len(cc_l) + len(bcc_l), bool(thread_id))
    return await get_draft(conn, gmail, account, draft_id)


# --------------------------------------------------------------------------- read / delete


async def get_draft(conn: sqlite3.Connection, gmail: GmailFactory, account: str, draft_id: str) -> dict:
    """A draft as Gmail has it now: recipients, subject, body and threading headers. Fetched live."""
    acct = _account(conn, account)
    draft = await _call(acct, gmail(acct["id"]).get_draft(draft_id))
    if draft is None:
        raise MailError(f"no draft {draft_id} in {acct['label']} (sent or deleted?)", status=404)
    return _view(conn, acct, draft)


async def delete_draft(conn: sqlite3.Connection, gmail: GmailFactory, account: str, draft_id: str) -> dict:
    """Delete a draft made with create_draft. Drafts the owner wrote in Gmail are left alone."""
    acct = _account(conn, account)
    _own_draft(conn, acct, draft_id)
    deleted = await _call(acct, gmail(acct["id"]).delete_draft(draft_id))
    with tx(conn):
        conn.execute("DELETE FROM mail_draft WHERE account_id = ? AND draft_id = ?", (acct["id"], draft_id))
    if not deleted:
        raise MailError(f"no draft {draft_id} in {acct['label']} (sent or deleted already?)", status=404)
    log.info("account %s: draft %s deleted", acct["id"], draft_id)
    return {"account": acct["label"], "draft_id": draft_id, "deleted": True}


# --------------------------------------------------------------------------- send


async def send_draft(conn: sqlite3.Connection, gmail: GmailFactory, account: str, draft_id: str) -> dict:
    """Send one draft made with create_draft, exactly as stored, and log the send."""
    acct = _account(conn, account)
    _own_draft(conn, acct, draft_id)
    since = (parse_iso(utcnow()) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = conn.execute("SELECT COUNT(*) FROM mail_send_log WHERE sent_at > ?", (since,)).fetchone()[0]
    if recent >= MAX_SENDS_PER_HOUR:
        raise MailError(f"Tether sends at most {MAX_SENDS_PER_HOUR} messages an hour; try again later",
                        status=429)
    api = gmail(acct["id"])
    draft = await _call(acct, api.get_draft(draft_id))
    if draft is None:
        with tx(conn):
            conn.execute("DELETE FROM mail_draft WHERE account_id = ? AND draft_id = ?", (acct["id"], draft_id))
        raise MailError(f"no draft {draft_id} in {acct['label']} (sent or deleted in Gmail?)", status=404)
    before = _view(conn, acct, draft)
    count = len(before["to"]) + len(before["cc"]) + len(before["bcc"])
    if count == 0 or count > MAX_RECIPIENTS:
        raise MailError(f"the draft has {count} recipients; a message needs 1 to {MAX_RECIPIENTS}", status=422)

    sent = await _call(acct, api.send_draft(draft_id))
    if sent is None:
        raise MailError(f"no draft {draft_id} in {acct['label']} (sent or deleted in Gmail?)", status=404)
    # Log what Gmail actually sent: the Sent copy's headers (Bcc included), else the draft as read.
    as_sent = before
    try:
        meta = await api.get_message_meta(sent["id"], ["To", "Cc", "Bcc", "Subject"])
        if meta:
            as_sent = _headers_view(meta.get("payload", {}).get("headers", []))
    except (httpx.HTTPError, AuthError, RateLimited) as exc:   # the send stands; the draft as read is a faithful fallback
        log.warning("account %s: couldn't read back sent message: %s", acct["id"], type(exc).__name__)
    sent_at = utcnow()
    with tx(conn):
        conn.execute(
            """INSERT INTO mail_send_log (account_id, account, draft_id, message_id, thread_id, to_addrs,
                                          cc_addrs, bcc_addrs, subject, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (acct["id"], acct["label"], draft_id, sent["id"], sent.get("threadId"), json.dumps(as_sent["to"]),
             json.dumps(as_sent["cc"]), json.dumps(as_sent["bcc"]), as_sent["subject"], sent_at))
        conn.execute("DELETE FROM mail_draft WHERE account_id = ? AND draft_id = ?", (acct["id"], draft_id))
    log.info("account %s: sent draft %s as message %s (%d recipients)", acct["id"], draft_id, sent["id"], count)
    return {
        "account": acct["label"],
        "message_id": sent["id"],
        "thread_id": sent.get("threadId"),
        "in_sent": "SENT" in sent.get("labelIds", []),
        "to": as_sent["to"], "cc": as_sent["cc"], "bcc": as_sent["bcc"],
        "subject": as_sent["subject"],
        "sent_at": sent_at,
        "url": thread_url(acct["label"], sent.get("threadId", "")),
    }


def send_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM mail_send_log ORDER BY sent_at DESC, id DESC LIMIT ?", (limit,))
    return [{**dict(r), **{k: json.loads(r[k]) for k in ("to_addrs", "cc_addrs", "bcc_addrs")}} for r in rows]


# --------------------------------------------------------------------------- helpers


def _addresses(values: list[str], field: str) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if any("\n" in v or "\r" in v for v in values):
        raise MailError(f"{field}: addresses must be on one line", status=422)
    out = []
    for name, addr in getaddresses(values):
        if not addr and not name:
            continue
        if not ADDRESS.match(addr):
            raise MailError(f"{field}: {addr or name!r} isn't an email address", status=422)
        out.append(formataddr((name, addr)))
    return _unique(out)


def _headers_view(headers: list[dict]) -> dict:
    h = {x["name"].lower(): x["value"] for x in headers}
    def addrs(key: str) -> list[str]:
        return [formataddr(pair) for pair in getaddresses([h[key]]) if pair[1]] if h.get(key) else []
    return {"from": h.get("from", ""), "to": addrs("to"), "cc": addrs("cc"), "bcc": addrs("bcc"),
            "subject": h.get("subject", ""), "in_reply_to": h.get("in-reply-to"),
            "references": h.get("references")}


def _view(conn: sqlite3.Connection, acct: sqlite3.Row, draft: dict) -> dict:
    message = draft.get("message", {})
    payload = message.get("payload", {})
    mine = conn.execute("SELECT 1 FROM mail_draft WHERE account_id = ? AND draft_id = ?",
                        (acct["id"], draft["id"])).fetchone() is not None
    return {
        "account": acct["label"],
        "draft_id": draft["id"],
        "message_id": message.get("id"),
        "thread_id": message.get("threadId"),
        **_headers_view(payload.get("headers", [])),
        "body": message_body(payload),
        "has_html": any(p.get("mimeType") == "text/html" for p in _parts(payload)),
        "made_by_tether": mine,
        "url": f"https://mail.google.com/mail/?authuser={quote(acct['label'])}#drafts?compose={message.get('id', '')}",
    }


def _parts(payload: dict):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _parts(part)


def _own_draft(conn: sqlite3.Connection, acct: sqlite3.Row, draft_id: str) -> None:
    if conn.execute("SELECT 1 FROM mail_draft WHERE account_id = ? AND draft_id = ?",
                    (acct["id"], draft_id)).fetchone() is None:
        raise MailError(f"draft {draft_id} wasn't made with create_draft in {acct['label']}; Tether only "
                        "sends or deletes its own drafts", status=403)


def _bare_subject(subject: str) -> str:
    return " ".join(REPLY_PREFIX.sub("", subject).split()).lower()


def _unique(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        if v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


async def _call(acct: sqlite3.Row, awaitable):
    try:
        return await awaitable
    except AuthError as exc:
        raise MailError(f"{acct['label']}: sign-in expired, revoked or missing permission. "
                        "Reconnect it in Settings.") from exc
    except RateLimited as exc:
        raise MailError(f"{acct['label']}: Gmail is rate-limiting this account. Try again in a minute.") from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            try:
                reason = exc.response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                reason = "bad request"
            raise MailError(f"{acct['label']}: Gmail refused it: {reason}", status=422) from exc
        log.warning("account %s: Gmail write failed: HTTP %s", acct["id"], exc.response.status_code)
        raise MailError(f"{acct['label']}: Gmail failed (HTTP {exc.response.status_code}).") from exc
    except httpx.HTTPError as exc:
        log.warning("account %s: Gmail write failed: %s", acct["id"], type(exc).__name__)
        raise MailError(f"{acct['label']}: couldn't reach Gmail ({type(exc).__name__}).") from exc
