"""Checks for what Zoe learns from finished work: the guides each finished turn or mind read, and the
lessons a review keeps in the guides, as commits of their own and out of protected parts, which the
owner sees and undoes in the menu."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, logged, message, worker_setup
from zoe import access, approve, improve, lessons, store
from zoe.claude_runner import Result, Step
from zoe.menu import Menu

SESSION = "0d1e2f3a-aaaa-bbbb-cccc-000000000001"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


@contextlib.contextmanager
def _claude_home(tmp: Path):
    """Claude Code's folder for the test, where transcripts are kept."""
    before = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp / "claude")
    try:
        yield tmp / "claude" / "projects"
    finally:
        if before is None:
            os.environ.pop("CLAUDE_CONFIG_DIR")
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = before


def _workdir(tmp: Path) -> Path:
    """A workdir in git with an index of guides, a guide, a protected folder and notes a turn is changing."""
    work = tmp / "work"
    (work / "garden").mkdir(parents=True)
    (work / "vault").mkdir()
    (work / "CLAUDE.md").write_text("# Instructions\n- Never water at noon.\n- Garden: `garden/GARDEN.md`\n")
    (work / "garden" / "GARDEN.md").write_text("# Garden\n1. Check the soil.\n2. Water the roses.\n")
    (work / "vault" / "KEYS.md").write_text("# Keys\nWhere the keys are.\n")
    (work / "NOTES.md").write_text("# Notes\n")
    _git(work, "init", "-q")
    _git(work, "config", "user.name", "Mia")
    _git(work, "config", "user.email", "mia@example.com")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "Start")
    (work / "NOTES.md").write_text("# Notes\nHalf written by a turn.\n")
    return work


def _tool(n: int, name: str, args: dict, work: Path) -> dict:
    return {"type": "assistant", "cwd": str(work),
            "message": {"content": [{"type": "tool_use", "id": f"t{n}", "name": name, "input": args}]}}


def _result(n: int, text: str, error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{n}", "content": text, "is_error": error}]}}


def _transcript(projects: Path, session: str, work: Path, tools: int = 12) -> Path:
    """A finished turn asked to water the garden: it read the guides, failed once, and ran ``tools`` tools."""
    events = [{"type": "user", "cwd": str(work), "message": {"content": "Format rules. " * 300
                                                              + "Water the garden for Alex"}},
              _tool(0, "Read", {"file_path": str(work / "garden" / "GARDEN.md")}, work),
              _result(0, "1. Check the soil."),
              _tool(1, "Bash", {"command": "cat garden/GARDEN.md CLAUDE.md /etc/hosts.md missing/NOPE.md",
                                "description": "Read the guides"}, work),
              _result(1, "pump: command not found", error=True),
              _tool(1, "Bash", {"command": "cat garden/GARDEN.md"}, work),
              _tool(2, "Read", {"file_path": "/etc/hostname"}, work)]
    events += [_tool(n, "Bash", {"command": f"echo {n}"}, work) for n in range(3, tools)]
    events.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "Roses watered"}]}})
    (projects / "-work").mkdir(parents=True, exist_ok=True)
    path = projects / "-work" / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
    return path


class EditingRunner:
    """A Claude that, on each run, makes the prepared edits through its tools and answers the prepared text."""

    def __init__(self, *runs: tuple[list[tuple[str, str, str]], str | None], session: str = "review-1") -> None:
        self.runs = list(runs)
        self.session = session
        self.prompts: list[str] = []
        self.profiles: list[object] = []

    def run(self, prompt_text: str, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0,
            model: str = "", effort: str = "") -> Result:
        self.prompts.append(prompt_text)
        self.profiles.append(profile)
        edits, answer = self.runs.pop(0)
        for tool, path, text in edits:
            target = Path(profile.cwd) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            on_step(Step("tool", tool, {"file_path": str(target)}))
        return Result(answer is not None, answer or "", False, False, 0 if answer is not None else 1, 1.0,
                      session_id=self.session)


def _setup(tmp: Path, on_disk: bool = False):
    conn, owner, worker, _, _ = worker_setup(tmp, on_disk=on_disk)
    cfg = replace(worker._cfg, workdir=_workdir(tmp))
    (tmp / lessons.PROTECTED).write_text("# Parts the review leaves alone\nvault/*\nCLAUDE.md +\n")
    return conn, owner, cfg


def _waiting(conn, cfg, projects: Path):
    """One finished chat turn, read and waiting for a review."""
    _transcript(projects, SESSION, cfg.workdir)
    lessons.note(conn, "chat", SESSION)
    lessons.gather(conn, cfg, time.time())
    return conn.execute("SELECT * FROM finished_work").fetchall()


# --- the guides' use ---


def test_a_transcript_tells_the_tools_the_guides_read_and_what_went_wrong():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        work = _workdir(Path(tmp))
        path = _transcript(projects, SESSION, work)
        trace = lessons.read_trace(path, work)
        assert (trace.tools, trace.reads, trace.lines) == (12, ("garden/GARDEN.md", "CLAUDE.md"), 18)
        assert "ERROR: pump: command not found" in trace.text and "Zoe: Roses watered" in trace.text
        assert "Water the garden for Alex" in trace.text and "> Bash: Read the guides | cat garden" in trace.text
        assert "Water the garden" not in lessons.read_trace(path, work, head=True).text
        assert lessons.read_trace(path, work, 3).reads == ("garden/GARDEN.md", "CLAUDE.md")
        assert lessons.read_trace(path, work, 4).reads == ("garden/GARDEN.md",)
        assert lessons.transcript(SESSION) == path
        assert lessons.transcript("../../etc/passwd") is None and lessons.transcript("missing") is None


def test_finished_turns_and_minds_are_read_once_from_the_first_look_on():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        _transcript(projects, SESSION, cfg.workdir)
        _transcript(projects, "mind-1", cfg.workdir, tools=4)
        member = access.create_person(conn, "Mia", "member", "it")

        def mind(session: str, person_id: int, finished: float) -> None:
            conn.execute("INSERT INTO minds (person_id, chat_id, title, brief, workdir, state, session_id, "
                         "finished_at, created_at) VALUES (?, ?, 'Roses', 'Water them', '/tmp', 'done', ?, ?, 1.0)",
                         (person_id, OWNER, session, finished))

        mind("before-1", owner.id, 900.0)
        lessons.gather(conn, cfg, 1000.0)
        mind("mind-1", owner.id, 1500.0)
        mind("member-1", member.id, 1600.0)
        lessons.note(conn, "chat", SESSION, now=1200.0)
        lessons.note(conn, "chat", "")
        lessons.gather(conn, cfg, 2000.0)
        lessons.gather(conn, cfg, 2100.0)
        rows = conn.execute("SELECT source, session_id, tools, read_at FROM finished_work ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("chat", SESSION, 12, 2000.0), ("mind", "mind-1", 4, 2000.0)]
        reads = [tuple(r) for r in conn.execute("SELECT guide, read_at FROM guide_reads ORDER BY id")]
        assert reads == [(g, at) for at in (1200.0, 1500.0) for g in ("garden/GARDEN.md", "CLAUDE.md")]
        assert store.get_state(conn, lessons.STATE_SINCE) == "1200.0"


def test_a_session_that_goes_on_is_read_again_from_where_it_stopped_and_reviewed_again():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        path = _transcript(projects, SESSION, cfg.workdir)
        lessons.note(conn, "mind", SESSION, now=1200.0)
        lessons.gather(conn, cfg, 2000.0)
        conn.execute("UPDATE finished_work SET reviewed_at = 2050.0")
        more = _tool(99, "Read", {"file_path": str(cfg.workdir / "NOTES.md")}, cfg.workdir)
        path.write_text(path.read_text() + json.dumps(more) + "\n")
        lessons.gather(conn, cfg, 2100.0)
        lessons.note(conn, "mind", SESSION, now=3000.0)
        lessons.gather(conn, cfg, 3100.0)
        reads = [tuple(r) for r in conn.execute("SELECT guide, read_at FROM guide_reads ORDER BY id")]
        assert reads == [("garden/GARDEN.md", 1200.0), ("CLAUDE.md", 1200.0), ("NOTES.md", 3000.0)]
        assert tuple(conn.execute("SELECT lines, tools, read_at, reviewed_at FROM finished_work").fetchone()) == (
            19, 1, 3100.0, None)


def test_guides_nobody_read_are_named_once_their_use_was_counted_long_enough():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, cfg = _setup(Path(tmp))
        now = 100 * 86400.0
        assert lessons.unused(conn, cfg.workdir, 60, now) == []
        store.set_state(conn, lessons.STATE_SINCE, str(now - 30 * 86400))
        assert lessons.unused(conn, cfg.workdir, 60, now) == []
        store.set_state(conn, lessons.STATE_SINCE, str(now - 90 * 86400))
        conn.execute("INSERT INTO guide_reads (guide, read_at) VALUES ('garden/GARDEN.md', ?)", (now - 86400,))
        conn.execute("INSERT INTO guide_reads (guide, read_at) VALUES ('NOTES.md', ?)", (now - 80 * 86400,))
        assert lessons.unused(conn, cfg.workdir, 60, now) == ["CLAUDE.md", "NOTES.md", "vault/KEYS.md"]
        assert lessons.unused(conn, cfg.workdir, 60, now, ("*/*",)) == ["vault/KEYS.md"]


def test_an_owner_turn_is_noted_as_finished_work_with_its_session():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = worker_setup(Path(tmp), "Fatto")
        store.record_inbound(conn, message(1, OWNER, "Annaffia le rose"), owner.id)
        worker._handle(conn, store.claim_next(conn))
        row = conn.execute("SELECT source, session_id, read_at FROM finished_work").fetchone()
        assert tuple(row) == ("chat", "session-1", None)


def test_the_thread_reads_and_reviews_until_the_service_stops_and_waits_after_a_failure():
    class Broken:
        calls = 0

        def run(self, *args, **kwargs):
            Broken.calls += 1
            raise RuntimeError("the review broke")

    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp), on_disk=True)
        _transcript(projects, SESSION, cfg.workdir)
        lessons.note(conn, "chat", SESSION)
        stop = threading.Event()
        thread = lessons.Lessons(cfg, stop, interval=0.05)
        thread._runner = Broken()
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and store.get_state(conn, lessons.STATE_RETRY) is None:
            time.sleep(0.05)
        time.sleep(0.3)
        stop.set()
        thread.join(5)
        assert not thread.is_alive() and len(conn.execute("SELECT * FROM guide_reads").fetchall()) == 2
        assert Broken.calls == 1 and float(store.get_state(conn, lessons.STATE_RETRY)) > time.time() + 800


# --- the review ---


def test_reviews_come_at_most_every_half_hour_and_only_for_long_work():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        _transcript(projects, SESSION, cfg.workdir)
        _transcript(projects, "short-1", cfg.workdir, tools=4)
        lessons.note(conn, "chat", SESSION)
        lessons.note(conn, "chat", "short-1")
        runner = EditingRunner(([], '{"lessons": [], "proposal": null}'), ([], '{"lessons": []}'))
        now = time.time()
        assert lessons.step(conn, cfg, runner, now) == [] and len(runner.prompts) == 1
        assert runner.prompts[0].count("===== TRACE") == 1
        assert [r[0] for r in conn.execute("SELECT reviewed_at IS NOT NULL FROM finished_work ORDER BY id")] == [1, 0]
        assert tuple(conn.execute("SELECT source, category, effort FROM model_choices").fetchone()) == (
            "study", "lesson", "high")
        path = lessons.transcript(SESSION)
        more = [_tool(100 + n, "Bash", {"command": "echo again"}, cfg.workdir) for n in range(10)]
        path.write_text(path.read_text() + "\n".join(json.dumps(e) for e in more) + "\n")
        lessons.note(conn, "chat", SESSION)
        assert lessons.step(conn, cfg, runner, now + 60) == []
        assert lessons.step(conn, replace(cfg, lesson_tools=0), runner, now + lessons._EVERY + 1) == []
        assert lessons.step(conn, replace(cfg, lessons_per_day=1), runner, now + lessons._EVERY + 1) == []
        assert len(runner.prompts) == 1
        assert lessons.step(conn, cfg, runner, now + lessons._EVERY + 1) == [] and len(runner.prompts) == 2
        lessons.note(conn, "chat", SESSION)
        lessons.gather(conn, cfg, now + lessons._STALE + 60)
        assert conn.execute("SELECT COUNT(*) FROM finished_work WHERE reviewed_at IS NULL").fetchone()[0] == 0


def test_lessons_land_in_the_guides_one_commit_each_and_never_in_protected_parts():
    answer = {"lessons": [
        {"title": "Annaffiare la sera", "what": "Le rose si annaffiano la sera.", "files": ["garden/GARDEN.md"]},
        {"title": "Indice dell'orto", "what": "L'orto ha la sua guida.", "files": ["garden/VEGETABLES.md",
                                                                                     "CLAUDE.md"]},
        {"title": "Chiavi", "what": "Mai.", "files": ["vault/KEYS.md"]}],
        "proposal": {"title": "Irrigazione automatica", "benefit": "Le rose si bagnano da sole.",
                     "plan": "A schedule.", "checked": "Guardare il terreno.", "careful": True}}
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        work, works = cfg.workdir, _waiting(conn, cfg, projects)
        claude = (work / "CLAUDE.md").read_text()
        runner = EditingRunner(([
            ("Edit", "garden/GARDEN.md", "# Garden\n1. Check the soil.\n2. Water the roses in the evening: at noon "
                                         "the sun burns the wet leaves.\n"),
            ("Write", "garden/VEGETABLES.md", "# Vegetables\nPick them in the morning.\n"),
            ("Edit", "CLAUDE.md", claude + "- Vegetables: `garden/VEGETABLES.md`\n"),
            ("Edit", "vault/KEYS.md", "# Keys\nThe keys are under the mat.\n"),
            ("Edit", "NOTES.md", "# Notes\nOverwritten.\n"),
            ("Write", "garden/pump.sh", "rm -rf /\n"),
        ], json.dumps(answer)))
        kept = lessons.review(conn, cfg, runner, owner, works, 5000.0)

        shown = [_git(work, "show", "--name-only", "--format=%s", sha).split("\n") for sha in kept]
        assert [[line for line in lines if line] for lines in shown] == [
            ["Annaffiare la sera", "garden/GARDEN.md"],
            ["Indice dell'orto", "CLAUDE.md", "garden/VEGETABLES.md"]]
        assert _git(work, "log", "--format=%b", "-1").strip() == "L'orto ha la sua guida."
        recorded = [(l.title, l.files, l.sha, l.state) for l in reversed(lessons.recent(conn, 0))]
        assert recorded == [("Annaffiare la sera", ("garden/GARDEN.md",), kept[0], "kept"),
                            ("Indice dell'orto", ("CLAUDE.md", "garden/VEGETABLES.md"), kept[1], "kept")]
        assert _git(work, "log", "--format=%s", "-3").splitlines()[-1] == "Start"
        assert (work / "vault" / "KEYS.md").read_text() == "# Keys\nWhere the keys are.\n"
        assert (work / "NOTES.md").read_text() == "# Notes\nHalf written by a turn.\n"
        assert not (work / "garden" / "pump.sh").exists()
        assert _git(work, "status", "--porcelain").split() == ["M", "NOTES.md"]
        [proposal] = improve.waiting(conn)
        assert proposal.title == "Irrigazione automatica" and proposal.careful
        assert conn.execute("SELECT reviewed_at FROM finished_work").fetchone()[0] == 5000.0

        prompt, profile = runner.prompts[0], runner.profiles[0]
        assert "- vault/*\n- CLAUDE.md (you may only add lines)" in prompt and "- NOTES.md" in prompt
        assert "Guides it read: garden/GARDEN.md, CLAUDE.md" in prompt
        assert profile.tools == lessons.TOOLS and profile.cwd == work
        assert f"Edit(/{work}/**/*.md)" in profile.args and "--dangerously-skip-permissions" not in profile.args
        assert profile.args[profile.args.index("--permission-mode") + 1] == "dontAsk"
        assert "--settings" in profile.args and profile.env[approve.CHAT] == ""


def test_an_add_only_guide_may_gain_lines_but_never_change():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        works = _waiting(conn, cfg, projects)
        runner = EditingRunner(([("Edit", "CLAUDE.md", "# Instructions\n- Water at noon.\n")], json.dumps(
            {"lessons": [{"title": "Mezzogiorno", "what": "Sì.", "files": ["CLAUDE.md"]}], "proposal": None})))
        assert lessons.review(conn, cfg, runner, owner, works, 5000.0) == []
        assert "Never water at noon" in (cfg.workdir / "CLAUDE.md").read_text()
        assert _git(cfg.workdir, "log", "--format=%s", "-1").strip() == "Start"


def test_quick_checks_ask_for_one_fix_and_keep_the_first_answer_when_the_fix_gives_none():
    dash = chr(0x2014)
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        works = _waiting(conn, cfg, projects)
        dashed = f"# Garden\n1. Check the soil {dash} always.\n2. Water the roses, see `garden/TOOLS.md`.\n"
        answer = json.dumps({"lessons": [{"title": "Terreno", "what": "Prima il terreno.",
                                          "files": ["garden/GARDEN.md"]}], "proposal": None})
        runner = EditingRunner(
            ([("Edit", "garden/GARDEN.md", dashed)], answer),
            ([("Edit", "garden/GARDEN.md", dashed.replace(f" {dash} ", ", ").replace("TOOLS", "GARDEN"))], "Fixed."))
        [sha] = lessons.review(conn, cfg, runner, owner, works, 5000.0)
        assert "long dashes" in runner.prompts[1] and "lead nowhere: garden/TOOLS.md;" in runner.prompts[1]
        assert runner.profiles[1].args[-2:] == ("--resume", "review-1")
        assert dash not in (cfg.workdir / "garden" / "GARDEN.md").read_text()
        assert _git(cfg.workdir, "show", "--format=%s", "--no-patch", sha).strip() == "Terreno"


def test_a_guide_past_its_size_is_named_and_what_the_fix_leaves_is_logged():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        works = _waiting(conn, cfg, projects)
        long = "# Vegetables\n" + "".join(f"- Row {n}\n" for n in range(300))
        answer = json.dumps({"lessons": [{"title": "Orto", "what": "", "files": ["garden/VEGETABLES.md"]}]})
        runner = EditingRunner(([("Write", "garden/VEGETABLES.md", long)], answer), ([], answer))
        with logged("zoe.lessons") as lines:
            assert len(lessons.review(conn, cfg, runner, owner, works, 5000.0)) == 1
        assert "garden/VEGETABLES.md: 301 lines, past 300" in runner.prompts[1]
        assert any(line.startswith("lesson review left after its fix: garden/VEGETABLES.md: 301 lines")
                   for line in lines)


def test_a_review_without_a_session_to_resume_is_not_sent_back():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        works = _waiting(conn, cfg, projects)
        runner = EditingRunner(([("Edit", "garden/GARDEN.md", f"# Garden {chr(0x2013)} soil\n")], json.dumps(
            {"lessons": [{"title": "Terreno", "what": "", "files": ["garden/GARDEN.md"]}]})), session="")
        assert len(lessons.review(conn, cfg, runner, owner, works, 5000.0)) == 1 and len(runner.prompts) == 1


def test_a_review_with_nothing_for_the_checks_runs_once():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        works = _waiting(conn, cfg, projects)
        runner = EditingRunner(([("Edit", "garden/GARDEN.md", "# Garden\n1. Check the soil, see `garden/GARDEN.md`, "
                                  "`GARDEN.md` and `/tmp`.\n")], json.dumps(
            {"lessons": [{"title": "Terreno", "what": "", "files": ["garden/GARDEN.md"]}]})))
        assert len(lessons.review(conn, cfg, runner, owner, works, 5000.0)) == 1 and len(runner.prompts) == 1


def test_changes_with_no_lesson_to_tell_are_put_back():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        works = _waiting(conn, cfg, projects)
        runner = EditingRunner(([("Edit", "garden/GARDEN.md", "# Garden\nSomething.\n"),
                                 ("Write", "garden/DRAFT.md", "# Draft\n")], '{"lessons": [], "proposal": null}'))
        assert lessons.review(conn, cfg, runner, owner, works, 5000.0) == []
        assert (cfg.workdir / "garden" / "GARDEN.md").read_text().startswith("# Garden\n1.")
        assert not (cfg.workdir / "garden" / "DRAFT.md").exists() and improve.waiting(conn) == []


def test_a_failed_review_keeps_nothing_and_waits_before_trying_again():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        _transcript(projects, SESSION, cfg.workdir)
        lessons.note(conn, "chat", SESSION)
        runner = EditingRunner(([("Edit", "garden/GARDEN.md", "# Half\n")], None),
                               ([("Edit", "garden/GARDEN.md", "# Half\n")], "no JSON at all"))
        now = time.time()
        assert lessons.step(conn, cfg, runner, now) == [] and len(runner.prompts) == 1
        assert (cfg.workdir / "garden" / "GARDEN.md").read_text().startswith("# Garden")
        assert float(store.get_state(conn, lessons.STATE_RETRY)) == now + lessons._RETRY_AFTER
        assert lessons.step(conn, cfg, runner, now + 60) == [] and len(runner.prompts) == 1
        conn.execute("UPDATE finished_work SET reviewed_at = NULL")
        assert lessons.step(conn, cfg, runner, now + lessons._EVERY + 1) == [] and len(runner.prompts) == 2
        assert (cfg.workdir / "garden" / "GARDEN.md").read_text().startswith("# Garden")
        assert conn.execute("SELECT reviewed_at FROM finished_work").fetchone()[0] is not None


def test_the_owner_sees_what_was_learned_and_undoes_a_lesson_from_the_menu():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        cfg = replace(worker._cfg, workdir=_workdir(Path(tmp)))
        menu, works = Menu(cfg, api), _waiting(conn, cfg, projects)
        runner = EditingRunner(([("Edit", "garden/GARDEN.md", "# Garden\n1. Check the soil at dawn.\n")], json.dumps(
            {"lessons": [{"title": "Terreno all'alba", "what": "Controllo il terreno all'alba.",
                          "files": ["garden/GARDEN.md"]}], "proposal": None})))
        lessons.review(conn, cfg, runner, owner, works, time.time())
        [kept] = lessons.recent(conn, 0)
        person = access.resolve(conn, OWNER).person
        view = menu.answer(conn, person, {"view": "improve"})
        sections = [s for block in view["blocks"] if "sections" in block for s in block["sections"]]
        [lesson] = [s for s in sections if s["title"] == "Terreno all'alba"]
        assert lesson["blocks"][:2] == [{"text": "Controllo il terreno all'alba."}, {"quote": "garden/GARDEN.md"}]
        assert lesson["blocks"][-1]["button"]["act"] == f"unlearn:{kept.id}"

        view = menu.answer(conn, person, {"action": f"unlearn:{kept.id}"})
        assert view["notice"] == "Lezione annullata: le guide sono tornate come prima."
        assert (cfg.workdir / "garden" / "GARDEN.md").read_text() == "# Garden\n1. Check the soil.\n2. Water the roses.\n"
        assert lessons.get(conn, kept.id).state == "undone"
        sections = [s for block in menu.answer(conn, person, {"view": "improve"})["blocks"] if "sections" in block
                    for s in block["sections"]]
        [lesson] = [s for s in sections if s["title"] == "Terreno all'alba"]
        assert lesson["detail"].endswith(", annullata") and "button" not in lesson["blocks"][-1]
        assert menu.answer(conn, person, {"action": f"unlearn:{kept.id}"})["notice"].startswith("Non riesco")


def test_a_lesson_a_later_change_stands_in_the_way_of_stays():
    with tempfile.TemporaryDirectory() as tmp, _claude_home(Path(tmp)) as projects:
        conn, owner, cfg = _setup(Path(tmp))
        works = _waiting(conn, cfg, projects)
        runner = EditingRunner(([("Edit", "garden/GARDEN.md", "# Garden\n1. Check the soil at dawn.\n")], json.dumps(
            {"lessons": [{"title": "Alba", "what": "", "files": ["garden/GARDEN.md"]}]})))
        lessons.review(conn, cfg, runner, owner, works, time.time())
        [kept] = lessons.recent(conn, 0)
        (cfg.workdir / "garden" / "GARDEN.md").write_text("# Garden\n1. Check the soil at dusk.\n")
        _git(cfg.workdir, "commit", "-q", "-am", "Dusk")
        assert not lessons.undo(conn, cfg, kept.id) and lessons.get(conn, kept.id).state == "kept"
        assert "Check the soil at dusk" in (cfg.workdir / "garden" / "GARDEN.md").read_text()
        assert not (cfg.workdir / ".git" / "REVERT_HEAD").exists()


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
