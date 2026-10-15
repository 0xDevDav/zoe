"""Checks for the launcher: one Zoe per home, restarts on request, rollback of a change that breaks the start."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import health, instance
from zoe.launcher import RESTART, decide, rollback


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_only_one_zoe_runs_per_home():
    with tempfile.TemporaryDirectory() as tmp:
        first = instance.hold(Path(tmp))
        assert first is not None and instance.hold(Path(tmp)) is None
        first.close()
        again = instance.hold(Path(tmp))
        assert again is not None
        again.close()


def test_decisions():
    assert decide(0, False, "new", "old") == "stop"
    assert decide(RESTART, False, "new", "old") == "restart"
    assert decide(1, False, "new", "old") == "rollback"
    assert decide(1, False, "old", "old") == "retry"
    assert decide(1, True, "new", "old") == "retry"
    assert decide(1, False, "new", None) == "retry"


def test_a_version_is_trusted_on_its_heartbeat_not_on_staying_up():
    with tempfile.TemporaryDirectory() as tmp:
        home, now = Path(tmp), time.time()
        assert health.judge(health.read(home), os.getpid(), now) == "waiting"
        health.write(home, healthy=True, ready=False)
        assert health.judge(health.read(home), os.getpid(), now) == "waiting"
        health.write(home, healthy=True, ready=True)
        assert health.judge(health.read(home), os.getpid(), now) == "good"
        assert health.judge(health.read(home), os.getpid() + 1, now) == "waiting"
        assert health.judge(health.read(home), os.getpid(), now + health.STALE + 1) == "sick"
        health.write(home, healthy=False, ready=True)
        assert health.judge(health.read(home), os.getpid(), now) == "sick"


def test_rollback_restores_the_last_good_commit_and_keeps_the_failed_one():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo = Path(tmp) / "repo"
        _git(Path(tmp), "init", "-q", "-b", "main", str(repo))
        _git(repo, "config", "user.name", "Tester")
        _git(repo, "config", "user.email", "tester@example.com")
        (repo / "app.py").write_text("version = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "good")
        good = _git(repo, "rev-parse", "HEAD")
        (repo / "app.py").write_text("version = 2\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "bad")
        bad = _git(repo, "rev-parse", "HEAD")

        (repo / "notes.txt").write_text("work in progress", encoding="utf-8")
        (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
        branch = rollback(repo, bad, good)
        assert _git(repo, "rev-parse", "HEAD") == good
        assert (repo / "app.py").read_text(encoding="utf-8") == "version = 1\n"
        assert _git(repo, "rev-parse", branch) == bad
        assert "before rolling back" in _git(repo, "stash", "list")


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
