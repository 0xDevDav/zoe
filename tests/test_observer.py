"""Checks for the observer: who is observed, batches, reading its answer, storing it, and the menu."""

import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FAKE_FAILURE, OWNER, FakeAPI, FakeRunner, logged, targets, worker_setup
from zoe import access, observer, prompt, signals, store, vocabulary
from zoe.brain import Brain
from zoe.ingest import Poller
from zoe.menu import Menu
from zoe.observer import Observer


def _chat(conn: sqlite3.Connection, person_id: int, *lines: tuple[int, str, str], at: float = 0.0) -> None:
    """Log a finished exchange: (telegram id, 'in' or 'out', text) per message."""
    for telegram_id, direction, text in lines:
        conn.execute(
            "INSERT INTO messages (chat_id, person_id, telegram_id, direction, text, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'answered', ?, ?)",
            (OWNER, person_id, telegram_id, direction, text, at or time.time(), at or time.time()),
        )


def test_owners_are_observed_unless_they_stop_and_others_only_when_asked():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        mia = access.create_person(conn, "Mia", "member", "it")
        assert observer.enabled(conn, owner) and not observer.enabled(conn, mia)
        observer.set_enabled(conn, owner.id, False)
        observer.set_enabled(conn, mia.id, True)
        assert not observer.enabled(conn, owner) and observer.enabled(conn, mia)


def test_a_batch_waits_for_a_pause_and_for_messages_still_in_progress():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        now = time.time()
        _chat(conn, owner.id, (1, "in", "ciao"), (2, "out", "ciao!"), at=now - 60)
        assert observer.unread(conn, owner.id) and not observer.ready(observer.unread(conn, owner.id), now, 600)
        assert observer.ready(observer.unread(conn, owner.id), now + 600, 600)

        store.enqueue(conn, OWNER, owner.id, "ancora in coda", "text")
        _chat(conn, owner.id, (3, "in", "dopo"), at=now - 60)
        assert [r["telegram_id"] for r in observer.unread(conn, owner.id)] == [1, 2]


def test_the_answer_is_read_even_with_words_around_it():
    found_signals, found_memories = observer.parse('Ecco:\n{"signals": [{"kind": "mood"}], "memories": []}\nFine.')
    assert found_signals == [{"kind": "mood"}] and found_memories == []
    for broken in ("niente", "[1, 2]", "{rotto"):
        try:
            observer.parse(broken)
        except ValueError:
            continue
        raise AssertionError(broken)


def test_findings_become_readings_and_memories_wired_to_their_conversation():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        brain = Brain(conn, owner.id)
        episode = brain.learn("episode", "[#41] Alex: Sono distrutto, il turno al bar è stato infinito\n[#42] Zoe: Riposati")
        known = brain.learn("habit", "Va in palestra il lunedì")
        mia = access.create_person(conn, "Mia", "member", "it")
        hers = Brain(conn, mia.id).learn("fact", "Abita a Roma")
        _chat(conn, owner.id, (41, "in", "Sono distrutto"), (42, "out", "Riposati"), at=1000.0)
        rows = observer.unread(conn, owner.id)

        kept = observer.apply(conn, owner, rows, [
            {"kind": "mood", "value": 2, "note": "stanco dopo il turno", "message": 41},
            {"kind": "mood", "value": 9}, {"kind": "hunger", "value": 3},
        ], [
            {"kind": "fact", "content": "Lavora a turni in un bar", "replaces": None, "messages": [41]},
            {"kind": "habit", "content": "Va in palestra il lunedì e il giovedì", "replaces": known},
            {"kind": "fact", "content": "Abita a Milano", "replaces": hers},
            {"kind": "fact", "content": "Lavora a turni in un bar"},
            {"kind": "secret", "content": "x"},
        ])
        assert kept == (1, 4)
        [trend] = signals.trends(conn, owner.id, now=1000.0)
        assert (trend.kind, trend.last, trend.last_note, trend.last_at) == ("mood", 2.0, "stanco dopo il turno", 1000.0)
        assert brain.get(known).content == "Va in palestra il lunedì e il giovedì"
        assert Brain(conn, mia.id).get(hers).content == "Abita a Roma"
        facts = [r for r in brain.top("fact") if r.content == "Lavora a turni in un bar"]
        assert len(facts) == 1 and episode in {r.id for r in brain.recall([facts[0].id])}


def test_memories_from_one_conversation_are_wired_together_and_to_the_words_they_name():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        brain = Brain(conn, owner.id)
        term = brain.ensure(vocabulary.TERM, "Kubernetes")
        _chat(conn, owner.id, (41, "in", "Sto migrando tutto su Kubernetes, e vado in palestra il lunedì"), at=1000.0)
        observer.apply(conn, owner, observer.unread(conn, owner.id), [], [
            {"kind": "fact", "content": "Sta migrando i suoi servizi su Kubernetes", "messages": [41]},
            {"kind": "habit", "content": "Va in palestra il lunedì", "messages": [41]},
        ])
        work = next(r.id for r in brain.top("fact") if "Kubernetes" in r.content)
        gym = next(r.id for r in brain.top("habit"))
        assert work in {r.id for r in brain.recall([term])}
        assert gym in {r.id for r in brain.recall([work])}


def test_the_observer_reads_a_paused_conversation_once_and_retries_a_failed_run():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        _chat(conn, owner.id, (7, "in", "Domani ho il colloquio, sono agitato"), (8, "out", "Andrà bene"), at=1000.0)
        watcher = Observer(worker._cfg, threading.Event())
        watcher._runner = FakeRunner(None, json.dumps({
            "signals": [{"kind": "stress", "value": 4, "note": "colloquio", "message": 7}],
            "memories": [{"kind": "goal", "content": "Cerca un nuovo lavoro", "replaces": None, "messages": [7]}],
        }))
        with logged("zoe.observer") as lines:
            assert not watcher.observe(conn, owner, 2000.0)
        assert any(FAKE_FAILURE in line for line in lines), lines
        assert observer.unread(conn, owner.id) and not watcher.observe(conn, owner, 2100.0)
        assert watcher.observe(conn, owner, 2000.0 + 900.0)
        assert "[#7]" in watcher._runner.prompts[-1] and "Alex: Domani ho il colloquio" in watcher._runner.prompts[-1]
        assert observer.unread(conn, owner.id) == [] and observer.counts(conn, owner.id) == {"goal": 1}
        assert [(t.kind, t.last) for t in signals.trends(conn, owner.id, now=2900.0)] == [("stress", 4.0)]

        signals.record(conn, owner.id, "mood", 2, observer.SOURCE, "giornata pesante")
        text, _ = prompt.build(conn, Brain(conn, owner.id), owner, OWNER, "ciao", Path(tmp))
        assert "mood over the last 7 days: 2.0/5" in text and "(giornata pesante)" in text
        assert "[goal] Cerca un nuovo lavoro" in text


def test_a_reaction_to_zoe_reaches_the_observer_and_the_next_turns_without_a_turn_of_its_own():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        _chat(conn, owner.id, (41, "in", "Riassumi la riunione"), (42, "out", "Ecco il riassunto\ncon i punti chiave"),
              at=1000.0)
        poller = Poller(worker._cfg, FakeAPI(), threading.Event())

        def reaction(update_id, message_id, *emoji, user=OWNER):
            return {"update_id": update_id, "message_reaction": {
                "chat": {"id": OWNER, "type": "private"}, "message_id": message_id, "user": {"id": user},
                "new_reaction": [{"type": "emoji", "emoji": e} for e in emoji]}}

        poller.process(conn, reaction(1, 42, "👍"))
        for ignored in (reaction(2, 41, "👍"), reaction(3, 42), reaction(4, 42, "👎", user=999), reaction(1, 42, "👍")):
            poller.process(conn, ignored)
        assert store.claim_next(conn) is None
        texts = [r["text"] for r in observer.unread(conn, owner.id)]
        assert texts[-1] == "[reaction 👍 to Zoe's message #42: «Ecco il riassunto con i punti chiave»]"
        assert len(texts) == 3 and store.recent_history(conn, OWNER)[-1]["text"] == texts[-1]


def test_the_menu_shows_what_was_learned_and_forgets_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        memory = Brain(conn, owner.id).learn("preference", "Preferisce risposte brevi")
        signals.record(conn, owner.id, "mood", 4, observer.SOURCE, "contento")
        menu = Menu(worker._cfg, FakeAPI())

        view = menu.answer(conn, owner, {"view": "known"})
        [stats] = [block["stats"] for block in view["blocks"] if "stats" in block]
        assert [(s["label"], s["value"]) for s in stats] == [("Umore", "4/5")] and "learned:preference" in targets(view)
        assert targets(menu.answer(conn, owner, {"view": "learned:preference"})) == [f"memory:{memory}"]
        view = menu.answer(conn, owner, {"action": f"forget:{memory}"})
        assert view["notice"] == "Dimenticato." and observer.counts(conn, owner.id) == {}


def test_observation_is_switched_by_the_person_or_by_an_owner_only():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        api = FakeAPI()
        poller = Poller(worker._cfg, api, threading.Event())
        poller.process(conn, {"update_id": 1, "message": {
            "message_id": 1, "chat": {"id": 333, "type": "private"}, "from": {"id": 333, "first_name": "Mia"},
            "text": "ciao"}})
        mia = access.approve(conn, access.pending(conn)[0].request_code, "Mia", "it")[0]
        menu = Menu(worker._cfg, api)

        assert menu.answer(conn, mia, {"action": f"observe:{owner.id}"})["notice"] == (
            "Questa funzione è riservata al proprietario.")
        assert observer.enabled(conn, owner)
        view = menu.answer(conn, owner, {"action": f"observe:{mia.id}"})
        assert observer.enabled(conn, mia) and view["notice"] == "Osservazione attiva." and view["at"] == f"people:{mia.id}"
        menu.answer(conn, mia, {"action": f"observe:{mia.id}"})
        assert not observer.enabled(conn, mia)


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
