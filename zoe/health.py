"""The service's heartbeat, read by the launcher.

Every few seconds the service writes ``ZOE_HOME/health.json``: when, its process
id, whether every part of it is alive and its poller still turns, and whether it
has read from Telegram since it started. The launcher trusts a new version once
it has proven itself this way, not merely stayed up, and stops a version that
freezes or loses a part.

This module uses the standard library only, so the launcher can read it without
loading the service.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILE = "health.json"
# A heartbeat older than this means the service stopped beating: it is frozen.
STALE = 60.0
# Runtime state: when the service last started.
STARTED = "started_at"


def write(home: Path, healthy: bool, ready: bool) -> None:
    """Record how the service is, replacing the file in one step so it is never read half written."""
    beat = {"at": time.time(), "pid": os.getpid(), "healthy": healthy, "ready": ready}
    temporary = home / f"{FILE}.tmp"
    temporary.write_text(json.dumps(beat), encoding="utf-8")
    os.replace(temporary, home / FILE)


def read(home: Path) -> dict | None:
    try:
        return json.loads((home / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def judge(beat: dict | None, pid: int, now: float) -> str:
    """How the service with ``pid`` is: ``good`` (every part alive and Telegram read since it
    started), ``waiting`` (no heartbeat of its own yet, or Telegram not read yet), or ``sick``
    (its heartbeat stopped, or a part of it stopped)."""
    if not beat or beat.get("pid") != pid:
        return "waiting"
    if now - beat.get("at", 0) > STALE or not beat.get("healthy"):
        return "sick"
    return "good" if beat.get("ready") else "waiting"
