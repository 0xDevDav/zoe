"""Whether Claude can work at all, shared by chat turns and parallel minds.

When Claude cannot work (logged out, or a usage limit reached), every turn and
every mind waits instead of failing: the turn goes back to the queue, the mind
back to 'queued' with its session, and nothing starts again before the retry
moment. A usage limit says when it resets ("resets 6pm (Europe/Rome)"), and the
retry waits for that moment; any other cause is tried again every minute. The
owner's main chat hears it once when Claude stops and once when it works again,
through the outbox, so the notice is logged in the chat like anything else Zoe says.
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime

from . import i18n, say, store
from .claude_runner import DOWN, RETRY_AT
from .config import Config
from .usage import resets_at

log = logging.getLogger("zoe.availability")

RETRY = 60.0
_RESETS = re.compile(r"\bresets\s+(.+?)\s*$", re.I)
_lock = threading.Lock()


def waiting(conn: sqlite3.Connection) -> bool:
    """Whether turns and minds must wait before trying Claude again."""
    return float(store.get_state(conn, RETRY_AT, "0") or 0) > time.time()


def resumes_at(reason: str, now: datetime | None = None) -> float | None:
    """When a usage limit lets Claude work again, from how Claude words it, or None."""
    found = _RESETS.search(reason)
    if not found:
        return None
    return resets_at(found.group(1), now or datetime.now().astimezone())


def down(conn: sqlite3.Connection, cfg: Config, reason: str) -> None:
    """Claude cannot work: hold every turn and mind until the retry moment, and tell the owner
    the first time."""
    now = time.time()
    resume = resumes_at(reason)
    store.set_state(conn, RETRY_AT, str(max(resume or 0.0, now + RETRY)))
    with _lock:
        told = store.get_state(conn, DOWN)
        store.set_state(conn, DOWN, reason)
    if told:
        return
    log.error("Claude is unavailable, turns wait: %s", reason)
    for owner_id, language in say.notice_recipients(conn, cfg):
        if resume and resume > now:
            text = i18n.t(language, "claude_limit", time=_moment(resume, now))
        else:
            text = i18n.t(language, "claude_down", reason=html.escape(reason))
        say.queue(conn, cfg.data_dir, owner_id, text)


def works(conn: sqlite3.Connection, cfg: Config) -> None:
    """A run worked: if Claude was unavailable, the owner hears it is back."""
    with _lock:
        was = store.get_state(conn, DOWN)
        if was:
            store.set_state(conn, DOWN, "")
    if not was:
        return
    log.info("Claude works again")
    for owner_id, language in say.notice_recipients(conn, cfg):
        say.queue(conn, cfg.data_dir, owner_id, i18n.t(language, "claude_back"))


def _moment(when: float, now: float) -> str:
    """A time of day, with the date when it is not today."""
    moment, today = datetime.fromtimestamp(when), datetime.fromtimestamp(now)
    return moment.strftime("%H:%M" if moment.date() == today.date() else "%d/%m %H:%M")
