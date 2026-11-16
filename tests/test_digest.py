"""The work digest: label-anchored parsing, field mapping, dedup and where items land."""

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from test_web import client, login, settings  # noqa: F401  (fixtures)
from tether.services import digest, items, overview, tasks, triage

TZ = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 15)

DIGEST = """Here is today's digest.

ITEM 1
From: Ann Lee
Subject: Closing docs for 12 Main St
Received: 2026-11-14 09:12
Needed: Sign and return the closing package
Due: 2026-11-17
Priority: high
Link: https://outlook.office.com/mail/id/AAA

ITEM 2
From: Bob Ray
Subject: Fwd: Due: Q4 invoice
Received: Fri 11/14/2026 4:05 PM
Needed: Approve the invoice
Due: none
Priority: whenever
Link: none
"""


def ingest(conn, text, **kw):
    return digest.ingest_digest(conn, text, tz=TZ, today=TODAY, **kw)


def test_parses_and_maps_fields(conn):
    out = ingest(conn, DIGEST)
    assert (out["created"], out["duplicates"], out["rejected"], out["warnings"]) == (2, 0, [], [])
    first, second = (items.get_by_id(conn, i) for i in out["item_ids"])
    assert first["kind"] == "digest" and first["state"] == "open"
    assert first["sender"] == "Ann Lee" and first["title"] == "Closing docs for 12 Main St"
    assert first["needed"] == "Sign and return the closing package"
    assert first["last_activity_at"] == "2026-11-14T14:12:00Z", "Received is read in TETHER_TIMEZONE"
    assert first["due_at"] == "2026-11-17" and first["priority"] == "high"
    assert first["url"] == "https://outlook.office.com/mail/id/AAA"
    # A label inside a line ("Fwd: Due: ...") stays in the subject when the digest keeps its lines.
    assert second["title"] == "Fwd: Due: Q4 invoice"
    assert second["last_activity_at"] == "2026-11-14T21:05:00Z"
    assert second["due_at"] is None and second["priority"] == "normal" and second["url"] is None
    account = conn.execute("SELECT * FROM account WHERE id = ?", (first["account_id"],)).fetchone()
    assert account["kind"] == "digest" and account["label"] == "Work"


def test_collapsed_into_one_paragraph(conn):
    """Teams can collapse the digest; labels still anchor the fields."""
    collapsed = ("ITEM From: Ann Lee Subject: Closing docs for 12 Main St Received: 2026-11-14 09:12 "
                 "Needed: Sign and return the closing package Due: 2026-11-17 Priority: high "
                 "Link: https://outlook.office.com/mail/id/AAA ITEM **From:** Cy **Subject:** Budget "
                 "**Needed:** Reply with numbers")
    out = ingest(conn, collapsed)
    assert out["created"] == 2 and out["rejected"] == []
    first, second = (items.get_by_id(conn, i) for i in out["item_ids"])
    assert first["due_at"] == "2026-11-17" and first["needed"] == "Sign and return the closing package"
    assert second["sender"] == "Cy" and second["needed"] == "Reply with numbers"


def test_rerun_is_idempotent_and_dates_in_any_format_dedupe(conn):
    assert ingest(conn, DIGEST)["created"] == 2
    again = ingest(conn, DIGEST)
    assert (again["created"], again["duplicates"]) == (0, 2)
    reworded = DIGEST.replace("2026-11-14 09:12", "November 14, 2026 at 9:12 AM").replace("Ann Lee", "ANN LEE")
    assert ingest(conn, reworded)["duplicates"] == 2
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 2


async def test_archived_item_stays_archived_when_it_reappears(conn):
    [item_id, _] = ingest(conn, DIGEST)["item_ids"]

    def no_gmail(account_id):
        raise AssertionError("digest archive is local only")
    await triage.archive_item(conn, no_gmail, item_id)
    assert ingest(conn, DIGEST)["duplicates"] == 2
    assert items.get_by_id(conn, item_id)["state"] == "archived"


def test_rejects_blocks_missing_required_fields_and_commits_the_rest(conn):
    text = DIGEST + "\nITEM 3\nFrom: Dee\nSubject: Lunch?\nReceived: 2026-11-14 12:00\nPriority: low\n"
    out = ingest(conn, text)
    assert out["created"] == 2
    [rejected] = out["rejected"]
    assert rejected["reason"] == "Missing Needed." and "Lunch?" in rejected["block"]


def test_no_action_items_and_empty_input(conn):
    assert ingest(conn, "NO ACTION ITEMS")["created"] == 0
    assert ingest(conn, "NO ACTION ITEMS")["rejected"] == []
    assert ingest(conn, "hello")["rejected"][0]["reason"] == "No ITEM blocks found."
    assert conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0, "nothing to store, no account"


def test_missing_received_uses_digest_date(conn):
    block = "ITEM\nFrom: Ann\nSubject: Call me\nNeeded: Call back\n"
    first = ingest(conn, block, digest_date="2026-11-10")
    assert items.get_by_id(conn, first["item_ids"][0])["last_activity_at"] == "2026-11-10T05:00:00Z"
    assert ingest(conn, block, digest_date="2026-11-10")["duplicates"] == 1
    assert ingest(conn, block, digest_date="2026-11-11")["created"] == 1, "another day is another ask"
    bad = ingest(conn, block.replace("Needed", "Received: whenever\nNeeded"), digest_date="2026-11-12")
    assert "Couldn't read Received" in bad["warnings"][0]["reason"]
    with pytest.raises(digest.DigestError):
        ingest(conn, block, digest_date="Monday")


def test_due_forms(conn):
    when = TODAY  # a Sunday
    assert digest.parse_due("Friday", when=when) == date(2026, 11, 20)
    assert digest.parse_due("EOD Friday", when=when) == date(2026, 11, 20)
    assert digest.parse_due("by 11/18", when=when) == date(2026, 11, 18)
    assert digest.parse_due("tomorrow", when=when) == date(2026, 11, 16)
    assert digest.parse_due("ASAP", when=when) is None
    out = ingest(conn, "ITEM From: A Subject: B Needed: Do it Due: ASAP")
    assert items.get_by_id(conn, out["item_ids"][0])["needed"] == "Do it (Due: ASAP)"
    assert "kept it in Needed" in out["warnings"][0]["reason"]


def test_markdown_link_and_bullets(conn):
    text = ("ITEM\n- **From:** Ann\n- **Subject:** Plat map\n- **Needed:** Review\n"
            "- **Link:** [open](https://outlook.office.com/x)\n")
    out = ingest(conn, text)
    item = items.get_by_id(conn, out["item_ids"][0])
    assert item["sender"] == "Ann" and item["needed"] == "Review" and item["url"] == "https://outlook.office.com/x"


def test_digest_items_are_urgent_and_convert_to_tasks(conn):
    out = ingest(conn, DIGEST)
    urgent = overview.get_overview(conn, date(2026, 11, 17))["urgent"]["items"]
    assert [i["title"] for i in urgent] == ["Closing docs for 12 Main St"]
    task = tasks.create_task(conn, title="Sign closing docs", item_id=out["item_ids"][0])
    assert task["item_title"] == "Closing docs for 12 Main St"


def test_digest_api(client, conn, monkeypatch):
    login(client, monkeypatch)
    out = client.post("/api/digest", json={"raw_text": DIGEST}).json()
    assert out["created"] == 2
    assert client.post("/api/digest", json={"raw_text": DIGEST, "digest_date": "x"}).status_code == 422
    listed = client.get("/api/items?account=Work").json()["items"]
    assert {i["kind"] for i in listed} == {"digest"}
