"""Checks for the intake path: who gets queued, who gets an access request, and the menu entry."""

import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeAPI, message, press, shown, targets
from zoe import access, store
from zoe.config import Config
from zoe.db import apply_schema
from zoe.ingest import Poller

OWNER = 111


def _setup() -> tuple[sqlite3.Connection, Poller, FakeAPI]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    access.bootstrap_owner(conn, frozenset({OWNER}), "Alex", "it")
    cfg = Config(
        bot_token="", owner_ids=frozenset({OWNER}), data_dir=Path("."), db_path=Path(":memory:"),
        vault_path=Path("v.db"), vault_key_path=Path("v.key"), workdir=Path("."),
    )
    api = FakeAPI()
    return conn, Poller(cfg, api, threading.Event()), api


def test_owner_message_is_queued():
    conn, poller, _ = _setup()
    poller.process(conn, message(1, OWNER, "ciao"))
    assert store.claim_next(conn).text == "ciao"


def test_a_stranger_gets_no_answer_and_the_owner_no_notice():
    conn, poller, api = _setup()
    poller.process(conn, message(1, 333, "hello", first_name="Mia", language_code="en"))
    poller.process(conn, message(2, 333, "anyone?"))
    assert api.sent == [] and store.claim_next(conn) is None

    owner, code = access.resolve(conn, OWNER).person, access.pending(conn)[0].request_code
    people = poller._menu.answer(conn, owner, {"view": "people"})
    assert "Mia" in shown(people) and f"request:{code}" in targets(people)
    view = poller._menu.answer(conn, owner, {"view": f"request:{code}"})
    styles = {b["button"]["act"].split(":")[0]: b["button"]["style"] for b in view["blocks"] if "button" in b}
    assert styles == {"approve": "success", "deny": "danger"}


def test_only_the_latest_strangers_are_remembered():
    conn, _, _ = _setup()
    for user_id in range(1000, 1000 + access.STRANGERS_KEPT + 10):
        access.note_stranger(conn, {"id": user_id})
    assert len(access.pending(conn)) == access.STRANGERS_KEPT


def test_owner_approves_from_the_menu_and_the_new_person_can_talk():
    conn, poller, api = _setup()
    poller.process(conn, message(1, 333, "hello", first_name="Mia", language_code="en"))
    code = access.pending(conn)[0].request_code

    view = poller._menu.answer(conn, access.resolve(conn, OWNER).person, {"action": f"approve:{code}"})
    assert any("Hi Mia" in text for text in api.to(333)) and "Mia" in view["notice"]

    poller.process(conn, message(3, 333, "thanks!"))
    queued = store.claim_next(conn)
    assert queued.text == "thanks!" and access.get_person(conn, queued.person_id).name == "Mia"


def test_an_update_that_fails_is_tried_again_then_set_aside_with_a_notice():
    conn, poller, api = _setup()
    poller._halt.wait = lambda seconds: False
    api.get_updates = lambda offset, timeout: [message(7, OWNER, "ciao")]
    failures = iter([True, True, True])

    def flaky(conn, update):
        if next(failures):
            raise RuntimeError("database is locked")

    poller.process = flaky
    assert poller._poll(conn, 7) == 7 and poller._poll(conn, 7) == 7 and api.to(OWNER) == []
    assert poller._poll(conn, 7) == 8 and "non sono riuscita a leggerlo" in api.to(OWNER)[0]
    assert store.get_offset(conn) == 8


def test_a_sleeping_worker_wakes_as_soon_as_work_is_queued():
    conn, _, _ = _setup()
    woke = threading.Event()
    sleeper = threading.Thread(target=lambda: (store.wait_for_work(5), woke.set()))
    sleeper.start()
    time.sleep(0.1)
    started = time.monotonic()
    store.enqueue(conn, OWNER, 1, "ciao", "text")
    assert woke.wait(2) and time.monotonic() - started < 1
    sleeper.join()


def test_owners_hear_once_an_hour_that_another_program_reads_the_bot():
    conn, poller, api = _setup()
    poller.conflict(conn)
    poller.conflict(conn)
    assert len(api.to(OWNER)) == 1 and "Un altro programma" in api.to(OWNER)[0]


def test_menu_and_start_are_answered_and_never_queued():
    conn, poller, api = _setup()
    poller.process(conn, message(1, OWNER, "/menu"))
    poller.process(conn, message(2, 333, "hi", first_name="Mia"))
    access.approve(conn, access.pending(conn)[0].request_code, "Mia", "it")
    poller.process(conn, message(3, 333, "/start"))
    assert len(api.to(OWNER)) == len(api.to(333)) == 1 and store.claim_next(conn) is None


def test_presses_from_strangers_or_on_buttons_nothing_answers_are_only_acknowledged():
    conn, poller, api = _setup()
    poller.process(conn, press(1, 999, "c:1:0"))
    poller.process(conn, press(2, OWNER, "v:people"))
    assert api.notices == [None, None] and not api.edits and not api.sent


def test_group_messages_are_ignored():
    conn, poller, api = _setup()
    poller.process(conn, message(1, OWNER, "ciao a tutti", chat_type="group"))
    assert store.claim_next(conn) is None and not api.sent


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
