"""Run Claude for a single turn and watch it while it works.

Claude streams one JSON event per line while it thinks, writes and uses tools.
The runner turns those events into steps for a live progress view, keeps the
final answer, and uses the stream as a heartbeat: a turn that emits nothing for
``inactivity_timeout`` seconds, or runs past ``hard_timeout``, is terminated and
reported as stuck. Each turn is its own process, gone when the turn ends, and
whatever it left running in the background is stopped with it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
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
    """One visible thing Claude is doing: ``thinking``, ``writing``, or using a ``tool``."""

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


# Runtime state while Claude cannot work at all: why, and when the next try may run.
DOWN = "claude_down"
RETRY_AT = "claude_retry_at"

# Words of a failure that stops every turn, not just this one: logged out, or out of usage.
_UNAVAILABLE = ("/login", "not logged in", "invalid api key", "oauth token", "authentication_error",
                "usage limit", "limit reached", "hit your limit", "credit balance")


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
        return [
            Step("tool", block.get("name", ""), block.get("input") or {})
            for block in (event.get("message") or {}).get("content", [])
            if block.get("type") == "tool_use"
        ]
    return []


# Seconds a running tool may stay silent before the turn counts as stuck.
TOOL_SILENCE = 1200.0

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
    ) -> Result:
        """Run one turn. Setting ``cancel`` stops it at once; setting ``interrupt`` stops it as
        soon as no tool is running, so a command is never cut in half. Both end with
        ``cancelled``, and the result carries the session id, so a later run can continue the
        same session with ``--resume``.

        Silence while a tool runs is work, not a stuck turn: a build or an install may print
        nothing for minutes, so the inactivity limit then grows to ``TOOL_SILENCE``."""
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

        last_output = time.monotonic()
        lock = threading.Lock()
        outcome: dict[str, object] = {}
        written: list[str] = []
        running: set[str] = set()

        def touch() -> None:
            nonlocal last_output
            with lock:
                last_output = time.monotonic()

        def read_events() -> None:
            for line in iter(proc.stdout.readline, ""):
                touch()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("session_id"):
                    outcome["session"] = event["session_id"]
                if event.get("subtype") == "init" and profile.tools is not None:
                    outcome["refused"] = beyond(event, profile.tools)
                if event.get("type") == "result":
                    outcome["text"] = event.get("result") or ""
                    outcome["error"] = bool(event.get("is_error"))
                    continue
                blocks = (event.get("message") or {}).get("content", [])
                blocks = blocks if isinstance(blocks, list) else []
                with lock:
                    running.update(b.get("id", "") for b in blocks if event.get("type") == "assistant"
                                   and b.get("type") == "tool_use")
                    running.difference_update(b.get("tool_use_id", "") for b in blocks if b.get("type") == "tool_result")
                if event.get("type") == "assistant":
                    written.extend(b.get("text", "") for b in blocks if b.get("type") == "text")
                if on_step:
                    for step in to_steps(event):
                        on_step(step)
            proc.stdout.close()

        errors: deque[str] = deque(maxlen=20)

        def read_errors() -> None:
            for line in iter(proc.stderr.readline, ""):
                touch()
                errors.append(line.rstrip())
            proc.stderr.close()

        readers = [threading.Thread(target=read_events, daemon=True), threading.Thread(target=read_errors, daemon=True)]
        for reader in readers:
            reader.start()

        start = time.monotonic()
        timed_out = inactivity = cancelled = False
        while proc.poll() is None:
            now = time.monotonic()
            with lock:
                idle = now - last_output
                busy = bool(running)
            if now - start > self._hard_timeout:
                timed_out = True
                break
            if idle > (max(self._inactivity_timeout, TOOL_SILENCE) if busy else self._inactivity_timeout):
                inactivity = True
                break
            if (cancel is not None and cancel.is_set()) or (interrupt is not None and interrupt.is_set() and not busy):
                cancelled = True
                break
            if outcome.get("refused"):
                break
            time.sleep(0.5)

        if timed_out or inactivity or cancelled or outcome.get("refused"):
            self._terminate(proc)

        proc.wait()
        self._sweep(proc)
        for reader in readers:
            reader.join(timeout=5)

        text = str(outcome.get("text") or "\n\n".join(written)).strip()
        return Result(
            ok=not (timed_out or inactivity or cancelled or outcome.get("error") or outcome.get("refused"))
            and proc.returncode == 0,
            text=text,
            timed_out=timed_out,
            inactivity=inactivity,
            returncode=proc.returncode,
            duration=time.monotonic() - start,
            session_id=str(outcome.get("session") or ""),
            cancelled=cancelled,
            refused=str(outcome.get("refused") or ""),
            failure="\n".join(([str(outcome.get("text") or "")] if outcome.get("error") else []) + list(errors)),
        )

    @staticmethod
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

    @staticmethod
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
