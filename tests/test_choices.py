"""Checks for questions with buttons: layout, the tapped answer coming back, and closed choices."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeAPI, press
from zoe import access, choices, store
from zoe.db import apply_schema


def _setup() -> tuple[sqlite3.Connection, access.Person, access.Person]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return conn, access.create_person(conn, "Alex", "owner", "it"), access.create_person(conn, "Mia", "member", "it")


def test_short_options_share_rows_and_long_ones_get_their_own():
    assert [len(row) for row in choices.keyboard(1, ["PDF", "Excel", "Entrambi"])] == [2, 1]
    assert [len(row) for row in choices.keyboard(1, ["Mandalo stasera alle nove", "Domani"])] == [1, 1]


def test_a_tapped_option_closes_the_question_and_comes_back_as_a_message():
    conn, alex, _ = _setup()
    choice = choices.create(conn, 111, alex.id, ["PDF", "Excel"])
    api = FakeAPI()

    choices.handle(conn, api, alex, press(1, 111, f"c:{choice}:1")["callback_query"])
    queued = store.claim_next(conn)
    assert (queued.text, queued.kind, queued.person_id) == ("Excel", "choice", alex.id)
    assert api.edits[-1][3] == [[{"text": "✅ Excel", "callback_data": choices.NOOP, "style": "success"}]]
    assert {b["style"] for row in choices.keyboard(choice, ["PDF", "Excel"]) for b in row} == {"primary"}
    assert api.notices[-1] is None

    choices.handle(conn, api, alex, press(2, 111, f"c:{choice}:0")["callback_query"])
    assert api.notices[-1] == "Questa scelta non è più disponibile."
    assert store.claim_next(conn) is None


def test_only_the_person_asked_can_answer():
    conn, alex, mia = _setup()
    choice = choices.create(conn, 111, alex.id, ["Sì", "No"])
    api = FakeAPI()
    choices.handle(conn, api, mia, press(1, 333, f"c:{choice}:0")["callback_query"])
    assert api.notices[-1] == "Questa scelta non è più disponibile."
    assert choices.resolve(conn, choice, 0, alex.id) == "Sì"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
