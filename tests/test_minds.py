"""Checks for parallel minds: starting one, its news reaching the chat, questions, notes, failures, stops."""

import dataclasses
import json
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OTHER_OWNER_ACCOUNTS, OWNER, FakeAPI, FakeRunner, message, press, shown, targets, worker_setup
from test_runner import ENV_CLAUDE, _claude, no_inherited_limit
from zoe import access, app, claude_runner, minds, plugins, store, strength
from zoe.brain import Brain
from zoe.claude_runner import ClaudeRunner, Result, Step
from zoe.config import Config
from zoe.db import connect
from zoe.ingest import Poller
from zoe.menu import Menu
from zoe.minds import Minds

BRIEF = "[[{k} mind: Sito pizzeria]]\nCrea un sito statico con menu [prezzi] e contatti.\nVerifica che si apra.\n[[/mind]]"


def _ask(conn, worker, person_id: int, text: str) -> None:
    store.enqueue(conn, OWNER, person_id, text, "text")
    worker._handle(conn, store.claim_next(conn))


def _engine(worker, runner) -> Minds:
    engine = Minds(worker._cfg, threading.Event())
    engine._runner = runner
    return engine


def _run_mind(conn, engine: Minds) -> int:
    mind_id = minds.claim(conn)
    engine.work(mind_id)
    return mind_id


def _command(command: str) -> Step:
    """A Bash command a mind launches."""
    return Step("tool", "Bash", {"command": command})


def _lines(*lines: str) -> Step:
    """The command a mind tells with while it works, as its instructions write it."""
    return _command("cat <<'ZOE'\n" + "\n".join(lines) + "\nZOE")


def _news(key: str, text: str) -> Step:
    return _lines(f"[[{key} news: {text}]]")


def _told(conn) -> list[tuple[str, str]]:
    """What the minds queued for the chat, each as its event and text, oldest first."""
    return [(json.loads(row["payload"])["event"], row["text"])
            for row in conn.execute("SELECT payload, text FROM messages WHERE kind = 'mind' ORDER BY id")]


def test_a_long_request_starts_a_mind_and_only_owners_can():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), f"Ci lavora una mente parallela 🔀\n{BRIEF}", BRIEF)
        _ask(conn, worker, owner.id, "Fammi un sito per la pizzeria di Marco")
        assert "your self-improvement skill" in runner.prompts[0]
        assert ("A short brief: the result expected" in runner.prompts[0]
                and "who tries the result, and the one or two checks that matter" in runner.prompts[0]
                and "list no cases to test for what the person tries for themselves in seconds" in runner.prompts[0]
                and f"ask for full tests only where a mistake costs: {strength.COSTLY}" in runner.prompts[0])
        assert api.to(OWNER) == ["Ci lavora una mente parallela 🔀"]
        [mind] = minds.active(conn, owner.id)
        poller = Poller(worker._cfg, api, threading.Event())
        poller.process(conn, press(52, OWNER, f"r:halt:{mind.id}"))
        assert minds.get(conn, mind.id).state == "stopped"
        conn.execute("UPDATE minds SET state = 'queued' WHERE id = ?", (mind.id,))
        [mind] = minds.active(conn, owner.id)
        assert (mind.title, mind.state) == ("Sito pizzeria", "queued") and "[prezzi]" in mind.brief
        assert Path(mind.workdir) == Path(tmp) / "minds" / str(mind.id) and Path(mind.workdir).is_dir()
        recalled = {r.content for r in Brain(conn, owner.id).recall([mind.neuron_id])}
        assert any("pizzeria di Marco" in content for content in recalled)

        mia = access.create_person(conn, "Mia", "member", "it")
        _ask(conn, worker, mia.id, "Fammi un sito")
        assert minds.active(conn, mia.id) == [] and "self-improvement" not in runner.prompts[-1]


def test_minds_at_work_leave_the_chat_to_the_conversation():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(
            Path(tmp), f"Ci lavora una mente 🔀\n{BRIEF}", "Va bene.", "Glielo dico.\n[[{k} tell: 1 | anche i contatti]]")
        for update_id, text in ((5, "Fammi un sito"), (6, "Come va?"), (7, "Digli di aggiungere i contatti")):
            store.record_inbound(conn, message(update_id, OWNER, text), owner.id)
            worker._handle(conn, store.claim_next(conn))
        assert api.to(OWNER) == ["Ci lavora una mente 🔀", "Va bene.", "Glielo dico."]
        assert api.reactions == [(OWNER, 7, "✍")] and minds.get(conn, 1).notes == ["anche i contatti"]


def test_a_finished_mind_reports_through_the_chat_and_a_reply_leads_back_to_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(
            Path(tmp), "Il sito è pronto, te lo apro? ✅", "Certo, eccolo.", on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito pizzeria", "Crea il sito", "", [], Path(tmp))
        _run_mind(conn, _engine(worker, FakeRunner("Fatto: il sito è in /srv/pizzeria, verificato nel browser.")))
        assert minds.get(conn, mind_id).state == "done"

        worker._handle(conn, store.claim_next(conn))
        assert f"Your parallel mind #{mind_id} «Sito pizzeria» has finished" in runner.prompts[-1]
        assert "/srv/pizzeria" in runner.prompts[-1] and "what was done, where the result is" in runner.prompts[-1]
        assert api.to(OWNER)[-1] == "Il sito è pronto, te lo apro? ✅"
        assert not any("/srv/pizzeria" in turn["text"] for turn in store.recent_history(conn, OWNER))

        sent = len(api.sent) + 100
        update = {"update_id": 9, "message": {"message_id": 9, "chat": {"id": OWNER, "type": "private"},
                  "from": {"id": OWNER}, "text": "sì", "reply_to_message": {"message_id": sent}}}
        store.record_inbound(conn, update, owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert f"is replying to a message of yours #{sent}" in runner.prompts[-1]
        conn.close()


def test_a_mind_is_told_to_study_and_check_in_proportion_to_what_a_mistake_costs():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Bottone", "Aggiungi il bottone Aggiorna", "", [],
                    Path(tmp))
        runner = FakeRunner("Fatto.")
        _run_mind(conn, _engine(worker, runner))
        told = runner.prompts[0]
        assert "in proportion to what a mistake would cost and how soon Alex would see it" in told, told
        assert "Read what the change touches, not everything around it" in told, told
        assert "you build, open once and hand over, saying what you did not try" in told, told
        assert f"Full tests are for where a mistake costs: {strength.COSTLY}." in told, told
        assert "test rig larger than the change" in told and "do not keep repairing it" in told, told
        assert "what you checked and what you did not" in told and "careful professional" not in told, told
        conn.close()


def test_a_mind_runs_at_xhigh_with_no_call_to_weigh_its_brief_and_keeps_its_choice_when_it_resumes():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        conn, owner, worker, *_ = worker_setup(home, on_disk=True)
        marker = home / "claude-started.txt"
        noting = (f"import sys\nsys.stdin.read()\n"
                  f"open({str(marker)!r}, 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n")
        worker._cfg = dataclasses.replace(worker._cfg, claude_bin=_claude(home, noting))
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Bottone", "Aggiungi il bottone Aggiorna", "",
                              [], home)
        runner = FakeRunner("[[{k} ask: Verde o blu?]]", "Fatto.")
        engine = _engine(worker, runner)
        _run_mind(conn, engine)
        assert minds.tell(conn, owner.id, mind_id, "Verde")
        _run_mind(conn, engine)
        assert not marker.exists(), marker.read_text(encoding="utf-8")
        assert runner.models == [(worker._cfg.claude_model, "xhigh")] * 2, runner.models
        rows = conn.execute("SELECT level, effort, judge, judge_level FROM model_choices WHERE source = 'mind' "
                            "AND mind_id = ?", (mind_id,)).fetchall()
        assert [tuple(r) for r in rows] == [(5, "xhigh", "", None)], [tuple(r) for r in rows]
        conn.close()


def test_a_mind_that_resumes_from_a_row_of_another_ladder_reaches_claude_on_the_one_model_below_max():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Resoconto", "Prepara il resoconto", "", [],
                              Path(tmp))
        runner = FakeRunner("[[{k} ask: Di quale mese?]]", "Fatto.")
        engine = _engine(worker, runner)
        _run_mind(conn, engine)
        conn.execute("UPDATE model_choices SET level = 6, model = 'other-model', effort = 'max' WHERE mind_id = ?",
                     (mind_id,))
        assert minds.tell(conn, owner.id, mind_id, "Marzo")
        _run_mind(conn, engine)
        model = worker._cfg.claude_model
        assert "--resume" in runner.profiles[1].args and runner.models[1] == (model, "xhigh"), runner.models
        rows = conn.execute("SELECT level, model, effort, outcome FROM model_choices WHERE mind_id = ? ORDER BY id",
                            (mind_id,)).fetchall()
        assert [tuple(r) for r in rows] == [(6, "other-model", "max", "answered"),
                                            (5, model, "xhigh", "answered")], [tuple(r) for r in rows]
        conn.close()


def test_a_question_waits_for_the_answer_then_the_same_session_goes_on():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = worker_setup(
            Path(tmp), "Ti chiede una cosa: fornitori solo italiani?\n[[{k} options: Solo Italia | Anche UE]]",
            "Glielo dico.", on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Fornitori", "Trova fornitori", "", [], Path(tmp))
        mind_runner = FakeRunner("Ho trovato 12 nomi.\n[[{k} ask: Solo fornitori italiani o anche UE?]]", "Lista pronta.")
        engine = _engine(worker, mind_runner)
        _run_mind(conn, engine)
        mind = minds.get(conn, mind_id)
        assert (mind.state, mind.question, mind.session_id) == ("asking", "Solo fornitori italiani o anche UE?", "session-1")
        assert "Never wait for something to happen on its own" in mind_runner.prompts[0]

        worker._handle(conn, store.claim_next(conn))
        assert re.search(rf"\[\[\w+ tell: {mind_id} \| the answer\]\]", runner.prompts[-1])
        _ask(conn, worker, owner.id, "Solo Italia")
        assert "waiting for an answer to: Solo fornitori italiani" in runner.prompts[-1]
        assert minds.tell(conn, owner.id, mind_id, "Solo Italia") and minds.get(conn, mind_id).state == "queued"

        _run_mind(conn, engine)
        assert "Zoe passes this on from Alex: Solo Italia" in mind_runner.prompts[-1]
        args = mind_runner.profiles[-1].args
        assert args[args.index("--resume") + 1] == "session-1"
        assert minds.get(conn, mind_id).state == "done"
        conn.close()


def test_a_failed_run_is_resumed_once_and_then_reported():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Report", "Prepara il report", "", [], Path(tmp))
        mind_runner = FakeRunner(None, None)
        _run_mind(conn, _engine(worker, mind_runner))
        assert "previous run failed (exit code 1)" in mind_runner.prompts[-1]
        assert minds.get(conn, mind_id).state == "failed"
        event = store.claim_next(conn)
        assert (event.kind, event.payload["event"], event.text) == ("mind", "failed", "failed (exit code 1)")
        conn.close()



class OutOfUsage:
    """A run that meets the plan's session limit, which resets two hours from now, then works once it resets."""

    def __init__(self) -> None:
        reset = datetime.now().astimezone() + timedelta(hours=2)
        self.limit = f"You've hit your session limit · resets {reset:%I:%M%p}"
        self.prompts: list[str] = []
        self.profiles: list[object] = []
        self.models: list[tuple[str, str]] = []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0, model="", effort="") -> Result:
        self.prompts.append(prompt_text)
        self.profiles.append(profile)
        self.models.append((model, effort))
        if len(self.prompts) == 1:
            return Result(False, self.limit, False, False, 1, 0.5, session_id="s-1", failure=self.limit)
        return Result(True, "Fatto: report pronto.", False, False, 0, 1.0, session_id="s-1")


def test_a_mind_that_meets_a_usage_limit_waits_queued_and_resumes_its_session_after():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp), on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Report", "Prepara il report", "", [], Path(tmp))
        runner = OutOfUsage()
        engine = _engine(worker, runner)
        _run_mind(conn, engine)
        mind = minds.get(conn, mind_id)
        assert (mind.state, mind.session_id, mind.attempts) == ("queued", "s-1", 0)
        assert store.claim_next(conn) is None
        assert float(store.get_state(conn, claude_runner.RETRY_AT)) > time.time()

        engine._start(conn)
        assert engine._working == [] and minds.get(conn, mind_id).state == "queued"
        notices = [r["text"] for r in conn.execute("SELECT text FROM outbox WHERE chat_id = ?", (OWNER,))]
        assert len(notices) == 1 and "riprendo alle <b>" in notices[0]

        store.set_state(conn, claude_runner.RETRY_AT, "0")
        _run_mind(conn, engine)
        assert "--resume" in runner.profiles[1].args and "s-1" in runner.profiles[1].args
        assert minds.get(conn, mind_id).state == "done"
        assert runner.models == [(worker._cfg.claude_model, "xhigh")] * 2, runner.models
        kept = conn.execute("SELECT source, mind_id, outcome FROM model_choices").fetchall()
        assert [tuple(r) for r in kept] == [("mind", mind_id, "answered")]
        event = store.claim_next(conn)
        assert (event.kind, event.payload["event"]) == ("mind", "done")
        assert not any(t.startswith("🔀") for t in api.to(OWNER))
        conn.close()


def test_a_mind_that_meets_a_usage_limit_tells_only_the_owners_main_chat():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, _ = worker_setup(Path(tmp), on_disk=True, others=OTHER_OWNER_ACCOUNTS)
        minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Report", "Prepara il report", "", [], Path(tmp))
        engine = _engine(worker, OutOfUsage())
        _run_mind(conn, engine)
        store.set_state(conn, claude_runner.RETRY_AT, "0")
        _run_mind(conn, engine)
        notices = [(r["chat_id"], r["text"]) for r in conn.execute("SELECT chat_id, text FROM outbox ORDER BY id")]
        assert [chat for chat, _ in notices] == [OWNER, OWNER]
        assert "riprendo alle <b>" in notices[0][1] and "Claude funziona di nuovo" in notices[1][1]
        conn.close()


class NoteDuringRun:
    """A run that receives a note while working, then finishes on the resumed session."""

    def __init__(self, home: Path, owner_id: int, mind_id: int) -> None:
        self.home, self.owner_id, self.mind_id = home, owner_id, mind_id
        self.prompts: list[str] = []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0, model="", effort="") -> Result:
        self.prompts.append(prompt_text)
        if len(self.prompts) == 1:
            other = connect(self.home / "zoe.db")
            minds.tell(other, self.owner_id, self.mind_id, "Aggiungi anche la pagina contatti")
            other.close()
            interrupt.wait(5)
            return Result(False, "", False, False, None, 1.0, session_id="s-1", cancelled=True)
        return Result(True, "Fatto, con i contatti.", False, False, 0, 1.0, session_id="s-1")


def test_a_note_interrupts_the_run_and_resumes_its_session_with_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito", "Crea il sito", "", [], Path(tmp))
        every, minds._WATCH_EVERY = minds._WATCH_EVERY, 0.05
        runner = NoteDuringRun(Path(tmp), owner.id, mind_id)
        try:
            _run_mind(conn, _engine(worker, runner))
        finally:
            minds._WATCH_EVERY = every
        assert len(runner.prompts) == 2 and "Aggiungi anche la pagina contatti" in runner.prompts[1]
        assert minds.get(conn, mind_id).state == "done"
        conn.close()


class NoteThenNews:
    """A run that receives a note while working. Resumed with it, it does what the note asks, sends news of
    it and lets the chat take its turn while it is still at work, then finishes."""

    def __init__(self, home: Path, owner_id: int, mind_id: int, chat) -> None:
        self.home, self.owner_id, self.mind_id, self.chat = home, owner_id, mind_id, chat
        self.prompts: list[str] = []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0, model="", effort="") -> Result:
        self.prompts.append(prompt_text)
        if len(self.prompts) == 1:
            other = connect(self.home / "zoe.db")
            minds.tell(other, self.owner_id, self.mind_id, "Centra il titolo della pagina contatti")
            other.close()
            interrupt.wait(5)
            return Result(False, "", False, False, None, 1.0, session_id="s-1", cancelled=True)
        key = re.search(r"Your key for this turn is (\w+)\.", prompt_text).group(1)
        on_step(_news(key, "Titolo centrato in /srv/sito/contatti.html"))
        on_step(_news("000000", "a command without the key tells nothing"))
        self.chat()
        return Result(True, "Fatto: il sito è pronto.", False, False, 0, 1.0, session_id="s-1")


def test_a_small_request_noted_to_a_working_mind_is_told_before_its_work_ends():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, chat, api = worker_setup(Path(tmp), "Il titolo è centrato ✅", "Il sito è pronto ✅",
                                                      on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito", "Crea il sito", "", [], Path(tmp))
        during: list = []

        def chat_turn() -> None:
            during.append(minds.get(conn, mind_id).state)
            worker._handle(conn, store.claim_next(conn))
            during.append(api.to(OWNER))
            assert store.claim_next(conn) is None

        every, minds._WATCH_EVERY = minds._WATCH_EVERY, 0.05
        runner = NoteThenNews(Path(tmp), owner.id, mind_id, chat_turn)
        try:
            _run_mind(conn, _engine(worker, runner))
        finally:
            minds._WATCH_EVERY = every
        noted = runner.prompts[1]
        assert "Zoe passes this on from Alex: Centra il titolo della pagina contatti" in noted, noted
        assert "do it and check it first" in noted and "with a news command of its own" in noted, noted
        assert re.search(r"\[\[\w{6} news: what is done", noted), noted
        assert "do it and check it first" not in runner.prompts[0], runner.prompts[0]
        assert during == ["running", ["Il titolo è centrato ✅"]], during
        told = chat.prompts[0]
        assert f"Your parallel mind #{mind_id} «Sito» is still at work and sends this news:" in told, told
        assert "Titolo centrato in /srv/sito/contatti.html" in told and "never kept for its final report" in told
        assert "never that it comes with the rest of the work" in told, told

        assert minds.get(conn, mind_id).state == "done"
        worker._handle(conn, store.claim_next(conn))
        assert "has finished" in chat.prompts[1] and api.to(OWNER)[-1] == "Il sito è pronto ✅"
        late = minds._Steps(conn, minds.get(conn, mind_id), "it")
        late.step(_news(minds.get(conn, mind_id).directive_key, "Troppo tardi"))
        assert store.claim_next(conn) is None
        conn.close()


class ResultThenClosing:
    """A run that tells the result as soon as it stands, with the command its instructions give, lets the
    chat take its turn while the closing work is still ahead, then reports."""

    def __init__(self, chat) -> None:
        self.chat = chat
        self.prompts: list[str] = []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0, model="", effort="") -> Result:
        self.prompts.append(prompt_text)
        taught = re.search(r"written like this:\n(cat <<'ZOE'\n\[\[\w{6} news: )[^\]]+(\]\]\nZOE)\n", prompt_text)
        on_step(_command(taught.group(1) + "Sito online in /srv/pizzeria, manca il logo di Alex" + taught.group(2)))
        self.chat()
        return Result(True, "Sito online in /srv/pizzeria, manca il logo. Guida aggiornata, commit 1a2b3c.",
                      False, False, 0, 1.0, session_id="s-1")


def test_a_mind_tells_the_result_before_its_closing_work_and_its_report_then_only_adds_to_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, chat, api = worker_setup(Path(tmp), "Il sito è online ✅", "Guida e commit fatti ✅",
                                                      on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito", "Crea il sito", "", [], Path(tmp))
        during: list = []

        def chat_turn() -> None:
            worker._handle(conn, store.claim_next(conn))
            during.append((minds.get(conn, mind_id).state, api.to(OWNER)))

        runner = ResultThenClosing(chat_turn)
        _run_mind(conn, _engine(worker, runner))
        # No note is needed: the first prompt asks for the result at once and puts the closing work after it.
        [first] = runner.prompts
        assert "Zoe passes this on" not in first, first
        assert "As soon as what Alex asked for is done and checked" in first, first
        assert re.search(r"say so with a Bash command of its own, written like this:\ncat <<'ZOE'\n\[\[\w{6} news: "
                         r"what is done, where they see it, and what waits for them\]\]\nZOE\n", first), first
        assert "news written as text, outside a command, is never told" in first, first
        assert "never holds the result back" in first and "is a last phase of its own" in first, first
        assert during == [("running", ["Il sito è online ✅"])], during

        worker._handle(conn, store.claim_next(conn))
        news, done = chat.prompts
        assert "is still at work and sends this news:\nSito online in /srv/pizzeria" in news, news
        assert "has finished" in done and "commit 1a2b3c" in done, done
        assert "you already passed on this news of it:\n- Sito online in /srv/pizzeria, manca il logo di Alex\n" in done
        assert "only what the report adds" in done and "what was done, where the result is" not in done, done
        assert api.to(OWNER)[-1] == "Guida e commit fatti ✅"
        conn.close()


@contextmanager
def _clock():
    """The clock minds.py reads, moved forward by its ``ahead`` seconds instead of waited on, with a
    watcher that looks often."""
    clock = SimpleNamespace(ahead=0.0)
    real = minds.time, minds._WATCH_EVERY
    minds.time = SimpleNamespace(time=lambda: time.time() + clock.ahead,
                                 monotonic=lambda: time.monotonic() + clock.ahead)
    minds._WATCH_EVERY = 0.05
    try:
        yield clock
    finally:
        minds.time, minds._WATCH_EVERY = real


COMMIT = _command('cd /srv/pizzeria && git add -A && git commit -qm "Sito online"')
NEWS = _news("{k}", "Sito online in /srv/pizzeria, manca la guida")
WRITTEN = "[[{k} news: Sito online, scritto come testo]]"
NEWS_DUE = "You saved or published work some minutes ago and Alex has not been told anything yet."


class ClosingWork:
    """A run that follows a script, one list of moves for each time it starts or resumes: a step it
    takes or a line it says, with {k} for its key, something that happens meanwhile, the minutes it
    lets pass, or None where it fails. It stops where an interruption reaches it and keeps whether
    each run met one."""

    def __init__(self, clock, *runs: list) -> None:
        self.clock, self.runs = clock, list(runs)
        self.prompts: list[str] = []
        self.interrupted: list[bool] = []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0, model="", effort="") -> Result:
        self.prompts.append(prompt_text)
        key = re.search(r"Your key for this turn is (\w+)\.", prompt_text).group(1)
        for move in self.runs.pop(0):
            if isinstance(move, Step):
                on_step(Step(move.kind, move.tool, {name: value.replace("{k}", key) for name, value in move.args.items()}))
            elif isinstance(move, str):
                on_step(Step("said", args={"text": move.replace("{k}", key)}))
            elif callable(move):
                move()
            elif move is None:
                self.interrupted.append(False)
                return Result(False, "", False, False, 1, 1.0, session_id="s-1")
            else:
                self.clock.ahead += move * 60
                if interrupt.wait(0.6):
                    self.interrupted.append(True)
                    return Result(False, "", False, False, None, 1.0, session_id="s-1", cancelled=True)
        self.interrupted.append(False)
        return Result(True, "Sito online in /srv/pizzeria. Guida aggiornata.", False, False, 0, 1.0, session_id="s-1")


def _site(conn, owner, home: Path) -> int:
    return minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito", "Crea il sito", "", [], home)


def _asked(conn) -> int:
    """How many minds were asked for news of the work they saved."""
    return conn.execute("SELECT count(*) FROM minds WHERE reminded_at IS NOT NULL").fetchone()[0]


def test_a_mind_that_saved_its_work_and_tells_nothing_for_five_minutes_is_asked_for_its_news_once():
    with tempfile.TemporaryDirectory() as tmp, _clock() as clock:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = _site(conn, owner, Path(tmp))
        # News written as text is not told, so the mind that wrote it is asked like one that wrote nothing.
        runner = ClosingWork(clock, [WRITTEN, COMMIT, 5],
                             [COMMIT, 5, lambda: minds.tell(conn, owner.id, mind_id, "Aggiungi i contatti"), 0], [NEWS])
        _run_mind(conn, _engine(worker, runner))
        assert runner.interrupted == [True, True, False], runner.interrupted
        first, asked, noted = runner.prompts
        assert NEWS_DUE not in first, first
        assert asked.startswith(NEWS_DUE + " If what they asked for, or a part they can already use, works"), asked
        assert re.search(r"say it now with a Bash command of its own:\ncat <<'ZOE'\n\[\[\w{6} news: what is done, "
                         r"where they see it, what is still coming\]\]\nZOE\nThen carry on\. If only your report is left, "
                         r"skip the command and finish\.", asked), asked
        assert "interrupted by a restart" not in asked and "Zoe passes this on" not in asked, asked
        # Asked once: after the request, a second commit and five more silent minutes interrupt nothing, and
        # the note that then resumes the mind comes without the request.
        assert "Zoe passes this on from Alex: Aggiungi i contatti" in noted and NEWS_DUE not in noted, noted
        assert _told(conn) == [("news", "Sito online in /srv/pizzeria, manca la guida"),
                               ("done", "Sito online in /srv/pizzeria. Guida aggiornata.")], _told(conn)
        assert minds.get(conn, mind_id).state == "done" and _asked(conn) == 1

        # A run that fails with the request unanswered is resumed with it; one that answered it is not.
        for failing, again in (([None], True), ([NEWS, None], False)):
            runner = ClosingWork(clock, [COMMIT, 5], failing, [])
            _site(conn, owner, Path(tmp))
            _run_mind(conn, _engine(worker, runner))
            retried = runner.prompts[2]
            assert retried.startswith("Your previous run failed (exit code 1).\n"), retried
            assert (NEWS_DUE in retried) == again, retried
        conn.close()


def test_a_mind_that_told_its_news_or_ends_within_five_minutes_closes_its_work_undisturbed():
    with tempfile.TemporaryDirectory() as tmp, _clock() as clock:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        reading = Step("tool", "Bash", {"command": "cd /srv/pizzeria && git log --oneline | head -3"})
        for script in ([NEWS, COMMIT, 5], [COMMIT, 4], [reading, 5]):
            mind_id = _site(conn, owner, Path(tmp))
            runner = ClosingWork(clock, script)
            _run_mind(conn, _engine(worker, runner))
            assert runner.interrupted == [False] and len(runner.prompts) == 1, script
            assert minds.get(conn, mind_id).state == "done"

        # News sent before a note resumed the mind counts too: the new run is not asked for it again.
        mind_id = _site(conn, owner, Path(tmp))
        runner = ClosingWork(clock, [NEWS, lambda: minds.tell(conn, owner.id, mind_id, "Aggiungi i contatti"), 0],
                             [COMMIT, 5])
        _run_mind(conn, _engine(worker, runner))
        assert runner.interrupted == [True, False], runner.interrupted
        assert "Aggiungi i contatti" in runner.prompts[1] and NEWS_DUE not in runner.prompts[1], runner.prompts[1]
        assert _asked(conn) == 0
        conn.close()


def test_the_five_minutes_count_from_the_first_save_across_a_note_and_the_request_comes_with_it():
    with tempfile.TemporaryDirectory() as tmp, _clock() as clock:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = _site(conn, owner, Path(tmp))

        def note() -> None:
            minds.tell(conn, owner.id, mind_id, "Aggiungi i contatti")

        # A note two minutes after the commit resumes the mind with the note alone; three minutes on, it is asked.
        runner = ClosingWork(clock, [COMMIT, note, 2], [3], [NEWS])
        _run_mind(conn, _engine(worker, runner))
        assert runner.interrupted == [True, True, False], runner.interrupted
        _, noted, asked = runner.prompts
        assert "Zoe passes this on from Alex: Aggiungi i contatti" in noted and NEWS_DUE not in noted, noted
        assert asked.startswith(NEWS_DUE) and "Zoe passes this on" not in asked, asked

        # A note that reaches a mind already five minutes silent brings the request along, in one resumed run.
        mind_id = _site(conn, owner, Path(tmp))
        runner = ClosingWork(clock, [COMMIT, note, 5], [NEWS])
        _run_mind(conn, _engine(worker, runner))
        assert runner.interrupted == [True, False], runner.interrupted
        both = runner.prompts[1]
        assert both.startswith(NEWS_DUE) and "Zoe passes this on from Alex: Aggiungi i contatti" in both, both
        assert _asked(conn) == 2
        conn.close()


def test_news_is_told_from_a_command_the_mind_launches_with_its_key_and_from_nothing_else():
    with tempfile.TemporaryDirectory() as tmp, _clock() as clock:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = _site(conn, owner, Path(tmp))
        line = "[[{k} news: Menu online | si vede in /srv/pizzeria | manca il logo di Alex]]"
        long = 'Il menu è "online":\ncosta $5, si apre con `apri /srv/pizzeria` e non serve altro'
        seen: list = []

        def look() -> None:
            seen.append((minds.get(conn, mind_id).state, _told(conn)))

        runner = ClosingWork(clock, [
            # Written as text, alone or after a sentence, news is not told.
            line, "Il menu è online.\n" + line,
            # Nor is it without the mind's key, whatever the command quotes or prints.
            _news("000000", "Con la chiave di un altro"), _command("echo '[[news: Senza chiave]]'"),
            _command("cat appunti.txt; grep -rn 'news:' /srv/pizzeria"),
            # Only a command carries it: the same line in a file the mind writes tells nothing.
            Step("tool", "Write", {"file_path": "/srv/pizzeria/appunti.txt", "content": line}), look,
            # The command its instructions give tells it at once, with the mind still at work.
            _lines(line), look,
            # A command that carries the line in another shape tells it too, and a news of several lines
            # arrives whole, with its quotes, backticks and dollars as written.
            _command('cd /srv/pizzeria && echo "[[{k} news: Logo aggiunto]]"'), _news("{k}", long), look,
            # A mind stopped in the meantime sends no more news.
            lambda: minds.stop(conn, owner.id, mind_id), _news("{k}", "Troppo tardi"), look])
        _run_mind(conn, _engine(worker, runner))
        first = ("news", "Menu online | si vede in /srv/pizzeria | manca il logo di Alex")
        others = [("news", "Logo aggiunto"), ("news", long)]
        assert seen == [("running", []), ("running", [first]), ("running", [first, *others]),
                        ("stopped", [first, *others])], seen
        assert _told(conn) == [first, *others] and minds.get(conn, mind_id).state == "stopped"
        conn.close()


# A mind's run as Claude Code streams it. The mind prints a file that holds a news line with the mind's own
# key, writes one as text with a plan under it, sends its news with the command, the phase that starts
# then under it, works on for a while and reports.
NEWS_CLAUDE = r"""
import json, re, sys, time
key = re.search(r"Your key for this turn is (\w+)\.", sys.stdin.read()).group(1)
def say(event):
    print(json.dumps(event), flush=True)
def bash(number, command, output):
    say({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{number}", "name": "Bash", "input": {"command": command}}]}})
    say({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{number}", "content": output}]}})
say({"type": "system", "subtype": "init", "session_id": "s-5", "tools": []})
bash(1, "cat /srv/pizzeria/arrivati.txt", f"[[{key} news: Letta in un file]]")
say({"type": "assistant", "message": {"content": [{"type": "text", "text":
    f"[[{key} news: Scritta come testo]]\n[[{key} phases: Scritte | Come | Testo]]"}]}})
lines = f"[[{key} news: Menu online in /srv/pizzeria]]\n[[{key} phases: Menu | Guida]]\n[[{key} phase: 2]]"
bash(2, f"cat <<'ZOE'\n{lines}\nZOE", lines)
time.sleep(1.5)
say({"type": "result", "result": "Menu online in /srv/pizzeria. Guida aggiornata.", "is_error": False})
"""


def test_news_joins_the_queue_when_its_command_starts_and_what_the_mind_reads_or_writes_sends_none():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = _site(conn, owner, Path(tmp))
        _run_mind(conn, _engine(worker, ClaudeRunner(_claude(Path(tmp), NEWS_CLAUDE), "model", 30, 20)))
        assert _told(conn) == [("news", "Menu online in /srv/pizzeria"),
                               ("done", "Menu online in /srv/pizzeria. Guida aggiornata.")], _told(conn)
        mind = minds.get(conn, mind_id)
        assert (mind.phases, mind.phase) == (["Menu", "Guida"], 2), (mind.phases, mind.phase)
        news, done = conn.execute("SELECT payload, created_at FROM messages WHERE kind = 'mind' ORDER BY id").fetchall()
        assert done["created_at"] - news["created_at"] > 1, "the news waits for the end of the run"
        assert json.loads(done["payload"])["told"] == ["Menu online in /srv/pizzeria"]
        conn.close()


def test_only_launching_a_commit_or_a_publish_command_counts_as_saving_work():
    publish = ("site-publish", "shop * deploy")
    for command in ('git commit -qm "Sito"',
                    'cd /srv/pizzeria && git add -A && git commit -qm "Sito"',
                    'git -C /srv/pizzeria commit -qm "Sito"',
                    'git add -A; git -c user.name=Alex -c user.email="alex@example.com" commit -qm "Sito"',
                    'GIT_AUTHOR_NAME=Alex git commit -qm "Sito"',
                    'cd /srv/pizzeria && site-publish "Menu nuovo"',
                    'ls; /usr/local/bin/site-publish "Menu nuovo" 2>&1 | tail -3',
                    'npm test || exit 1\nshop pizzeria deploy; echo "rc=$?"'):
        assert minds._closing(command, publish), command
    for command in ("cat /usr/local/bin/site-publish",
                    "cd /srv/pizzeria && git log --oneline | head -3",
                    'grep -rn "git commit" docs; which site-publish && cat "$(which site-publish)"',
                    "git commit-tree HEAD^{tree}",
                    "echo site-publish",
                    "site-publisher --help",
                    "shop deploy",
                    "ls shop pizzeria deploy"):
        assert not minds._closing(command, publish), command
    assert minds._closing('git commit -qm "Sito"') and not minds._closing('site-publish "Menu nuovo"')

    before = os.environ.get("ZOE_PUBLISH_COMMANDS")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("ZOE_PUBLISH_COMMANDS", None)
            assert Config.from_env(Path(tmp) / "none.env").publish_commands == ()
            os.environ["ZOE_PUBLISH_COMMANDS"] = " site-publish,shop  *  deploy, "
            assert Config.from_env(Path(tmp) / "none.env").publish_commands == publish
    finally:
        os.environ.pop("ZOE_PUBLISH_COMMANDS", None)
        if before is not None:
            os.environ["ZOE_PUBLISH_COMMANDS"] = before


def test_stopped_minds_stay_stopped_and_working_ones_resume_after_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        brain = Brain(conn, owner.id)
        first = minds.start(conn, brain, owner, OWNER, "Uno", "a", "", [], Path(tmp))
        second = minds.start(conn, brain, owner, OWNER, "Due", "b", "", [], Path(tmp))
        assert minds.stop(conn, owner.id, first) and not minds.stop(conn, owner.id, first)
        assert minds.claim(conn) == second and minds.reset_running(conn) == 1
        assert minds.get(conn, second).state == "queued"
        conn.close()


def test_a_requested_restart_waits_for_turns_and_minds_at_work():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp), on_disk=True)
        db = Path(tmp) / "zoe.db"
        assert not app.at_work(db)
        store.enqueue(conn, OWNER, owner.id, "ciao", "text")
        assert app.at_work(db)
        store.claim_next(conn)
        assert app.at_work(db)
        conn.execute("UPDATE messages SET state = 'answered'")
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito", "Crea il sito", "", [], Path(tmp))
        assert not app.at_work(db) and minds.claim(conn) == mind_id and app.at_work(db)
        minds.stop(conn, owner.id, mind_id)
        assert not app.at_work(db)
        working, claude_runner.working = claude_runner.working, lambda: 1
        try:
            assert app.at_work(db), "a turn's session followed while it works is work too"
        finally:
            claude_runner.working = working
        assert not app.at_work(db)
        conn.close()


def test_two_minds_never_work_in_the_same_folder_at_once():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as projects:
        conn, owner, *_ = worker_setup(Path(tmp))
        brain, site = Brain(conn, owner.id), Path(projects) / "sito"
        first = minds.start(conn, brain, owner, OWNER, "Grafica", "a", str(site), [], Path(tmp))
        second = minds.start(conn, brain, owner, OWNER, "Testi", "b", str(site / "testi"), [], Path(tmp))
        third = minds.start(conn, brain, owner, OWNER, "Altro", "c", "", [], Path(tmp))
        assert minds.claim(conn) == first and minds.claim(conn) == third and minds.claim(conn) is None
        conn.execute("UPDATE minds SET state = 'done' WHERE id = ?", (first,))
        assert minds.claim(conn) == second


def test_a_mind_never_works_where_it_could_harm_more_than_its_task():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        brain = Brain(conn, owner.id)
        root = str(Path(Path(tmp).anchor))
        for folder in (root, str(Path.home()), str(Path(tmp)), str(Path(tmp) / "people"), str(plugins.CORE_ROOT / "zoe")):
            mind_id = minds.start(conn, brain, owner, OWNER, "Prova", "brief", folder, [], Path(tmp))
            assert Path(minds.get(conn, mind_id).workdir) == Path(tmp) / "minds" / str(mind_id), folder


def test_a_mind_ignores_the_settings_of_the_project_it_works_in():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Repo", "Analizza il repo", "", [], Path(tmp))
        engine = _engine(worker, mind_runner := FakeRunner("Analizzato."))
        _run_mind(conn, engine)
        args = mind_runner.profiles[-1].args
        assert args[args.index("--setting-sources") + 1] == "user"
        conn.close()


def test_no_command_holds_a_mind_in_the_foreground_past_the_limit():
    with no_inherited_limit(), tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Suite", "Lancia i test", "", [], Path(tmp))
        _run_mind(conn, _engine(worker, ClaudeRunner(_claude(Path(tmp), ENV_CLAUDE), "model", 30, 20)))
        assert minds.get(conn, mind_id).result == "120000"
        conn.close()


def test_free_memory_is_read_from_the_system():
    assert minds.available_mb("MemTotal: 32864256 kB\nMemFree: 1 kB\nMemAvailable: 2097152 kB\n") == 2048
    assert minds.available_mb("MemTotal: 1 kB\n") is None


def test_the_menu_follows_and_stops_minds_for_owners_only():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito pizzeria", "Crea il sito", "", [], Path(tmp))
        started = time.time() - 1500
        conn.execute("UPDATE minds SET state = 'running', started_at = ?, steps = ? WHERE id = ?",
                     (started, '["Scrivo: index.html"]', mind_id))
        menu = Menu(worker._cfg, FakeAPI())

        view = menu.answer(conn, owner, {"view": "minds"})
        [row] = view["blocks"][1]["items"]
        assert (row["to"], row["detail"], row["since"]) == (f"mind:{mind_id}", "{elapsed}, Scrivo: index.html", started)
        view = menu.answer(conn, owner, {"view": f"mind:{mind_id}"})
        assert view["blocks"][0] == {"text": "⚙️ Al lavoro da {elapsed}", "since": started}
        assert "• Scrivo: index.html" in shown(view)
        assert targets(view) == [f"halt:{mind_id}"] and view["back"] == "minds"
        view = menu.answer(conn, owner, {"action": f"halt:{mind_id}"})
        assert view["notice"] == "Fermata." and minds.get(conn, mind_id).state == "stopped"
        assert targets(view) == [] and "since" not in view["blocks"][0]
        assert targets(menu.answer(conn, owner, {"view": "minds"})) == [f"mind:{mind_id}"]

        mia = access.create_person(conn, "Mia", "member", "it")
        assert menu.answer(conn, mia, {"view": "minds"})["notice"] == "Questa funzione è riservata al proprietario."


class SearchThenFail:
    """A run that searches the web once and then fails."""

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0, model="", effort="") -> Result:
        on_step(Step("tool", "WebSearch", {"query": '"accento napoletano" & <dialetto>'}))
        return Result(False, "", False, False, 1, 1.0, session_id="s-1")


def test_steps_are_kept_and_shown_as_plain_text():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Ricerca", "Cerca", "", [], Path(tmp))
        _run_mind(conn, _engine(worker, SearchThenFail()))
        step = 'Cerco sul web: "accento napoletano" & <dialetto>'
        mind = minds.get(conn, mind_id)
        assert mind.steps == [step] and mind.result.endswith(f"Last steps: {step}")
        conn.execute("UPDATE minds SET state = 'running', started_at = ? WHERE id = ?", (time.time(), mind_id))
        view = Menu(worker._cfg, FakeAPI()).answer(conn, owner, {"view": f"mind:{mind_id}"})
        assert {"text": f"• {step}"} in view["blocks"]
        conn.close()


class PlansInPhases:
    """A run that plans its phases with the command its instructions give, starts the second, replans
    with a phase for a fix and fails; resumed, it reports. It keeps the mind as saved after each command."""

    def __init__(self, conn, mind_id: int) -> None:
        self.conn, self.mind_id = conn, mind_id
        self.prompts: list[str] = []
        self.seen: list = []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0, model="", effort="") -> Result:
        self.prompts.append(prompt_text)
        if len(self.prompts) > 1:
            return Result(True, "Sito online in /srv/pizzeria.", False, False, 0, 1.0, session_id="s-1")
        key = re.search(r"Your key for this turn is (\w+)\.", prompt_text).group(1)
        taught = re.search(r"written like this:\n(cat <<'ZOE'\n\[\[\w{6} phases: )[^\]]+(\]\]\nZOE)\n", prompt_text)
        for command in (_command(taught.group(1) + "Leggo il codice | Scrivo | Test" + taught.group(2)),
                        _lines(f"[[{key} phase: 2]]"),
                        _lines(f"[[{key} phases: Leggo il codice | Scrivo | Test | Correggo]]")):
            on_step(command)
            mind = minds.get(self.conn, self.mind_id)
            self.seen.append((mind.phases, mind.phase))
        return Result(False, "", False, False, 1, 1.0, session_id="s-1")


def test_a_mind_plans_its_phases_and_the_menu_shows_how_far_it_is():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito pizzeria", "Crea il sito", "", [],
                              Path(tmp))
        runner = PlansInPhases(conn, mind_id)
        engine = _engine(worker, runner)
        _run_mind(conn, engine)
        # The plan comes right after the task, before the long rules, and it is the mind's first command; a
        # resumed mind that planned is not reminded.
        first, again = runner.prompts
        assert first.index("Your very first command, before any other tool, plans the work") < first.index(
            "Work until the task is done")
        assert re.search(r"The plan starts the first phase: each time you start another, send \[\[\w{6} phase: 2\]\] "
                         r"with its number the same way\. The service reads these lines in the command the moment "
                         r"you launch it; written as text, outside a command, they are never read\.", first), first
        assert "A phase that starts with the news goes in the same command, its line under it." in first, first
        assert "You have not planned your phases yet" not in again
        # Each command is saved the moment it starts: the plan with the work in its first phase, the phase
        # the mind starts, and a new plan that keeps the phase the mind is in.
        plan, fixed = ["Leggo il codice", "Scrivo", "Test"], ["Leggo il codice", "Scrivo", "Test", "Correggo"]
        assert runner.seen == [(plan, 1), (plan, 2), (fixed, 2)], runner.seen
        mind = minds.get(conn, mind_id)
        assert (mind.phases, mind.phase) == (fixed, 2)
        assert minds.progress(mind) == minds.Progress(1, 4, "Scrivo") and minds.progress(mind).share == 25

        started = time.time() - 60
        conn.execute("UPDATE minds SET state = 'running', started_at = ? WHERE id = ?", (started, mind_id))
        other = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Ricerca", "Cerca", "", [], Path(tmp))
        menu = Menu(worker._cfg, FakeAPI())
        view = menu.answer(conn, owner, {"view": "minds"})
        [meter] = view["blocks"][1]["meters"]
        assert meter == {"label": "⚙️ Sito pizzeria", "value": "25%", "fill": 25, "since": started,
                         "detail": "{elapsed}, Fase 2 di 4: Scrivo", "to": f"mind:{mind_id}"}
        assert [row["to"] for row in view["blocks"][2]["items"]] == [f"mind:{other}"]
        view = menu.answer(conn, owner, {"view": f"mind:{mind_id}"})
        assert {"meters": [{"label": "Fase 2 di 4: Scrivo", "value": "25%", "fill": 25}]} in view["blocks"]
        assert {"text": "✅ Leggo il codice\n▶️ Scrivo\n⬜ Test\n⬜ Correggo"} in view["blocks"]
        assert minds.progress(minds.get(conn, other)) is None
        # A mind resumed without a plan, as one that sent only a phase line, is asked for it first, with
        # the phase it is in under the plan, in one command.
        conn.execute("UPDATE minds SET session_id = 's-2', phase = 2 WHERE id = ?", (other,))
        resumed = engine._prompt(conn, owner, minds.get(conn, other), [], "")
        assert resumed.index("You have not planned your phases yet") < resumed.index("Your very first command")
        assert "send the phases line for the whole task and, under it in the same command, the phase line" in resumed
        conn.close()


def test_phases_are_saved_from_a_command_the_mind_launches_with_its_key_and_from_nothing_else():
    with tempfile.TemporaryDirectory() as tmp, _clock() as clock:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = _site(conn, owner, Path(tmp))
        plan, start = "[[{k} phases: Menu | Logo | Guida]]", "[[{k} phase: 3]]"
        seen: list = []

        def look() -> None:
            mind = minds.get(conn, mind_id)
            seen.append((mind.phases, mind.phase, _told(conn)))

        runner = ClosingWork(clock, [
            # Written as text, alone or after a sentence, a plan and a phase are not read.
            plan, "Passo alla guida.\n" + start,
            # Nor are they without the mind's key, or in a file the mind writes.
            _lines("[[000000 phases: Con la chiave | di un altro]]"), _command("echo '[[phase: 3]]'"),
            Step("tool", "Write", {"file_path": "/srv/pizzeria/appunti.txt", "content": f"{plan}\n{start}"}), look,
            # The plan and the phase the mind is in travel in one command, saved the moment it starts.
            _lines(plan, "[[{k} phase: 2]]"), look,
            # So do news and the phase that starts with it: the news is told and the bar moves at once.
            _lines("[[{k} news: Menu e logo online in /srv/pizzeria]]", start), look])
        _run_mind(conn, _engine(worker, runner))
        phases, news = ["Menu", "Logo", "Guida"], ("news", "Menu e logo online in /srv/pizzeria")
        assert seen == [([], 0, []), (phases, 2, []), (phases, 3, [news])], seen
        conn.close()


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
