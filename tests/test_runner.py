"""Checks for the Claude runner against a stand-in program that streams like Claude Code:
the prompt through stdin, the session id, silence while a tool runs, polite interrupts."""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe.claude_runner import ClaudeRunner, Profile

FAKE_CLAUDE = r'''
import json, sys, time
def say(event):
    print(json.dumps(event), flush=True)
prompt = sys.stdin.read()
say({"type": "system", "subtype": "init", "session_id": "s-42", "tools": []})
say({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}})
time.sleep(1.5)
say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}})
time.sleep(float(sys.argv[-1]))
say({"type": "result", "result": f"read {len(prompt)} characters", "is_error": False})
'''


def _claude(folder: Path) -> str:
    """A command that runs the stand-in, as ``claude`` would be run."""
    script = folder / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":
        launcher = folder / "claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = folder / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return str(launcher)


def test_a_silent_tool_is_work_and_the_prompt_goes_through_stdin():
    with tempfile.TemporaryDirectory() as tmp:
        runner = ClaudeRunner(_claude(Path(tmp)), "model", hard_timeout=30, inactivity_timeout=0.8)
        result = runner.run("x" * 50_000, Profile(("0.1",), Path(tmp)))
        assert result.ok and not result.inactivity, result
        assert result.text == "read 50000 characters" and result.session_id == "s-42"


def test_a_polite_interrupt_waits_for_the_running_tool():
    with tempfile.TemporaryDirectory() as tmp:
        runner = ClaudeRunner(_claude(Path(tmp)), "model", hard_timeout=30, inactivity_timeout=20)
        interrupt = threading.Event()
        threading.Timer(0.3, interrupt.set).start()
        started = time.monotonic()
        result = runner.run("ciao", Profile(("10",), Path(tmp)), interrupt=interrupt)
        took = time.monotonic() - started
        assert result.cancelled and result.session_id == "s-42"
        assert 1.3 < took < 8, took


def test_a_cancel_stops_at_once():
    with tempfile.TemporaryDirectory() as tmp:
        runner = ClaudeRunner(_claude(Path(tmp)), "model", hard_timeout=30, inactivity_timeout=20)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        result = runner.run("ciao", Profile(("10",), Path(tmp)), cancel=cancel)
        assert result.cancelled and time.monotonic() - started < 1.4


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
