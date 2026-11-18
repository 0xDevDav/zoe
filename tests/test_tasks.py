"""Tasks programs hand to Zoe: a turn of hers, or a note for the mind already at work on the same thing."""

import html
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FAKE_FAILURE, OWNER, message, worker_setup
from zoe import minds, observer, review, say, store, tasks
from zoe.brain import Brain
from zoe.claude_runner import Result
from zoe.db import connect

ALERT = "Il controllo del sito segnala che la regola «menu» non trova più il suo punto in menu-Ab12Cd34.js. " \
        "Avvia una mente che la riallinei e di' ad Alex cosa può non funzionare."
AGAIN = "La regola «menu» ha perso il suo punto anche in menu-Ef56Gh78.js."
STARTS_A_MIND = ("Il menu del sito può non funzionare: lo sto già sistemando 🛠️\n"
                 "[[{k} mind: Riallineare il menu | ]]\nRiallinea la regola «menu» su menu-Ab12Cd34.js.\n[[/mind]]")


def _task(conn, owner, text: str = ALERT, keys: tuple[str, ...] = ("site:menu",)) -> int:
    return tasks.queue(conn, OWNER, owner.id, text, "site check", list(keys))


def _turn(worker, conn) -> None:
    worker._handle(conn, store.claim_next(conn))


def _states(conn) -> list[str]:
    return [r["state"] for r in conn.execute("SELECT state FROM messages WHERE direction = 'in' ORDER BY id")]


def test_a_task_gives_zoe_a_turn_and_the_mind_she_starts_takes_its_keys_on():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), STARTS_A_MIND)
        _task(conn, owner)
        _turn(worker, conn)

        assert f"The program «site check» on this machine hands you a task: {ALERT}" in runner.prompts[0]
        assert "a later task on the same thing goes to it as a note" in runner.prompts[0]
        assert api.to(OWNER) == ["Il menu del sito può non funzionare: lo sto già sistemando 🛠️"]
        assert api.actions == [], "no progress shows for a task while nobody writes"
        (mind,) = minds.active(conn, owner.id)
        assert mind.title == "Riallineare il menu"
        assert minds.holding(conn, owner.id, ["site:menu"]) == mind.id
        assert _states(conn) == ["answered"]
        episodes = [r.content for r in Brain(conn, owner.id).top("episode")]
        assert any(f"Program «site check»: {ALERT}" in e for e in episodes)


def test_a_task_on_what_a_working_mind_holds_goes_to_it_with_no_turn_and_no_message():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), STARTS_A_MIND, on_disk=True)
        _task(conn, owner)
        _turn(worker, conn)
        (mind,) = minds.active(conn, owner.id)
        conn.execute("UPDATE minds SET state = 'running' WHERE id = ?", (mind.id,))

        _task(conn, owner, AGAIN, ("site:menu", "site:footer"))
        _turn(worker, conn)

        assert len(runner.prompts) == 1 and len(api.to(OWNER)) == 1, "no second turn and no second message"
        assert minds.take_notes(conn, mind.id) == [f"The program «site check» sent a new task on your work: {AGAIN}"]
        assert minds.holding(conn, owner.id, ["site:footer"]) == mind.id, "the mind takes the new key on too"
        last = conn.execute("SELECT state, payload FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        assert last["state"] == "answered" and json.loads(last["payload"])["mind"] == mind.id
        assert len(minds.active(conn, owner.id)) == 1


def test_a_mind_waiting_for_an_answer_keeps_waiting_and_reads_the_task_with_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), STARTS_A_MIND, on_disk=True)
        _task(conn, owner)
        _turn(worker, conn)
        (mind,) = minds.active(conn, owner.id)
        conn.execute("UPDATE minds SET state = 'asking', question = 'Tengo il colore vecchio?' WHERE id = ?",
                     (mind.id,))

        _task(conn, owner, AGAIN)
        _turn(worker, conn)

        waiting = minds.get(conn, mind.id)
        assert waiting.state == "asking" and waiting.question == "Tengo il colore vecchio?"
        assert minds.tell(conn, owner.id, mind.id, "Sì")
        assert minds.take_notes(conn, mind.id) == [
            f"The program «site check» sent a new task on your work: {AGAIN}", "Sì"]
        assert len(runner.prompts) == 1


def test_once_the_mind_ends_the_next_task_on_the_same_thing_is_a_turn_again():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), STARTS_A_MIND, "[[{k} skip: già sistemato]]")
        _task(conn, owner)
        _turn(worker, conn)
        (mind,) = minds.active(conn, owner.id)
        conn.execute("UPDATE minds SET state = 'done' WHERE id = ?", (mind.id,))

        _task(conn, owner, AGAIN)
        _turn(worker, conn)

        assert len(runner.prompts) == 2 and f"hands you a task: {AGAIN}" in runner.prompts[1]
        assert len(api.to(OWNER)) == 1, "a task's turn may send nothing"
        assert _states(conn) == ["answered", "answered"]


class _Slow:
    """Zoe's turn on the first task waits until the test lets it go, then starts a mind."""

    def __init__(self) -> None:
        self.prompts, self.started, self.go = [], threading.Event(), threading.Event()

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0, model="",
            effort="") -> Result:
        self.prompts.append(prompt_text)
        self.started.set()
        self.go.wait(10)
        key = prompt_text.split("Your key for this turn is ", 1)[1].split(".", 1)[0]
        return Result(True, STARTS_A_MIND.replace("{k}", key), False, False, 0, 1.0, session_id="s-1")


def test_a_task_that_comes_during_the_turn_of_another_on_the_same_thing_waits_for_its_mind():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp), on_disk=True)
        worker._runner = runner = _Slow()
        _task(conn, owner)
        first = threading.Thread(target=_turn, args=(worker, connect(Path(tmp) / "zoe.db")))
        first.start()
        assert runner.started.wait(10)

        second_id = _task(conn, owner, AGAIN)
        second = conn.execute("SELECT * FROM messages WHERE id = ?", (second_id,)).fetchone()
        claimed = store.Message(second_id, OWNER, owner.id, None, AGAIN, tasks.TASK, json.loads(second["payload"]), 1)
        other = threading.Thread(target=worker._handle, args=(connect(Path(tmp) / "zoe.db"), claimed))
        other.start()
        other.join(0.5)
        assert other.is_alive(), "the second task waits while the first one's turn runs"
        runner.go.set()
        first.join(10)
        other.join(10)

        (mind,) = minds.active(conn, owner.id)
        assert len(runner.prompts) == 1 and len(api.to(OWNER)) == 1
        assert minds.take_notes(conn, mind.id) == [f"The program «site check» sent a new task on your work: {AGAIN}"]


def test_a_task_on_another_thing_does_not_wait_for_the_turn_of_a_task_on_the_first():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp), on_disk=True)
        worker._runner = runner = _Slow()
        _task(conn, owner)
        first = threading.Thread(target=_turn, args=(worker, connect(Path(tmp) / "zoe.db")))
        first.start()
        assert runner.started.wait(10)

        other_id = _task(conn, owner, "Il modulo dei contatti non trova più il suo punto.", ("site:contacts",))
        claimed = store.Message(other_id, OWNER, owner.id, None, "", tasks.TASK, {"keys": ["site:contacts"]}, 1)
        decided = threading.Event()

        def decide() -> None:
            with tasks.deciding(claimed):
                decided.set()

        threading.Thread(target=decide, daemon=True).start()
        assert decided.wait(2) and first.is_alive(), "it goes on while the first task's turn still runs"
        runner.go.set()
        first.join(10)


def test_a_task_never_joins_the_turn_of_the_persons_message_and_stays_out_of_the_chat():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), "Ciao! 👋", STARTS_A_MIND)
        store.record_inbound(conn, message(5, OWNER, "ciao"), owner.id)
        _task(conn, owner)
        _turn(worker, conn)
        assert ALERT not in runner.prompts[0] and _states(conn) == ["answered", "received"]
        _turn(worker, conn)
        assert all(ALERT not in turn["text"] for turn in store.recent_history(conn, OWNER))
        assert all(ALERT not in row["text"] for row in observer.unread(conn, owner.id))
        assert all(ALERT not in row["text"] for row in review.conversations(conn, owner.id, 0))


def test_a_task_whose_turn_keeps_failing_reaches_the_person_as_it_is():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), None, None)
        _task(conn, owner)
        _turn(worker, conn)
        _turn(worker, conn)
        assert FAKE_FAILURE not in "".join(api.to(OWNER))
        assert api.to(OWNER) == [
            f"⚙️ <b>site check</b> mi ha chiesto questo, ma non ci sono riuscita:\n{html.escape(ALERT)}"]


def test_zoe_say_hands_a_task_with_its_keys_to_an_owners_chat_only():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        conn, owner, *_ = worker_setup(home, on_disk=True)
        env = {"ZOE_HOME": str(home), "ZOE_ENV_FILE": str(home / "none.env"), "ZOE_OWNER_IDS": str(OWNER)}
        with _environment(env):
            assert say.main(["--task", "--from", "site check", "--key", "site:menu", "--key", "site:menu", ALERT]) == 0
            for wrong in (["--task", "--from", "site check", "--chat", "999", "x"], ["--key", "site:menu", "x"],
                          ["--task", "--from", "site check", "--silent", "x"], ["--task", "x"]):
                try:
                    say.main(wrong)
                except SystemExit as exc:
                    assert exc.code == 2
                else:
                    raise AssertionError(f"zoe-say {wrong} should refuse")
        row = conn.execute("SELECT * FROM messages").fetchone()
        assert (row["chat_id"], row["kind"], row["text"]) == (OWNER, tasks.TASK, ALERT)
        assert json.loads(row["payload"]) == {"origin": "site check", "keys": ["site:menu"]}
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


class _environment:
    def __init__(self, values: dict) -> None:
        self.values, self.saved = values, {}

    def __enter__(self) -> None:
        import os
        for name, value in self.values.items():
            self.saved[name] = os.environ.get(name)
            os.environ[name] = value

    def __exit__(self, *exc) -> None:
        import os
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
