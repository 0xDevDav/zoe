"""Parallel minds: Zoe hands long work to a mind of her own and keeps the chat free.

When a request would take long, such as building a site or a program or a large
piece of research, Zoe answers at once and writes a brief for a parallel mind: a
separate Claude run with her powers, in its own folder, allowed hours rather
than minutes. The chat stays free while it works.

- Every mind is kept in the database with its Claude session, so after a restart
  it resumes where it was instead of starting over.
- Zoe sees in every turn which minds are working, for how long and on what; the
  person follows and stops them in the menu (``menu.py``).
- A mind plans its work in phases and marks the one it starts, so the menu shows
  how far it is.
- A note for a working mind interrupts it and resumes its session with the note.
- A mind that needs a decision ends with a question; Zoe asks the person, and
  the answer resumes the mind.
- When a mind finishes, fails or asks, the news joins its chat's queue like a
  plan, so Zoe tells the person in her own words, in order with the chat.
- The task is a neuron wired to the conversation it came from, and what Zoe
  says about its outcome is wired back to it.

Only owners start minds.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import access, approve, availability, directives, i18n, plugins, store
from .access import Person
from .brain import Brain
from .claude_runner import ClaudeRunner, Profile, Step, unavailable
from .config import Config
from .db import connect
from .progress import describe

log = logging.getLogger("zoe.minds")

TASK = "task"
_UNTRUSTED_PROJECT = ("--setting-sources", "user")
EVENT = "mind"
ACTIVE = ("queued", "running", "asking")
_STEPS = 5
_SAVE_EVERY = 5.0
_WATCH_EVERY = 3.0
_RESULT = 20000
_MEMORIES = 8


@dataclass(frozen=True)
class Mind:
    id: int
    neuron_id: int | None
    person_id: int
    chat_id: int
    title: str
    brief: str
    workdir: str
    state: str
    session_id: str
    directive_key: str
    steps: list[str]
    phases: list[str]
    phase: int
    notes: list[str]
    question: str
    result: str
    attempts: int
    started_at: float | None
    finished_at: float | None
    created_at: float


def _mind(row: sqlite3.Row) -> Mind:
    return Mind(
        row["id"], row["neuron_id"], row["person_id"], row["chat_id"], row["title"], row["brief"], row["workdir"],
        row["state"], row["session_id"] or "", row["directive_key"] or "",
        json.loads(row["steps"] or "[]"), json.loads(row["phases"] or "[]"), row["phase"],
        json.loads(row["notes"] or "[]"),
        row["question"] or "", row["result"] or "", row["attempts"], row["started_at"], row["finished_at"],
        row["created_at"],
    )


@dataclass(frozen=True)
class Progress:
    done: int
    total: int
    current: str

    @property
    def share(self) -> int:
        return round(100 * self.done / self.total)


def progress(mind: Mind) -> Progress | None:
    """How far a mind is by the phases it planned: those before the one it works on are done.
    None while it has planned none."""
    if not mind.phases:
        return None
    current = min(max(mind.phase, 1), len(mind.phases))
    return Progress(current - 1, len(mind.phases), mind.phases[current - 1])


def duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _overlap(a: Path, b: Path) -> bool:
    """Whether two folders are the same or one holds the other."""
    a, b = a.resolve(), b.resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _folder(folder: str, home: Path) -> Path | None:
    """The folder a brief names, created if missing. None, so the mind gets a folder of its
    own, when it is not an absolute path that can be made, or when working there could harm
    more than the task: the root of a disk, the home folder itself, Zoe's own code (changed
    only through her pipeline) or her home, apart from the minds' folders."""
    path = Path(folder).expanduser() if folder else None
    if path is None or not path.is_absolute():
        return None
    resolved = path.resolve()
    if (resolved.parent == resolved or resolved == Path.home().resolve()
            or _overlap(resolved, plugins.CORE_ROOT)
            or (resolved.is_relative_to(home.resolve()) and not resolved.is_relative_to((home / "minds").resolve()))):
        return None
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def start(
    conn: sqlite3.Connection,
    brain: Brain,
    person: Person,
    chat_id: int,
    title: str,
    brief: str,
    folder: str,
    related: list[int],
    home: Path,
) -> int:
    """Queue a parallel mind for ``person``; its task becomes a neuron wired to ``related``."""
    neuron = brain.learn(TASK, title, related=related)
    cur = conn.execute(
        "INSERT INTO minds (neuron_id, person_id, chat_id, title, brief, workdir, directive_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, '', ?, ?)",
        (neuron, person.id, chat_id, title, brief, directives.new_key(), time.time()),
    )
    mind_id = int(cur.lastrowid)
    workdir = _folder(folder, home) or home / "minds" / str(mind_id)
    workdir.mkdir(parents=True, exist_ok=True)
    conn.execute("UPDATE minds SET workdir = ? WHERE id = ?", (str(workdir), mind_id))
    return mind_id


def get(conn: sqlite3.Connection, mind_id: int, person_id: int | None = None) -> Mind | None:
    """A mind by id, only if it belongs to ``person_id`` when one is given."""
    row = conn.execute("SELECT * FROM minds WHERE id = ?", (mind_id,)).fetchone()
    if row is None or (person_id is not None and row["person_id"] != person_id):
        return None
    return _mind(row)


def active(conn: sqlite3.Connection, person_id: int) -> list[Mind]:
    rows = conn.execute(
        "SELECT * FROM minds WHERE person_id = ? AND state IN ('queued', 'running', 'asking') ORDER BY id",
        (person_id,),
    ).fetchall()
    return [_mind(r) for r in rows]


def at_work(conn: sqlite3.Connection, chat_id: int) -> list[Mind]:
    """The minds at work for a chat, the oldest first."""
    rows = conn.execute(
        f"SELECT * FROM minds WHERE chat_id = ? AND state IN ({','.join('?' * len(ACTIVE))}) ORDER BY id",
        (chat_id, *ACTIVE),
    ).fetchall()
    return [_mind(r) for r in rows]


def recent(conn: sqlite3.Connection, person_id: int, limit: int = 5) -> list[Mind]:
    """The last minds of a person that ended, most recent first."""
    rows = conn.execute(
        "SELECT * FROM minds WHERE person_id = ? AND state IN ('done', 'failed', 'stopped') "
        "ORDER BY finished_at DESC LIMIT ?",
        (person_id, limit),
    ).fetchall()
    return [_mind(r) for r in rows]


def tell(conn: sqlite3.Connection, person_id: int, mind_id: int, note: str) -> bool:
    """Pass a note or an answer to a mind; a mind waiting for an answer goes back to work."""
    cur = conn.execute(
        "UPDATE minds SET notes = json_insert(COALESCE(notes, '[]'), '$[#]', ?), "
        "question = CASE WHEN state = 'asking' THEN NULL ELSE question END, "
        "state = CASE WHEN state = 'asking' THEN 'queued' ELSE state END "
        "WHERE id = ? AND person_id = ? AND state IN ('queued', 'running', 'asking')",
        (note, mind_id, person_id),
    )
    return cur.rowcount > 0


def stop(conn: sqlite3.Connection, person_id: int, mind_id: int) -> bool:
    cur = conn.execute(
        "UPDATE minds SET state = 'stopped', finished_at = ? "
        "WHERE id = ? AND person_id = ? AND state IN ('queued', 'running', 'asking')",
        (time.time(), mind_id, person_id),
    )
    return cur.rowcount > 0


def take_notes(conn: sqlite3.Connection, mind_id: int) -> list[str]:
    """Return the notes waiting for a mind and clear them, in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT notes FROM minds WHERE id = ?", (mind_id,)).fetchone()
        conn.execute("UPDATE minds SET notes = NULL WHERE id = ?", (mind_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return json.loads(row["notes"] or "[]") if row else []


def claim(conn: sqlite3.Connection) -> int | None:
    """Take the oldest queued mind whose folder neither is, nor holds, nor lies inside the
    folder of a working mind, so two minds never change the same files at once."""
    working = [Path(r["workdir"]) for r in conn.execute("SELECT workdir FROM minds WHERE state = 'running'")]
    for row in conn.execute("SELECT id, workdir FROM minds WHERE state = 'queued' ORDER BY id").fetchall():
        if any(_overlap(Path(row["workdir"]), other) for other in working):
            continue
        taken = conn.execute(
            "UPDATE minds SET state = 'running', started_at = COALESCE(started_at, ?) WHERE id = ? AND state = 'queued'",
            (time.time(), row["id"]),
        ).rowcount
        if taken:
            return row["id"]
    return None


def available_mb(meminfo: str) -> int | None:
    """The memory the system can still give, from the text of /proc/meminfo."""
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


def _free_mb() -> int | None:
    try:
        return available_mb(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except OSError:
        return None


def reset_running(conn: sqlite3.Connection) -> int:
    """Return to the queue the minds an interrupted run left working, to resume them."""
    return conn.execute("UPDATE minds SET state = 'queued' WHERE state = 'running'").rowcount


def _end(conn: sqlite3.Connection, mind: Mind, event: str, text: str) -> None:
    """Record how a working mind's run ended (``done``, ``ask`` or ``failed``) and queue the news
    for Zoe in its chat. A mind stopped in the meantime stays stopped and sends no news."""
    text = text[:_RESULT]
    if event == "ask":
        cur = conn.execute(
            "UPDATE minds SET state = 'asking', question = ? WHERE id = ? AND state = 'running'", (text, mind.id)
        )
    else:
        cur = conn.execute(
            "UPDATE minds SET state = ?, result = ?, finished_at = ? WHERE id = ? AND state = 'running'",
            ("done" if event == "done" else "failed", text, time.time(), mind.id),
        )
    if cur.rowcount:
        store.enqueue(conn, mind.chat_id, mind.person_id, text, EVENT,
                      {"mind": mind.id, "neuron": mind.neuron_id, "event": event, "title": mind.title})


# The plan in phases comes first, where a long brief cannot bury it: the menu draws the bar from it.
_PHASES = (
    "Your very first message, before any tool, is one line that plans the work in two to eight phases of a few "
    "words each, studying the task included: [[{k} phases: Read the code | Write | Test | Fix]]. Each time you "
    "start a phase, write [[{k} phase: 2]] with its number. {name} follows the bar they draw in the menu. When "
    "the plan changes, for example a test fails and the fix needs a phase of its own, write the phases line "
    "again with the whole new list. Only a task of one or two steps skips them."
)
_PHASES_MISSING = (
    "You have not planned your phases yet: before anything else, write the phases line for the whole task, "
    "then the phase line of the one you are in."
)
_ENDING = (
    "Work until the task is done, and verify it the way the brief asks or the way a careful professional "
    "would: build it, run the tests, open the result. Never wait for something to happen on its own (a "
    "scheduled run, a message, a file changing): run it once yourself to check it, and leave the watching to "
    "the schedule or the service you set up. Then end with a short report for Zoe: what you did, "
    "where the result is, what you verified, and anything left open. You never write to {name} yourself, "
    "even when the brief asks: Zoe tells them what you report and sends them the files you list by their full "
    "path, several images together as an album. If you cannot go on without a decision "
    "only {name} can make, end with a line [[{k} ask: the question]] and nothing after it: you will continue in "
    "this same session with the answer. Write the report in {language}. Whatever you start in the background "
    "stops when you finish: anything meant to keep running, such as a site that must stay online, belongs in "
    "a system service."
)


class _Steps:
    """Keeps the last steps of a run and the phases its text plans and starts, saved now and then
    so Zoe and the menu can show them; a phase change is saved at once."""

    def __init__(self, conn: sqlite3.Connection, mind: Mind, language: str) -> None:
        self._conn = conn
        self._id = mind.id
        self._key = mind.directive_key
        self._language = language
        self.lines = list(mind.steps)
        self.phases = list(mind.phases)
        self.phase = mind.phase
        self._saved = 0.0

    def step(self, step: Step) -> None:
        if step.kind == "said":
            self._plan(step.args.get("text", ""))
            return
        if step.kind != "tool":
            return
        line = describe(step, self._language)
        if not self.lines or self.lines[-1] != line:
            self.lines = (self.lines + [line])[-_STEPS:]
        if time.monotonic() - self._saved > _SAVE_EVERY:
            self.save()

    def _plan(self, text: str) -> None:
        _, found = directives.extract(text, self._key)
        planned, started = directives.phases(found), directives.phase(found)
        if planned is None and started is None:
            return
        if planned is not None:
            self.phases, self.phase = planned, started or min(max(self.phase, 1), len(planned))
        else:
            self.phase = started
        self.save()

    def save(self) -> None:
        self._saved = time.monotonic()
        self._conn.execute("UPDATE minds SET steps = ?, phases = ?, phase = ? WHERE id = ?",
                           (json.dumps(self.lines), json.dumps(self.phases), self.phase, self._id))


class Minds(threading.Thread):
    """Starts queued minds, up to ``cfg.minds`` at a time, each in its own thread."""

    def __init__(self, cfg: Config, stop: threading.Event, interval: float = 2.0) -> None:
        super().__init__(name="minds", daemon=True)
        self._cfg = cfg
        self._halt = stop
        self._interval = interval
        self._runner = ClaudeRunner(cfg.claude_bin, cfg.claude_model, cfg.mind_timeout, cfg.mind_inactivity_timeout)
        self._working: list[threading.Thread] = []
        self._warned = float("-inf")

    def run(self) -> None:
        conn = connect(self._cfg.db_path)
        resumed = reset_running(conn)
        log.info("minds started%s", f", resuming {resumed}" if resumed else "")
        while not self._halt.wait(self._interval):
            try:
                self._start(conn)
            except Exception:
                log.exception("parallel minds not started")
        for thread in self._working:
            thread.join(timeout=20)
        conn.close()
        log.info("minds stopped")

    def _start(self, conn: sqlite3.Connection) -> None:
        """Start queued minds while there is room for them and Claude can work."""
        self._working = [t for t in self._working if t.is_alive()]
        while (len(self._working) < self._cfg.minds and not availability.waiting(conn) and self._room()
               and (mind_id := claim(conn)) is not None):
            thread = threading.Thread(target=self.work, args=(mind_id,), name=f"mind-{mind_id}", daemon=True)
            thread.start()
            self._working.append(thread)

    def _room(self) -> bool:
        """Whether the system has the memory for one more mind; queued minds wait until it does."""
        free = _free_mb()
        if free is None or free >= self._cfg.mind_min_free_mb:
            return True
        if time.monotonic() - self._warned > 600:
            log.warning("parallel minds wait: %d MB free, %d MB needed", free, self._cfg.mind_min_free_mb)
            self._warned = time.monotonic()
        return False

    def work(self, mind_id: int) -> None:
        conn = connect(self._cfg.db_path)
        try:
            self._drive(conn, mind_id)
        except Exception:
            log.exception("parallel mind %d crashed", mind_id)
            mind = get(conn, mind_id)
            if mind:
                _end(conn, mind, "failed", "internal error")
        finally:
            conn.close()

    def _drive(self, conn: sqlite3.Connection, mind_id: int) -> None:
        """Run a mind until it finishes, asks, fails, is stopped, or the service stops."""
        interrupted = ""
        while True:
            mind = get(conn, mind_id)
            person = access.get_person(conn, mind.person_id) if mind else None
            if mind is None or person is None or mind.state != "running":
                return
            if not mind.directive_key:
                conn.execute("UPDATE minds SET directive_key = ? WHERE id = ?", (directives.new_key(), mind_id))
                mind = get(conn, mind_id)
            notes = take_notes(conn, mind_id)
            prompt = self._prompt(conn, person, mind, notes, interrupted)
            base = access.turn_profile(person, self._cfg)
            resume = ("--resume", mind.session_id) if mind.session_id else ()
            # Only the owner's own settings: a project's settings in the folder (hooks that run
            # commands, MCP servers, environment) would act with the owner's powers before the
            # mind has read a single file of it.
            profile = approve.asking(Profile(base.args + _UNTRUSTED_PROJECT + resume, Path(mind.workdir), base.env),
                                     self._cfg.data_dir, mind.chat_id, mind.title)

            done, cancel, interrupt, why = threading.Event(), threading.Event(), threading.Event(), {}
            watcher = threading.Thread(target=self._watch, args=(mind_id, done, cancel, interrupt, why), daemon=True)
            watcher.start()
            steps = _Steps(conn, mind, person.language)
            result = self._runner.run(prompt, profile, on_step=steps.step, cancel=cancel, interrupt=interrupt)
            done.set()
            watcher.join()
            steps.save()
            if result.session_id:
                conn.execute("UPDATE minds SET session_id = ? WHERE id = ?", (result.session_id, mind_id))
            mind = get(conn, mind_id)

            if result.cancelled:
                if why.get("reason") == "notes":
                    interrupted = ""
                    continue
                if why.get("reason") == "shutdown":
                    conn.execute("UPDATE minds SET state = 'queued' WHERE id = ? AND state = 'running'", (mind_id,))
                return
            reason = unavailable(result)
            if reason:
                # Claude cannot work at all: the mind waits in the queue with its session and
                # resumes it once Claude can, without spending its one retry.
                conn.execute("UPDATE minds SET state = 'queued' WHERE id = ? AND state = 'running'", (mind_id,))
                availability.down(conn, self._cfg.data_dir, reason)
                log.warning("parallel mind %d waits for Claude: %s", mind_id, reason)
                return
            availability.works(conn, self._cfg.data_dir)
            if result.ok:
                report, found = directives.extract(result.text, mind.directive_key)
                question = directives.ask(found)
                if question:
                    _end(conn, mind, "ask", question)
                else:
                    _end(conn, mind, "done", report)
                log.info("parallel mind %d %s after %s", mind_id, "asks" if question else "is done",
                         duration(result.duration))
                return

            reason = "ran past its time limit" if result.timed_out else (
                "produced no output for too long" if result.inactivity else f"failed (exit code {result.returncode})")
            if not result.timed_out and mind.attempts < 1 and mind.session_id:
                conn.execute("UPDATE minds SET attempts = attempts + 1 WHERE id = ?", (mind_id,))
                interrupted = reason
                continue
            last = "; ".join(steps.lines[-3:])
            _end(conn, mind, "failed", reason + (f". Last steps: {last}" if last else ""))
            log.warning("parallel mind %d failed: %s", mind_id, reason)
            return

    def _watch(
        self, mind_id: int, done: threading.Event, cancel: threading.Event, interrupt: threading.Event, why: dict
    ) -> None:
        """Stop the run at once when the service stops or the mind is stopped; when a note
        arrives, stop it as soon as no command is running, to resume with the note."""
        conn = connect(self._cfg.db_path)
        try:
            while not done.wait(_WATCH_EVERY):
                row = conn.execute("SELECT state, notes FROM minds WHERE id = ?", (mind_id,)).fetchone()
                if self._halt.is_set():
                    why["reason"] = "shutdown"
                    cancel.set()
                elif row is None or row["state"] != "running":
                    why["reason"] = "stopped"
                    cancel.set()
                elif json.loads(row["notes"] or "[]"):
                    why.setdefault("reason", "notes")
                    interrupt.set()
        finally:
            conn.close()

    def _prompt(self, conn: sqlite3.Connection, person: Person, mind: Mind, notes: list[str], interrupted: str) -> str:
        ending = directives.how(mind.directive_key) + " " + _ENDING.format(
            name=person.name, language=i18n.language_name(person.language), k=mind.directive_key)
        phases = _PHASES.format(name=person.name, k=mind.directive_key)
        passed = f"Zoe passes this on from {person.name}: " + " | ".join(notes) if notes else ""
        if mind.session_id:
            why = f"Your previous run {interrupted}." if interrupted else (
                "" if notes else "Your run was interrupted by a restart.")
            missing = "" if mind.phases else _PHASES_MISSING
            return "\n".join(p for p in (why, passed, "Carry on with the task from where you were.", missing, phases,
                                          ending) if p)

        brain = Brain(conn, person.id)
        seeds = {**brain.search(mind.brief), **({mind.neuron_id: 1.0} if mind.neuron_id else {})}
        memories = [r.content for r in brain.recall(seeds, limit=_MEMORIES * 2) if r.kind not in (TASK, "plan")]
        instructions = self._cfg.workdir / "CLAUDE.md"
        parts = [
            f"You are a parallel mind of Zoe, the personal assistant of {person.name}. Zoe handed you this task "
            f"so that her chat stays free: you work on your own and Zoe tells {person.name} what you report.",
            f"\nTask: {mind.title}\n{mind.brief}",
            f"\nWork in {mind.workdir}. Now: {datetime.now().astimezone():%a %d %b %Y %H:%M %z}.",
            f"\n{phases}",
        ]
        if instructions.exists():
            parts.append(f"Read {instructions} before you work: it holds {person.name}'s standing instructions.")
        if memories:
            parts += ["\nWhat Zoe remembers that may help:", *(f"- {m}" for m in memories[:_MEMORIES])]
        if passed:
            parts.append(f"\n{passed}")
        parts.append(f"\n{ending}")
        return "\n".join(parts)
