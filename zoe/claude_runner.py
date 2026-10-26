"""Run Claude for a single turn and watch it while it works.

Claude streams one JSON event per line while it thinks, writes and uses tools.
The runner turns those events into steps for a live progress view, keeps the
final answer, and uses the stream as a heartbeat: a turn that emits nothing for
``inactivity_timeout`` seconds, or runs past ``hard_timeout``, is terminated and
reported as stuck. Each turn is its own process, and whatever it left running
in the background is stopped when that process ends. A turn may ask to have its
answer at once while background tasks are still open: its process then goes on
by itself, followed until those tasks end, and its later answers come one by one.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

_STREAM_ARGS = ("--output-format", "stream-json", "--verbose", "--include-partial-messages")


@dataclass(frozen=True)
class Profile:
    """What a turn may do: extra Claude arguments, the directory it runs in, extra environment,
    and, when set, the only tools it may come up with: a turn that starts with more, or with
    any MCP server or plugin of its own, is stopped before it does anything."""

    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    tools: frozenset[str] | None = None


@dataclass(frozen=True)
class Step:
    """One visible thing Claude is doing: ``thinking``, ``writing``, or using a ``tool``; ``said``
    carries the text Claude wrote, once it is whole."""

    kind: str
    tool: str = ""
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    ok: bool
    text: str
    timed_out: bool
    inactivity: bool
    returncode: int | None
    duration: float
    session_id: str = ""
    cancelled: bool = False
    refused: str = ""
    # What Claude itself said when it could not work: its error result and the end of its stderr.
    failure: str = ""
    # The session still at work in the background after this answer, when the turn asked for it.
    later: Later | None = None


# Runtime state while Claude cannot work at all: why, and when the next try may run.
DOWN = "claude_down"
RETRY_AT = "claude_retry_at"

# Words of a failure that stops every turn, not just this one: logged out, or out of usage.
_UNAVAILABLE = ("/login", "not logged in", "invalid api key", "oauth token", "authentication_error",
                "usage limit", "limit reached", "hit your limit", "you've hit your", "session limit",
                "weekly limit", "credit balance")


def unavailable(result: Result) -> str:
    """Why Claude cannot work at all, from its own error (never from what the turn wrote), or ""."""
    if result.ok:
        return ""
    for line in result.failure.splitlines():
        if any(word in line.lower() for word in _UNAVAILABLE):
            return line.strip()[:200]
    return ""


def to_steps(event: dict) -> list[Step]:
    """Translate one stream event into the steps it reveals, if any."""
    kind = event.get("type")
    if kind == "stream_event":
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_start":
            block = (inner.get("content_block") or {}).get("type")
            if block == "thinking":
                return [Step("thinking")]
            if block == "text":
                return [Step("writing")]
    elif kind == "assistant":
        return [Step("tool", block.get("name", ""), block.get("input") or {}) if block.get("type") == "tool_use"
                else Step("said", args={"text": block.get("text", "")})
                for block in _blocks(event) if block.get("type") in ("tool_use", "text")]
    return []


def _blocks(event: dict) -> list[dict]:
    """The content blocks of an event's message; events whose message is plain text have none."""
    message = event.get("message")
    blocks = message.get("content", []) if isinstance(message, dict) else []
    return [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []


# Seconds a running tool may stay silent before the turn counts as stuck.
TOOL_SILENCE = 1200.0

# Milliseconds a command may hold a turn that answers first: Claude Code then moves it to
# the background, where it runs on and is followed like any background task. 120 seconds
# is the lowest limit Claude Code takes.
FOREGROUND_LIMIT_MS = 120_000

# Secrets the service itself reads from its environment, kept from every Claude run and from
# the tools and MCP servers it starts. The owner's turns reach the vault through its key file
# or the vault-key credential instead.
SECRET_ENV = ("ZOE_VAULT_KEY", "ZOE_BOT_TOKEN")


def _inherited() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name not in SECRET_ENV}


def beyond(init: dict, tools: frozenset[str]) -> str:
    """What a session started with beyond ``tools``: extra tools, MCP servers or plugins that
    are not built into Claude Code. Empty when it has nothing more."""
    extra = sorted(set(init.get("tools") or []) - tools)
    servers = [s.get("name", "?") if isinstance(s, dict) else str(s) for s in init.get("mcp_servers") or []]
    plugins = [p.get("name", "?") for p in init.get("plugins") or [] if isinstance(p, dict) and p.get("path") != "builtin"]
    found = [f"tools {extra}" if extra else "", f"MCP servers {servers}" if servers else "",
             f"plugins {plugins}" if plugins else ""]
    return ", ".join(part for part in found if part)


class ClaudeRunner:
    def __init__(self, claude_bin: str, model: str, hard_timeout: float, inactivity_timeout: float) -> None:
        self._bin = claude_bin
        self._model = model
        self._hard_timeout = hard_timeout
        self._inactivity_timeout = inactivity_timeout

    def run(
        self,
        prompt: str,
        profile: Profile,
        on_step: Callable[[Step], None] | None = None,
        cancel: threading.Event | None = None,
        interrupt: threading.Event | None = None,
        follow_for: float = 0.0,
    ) -> Result:
        """Run one turn. Setting ``cancel`` stops it at once; setting ``interrupt`` stops it as
        soon as no tool is running, so a command is never cut in half. Both end with
        ``cancelled``, and the result carries the session id, so a later run can continue the
        same session with ``--resume``.

        Silence while a tool or a background task runs is work, not a stuck turn: a build, an
        install or a wait for a render may print nothing for minutes, so the inactivity limit
        then grows to ``TOOL_SILENCE``. Claude lists its open background tasks in each
        ``background_tasks_changed`` event, and keeps the session going until they end.

        With ``follow_for`` seconds, a turn whose answer comes while background tasks are still
        open returns that answer at once, and the result's ``later`` follows the session on for
        at most that long: what Claude says when those tasks end arrives through
        ``Later.answers``. That session goes on by itself and hears nothing said meanwhile.
        No command holds such a turn longer than ``FOREGROUND_LIMIT_MS``."""
        if follow_for:
            profile = replace(profile, env={**profile.env, "BASH_MAX_TIMEOUT_MS": str(FOREGROUND_LIMIT_MS)})
        session = _Session(self._start(prompt, profile), profile, on_step)
        start = time.monotonic()
        answer: _Answer | None = None
        while True:
            stop, got = session.wait(start + self._hard_timeout, self._inactivity_timeout, cancel, interrupt)
            if got is None:
                break
            answer = got
            if follow_for and got.open and not got.error:
                session.detach()
                return session.result(got, start, running=True,
                                      later=Later(session, follow_for, self._inactivity_timeout))
        session.end(stop)
        return session.result(answer, start, stop=stop)

    def _start(self, prompt: str, profile: Profile) -> subprocess.Popen:
        # The prompt goes through stdin: an argument would be capped by the command line
        # (32K characters on Windows) and readable by every local user in the process list.
        proc = subprocess.Popen(
            [self._bin, "-p", "--model", self._model, *_STREAM_ARGS, *profile.args],
            cwd=str(profile.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**_inherited(), "IS_SANDBOX": "1", **profile.env},
            start_new_session=True,
        )

        def feed() -> None:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except OSError:
                pass

        threading.Thread(target=feed, daemon=True).start()
        return proc

_following: set[Later] = set()
_following_lock = threading.Condition()


def following() -> int:
    """How many sessions are still followed in the background after their turn answered."""
    with _following_lock:
        return len(_following)


def working() -> int:
    """How many followed sessions are doing something: replying or running a command, rather
    than only waiting in silence for their background tasks to end."""
    with _following_lock:
        return sum(not later.waiting() for later in _following)


def settle(timeout: float) -> bool:
    """Wait until no session is followed any more, at most ``timeout`` seconds."""
    with _following_lock:
        return _following_lock.wait_for(lambda: not _following, timeout)


@dataclass(frozen=True)
class _Answer:
    """One answer of a session: its text, whether Claude flagged it as an error, and whether
    background tasks were still open when it came."""

    text: str
    error: bool
    open: bool


class _Session:
    """A running Claude process, read event by event: its steps, its answers as they come,
    the tools and background tasks it has open, and the end of its stderr."""

    def __init__(self, proc: subprocess.Popen, profile: Profile, on_step: Callable[[Step], None] | None) -> None:
        self.proc = proc
        self._profile = profile
        self._on_step = on_step
        self._lock = threading.Lock()
        self._last_output = time.monotonic()
        self._answers: deque[_Answer] = deque()
        self._written: list[str] = []
        self._running: set[str] = set()
        self._replying = True
        self._background: dict[str, str] = {}
        self._commands: dict[str, str] = {}
        self._errors: deque[str] = deque(maxlen=20)
        self.session_id = ""
        self.refused = ""
        self._readers = [threading.Thread(target=self._read_events, daemon=True),
                         threading.Thread(target=self._read_errors, daemon=True)]
        for reader in self._readers:
            reader.start()

    def detach(self) -> None:
        """Stop showing steps: the turn that watched them has its answer."""
        self._on_step = None

    def _touch(self) -> None:
        with self._lock:
            self._last_output = time.monotonic()

    def _read_events(self) -> None:
        for line in iter(self.proc.stdout.readline, ""):
            self._touch()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("session_id"):
                self.session_id = event["session_id"]
            if event.get("subtype") == "init" and self._profile.tools is not None:
                self.refused = beyond(event, self._profile.tools)
            if event.get("type") == "result":
                with self._lock:
                    text = event.get("result") or "\n\n".join(self._written)
                    self._answers.append(_Answer(text, bool(event.get("is_error")), bool(self._background)))
                    self._written.clear()
                    self._replying = False
                continue
            if event.get("type") == "system" and event.get("subtype") == "background_tasks_changed":
                with self._lock:
                    self._background = {t.get("task_id", ""): t.get("description", "")
                                        for t in event.get("tasks") or [] if isinstance(t, dict)}
            blocks = _blocks(event)
            with self._lock:
                if event.get("type") in ("assistant", "user", "stream_event"):
                    self._replying = True
                self._remember_commands(event, blocks)
                self._running.update(b.get("id", "") for b in blocks if event.get("type") == "assistant"
                                     and b.get("type") == "tool_use")
                self._running.difference_update(b.get("tool_use_id", "") for b in blocks if b.get("type") == "tool_result")
                if event.get("type") == "assistant":
                    self._written.extend(b.get("text", "") for b in blocks if b.get("type") == "text")
            on_step = self._on_step
            if on_step:
                for step in to_steps(event):
                    on_step(step)
        self.proc.stdout.close()

    def _remember_commands(self, event: dict, blocks: list[dict]) -> None:
        """Keep the command behind each background task, from the tool call that started it."""
        for block in blocks:
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict) and block["input"].get("command"):
                self._commands[block.get("id", "")] = str(block["input"]["command"])
        started = event.get("tool_use_result")
        if isinstance(started, dict) and started.get("backgroundTaskId"):
            for block in blocks:
                if block.get("type") == "tool_result":
                    self._commands[started["backgroundTaskId"]] = self._commands.get(block.get("tool_use_id", ""), "")

    def waiting(self) -> bool:
        """Whether the session only waits for its background tasks: it has answered, and
        nothing has run or been written since."""
        with self._lock:
            return bool(self._background) and not self._running and not self._replying

    def waits(self) -> list[str]:
        """The background tasks still open, each as its description and the command behind it."""
        with self._lock:
            return [" | ".join(part for part in (description, self._commands.get(task, "")) if part) or task
                    for task, description in self._background.items()]

    def _read_errors(self) -> None:
        for line in iter(self.proc.stderr.readline, ""):
            self._touch()
            self._errors.append(line.rstrip())
        self.proc.stderr.close()

    def wait(
        self,
        deadline: float,
        inactivity_timeout: float,
        cancel: threading.Event | None = None,
        interrupt: threading.Event | None = None,
        background_silence: float | None = None,
    ) -> tuple[str, _Answer | None]:
        """Wait for the next answer. Without one, say why the wait ended: ``exited``,
        ``timed_out``, ``inactivity``, ``cancelled`` or ``refused``. Silence while background
        tasks are open and no tool runs may last ``background_silence`` seconds, or
        ``TOOL_SILENCE`` when not given; ``0`` means as long as the deadline allows."""
        while True:
            now = time.monotonic()
            with self._lock:
                if self._answers:
                    return "", self._answers.popleft()
                idle = now - self._last_output
                busy = bool(self._running)
                background = bool(self._background)
            if self.proc.poll() is not None:
                for reader in self._readers:
                    reader.join(timeout=5)
                with self._lock:
                    if self._answers:
                        return "", self._answers.popleft()
                return "exited", None
            if now > deadline:
                return "timed_out", None
            limit = self._inactivity_timeout_for(busy, background, inactivity_timeout, background_silence)
            if limit and idle > limit:
                return "inactivity", None
            if (cancel is not None and cancel.is_set()) or (interrupt is not None and interrupt.is_set() and not busy):
                return "cancelled", None
            if self.refused:
                return "refused", None
            time.sleep(0.5)

    @staticmethod
    def _inactivity_timeout_for(busy: bool, background: bool, inactivity_timeout: float,
                                background_silence: float | None) -> float:
        if busy:
            return max(inactivity_timeout, TOOL_SILENCE)
        if background:
            return max(inactivity_timeout, TOOL_SILENCE) if background_silence is None else background_silence
        return inactivity_timeout

    def end(self, stop: str) -> None:
        """Stop the process when the wait ended without it, then whatever it left behind."""
        if stop not in ("", "exited"):
            _terminate(self.proc)
        self.proc.wait()
        _sweep(self.proc)
        for reader in self._readers:
            reader.join(timeout=5)

    def result(self, answer: _Answer | None, start: float, stop: str = "", later: Later | None = None,
               running: bool = False) -> Result:
        """The result of one answer, or of the wait that ended without one. An answer given
        while the process ``running`` on is complete without its exit code."""
        failed = stop not in ("", "exited")
        error = answer is not None and answer.error
        text = (answer.text if answer else "\n\n".join(self._written)).strip()
        return Result(
            ok=not (failed or error) and (running or self.proc.returncode == 0),
            text=text,
            timed_out=stop == "timed_out",
            inactivity=stop == "inactivity",
            returncode=self.proc.returncode,
            duration=time.monotonic() - start,
            session_id=self.session_id,
            cancelled=stop == "cancelled",
            refused=self.refused,
            failure="\n".join(([answer.text] if error else []) + list(self._errors)),
            later=later,
        )


class Later:
    """A session that answered while background tasks were still open, followed on until it
    ends or runs past its own time limit. While those tasks run in silence nothing counts as
    stuck; once Claude works again, the usual limits apply. It stays followed until whoever
    reads its answers has taken the last one."""

    def __init__(self, session: _Session, timeout: float, inactivity_timeout: float) -> None:
        self._session = session
        self._timeout = timeout
        self._inactivity_timeout = inactivity_timeout
        with _following_lock:
            _following.add(self)

    def waiting(self) -> bool:
        """Whether the session only waits in silence for its background tasks."""
        return self._session.waiting()

    def waits(self) -> list[str]:
        """What the session still waits for, one background task per line."""
        return self._session.waits()

    def answers(self, cancel: threading.Event | None = None) -> Iterator[Result]:
        """Each further answer of the session, as it comes. A session stopped by its time limit
        or by ``cancel`` gives one last result that says so."""
        start = time.monotonic()
        stop = ""
        try:
            try:
                while True:
                    stop, answer = self._session.wait(start + self._timeout, self._inactivity_timeout,
                                                      cancel, background_silence=0)
                    if answer is None:
                        break
                    yield self._session.result(answer, start, running=True)
            finally:
                self._session.end(stop or "cancelled")
            if stop != "exited":
                yield self._session.result(None, start, stop=stop)
        finally:
            with _following_lock:
                _following.discard(self)
                _following_lock.notify_all()


def _terminate(proc: subprocess.Popen) -> None:
    """Stop the whole process group so Claude's own children go down with it."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(10):
            if proc.poll() is not None:
                return
            time.sleep(0.5)
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _sweep(proc: subprocess.Popen) -> None:
    """Stop what the turn left running in the background, so finished turns hold no memory.

    Claude runs as the leader of its own process group, and whatever it starts stays
    in that group unless it detaches on purpose.
    """
    if os.name == "nt":
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.2)
            os.killpg(proc.pid, 0)
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
