import time

import pytest

from tether import db
from tether.connectors.gmail import HistoryExpired, message_category
from tether.crypto import Vault, generate_key


@pytest.fixture
def conn():
    # TestClient runs the app on its own thread.
    c = db.connect(":memory:", check_same_thread=False)
    yield c
    c.close()


@pytest.fixture
def vault():
    return Vault(generate_key())


def msg(mid, thread_id, *, labels, frm="Ann Lee <ann@example.com>", to="me@example.com",
        subject="Hello", snippet="hi there", minutes_ago=10):
    return {
        "id": mid, "threadId": thread_id, "labelIds": labels, "snippet": snippet,
        "internalDate": str(int((time.time() - minutes_ago * 60) * 1000)),
        "payload": {"headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": frm},
                                {"name": "To", "value": to}]},
    }


class FakeGmail:
    """In-memory Gmail implementing the bits of query syntax Tether uses."""

    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.history: list[dict] = []
        self.history_id = 100
        self.expired = False
        self.fetches: list[str] = []

    def put(self, thread_id, *messages):
        self.threads[thread_id] = {"id": thread_id, "messages": list(messages)}

    def record(self, **record):
        self.history_id += 1
        self.history.append({"id": str(self.history_id), **record})

    async def get_profile(self):
        return {"historyId": str(self.history_id)}

    async def list_thread_ids(self, query):
        terms = query.split()
        excluded = {t.removeprefix("-category:") for t in terms if t.startswith("-category:")}
        days = next((int(t[len("newer_than:"):-1]) for t in terms if t.startswith("newer_than:")), None)
        cutoff = (time.time() - days * 86400) * 1000 if days else 0
        out = []
        for tid, thread in self.threads.items():
            if any("INBOX" in m["labelIds"] and message_category(m["labelIds"]) not in excluded
                   and int(m["internalDate"]) >= cutoff for m in thread["messages"]):
                out.append(tid)
        return out

    async def get_thread(self, thread_id):
        self.fetches.append(thread_id)
        return self.threads.get(thread_id)

    async def list_history(self, start):
        if self.expired:
            raise HistoryExpired(start)
        return [r for r in self.history if int(r["id"]) > int(start)], str(self.history_id)


@pytest.fixture
def fake_gmail():
    return FakeGmail()
