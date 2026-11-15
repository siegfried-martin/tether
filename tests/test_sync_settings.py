"""Per-account sync settings: validation, reconcile trigger, and the re-mirror it causes."""

import pytest
from conftest import msg

from tether.connectors.gmail import GmailSync
from tether.services import accounts, items, sync_state


@pytest.fixture
def account(conn, vault):
    return accounts.upsert_gmail_account(conn, vault, email="me@example.com",
                                         secret={"refresh_token": "rt", "email": "me@example.com"}, scopes="x")


def test_defaults(conn, account):
    row = accounts.get_account(conn, account)
    assert accounts.excluded_categories(row) == {"promotions", "social", "forums"}
    assert accounts.sync_days(row, 14) == 14


def test_change_requests_reconcile(conn, account):
    sync_state.record_ok(conn, account, sync_state.GMAIL_HISTORY, "500")
    assert accounts.update_sync_settings(conn, account, excluded_categories=["updates"], initial_sync_days=3)
    row = accounts.get_account(conn, account)
    assert accounts.excluded_categories(row) == {"updates"} and row["initial_sync_days"] == 3
    assert sync_state.get_cursor(conn, account, sync_state.GMAIL_HISTORY) == sync_state.RECONCILE


def test_no_change_keeps_cursor(conn, account):
    sync_state.record_ok(conn, account, sync_state.GMAIL_HISTORY, "500")
    assert not accounts.update_sync_settings(conn, account, excluded_categories=["forums", "social", "promotions"])
    assert sync_state.get_cursor(conn, account, sync_state.GMAIL_HISTORY) == "500"


@pytest.mark.parametrize("kwargs", [{"excluded_categories": ["spam"]}, {"initial_sync_days": 0},
                                    {"initial_sync_days": 91}])
def test_validation(conn, account, kwargs):
    with pytest.raises(ValueError):
        accounts.update_sync_settings(conn, account, **kwargs)


async def test_reconcile_applies_new_settings(conn, fake_gmail, account):
    fake_gmail.put("upd", msg("a", "upd", labels=["INBOX", "CATEGORY_UPDATES"]))
    fake_gmail.put("promo", msg("b", "promo", labels=["INBOX", "CATEGORY_PROMOTIONS"]))
    fake_gmail.put("oldpromo", msg("c", "oldpromo", labels=["INBOX", "CATEGORY_PROMOTIONS"],
                                   minutes_ago=60 * 24 * 10))

    def sync():
        row = accounts.get_account(conn, account)
        return GmailSync(conn, fake_gmail, account_id=account, email="me@example.com",
                         excluded=accounts.excluded_categories(row), initial_sync_days=accounts.sync_days(row, 14))

    await sync().run()
    assert set(items.open_item_ids(conn, account)) == {"upd"}

    # Exclude Updates, include Promotions, shrink the window to 3 days.
    accounts.update_sync_settings(conn, account, excluded_categories=["social", "forums", "updates"],
                                  initial_sync_days=3)
    await sync().run()
    assert set(items.open_item_ids(conn, account)) == {"promo"}, "old promo is outside the 3-day window"
    assert items.get_item(conn, account, "upd")["state"] == "archived"
    assert sync_state.get_cursor(conn, account, sync_state.GMAIL_HISTORY) == "100"
