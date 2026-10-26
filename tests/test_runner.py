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

from zoe import claude_runner
from zoe.claude_runner import ClaudeRunner, Profile, following

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


# A turn that answers, then waits in silence for its background task and answers again.
BACKGROUND_CLAUDE = r'''
import json, sys, time
def say(event):
    print(json.dumps(event), flush=True)
sys.stdin.read()
say({"type": "system", "subtype": "init", "session_id": "s-7", "tools": []})
say({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}})
say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}})
say({"type": "system", "subtype": "background_tasks_changed", "tasks": [{"task_id": "b1"}]})
say({"type": "result", "result": "waiting for the render", "is_error": False})
time.sleep(2)
say({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
say({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]}})
say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2"}]}})
say({"type": "result", "result": "the render is ready", "is_error": False})
'''

# A turn whose stream carries events of unexpected shapes.
ODD_CLAUDE = r'''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-8", "tools": []}), flush=True)
print(json.dumps({"type": "system", "message": "a plain note"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": ["loose text", {"type": "text", "text": "hi"}]}}), flush=True)
print(json.dumps(["not", "an", "event"]), flush=True)
print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
    "five_hour": {"utilization": 0.11, "resetsAt": 4000000000}}}}), flush=True)
print(json.dumps({"type": "rate_limit_event", "rate_limit_info": "not a report"}), flush=True)
print(json.dumps({"type": "result", "result": "done", "is_error": False}), flush=True)
'''


def _claude(folder: Path, source: str = FAKE_CLAUDE) -> str:
    """A command that runs the stand-in, as ``claude`` would be run."""
    script = folder / "fake_claude.py"
    script.write_text(source, encoding="utf-8")
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


def test_a_silent_background_task_is_work_and_its_final_answer_is_kept():
    with tempfile.TemporaryDirectory() as tmp:
        runner = ClaudeRunner(_claude(Path(tmp), BACKGROUND_CLAUDE), "model", hard_timeout=30, inactivity_timeout=0.8)
        result = runner.run("ciao", Profile((), Path(tmp)))
        assert result.ok and not result.inactivity, result
        assert result.text == "the render is ready"


def test_a_turn_answers_while_its_background_task_runs_and_its_later_answer_follows():
    with tempfile.TemporaryDirectory() as tmp:
        runner = ClaudeRunner(_claude(Path(tmp), BACKGROUND_CLAUDE), "model", hard_timeout=30, inactivity_timeout=0.8)
        started, steps = time.monotonic(), []
        result = runner.run("ciao", Profile((), Path(tmp)), on_step=steps.append, follow_for=30)
        shown = len(steps)
        assert result.ok and result.text == "waiting for the render" and result.session_id == "s-7", result
        assert time.monotonic() - started < 1.5 and result.later is not None and following() == 1
        later = list(result.later.answers())
        assert [r.text for r in later] == ["the render is ready"] and later[0].ok
        assert following() == 0 and len(steps) == shown, "the turn's progress no longer shows steps"


def test_background_work_followed_past_its_own_limit_is_stopped_and_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        runner = ClaudeRunner(_claude(Path(tmp), BACKGROUND_CLAUDE.replace("time.sleep(2)", "time.sleep(30)")),
                              "model", hard_timeout=30, inactivity_timeout=0.8)
        result = runner.run("ciao", Profile((), Path(tmp)), follow_for=1)
        started = time.monotonic()
        later = list(result.later.answers())
        assert len(later) == 1 and later[0].timed_out and not later[0].ok
        assert time.monotonic() - started < 10 and following() == 0


ENV_CLAUDE = r'''
import json, os, sys
sys.stdin.read()
print(json.dumps({"type": "result", "result": os.environ.get("BASH_MAX_TIMEOUT_MS", "none"), "is_error": False}), flush=True)
'''


def test_a_turn_that_answers_first_lets_no_command_hold_it_past_the_foreground_limit():
    with tempfile.TemporaryDirectory() as tmp:
        runner = ClaudeRunner(_claude(Path(tmp), ENV_CLAUDE), "model", hard_timeout=30, inactivity_timeout=20)
        assert runner.run("ciao", Profile((), Path(tmp), {"A": "1"}), follow_for=60).text == "120000"
        assert runner.run("ciao", Profile((), Path(tmp))).text == "none"


def test_events_of_unexpected_shapes_are_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        failures = []
        hook, threading.excepthook = threading.excepthook, failures.append
        try:
            steps = []
            runner = ClaudeRunner(_claude(Path(tmp), ODD_CLAUDE), "model", hard_timeout=30, inactivity_timeout=20)
            result = runner.run("ciao", Profile((), Path(tmp)), on_step=steps.append)
        finally:
            threading.excepthook = hook
        assert not failures, failures
        assert result.ok and result.text == "done" and result.session_id == "s-8", result
        assert claude_runner.reported()[0] == {"unifiedWindows": {"five_hour": {"utilization": 0.11, "resetsAt": 4000000000}}}


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
