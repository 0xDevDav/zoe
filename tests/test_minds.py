"""Checks for parallel minds: starting one, its news reaching the chat, questions, notes, failures, stops."""

import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeAPI, FakeRunner, message, press, shown, targets, worker_setup
from zoe import access, app, claude_runner, minds, plugins, store
from zoe.brain import Brain
from zoe.claude_runner import Result, Step
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


def test_a_long_request_starts_a_mind_and_only_owners_can():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), f"Ci lavora una mente parallela 🔀\n{BRIEF}", BRIEF)
        _ask(conn, worker, owner.id, "Fammi un sito per la pizzeria di Marco")
        assert "your self-improvement skill" in runner.prompts[0]
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
        assert "/srv/pizzeria" in runner.prompts[-1]
        assert api.to(OWNER)[-1] == "Il sito è pronto, te lo apro? ✅"
        assert not any("/srv/pizzeria" in turn["text"] for turn in store.recent_history(conn, OWNER))

        sent = len(api.sent) + 100
        update = {"update_id": 9, "message": {"message_id": 9, "chat": {"id": OWNER, "type": "private"},
                  "from": {"id": OWNER}, "text": "sì", "reply_to_message": {"message_id": sent}}}
        store.record_inbound(conn, update, owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert f"is replying to a message of yours #{sent}" in runner.prompts[-1]
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

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0) -> Result:
        self.prompts.append(prompt_text)
        self.profiles.append(profile)
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
        event = store.claim_next(conn)
        assert (event.kind, event.payload["event"]) == ("mind", "done")
        assert not any(t.startswith("🔀") for t in api.to(OWNER))
        conn.close()

class NoteDuringRun:
    """A run that receives a note while working, then finishes on the resumed session."""

    def __init__(self, home: Path, owner_id: int, mind_id: int) -> None:
        self.home, self.owner_id, self.mind_id = home, owner_id, mind_id
        self.prompts: list[str] = []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0) -> Result:
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
        minds._WATCH_EVERY = 0.05
        runner = NoteDuringRun(Path(tmp), owner.id, mind_id)
        _run_mind(conn, _engine(worker, runner))
        assert len(runner.prompts) == 2 and "Aggiungi anche la pagina contatti" in runner.prompts[1]
        assert minds.get(conn, mind_id).state == "done"
        conn.close()


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

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0) -> Result:
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
    """A run that plans three phases, starts the second, then replans with a phase for a fix and fails."""

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0) -> Result:
        key = re.search(r"Your key for this turn is (\w+)\.", prompt_text).group(1)
        on_step(Step("said", args={"text": f"Parto.\n[[{key} phases: Leggo il codice | Scrivo | Test]]"}))
        on_step(Step("said", args={"text": f"[[{key} phase: 2]]"}))
        self.seen = minds.get(self.conn, self.mind_id)
        on_step(Step("said", args={"text": "[[000000 phase: 3]] a line without the key does nothing"}))
        on_step(Step("said", args={"text": f"[[{key} phases: Leggo il codice | Scrivo | Test | Correggo]]"}))
        return Result(False, "", False, False, 1, 1.0, session_id="s-1")


def test_a_mind_plans_its_phases_and_the_menu_shows_how_far_it_is():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), on_disk=True)
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito pizzeria", "Crea il sito", "", [],
                              Path(tmp))
        runner = PlansInPhases()
        runner.conn, runner.mind_id = conn, mind_id
        _run_mind(conn, _engine(worker, runner))
        assert (runner.seen.phases, runner.seen.phase) == (["Leggo il codice", "Scrivo", "Test"], 2)
        mind = minds.get(conn, mind_id)
        assert (mind.phases, mind.phase) == (["Leggo il codice", "Scrivo", "Test", "Correggo"], 2)
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
        conn.close()


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
