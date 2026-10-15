"""The plan's usage limits as Claude Code's /usage reports them: the 5-hour session and the
weekly limits, each with how much is used and when it resets.

``claude -p /usage`` answers on its own, without a model call, in a few seconds. So the
menu never waits for it: a reading runs in the background when someone looks and the
last one is older than FRESH, and the menu shows the last reading meanwhile.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .claude_runner import _inherited
from .config import Config

log = logging.getLogger("zoe.usage")

# A reading is reused this long: limits move slowly, and /usage is asked as rarely as a person would.
FRESH = 300.0
_TIMEOUT = 90
# "Current session: 42% used · resets Oct 14, 5:29pm (Europe/Rome)"
_LINE = re.compile(r"^(?P<label>[^:\n]+):\s*(?P<used>\d+(?:\.\d+)?)% used(?:\W+resets\s+(?P<resets>.+?))?\s*$", re.M)
_FORMATS = ("%b %d, %I:%M%p", "%b %d, %I%p")


@dataclass(frozen=True)
class Limit:
    label: str
    used: float
    resets_at: float | None


def resets_at(text: str, now: datetime) -> float | None:
    """When a limit resets, from how /usage writes it ("Oct 19, 3am (Europe/Rome)"), as a
    timestamp; ``now`` carries its time zone."""
    zone = re.search(r"\s*\(([^)]+)\)\s*$", text)
    when = text[:zone.start()] if zone else text
    try:
        tz = ZoneInfo(zone.group(1)) if zone else now.tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        tz = now.tzinfo
    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(when.strip(), fmt)
        except ValueError:
            continue
        moment = parsed.replace(year=now.year, tzinfo=tz)
        # /usage leaves the year out: a reset more than a day behind is next year's.
        if moment < now - timedelta(days=1):
            moment = moment.replace(year=now.year + 1)
        return moment.timestamp()
    return None


def parse(text: str, now: datetime | None = None) -> list[Limit]:
    """The limits in /usage's answer, in its order."""
    now = now or datetime.now().astimezone()
    return [Limit(m["label"].strip(), float(m["used"]), resets_at(m["resets"], now) if m["resets"] else None)
            for m in _LINE.finditer(text)]


def read(claude_bin: str, cwd: Path) -> list[Limit]:
    """Ask Claude Code for the limits now; raises OSError or ValueError when it cannot say."""
    done = subprocess.run(
        [claude_bin, "-p", "/usage", "--output-format", "json", "--no-session-persistence"],
        cwd=cwd, env=_inherited(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=_TIMEOUT,
    )
    answer = json.loads(done.stdout or "{}")
    if answer.get("is_error") or not isinstance(answer.get("result"), str):
        raise ValueError(f"/usage answered no limits: {done.stderr.strip()[-300:]}")
    return parse(answer["result"])


class Usage:
    """The last reading of the limits, read again in the background when it is old."""

    def __init__(self, cfg: Config, reader: Callable[[], list[Limit]] | None = None) -> None:
        self._read_now = reader or (lambda: read(cfg.claude_bin, cfg.data_dir))
        self._lock = threading.Lock()
        self._limits: list[Limit] = []
        self._read_at = 0.0
        self._reading = False

    def latest(self) -> list[Limit]:
        with self._lock:
            if not self._reading and time.time() - self._read_at > FRESH:
                self._reading = True
                threading.Thread(target=self._refresh, name="usage", daemon=True).start()
            return self._limits

    def _refresh(self) -> None:
        try:
            limits = self._read_now()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.warning("usage limits not read: %s", exc)
            limits = None
        with self._lock:
            self._reading, self._read_at = False, time.time()
            if limits is not None:
                self._limits = limits
