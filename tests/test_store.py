"""Checks for the durable message queue and per-chat ordering."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import access, store
from zoe.db import apply_schema


def _conn() -> tuple[sqlite3.Connection, int]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return conn, access.create_person(conn, "Tester", "owner", "it").id


def _update(update_id: int, chat_id: int = 42, text: str = "hi") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id},
            "text": text,
        },
    }


def test_record_then_claim():
    conn, pid = _conn()
    assert store.record_inbound(conn, _update(1), pid)
    msg = store.claim_next(conn)
    assert msg is not None and msg.text == "hi" and msg.attempts == 1 and msg.person_id == pid
    assert store.claim_next(conn) is None


def test_duplicate_update_is_ignored():
    conn, pid = _conn()
    assert store.record_inbound(conn, _update(7), pid)
    assert store.record_inbound(conn, _update(7), pid) is None


def test_one_turn_at_a_time_per_chat_and_chats_in_parallel():
    conn, pid = _conn()
    store.record_inbound(conn, _update(1, chat_id=1, text="first in chat 1"), pid)
    store.record_inbound(conn, _update(2, chat_id=1, text="second in chat 1"), pid)
    store.record_inbound(conn, _update(3, chat_id=2, text="first in chat 2"), pid)

    a = store.claim_next(conn)
    b = store.claim_next(conn)
    assert (a.chat_id, a.text) == (1, "first in chat 1")
    assert (b.chat_id, b.text) == (2, "first in chat 2")
    assert store.claim_next(conn) is None

    store.mark(conn, a.id, "answered")
    c = store.claim_next(conn)
    assert (c.chat_id, c.text) == (1, "second in chat 1")


def test_stale_processing_is_requeued():
    conn, pid = _conn()
    store.record_inbound(conn, _update(3), pid)
    store.claim_next(conn)
    assert store.claim_next(conn) is None
    assert store.reset_stale_processing(conn) == 1
    assert store.claim_next(conn) is not None


def test_history_excludes_the_message_in_progress():
    conn, pid = _conn()
    store.record_inbound(conn, _update(5, text="first"), pid)
    first = store.claim_next(conn)
    store.mark(conn, first.id, "answered")
    store.record_outbound(conn, 42, 100, "reply", pid)
    store.record_inbound(conn, _update(6, text="in progress"), pid)
    store.claim_next(conn)
    assert [h["text"] for h in store.recent_history(conn, 42)] == ["first", "reply"]


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
