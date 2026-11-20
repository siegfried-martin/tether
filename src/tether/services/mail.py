"""Mail search and reading across every Gmail account (spec: "Mail search and reading").

Bodies are fetched live for the caller and never stored or logged. Search
results and thread text pass straight through to the response.
"""

import asyncio
import base64
import logging
import re
import sqlite3
from datetime import UTC, datetime
from html.parser import HTMLParser

import httpx

from tether.connectors.gmail import RateLimited, derive_thread
from tether.connectors.google_oauth import AuthError
from tether.db import iso, tx
from tether.services import accounts, items
from tether.services.triage import GmailFactory

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
BODY_CAP = 20_000


class MailError(Exception):
    """A mail call failed. The message is safe to show the user."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- search


async def search_mail(conn: sqlite3.Connection, gmail: GmailFactory, query: str, *,
                      account: str | None = None, limit: int = DEFAULT_LIMIT) -> dict:
    """Run a Gmail search on one account, or all of them concurrently.

    The query is Gmail syntax, passed as written. Results merge newest first.
    An account that fails is reported in `errors`; the others still return.
    """
    limit = min(max(int(limit), 1), MAX_LIMIT)
    targets = [_account(conn, account)] if account else accounts.list_accounts(conn, kind="gmail")
    targets = [a for a in targets if a["status"] == "ok"] if not account else targets
    outcomes = await asyncio.gather(*(_search_one(conn, gmail, a, query, limit) for a in targets),
                                    return_exceptions=True)
    results, errors = [], []
    for acct, outcome in zip(targets, outcomes):
        if isinstance(outcome, BaseException):
            errors.append({"account": acct["label"], "error": _describe(acct, outcome)})
        else:
            results += outcome
    results.sort(key=lambda r: r["date"], reverse=True)
    return {"results": results, "errors": errors}


async def _search_one(conn, gmail: GmailFactory, acct: sqlite3.Row, query: str, limit: int) -> list[dict]:
    api = gmail(acct["id"])
    out = []
    for thread_id in await api.search_thread_ids(query, limit):
        thread = await api.get_thread(thread_id)
        view = derive_thread(thread, excluded=frozenset(), email=acct["label"]) if thread else None
        if view is None:
            continue
        item = items.get_item(conn, acct["id"], thread_id)
        out.append({
            "account": acct["label"],
            "thread_id": thread_id,
            "subject": view.fields["title"],
            "sender": view.fields["sender"],
            "date": view.fields["last_activity_at"],
            "snippet": view.fields["snippet"],
            "in_inbox": any("INBOX" in m.get("labelIds", []) for m in thread.get("messages", [])),
            "item_id": item["id"] if item else None,
            "url": view.fields["url"],
        })
    return out


# --------------------------------------------------------------------------- read


async def get_thread(conn: sqlite3.Connection, gmail: GmailFactory, account: str, thread_id: str,
                     *, body_cap: int = BODY_CAP) -> dict:
    """A thread's messages as plain text, fetched live. Nothing here is stored."""
    acct = _account(conn, account)
    try:
        thread = await gmail(acct["id"]).get_thread_full(thread_id)
    except Exception as exc:
        raise MailError(_describe(acct, exc)) from exc
    if thread is None:
        raise MailError(f"no thread {thread_id} in {acct['label']}", status=404)
    item = items.get_item(conn, acct["id"], thread_id)
    return {
        "account": acct["label"],
        "thread_id": thread_id,
        "item_id": item["id"] if item else None,
        **thread_text(thread, body_cap=body_cap),
    }


async def get_item_thread(conn: sqlite3.Connection, gmail: GmailFactory, item_id: int) -> dict:
    """The thread behind an email item, for reading it inside Tether."""
    item = items.get_by_id(conn, item_id)
    if item is None:
        raise MailError(f"no item {item_id}", status=404)
    acct = accounts.get_account(conn, item["account_id"])
    if item["kind"] != "email" or acct is None or acct["kind"] != "gmail":
        raise MailError("only Gmail threads can be read in Tether", status=422)
    return await get_thread(conn, gmail, acct["label"], item["external_id"])


def thread_text(thread: dict, *, body_cap: int = BODY_CAP) -> dict:
    messages = [m for m in thread.get("messages", []) if "DRAFT" not in m.get("labelIds", [])]
    messages.sort(key=lambda m: int(m.get("internalDate", 0)))
    out = []
    for m in messages:
        payload = m.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        body = message_body(payload)
        truncated = len(body) > body_cap
        out.append({
            "id": m.get("id"),   # for create_draft's in_reply_to_message_id
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "date": iso(datetime.fromtimestamp(int(m.get("internalDate", 0)) / 1000, UTC)),
            "sent_by_me": "SENT" in m.get("labelIds", []),
            "body": body[:body_cap],
            "truncated": truncated,
            "attachments": attachments(payload),
        })
    subject = next((h["value"] for h in (messages[0].get("payload", {}).get("headers", []) if messages else [])
                    if h["name"].lower() == "subject"), "")
    return {"subject": subject or "(no subject)", "messages": out}


def _parts(payload: dict):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _parts(part)


def _decode(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    content_type = next((h["value"] for h in part.get("headers", []) if h["name"].lower() == "content-type"), "")
    match = re.search(r'charset="?([\w.-]+)', content_type, re.I)
    try:
        return raw.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def message_body(payload: dict) -> str:
    """Plain text of a message: its text/plain parts, else its HTML converted to text."""
    inline = [p for p in _parts(payload) if not p.get("filename")]
    plain = [_decode(p) for p in inline if p.get("mimeType") == "text/plain"]
    if any(t.strip() for t in plain):
        text = "\n\n".join(t for t in plain if t.strip())
    else:
        text = "\n\n".join(html_to_text(_decode(p)) for p in inline if p.get("mimeType") == "text/html")
    text = text.replace("\r\n", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def attachments(payload: dict) -> list[dict]:
    return [{"name": p["filename"], "size": p.get("body", {}).get("size", 0), "type": p.get("mimeType", "")}
            for p in _parts(payload) if p.get("filename")]


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "hr"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.chunks.append("\n")
        if tag == "li":
            self.chunks.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in "".join(parser.chunks).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --------------------------------------------------------------------------- thread -> item


async def ensure_item(conn: sqlite3.Connection, gmail: GmailFactory, account: str, thread_id: str) -> int:
    """The item for a Gmail thread, creating it (state mirrored from Gmail) if needed.

    This puts a thread found by search under sync, so a task can link to it:
    a reply reopens it and completing the task archives it.
    """
    acct = _account(conn, account)
    existing = items.get_item(conn, acct["id"], thread_id)
    if existing:
        return existing["id"]
    try:
        thread = await gmail(acct["id"]).get_thread(thread_id)
    except Exception as exc:
        raise MailError(_describe(acct, exc)) from exc
    view = derive_thread(thread, excluded=accounts.excluded_categories(acct), email=acct["label"]) if thread else None
    if view is None:
        raise MailError(f"no thread {thread_id} in {acct['label']}", status=404)
    with tx(conn):
        result = items.upsert_item(conn, account_id=acct["id"], external_id=thread_id, kind="email",
                                   fields=view.fields, state="open" if view.is_in else "archived")
    return result.id


# --------------------------------------------------------------------------- helpers


def _account(conn: sqlite3.Connection, label: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM account WHERE label = ? AND kind = 'gmail'", (label,)).fetchone()
    if row is None:
        known = ", ".join(a["label"] for a in accounts.list_accounts(conn, kind="gmail"))
        raise MailError(f"no Gmail account {label!r}; accounts: {known}", status=404)
    return row


def _describe(acct: sqlite3.Row, exc: BaseException) -> str:
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, AuthError):
        return f"{acct['label']}: sign-in expired or revoked. Reconnect it in Settings."
    if isinstance(exc, RateLimited):
        return f"{acct['label']}: Gmail is rate-limiting this account. Try again in a minute."
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400:
        return f"{acct['label']}: Gmail rejected the search query."
    log.warning("account %s: mail call failed: %s", acct["id"], type(exc).__name__)
    return f"{acct['label']}: couldn't reach Gmail ({type(exc).__name__})."
