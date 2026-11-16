"""The work digest: parse a pasted digest and upsert its items (spec: "Work digest: the ingest_digest contract").

A digest looks like this, one block per email that needs action:

    ITEM
    From: Ann Lee
    Subject: Closing docs for 12 Main St
    Received: 2026-11-14 09:12
    Needed: Sign and return the closing package
    Due: 2026-11-17
    Priority: high
    Link: https://outlook.office.com/mail/...

The parser anchors on labels, not line breaks, so a digest that Teams has
collapsed into one paragraph parses the same. The text is never logged.
"""

import hashlib
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

from tether.db import iso, tx
from tether.services import accounts, items

LABELS = ("From", "Subject", "Received", "Needed", "Due", "Priority", "Link")
REQUIRED = ("From", "Subject", "Needed")
PRIORITIES = ("high", "normal", "low")
NO_ITEMS = re.compile(r"\bNO\s+ACTION\s+ITEMS\b")
# A block starts at the keyword ITEM (upper case, optionally numbered: "ITEM 2", "ITEM #2:").
# Only ITEM exists in v1; a WAITING block would get its own keyword here.
BLOCK = re.compile(r"\bITEM\b(?:\s*#?\d+)?\s*:?")
LINE_BLOCK = re.compile(r"^[ \t]*#*[ \t]*ITEM\b(?:\s*#?\d+)?\s*:?", re.MULTILINE)
FIELD = re.compile(rf"(?<![\w-])({'|'.join(LABELS)})\s*:", re.IGNORECASE)
# The same labels at the start of a line, optionally bulleted.
LINE_FIELD = re.compile(rf"^[ \t]*(?:[-*•][ \t]*)?({'|'.join(LABELS)})[ \t]*:", re.IGNORECASE | re.MULTILINE)
MARKUP = re.compile(r"\*\*|__|`")
MD_LINK = re.compile(r"^\[[^\]]*\]\((\S+)\)$")
DUE_FILLER = re.compile(r"^(?:by|before|due|eod|cob|end of day|close of business)\b[\s,:]*", re.IGNORECASE)
SHOWN_BLOCK = 500


class DigestError(Exception):
    """Bad input to ingest_digest. The message is safe to show the user."""

    status = 422


def ingest_digest(conn: sqlite3.Connection, raw_text: str, *, tz: ZoneInfo, today: date,
                  digest_date: date | str | None = None, label: str = "Work") -> dict:
    """Parse a digest and upsert one digest item per valid block.

    Returns created and duplicate counts, the rejected blocks with a reason
    each, and warnings for fields that were kept but could not be read.
    """
    when = _digest_date(digest_date, today)
    result: dict = {"created": 0, "duplicates": 0, "rejected": [], "warnings": [], "item_ids": []}
    text = MARKUP.sub("", raw_text or "")
    # A digest that kept its line breaks counts only ITEM at a line start, so "LINE ITEM REVIEW"
    # in a subject is not a block. A collapsed digest counts ITEM anywhere.
    kept_lines = len(LINE_FIELD.findall(text)) >= 3
    starts = list((LINE_BLOCK if kept_lines else BLOCK).finditer(text))
    if not starts:
        if not NO_ITEMS.search(text):
            result["rejected"].append({"block": _shown(text), "reason": "No ITEM blocks found."})
        return result

    parsed = []
    for n, start in enumerate(starts):
        end = starts[n + 1].start() if n + 1 < len(starts) else len(text)
        block = text[start.end():end]
        fields = parse_fields(block)
        missing = [name for name in REQUIRED if not fields.get(name)]
        if missing:
            result["rejected"].append({"block": _shown("ITEM " + block),
                                       "reason": f"Missing {', '.join(missing)}."})
            continue
        parsed.append(_to_item(fields, tz=tz, when=when, warnings=result["warnings"]))

    if not parsed:
        return result
    with tx(conn):
        account_id = digest_account(conn, label)
        seen = set()
        for external_id, values in parsed:
            if external_id in seen:
                result["duplicates"] += 1
                continue
            seen.add(external_id)
            # state=None: an archived digest item stays archived when the ask reappears.
            upsert = items.upsert_item(conn, account_id=account_id, external_id=external_id, kind="digest",
                                       fields=values)
            result["created" if upsert.created else "duplicates"] += 1
            result["item_ids"].append(upsert.id)
    return result


def parse_fields(block: str) -> dict[str, str]:
    """Capture each label's value up to the next label.

    When the block keeps its line breaks and at least three labels start a
    line, only line-start labels count, so "Fwd: Due: invoice" in a subject
    stays in the subject. A collapsed block falls back to labels anywhere.
    A label that repeats inside a block is kept as text of the field before
    it, so the first occurrence wins.
    """
    fields: dict[str, str] = {}
    block = block.lstrip()  # "ITEM From: ..." puts the first label at the start
    matches = list(LINE_FIELD.finditer(block))
    if len(matches) < 3:
        matches = list(FIELD.finditer(block))
    last = None
    for n, m in enumerate(matches):
        end = matches[n + 1].start() if n + 1 < len(matches) else len(block)
        name = next(label for label in LABELS if label.lower() == m.group(1).lower())
        value = _clean(block[m.end():end])
        if name in fields and last:
            fields[last] = f"{fields[last]} {m.group(0)} {value}".strip()
            continue
        fields[name] = value
        last = name
    return fields


def _to_item(fields: dict[str, str], *, tz: ZoneInfo, when: date, warnings: list) -> tuple[str, dict]:
    sender, subject = fields["From"], fields["Subject"]
    received = parse_received(fields.get("Received", ""), tz=tz, when=when)
    if received is None:
        stamp = when.isoformat()
        activity = iso(datetime.combine(when, time(0), tz))
        if fields.get("Received"):
            warnings.append({"block": subject, "reason": f"Couldn't read Received ({fields['Received']}); "
                                                          f"used the digest date {stamp}."})
    else:
        stamp = activity = iso(received)

    needed = fields["Needed"]
    due_raw = fields.get("Due", "")
    due = parse_due(due_raw, when=when)
    if due is None and due_raw and due_raw.lower() not in ("none", "n/a", "-"):
        # Keep what the digest said rather than lose it.
        needed = f"{needed} (Due: {due_raw})"
        warnings.append({"block": subject, "reason": f"Couldn't read Due ({due_raw}); kept it in Needed."})

    priority = fields.get("Priority", "").lower()
    link = _link(fields.get("Link", ""))
    values = {
        "sender": sender,
        "title": subject,
        "needed": needed,
        "snippet": needed,
        "last_activity_at": activity,
        "last_direction": "in",
        "due_at": due.isoformat() if due else None,
        "priority": priority if priority in PRIORITIES else "normal",
        "url": link,
    }
    return external_id(sender, subject, stamp), values


def external_id(sender: str, subject: str, received: str) -> str:
    """SHA-256 of lowercased From and Subject and the parsed Received (UTC ISO), or the digest date."""
    key = "\n".join((_norm(sender), _norm(subject), received))
    return hashlib.sha256(key.encode()).hexdigest()


def parse_received(raw: str, *, tz: ZoneInfo, when: date) -> datetime | None:
    """Read a Received time in the owner's zone (the digest carries no offset). None if unreadable."""
    raw = raw.strip()
    if not raw or raw.lower() == "none":
        return None
    try:
        dt = dateparser.parse(raw, default=datetime.combine(when, time(0)))
    except (ValueError, OverflowError):
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def parse_due(raw: str, *, when: date) -> date | None:
    """A due date: ISO, any common date form, today/tomorrow, or a weekday (the next one on or after)."""
    value = DUE_FILLER.sub("", raw.strip()).strip()
    lowered = value.lower()
    if not value or lowered in ("none", "n/a", "-"):
        return None
    if lowered == "today":
        return when
    if lowered == "tomorrow":
        return when + timedelta(days=1)
    try:
        return dateparser.parse(value, default=datetime.combine(when, time(0))).date()
    except (ValueError, OverflowError):
        return None


def digest_account(conn: sqlite3.Connection, label: str) -> int:
    """The digest account, created on first ingest."""
    rows = accounts.list_accounts(conn, kind="digest")
    if rows:
        return rows[0]["id"]
    return conn.execute("INSERT INTO account (kind, label, color) VALUES ('digest', ?, ?)",
                        (label, accounts.next_color(conn, "digest"))).lastrowid


def _digest_date(value: date | str | None, today: date) -> date:
    if value is None or value == "":
        return today
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise DigestError("digest_date must be a date (YYYY-MM-DD)") from None


def _link(raw: str) -> str | None:
    value = raw.strip().strip("<>")
    m = MD_LINK.match(value)
    if m:
        value = m.group(1)
    if not value or value.lower() == "none" or not value.startswith(("http://", "https://")):
        return None
    return value.split()[0]


def _clean(value: str) -> str:
    """Collapse whitespace and drop list markers and trailing separators left by chat formatting."""
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" -•|;").strip()


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _shown(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= SHOWN_BLOCK else text[:SHOWN_BLOCK] + "…"
