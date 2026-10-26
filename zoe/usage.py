"""The plan's usage limits: the 5-hour session and the week, each with how much is used and
when it resets.

Every Claude run reports them along with its requests, so the menu shows what the latest
run reported. When nothing newer than FRESH is known, as after a start or while a long
parallel mind works alone, it also asks ``claude -p /usage``, which answers on its own,
without a model call, in a few seconds: a reading runs in the background when someone
looks, at most once every FRESH, and the menu shows the newest of the two meanwhile.
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

from . import claude_runner
from .claude_runner import _inherited
from .config import Config

log = logging.getLogger("zoe.usage")

# A reading is reused this long: limits move slowly, and /usage is asked as rarely as a person would.
FRESH = 300.0
_TIMEOUT = 90
# "Current session: 42% used · resets Oct 14, 5:29pm (Europe/Rome)"
_LINE = re.compile(r"^(?P<label>[^:\n]+):\s*(?P<used>\d+(?:\.\d+)?)% used(?:\W+resets\s+(?P<resets>.+?))?\s*$", re.M)
_FORMATS = ("%b %d, %I:%M%p", "%b %d, %I%p")
# A reset within the day is written as a bare time ("6pm", "5:30pm").
_TIMES = ("%I:%M%p", "%I%p")


@dataclass(frozen=True)
class Limit:
    label: str
    used: float
    resets_at: float | None


# The windows a run reports, by the labels /usage gives them.
_WINDOWS = (("five_hour", "Current session"), ("seven_day", "Current week (all models)"))


def from_report(info: dict, now: float) -> list[Limit]:
    """The limits a run reported with its request; a window past its reset is unused again."""
    windows = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    limits = []
    for key, label in _WINDOWS:
        window = windows.get(key)
        if not isinstance(window, dict) or not isinstance(window.get("utilization"), (int, float)):
            continue
        resets = window.get("resetsAt") if isinstance(window.get("resetsAt"), (int, float)) else None
        if resets is not None and resets <= now:
            limits.append(Limit(label, 0.0, None))
        else:
            limits.append(Limit(label, round(window["utilization"] * 100, 1), resets))
    return limits


def resets_at(text: str, now: datetime) -> float | None:
    """When a limit resets, from how /usage writes it ("Oct 19, 3am (Europe/Rome)", or "6pm
    (Europe/Rome)" within the day), as a timestamp; ``now`` carries its time zone."""
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
    local = now.astimezone(tz)
    for fmt in _TIMES:
        try:
            parsed = datetime.strptime(when.strip(), fmt)
        except ValueError:
            continue
        moment = local.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        # A bare time more than half a day behind is tomorrow's; one just passed is today's.
        if moment < local - timedelta(hours=12):
            moment += timedelta(days=1)
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
        self._limits_at = 0.0
        self._read_at = 0.0
        self._reading = False

    def latest(self) -> list[Limit]:
        """The session and the week, from the latest run's report or from /usage, whichever is
        newer; /usage is read again in the background when that is older than FRESH."""
        now = time.time()
        info, at = claude_runner.reported()
        reported = from_report(info, now)
        windows = {label for _, label in _WINDOWS}
        with self._lock:
            if reported and at >= self._limits_at:
                newest, since = reported, at
            else:
                newest, since = [limit for limit in self._limits if limit.label in windows], self._limits_at
            if now - since > FRESH and not self._reading and now - self._read_at > FRESH:
                self._reading = True
                threading.Thread(target=self._refresh, name="usage", daemon=True).start()
            return newest

    def _refresh(self) -> None:
        try:
            limits = self._read_now()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.warning("usage limits not read: %s", exc)
            limits = None
        with self._lock:
            self._reading, self._read_at = False, time.time()
            if limits is not None:
                self._limits, self._limits_at = limits, time.time()
