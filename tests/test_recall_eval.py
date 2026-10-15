"""Checks for the recall measure: replayed against memory as it was then, judged once by Claude."""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeRunner
from zoe import access, recall_eval
from zoe.brain import Brain
from zoe.claude_runner import Profile
from zoe.config import Config
from zoe.db import apply_schema


def _setup():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    owner = access.bootstrap_owner(conn, frozenset({OWNER}), "Alex", "it")
    return conn, owner, Brain(conn, owner.id)


def _at(conn, neuron: int, when: float) -> None:
    conn.execute("UPDATE neurons SET created_at = ? WHERE id = ?", (when, neuron))


def test_a_message_is_scored_against_memory_as_it_was_then():
    conn, owner, brain = _setup()
    tree = brain.add("fact", "Alex grows a lemon tree")
    sold = brain.add("fact", "Alex sold the lemon tree")
    _at(conn, tree, 100.0), _at(conn, sold, 300.0)
    cases = [{"text": "how is my lemon tree", "at": 200.0, "needed": [tree, sold]},
             {"text": "and the lemon tree now", "at": 200.0, "needed": [sold]}]
    assert recall_eval.score(conn, owner, cases) == recall_eval.Score(1, 1.0, 1.0, 1.0)


def test_a_memory_merged_after_the_judging_still_counts_through_the_one_that_kept_it():
    conn, owner, brain = _setup()
    tree = brain.add("fact", "Alex grows a lemon tree")
    twin = brain.add("fact", "Alex has a lemon tree in the garden")
    _at(conn, tree, 150.0), _at(conn, twin, 100.0)
    brain.merge(tree, twin)
    result = recall_eval.score(conn, owner, [{"text": "how is my lemon tree", "at": 120.0, "needed": [twin]}])
    assert result.cases == 1 and result.found == 1.0


def test_a_short_message_is_replayed_with_the_messages_before_it():
    conn, owner, brain = _setup()
    tree = brain.add("fact", "Alex grows a lemon tree")
    _at(conn, tree, 100.0)
    for number, text, when in ((6, "the lemon tree has yellow leaves", 150.0), (7, "what should I do now?", 200.0)):
        conn.execute(
            "INSERT INTO messages (chat_id, person_id, telegram_id, direction, text, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'in', ?, 'answered', ?, ?)",
            (OWNER, owner.id, number, text, when, when),
        )
    case = {"message": 2, "text": "what should I do now?", "at": 200.0, "needed": [tree]}
    assert recall_eval.score(conn, owner, [case]).found == 1.0
    assert recall_eval.score(conn, owner, [{**case, "message": None}]).found == 0.0


def test_the_score_shows_what_the_links_bring_beyond_search_alone():
    conn, owner, brain = _setup()
    tree = brain.add("fact", "Alex grows a lemon tree")
    water = brain.add("habit", "Alex waters the plants on Sundays")
    brain.connect(tree, water, delta=0.6)
    _at(conn, tree, 100.0), _at(conn, water, 100.0)
    result = recall_eval.score(conn, owner, [{"text": "how is my lemon tree", "at": 200.0, "needed": [water]}])
    assert result.found == 1.0 and result.alone == 0.0


def test_a_judge_names_what_each_sampled_message_needed():
    conn, owner, brain = _setup()
    tree = brain.add("fact", "Alex grows a lemon tree")
    _at(conn, tree, 100.0)
    for number, text, when in ((6, "a message from before Zoe remembered anything", 50.0),
                               (7, "how is my lemon tree doing?", 200.0)):
        conn.execute(
            "INSERT INTO messages (chat_id, person_id, telegram_id, direction, text, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'in', ?, 'answered', ?, ?)",
            (OWNER, owner.id, number, text, when, when),
        )
    runner = FakeRunner(json.dumps({"1": [tree]}))
    cases = recall_eval.build(conn, owner, runner, Profile((), Path(".")))
    assert cases == [{"message": 2, "at": 200.0, "text": "how is my lemon tree doing?", "needed": [tree]}]
    assert f"- {tree} [fact] Alex grows a lemon tree" in runner.prompts[0]
    assert "Alex: how is my lemon tree doing?" in runner.prompts[0]


def test_the_first_set_waits_for_enough_messages_then_is_renewed_every_two_weeks():
    conn, owner, brain = _setup()
    tree = brain.add("fact", "Alex grows a lemon tree")
    _at(conn, tree, 100.0)

    def write(number: int) -> None:
        conn.execute(
            "INSERT INTO messages (chat_id, person_id, telegram_id, direction, text, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'in', 'how is my lemon tree doing today?', 'answered', 200.0, 200.0)",
            (OWNER, owner.id, number),
        )

    for number in range(recall_eval.ENOUGH - 1):
        write(number)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(bot_token="", owner_ids=frozenset({OWNER}), data_dir=Path(tmp), db_path=Path(tmp) / "zoe.db",
                     vault_path=Path(tmp) / "v.db", vault_key_path=Path(tmp) / "v.key", workdir=Path(tmp))
        runner = FakeRunner(*[json.dumps({"1": [tree]})] * 20)
        assert not recall_eval.renew(conn, cfg, owner, 1000.0, runner) and not runner.prompts
        assert recall_eval.summary(conn, cfg, owner) == ""
        write(recall_eval.ENOUGH)
        assert recall_eval.renew(conn, cfg, owner, 1000.0, runner)
        judged = len(runner.prompts)
        assert not recall_eval.renew(conn, cfg, owner, 1000.0 + recall_eval.RENEW - 1, runner)
        assert recall_eval.renew(conn, cfg, owner, 1000.0 + recall_eval.RENEW, runner) and len(runner.prompts) == 2 * judged
        assert "labelled on" in recall_eval.summary(conn, cfg, owner)


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
