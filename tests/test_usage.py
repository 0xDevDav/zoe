"""Checks for Claude's usage limits: read from /usage's answer, and read again only when old."""

import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import claude_runner, usage
from zoe.config import Config
from zoe.usage import Limit, Usage

ANSWER = """You are currently using your subscription to power your Claude Code usage

Current session: 42% used · resets Oct 14, 5:29pm (Europe/Rome)
Current week (all models): 39% used · resets Oct 19, 2:59am (Europe/Rome)
Current week (Fable): 0% used · resets Jan 2, 3am (Europe/Rome)

Last 24h · 1338 requests · 187 sessions
  29% of your usage was while 4+ sessions ran in parallel"""


def test_the_limits_are_read_with_when_they_reset():
    rome = ZoneInfo("Europe/Rome")
    now = datetime(2026, 10, 14, 15, 0, tzinfo=rome)
    session, week, fable = usage.parse(ANSWER, now)
    assert (session.label, session.used) == ("Current session", 42.0)
    assert session.resets_at == datetime(2026, 10, 14, 17, 29, tzinfo=rome).timestamp()
    assert (week.label, week.used, fable.label) == ("Current week (all models)", 39.0, "Current week (Fable)")
    assert week.resets_at == datetime(2026, 10, 19, 2, 59, tzinfo=rome).timestamp()
    assert fable.resets_at == datetime(2027, 1, 2, 3, 0, tzinfo=rome).timestamp()
    assert usage.parse("You are using an API key.") == []


def test_a_reading_runs_in_the_background_and_only_when_old():
    readings = []

    def reader() -> list[Limit]:
        readings.append(time.time())
        return [Limit("Current session", 10.0, None)]

    found = Usage(Config(bot_token="", owner_ids=frozenset(), data_dir=Path("."), db_path=Path("x"),
                         vault_path=Path("x"), vault_key_path=Path("x"), workdir=Path(".")), reader)
    assert found.latest() == []
    deadline = time.time() + 5
    while not found.latest() and time.time() < deadline:
        time.sleep(0.01)
    assert found.latest() == [Limit("Current session", 10.0, None)] and len(readings) == 1


def test_the_limits_a_run_reported_come_first_and_a_window_past_its_reset_is_unused():
    readings = []

    def reader() -> list[Limit]:
        readings.append(time.time())
        return [Limit("Current session", 30.0, None), Limit("Current week (Fable)", 0.0, None)]

    found = Usage(Config(bot_token="", owner_ids=frozenset(), data_dir=Path("."), db_path=Path("x"),
                         vault_path=Path("x"), vault_key_path=Path("x"), workdir=Path(".")), reader)
    later = time.time() + 3600
    report = {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.11, "resetsAt": later}, "seven_day": {"utilization": 0.07, "resetsAt": 1.0},
        "overage": "not a window"}}
    saved, claude_runner._reported = claude_runner._reported, (report, time.time())
    try:
        assert found.latest() == [Limit("Current session", 11.0, later), Limit("Current week (all models)", 0.0, None)]
        assert readings == []

        # A report older than FRESH, as while a long mind works alone: /usage is read, and its
        # newer reading shows, without the limits of a single model.
        claude_runner._reported = (report, time.time() - usage.FRESH - 1)
        found.latest()
        deadline = time.time() + 5
        newer = [Limit("Current session", 30.0, None)]
        while found.latest() != newer and time.time() < deadline:
            time.sleep(0.01)
        assert found.latest() == newer and len(readings) == 1
        claude_runner._reported = (report, time.time() + 1)
        assert found.latest()[0] == Limit("Current session", 11.0, later)
    finally:
        claude_runner._reported = saved
    assert usage.from_report({"unifiedWindows": "none"}, time.time()) == []


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
