"""Checks for the nightly review: when it runs, merging memories, what it may change, and the record it leaves."""

import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeRunner, worker_setup
from zoe import access, review, store, vocabulary
from zoe.brain import Brain


def _chat(conn, person_id, *lines, at):
    for telegram_id, direction, text in lines:
        conn.execute(
            "INSERT INTO messages (chat_id, person_id, telegram_id, direction, text, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'answered', ?, ?)",
            (OWNER, person_id, telegram_id, direction, text, at, at),
        )


def test_the_review_runs_once_a_night_and_the_first_start_only_sets_the_clock():
    with tempfile.TemporaryDirectory() as tmp:
        conn, *_ = worker_setup(Path(tmp))
        evening = datetime(2026, 10, 13, 20, 0).timestamp()
        assert not review.due(conn, "0 3 * * *", evening)
        assert not review.due(conn, "0 3 * * *", datetime(2026, 10, 14, 2, 59).timestamp())
        assert review.due(conn, "0 3 * * *", datetime(2026, 10, 14, 3, 0).timestamp())


def test_a_merged_memory_keeps_the_links_of_both_and_only_the_persons_own_merge():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        brain = Brain(conn, owner.id)
        coffee, espresso = brain.learn("preference", "Ama il caffè"), brain.learn("preference", "Beve espresso")
        bar, cup = brain.ensure(vocabulary.TERM, "Bar"), brain.ensure(vocabulary.TERM, "Tazza")
        brain.connect(coffee, bar)
        brain.connect(espresso, bar)
        brain.connect(espresso, cup)
        mia = access.create_person(conn, "Mia", "member", "it")
        hers = Brain(conn, mia.id).learn("fact", "Abita a Roma")
        conn.execute("UPDATE neurons SET created_at = 100.0 WHERE id = ?", (espresso,))
        assert brain.merge(coffee, espresso) and brain.get(espresso) is None
        assert {r.id for r in brain.recall([coffee])} >= {bar, cup}
        assert brain.kept(espresso) == coffee and brain.kept(coffee) == coffee
        assert conn.execute("SELECT created_at FROM neurons WHERE id = ?", (coffee,)).fetchone()[0] == 100.0
        assert not brain.merge(coffee, hers) and Brain(conn, mia.id).get(hers) is not None


def test_the_review_sorts_only_what_it_was_shown_and_records_what_it_replaced():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        brain = Brain(conn, owner.id)
        a = brain.learn("preference", "Preferisce risposte brevi")
        b = brain.learn("preference", "Vuole messaggi corti")
        c = brain.learn("fact", "Lavora a turni")
        d = brain.learn("goal", "Vuole correre una mezza maratona, forse")
        episode = brain.learn("episode", "[#41] Alex: i link di Grafana non si vedono tutti\n[#43] Alex: lo so già")
        brain.ensure(vocabulary.TERM, "Grafana")
        mia = access.create_person(conn, "Mia", "member", "it")
        hers = Brain(conn, mia.id).learn("fact", "Abita a Roma")
        _chat(conn, owner.id, (41, "in", "i link di Grafana non si vedono tutti"), (42, "out", "Ok"),
              (43, "in", "lo so già, quella funzione di Grafana non è finita"), at=time.time() - 60)

        reviewer = review.Reviewer(worker._cfg)
        reviewer._runner = FakeRunner(json.dumps({
            "merge": [{"keep": a, "drop": [b, hers, episode], "content": "Preferisce risposte brevi e messaggi corti"}],
            "revise": [{"id": a, "content": "doppio uso"}, {"id": c, "content": "Lavora a turni in un bar"}],
            "forget": [{"id": d, "why": "detto per scherzo"}, {"id": episode, "why": "x"}],
            "learn": [{"kind": "fact", "content": "La funzione dei link di Grafana non è completa", "messages": [43]},
                      {"kind": "secret", "content": "x"}],
            "summary": "Ho unito due preferenze e imparato che i link di Grafana sono incompleti.",
        }))
        assert reviewer.run(conn, time.time()) == ["Ho unito due preferenze e imparato che i link di Grafana sono incompleti."]

        prompt = reviewer._runner.prompts[0]
        assert f"#{c} [fact] Lavora a turni" in prompt and "[#43]" in prompt and "lo so già" in prompt
        assert "Abita a Roma" not in prompt and "[episode]" not in prompt
        assert brain.get(a).content == "Preferisce risposte brevi e messaggi corti" and brain.get(b) is None
        assert brain.get(c).content == "Lavora a turni in un bar" and brain.get(d) is None
        assert brain.get(episode) is not None and Brain(conn, mia.id).get(hers) is not None
        learned = next(r for r in brain.top("fact") if "Grafana" in r.content)
        assert episode in {r.id for r in brain.recall([learned.id])}
        [record] = (reviewer.folder).glob("*-1.json")
        logged = json.loads(record.read_text(encoding="utf-8"))
        assert logged["merged"][0]["replaced"] == ["Preferisce risposte brevi", "Vuole messaggi corti"]
        assert logged["forgot"] == [{"content": "Vuole correre una mezza maratona, forse", "why": "detto per scherzo"}]
        assert (reviewer.folder / "last.txt").read_text(encoding="utf-8").startswith("Ho unito")
        assert store.get_state(conn, "review_last") is not None


def test_an_answer_that_cannot_be_read_changes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        kept = Brain(conn, owner.id).learn("fact", "Lavora a turni")
        reviewer = review.Reviewer(worker._cfg)
        reviewer._runner = FakeRunner("non so")
        assert reviewer.run(conn, time.time()) == [] and Brain(conn, owner.id).get(kept) is not None
        assert (reviewer.folder / "last.txt").read_text(encoding="utf-8") == ""


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
