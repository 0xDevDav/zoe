"""Checks for the model and effort of each turn, mind and study: the one model at xhigh, news from a mind at
high, max never, and no call to Claude to choose them."""

import dataclasses
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeRunner, message, worker_setup
from test_runner import _claude
from test_worker import _due_plan, _news_of_a_mind
from zoe import minds, store, strength
from zoe.claude_runner import ClaudeRunner, Profile, Result
from zoe.config import Config
from zoe.db import apply_schema

MODEL = "one-model"
SECOND = 7001


def _cfg(home: Path) -> Config:
    return Config(bot_token="", owner_ids=frozenset({1}), data_dir=home, db_path=home / "z.db", vault_path=home / "v",
                  vault_key_path=home / "k", workdir=home, claude_model=MODEL)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return conn


def _row(conn: sqlite3.Connection, choice: strength.Choice) -> sqlite3.Row:
    return conn.execute("SELECT * FROM model_choices WHERE id = ?", (choice.id,)).fetchone()


def _calls(marker: Path) -> str:
    """A stand-in for claude that only notes it was started: every turn here runs on the fake runner, so any
    start of it is a call made to choose a turn's model or effort."""
    return f'''
import pathlib, sys
sys.stdin.read()
with open({str(marker)!r}, "a", encoding="utf-8") as noted:
    noted.write(" ".join(sys.argv[1:]) + "\\n")
'''


def test_every_turn_starts_on_the_one_model_at_xhigh_with_no_call_to_choose_it_and_news_of_a_mind_at_high():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        conn, owner, worker, _, api = worker_setup(home, others=(SECOND,))
        marker = home / "claude-started.txt"
        cfg = dataclasses.replace(worker._cfg, claude_model=MODEL, claude_bin=_claude(home, _calls(marker)))
        worker = type(worker)(cfg, api, threading.Event())
        worker._runner = runner = FakeRunner(*["Fatto."] * 8)
        for update_id, chat, text in ((1, OWNER, "Grazie"), (2, OWNER, "Segna 3 uova"),
                                      (3, OWNER, "Ripensaci con il massimo sforzo"),
                                      (4, OWNER, "Basta poco sforzo: che ore sono?"),
                                      (5, SECOND, "Sistemami il codice del sito di Mia")):
            store.record_inbound(conn, message(update_id, chat, text), owner.id)
            worker._handle(conn, store.claim_next(conn))
        store.enqueue(conn, OWNER, owner.id, "Sì", "choice")
        worker._handle(conn, store.claim_next(conn))
        _due_plan(conn, owner)
        worker._handle(conn, store.claim_next(conn))
        _news_of_a_mind(conn, owner)
        worker._handle(conn, store.claim_next(conn))
        assert not marker.exists(), marker.read_text(encoding="utf-8")
        assert runner.models == [(MODEL, "xhigh")] * 7 + [(MODEL, "high")], runner.models
        rows = conn.execute("SELECT source, category, model, effort, explicit, outcome FROM model_choices "
                            "ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [
            ("chat", kind, MODEL, "xhigh", 0, "answered")
            for kind in ("text", "text", "text", "text", "text", "choice", "plan")] + [
            ("chat", "mind_news", MODEL, "high", 0, "answered")], [tuple(r) for r in rows]


def test_no_run_asks_claude_for_max():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, conn = _cfg(Path(tmp)), _db()
        # A mind whose kept choice names max and another model resumes on a choice of its own.
        conn.execute("INSERT INTO model_choices (source, mind_id, category, level, model, effort, reason, created_at) "
                     "VALUES ('mind', 1, 'code', 6, 'other-model', 'max', 'made up', ?)", (time.time(),))
        ran = [strength.for_turn(cfg, kind) for kind in ("text", "voice", "photo", "choice", "plan", "reaction")]
        ran += [strength.for_turn(cfg, minds.EVENT), strength.for_mind(conn, cfg, 1), strength.for_mind(conn, cfg, 2),
                strength.for_study(cfg), strength.for_lesson(cfg)]
        assert [(c.model, c.effort) for c in ran] == [(MODEL, "xhigh")] * 6 + [(MODEL, "high")] + [(MODEL, "xhigh")] * 3 + [
            (MODEL, "high")]
        assert not any(c.id for c in ran), ran


def test_a_kept_choice_names_its_effort_and_the_step_it_has_in_the_table():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, conn = _cfg(Path(tmp)), _db()
        turn = strength.record(conn, strength.for_turn(cfg, "text"), "chat", chat_id=OWNER, message_id=9)
        news = strength.record(conn, strength.for_turn(cfg, minds.EVENT), "chat", chat_id=OWNER, message_id=10)
        mind = strength.record(conn, strength.for_mind(conn, cfg, 4), "mind", chat_id=OWNER, mind_id=4)
        study = strength.record(conn, strength.for_study(cfg), "study")
        assert [tuple(_row(conn, c))[1:11] for c in (turn, news, mind, study)] == [
            ("chat", OWNER, 9, None, "text", 5, MODEL, "xhigh", "every turn runs at the one effort", 0),
            ("chat", OWNER, 10, None, "mind_news", 3, MODEL, "high", "news of a parallel mind to pass on", 0),
            ("mind", OWNER, None, 4, "mind", 5, MODEL, "xhigh", "a parallel mind works alone", 0),
            ("study", None, None, None, "study", 5, MODEL, "xhigh", "the nightly study reads and weighs many days", 0)]
        assert strength.for_mind(conn, cfg, 4).id == mind.id
        strength.ended(conn, turn.id, "answered", 12.5)
        assert (_row(conn, turn)["outcome"], _row(conn, turn)["duration"]) == ("answered", 12.5)


def test_an_older_table_of_choices_gains_the_newer_columns_and_keeps_recording():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE model_choices (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, chat_id INTEGER, "
        "message_id INTEGER, mind_id INTEGER, category TEXT NOT NULL, level INTEGER NOT NULL, model TEXT NOT NULL, "
        "effort TEXT NOT NULL, reason TEXT NOT NULL, explicit INTEGER NOT NULL DEFAULT 0, "
        "outcome TEXT NOT NULL DEFAULT '', duration REAL, created_at REAL NOT NULL)")
    apply_schema(conn)
    with tempfile.TemporaryDirectory() as tmp:
        choice = strength.record(conn, strength.for_turn(_cfg(Path(tmp)), "text"), "chat", chat_id=OWNER)
    row = _row(conn, choice)
    assert (row["effort"], row["judge"], row["needed"], row["judge_level"]) == ("xhigh", "", None, None), dict(row)


ARGS_CLAUDE = r'''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "result": " ".join(sys.argv[1:]), "is_error": False}), flush=True)
'''


def test_the_chosen_model_and_effort_reach_claude_and_the_runner_default_stays_otherwise():
    with tempfile.TemporaryDirectory() as tmp:
        runner = ClaudeRunner(_claude(Path(tmp), ARGS_CLAUDE), "base-model", hard_timeout=30, inactivity_timeout=20)
        chosen = runner.run("ciao", Profile((), Path(tmp)), model="one-model", effort="xhigh").text
        assert "--model one-model --effort xhigh" in chosen, chosen
        plain = runner.run("ciao", Profile((), Path(tmp))).text
        assert "--model base-model" in plain and "--effort" not in plain, plain


def test_how_a_turn_ended():
    assert strength.outcome(Result(True, "x", False, False, 0, 1.0)) == "answered"
    assert strength.outcome(Result(False, "", True, False, None, 1.0)) == "stuck"
    assert strength.outcome(Result(False, "", False, False, 1, 1.0)) == "failed"
    assert strength.outcome(Result(False, "", False, False, None, 1.0, cancelled=True)) == "cancelled"
    assert strength.outcome(Result(False, "", False, False, 1, 1.0, refused="tools ['X']")) == "refused"
    limit = "You've hit your session limit · resets 4pm"
    assert strength.outcome(Result(False, limit, False, False, 1, 1.0, failure=limit)) == "unavailable"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
