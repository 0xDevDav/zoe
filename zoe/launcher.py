"""Keep Zoe running across restarts and roll back a change that breaks her start.

The launcher is deliberately small and is one of the files Zoe does not change on
her own. It runs the service as a child process and:

- refuses to start when another Zoe runs with the same home, and hands its lock
  to the service;
- starts it again when it exits with ``RESTART``, as it does after a core change;
- trusts a commit only once it has proven itself: for ``STABLE_SECONDS`` the
  service's heartbeat (``health.py``) says every part of it is alive, and it has
  read from Telegram. Staying up is not enough: a version can run deaf;
- stops a service whose heartbeat stops or reports a part gone, so a frozen
  version is caught too;
- when a commit that has not proven itself fails, moves the repository back to
  the last good commit, keeps the failed one on a branch for inspection, leaves a
  notice for the owners, and starts again. Uncommitted changes are stashed
  first, never lost. The database stays as it is: schema changes only add, so an
  older version runs on a newer database;
- otherwise retries with a growing pause, and stops when the service stops cleanly.

Run it with ``python -m zoe.launcher`` (or ``zoe-run``) from the repository.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import health, instance, notices
from .config import Config

RESTART = 3
STABLE_SECONDS = 90
# Seconds after STABLE_SECONDS a service may go without its first heartbeat before it counts as sick.
START_GRACE = 120

CORE_ROOT = Path(__file__).resolve().parents[1]


def decide(code: int | None, proven: bool, head: str, last_good: str | None) -> str:
    """Return what to do after the service exits: stop, restart, rollback or retry."""
    if code == 0:
        return "stop"
    if code == RESTART:
        return "restart"
    if not proven and last_good and head != last_good:
        return "rollback"
    return "retry"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def rollback(repo: Path, bad: str, good: str) -> str:
    """Return the repository to ``good``, keeping ``bad`` on a branch; return that branch name.

    A git killed half way leaves ``index.lock`` behind and every later git command would fail,
    so it goes first; changes not committed are stashed, never thrown away.
    """
    (repo / ".git" / "index.lock").unlink(missing_ok=True)
    if _git(repo, "status", "--porcelain"):
        _git(repo, "stash", "push", "--include-untracked", "-m", f"before rolling back {bad[:8]}")
    branch = f"failed/{bad[:8]}-{time.strftime('%Y%m%d-%H%M%S')}"
    _git(repo, "branch", "-f", branch, bad)
    _git(repo, "reset", "--hard", good)
    return branch


def _stop(child: subprocess.Popen) -> None:
    """Stop a sick service: politely, so its turns are handed back, then for good."""
    child.terminate()
    try:
        child.wait(timeout=60)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def main() -> int:
    cfg = Config.from_env()
    lock = instance.hold(cfg.data_dir)
    if lock is None:
        print(f"another Zoe is already running with {cfg.data_dir}", file=sys.stderr)
        return 1
    good_file = cfg.data_dir / "last_good"
    stopping = False
    child: subprocess.Popen | None = None

    def forward(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)

    pause = 1.0
    while True:
        head = _git(CORE_ROOT, "rev-parse", "HEAD")
        (cfg.data_dir / health.FILE).unlink(missing_ok=True)
        started = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, "-m", "zoe"], cwd=CORE_ROOT, env={**os.environ, instance.HELD: "1"},
            pass_fds=() if os.name == "nt" else (lock.fileno(),),
        )
        proven = False
        while child.poll() is None:
            time.sleep(1)
            beat = health.read(cfg.data_dir)
            state = health.judge(beat, child.pid, time.time())
            elapsed = time.monotonic() - started
            if state == "good" and not proven and elapsed >= STABLE_SECONDS:
                good_file.write_text(head, encoding="utf-8")
                proven = True
            mute = (not beat or beat.get("pid") != child.pid) and elapsed > STABLE_SECONDS + START_GRACE
            if (state == "sick" or mute) and not stopping:
                _stop(child)
        if stopping:
            return 0

        last_good = good_file.read_text(encoding="utf-8").strip() if good_file.exists() else None
        action = decide(child.returncode, proven, head, last_good)
        if action == "stop":
            return 0
        if action == "rollback":
            branch = rollback(CORE_ROOT, head, last_good)
            notices.discard(cfg.data_dir, "self_updated")
            notices.post(cfg.data_dir, "rolled_back", branch=branch)
        if action in ("restart", "rollback"):
            pause = 1.0
            continue
        time.sleep(pause)
        pause = min(pause * 2, 60.0)


if __name__ == "__main__":
    sys.exit(main())
