"""How strongly a turn runs: the model and the reasoning effort it gets, chosen with no call to Claude.

Every chat turn, parallel mind and nightly study runs on the configured Claude model, so a newer
model takes over by changing one id, and at xhigh effort: the person's messages, the plans that
come due, notices and minds alike. News from a parallel mind runs at high, as the owner chose: it
only passes on work done alone; a lesson review runs at high too. ``max``, the highest effort
Claude offers, is never asked for, and no effort named in a message changes the choice.

Every choice is kept in ``model_choices`` with how the turn ended and how long it took.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, replace

from . import i18n
from .claude_runner import Result, unavailable
from .config import Config

log = logging.getLogger("zoe.strength")

# The effort of every run, and the one of news from a parallel mind and of a lesson review.
EFFORT, NEWS, LESSON = "xhigh", "high", "high"
# The step each effort has in the level column of model_choices, which a version of Zoe restored by an undo
# reads to resume a conversation or a mind at the same effort.
_LEVELS = {"high": 3, "xhigh": 5}
# Where a mistake costs, as the minds and Zoe's briefs are told.
COSTLY = "money, messages to other people, data that cannot be recovered, services that are running"


@dataclass(frozen=True)
class Choice:
    category: str
    model: str
    effort: str
    reason: str
    id: int = 0


def for_turn(cfg: Config, kind: str) -> Choice:
    """The model and effort of a chat turn, by the ``kind`` of its first message: news from a parallel mind
    runs at ``NEWS``, every other turn at ``EFFORT``."""
    if kind == "mind":
        return Choice("mind_news", cfg.claude_model, NEWS, "news of a parallel mind to pass on")
    return Choice(kind, cfg.claude_model, EFFORT, "every turn runs at the one effort")


def _of_mind(conn: sqlite3.Connection, mind_id: int) -> sqlite3.Row | None:
    """The latest choice a parallel mind runs on, once made."""
    return conn.execute("SELECT * FROM model_choices WHERE source = 'mind' AND mind_id = ? ORDER BY id DESC LIMIT 1",
                        (mind_id,)).fetchone()


def for_mind(conn: sqlite3.Connection, cfg: Config, mind_id: int) -> Choice:
    """The model and effort of a parallel mind. The runs that resume it keep its choice, with its id, while the
    choice kept names the same model and effort; otherwise the choice comes without an id, to be kept anew."""
    made = Choice("mind", cfg.claude_model, EFFORT, "a parallel mind works alone")
    row = _of_mind(conn, mind_id)
    return replace(made, id=row["id"]) if row and (row["model"], row["effort"]) == (made.model, made.effort) else made


def for_study(cfg: Config) -> Choice:
    return Choice("study", cfg.claude_model, EFFORT, "the nightly study reads and weighs many days")


def for_lesson(cfg: Config) -> Choice:
    return Choice("lesson", cfg.claude_model, LESSON, "a lesson review reads finished work and keeps what it taught")


def record(conn: sqlite3.Connection, choice: Choice, source: str, chat_id: int | None = None,
           message_id: int | None = None, mind_id: int | None = None) -> Choice:
    """Keep a choice; return it with its id."""
    cur = conn.execute(
        "INSERT INTO model_choices (source, chat_id, message_id, mind_id, category, level, model, effort, reason, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, chat_id, message_id, mind_id, choice.category, _LEVELS[choice.effort], choice.model, choice.effort,
         choice.reason, time.time()))
    log.info("%s %s runs on %s at %s effort: %s", source, message_id or mind_id or "", choice.model, choice.effort,
             choice.reason)
    return replace(choice, id=cur.lastrowid)


def outcome(result: Result) -> str:
    """How a run ended, as kept with the model and effort it ran on."""
    if unavailable(result):
        return "unavailable"
    if result.refused:
        return "refused"
    if result.timed_out or result.inactivity:
        return "stuck"
    if result.cancelled:
        return "cancelled"
    return "answered" if result.ok else "failed"


def ended(conn: sqlite3.Connection, choice_id: int, outcome: str, duration: float | None = None) -> None:
    """How the turn of a choice ended."""
    conn.execute("UPDATE model_choices SET outcome = ?, duration = ? WHERE id = ?", (outcome, duration, choice_id))


def model_name(model: str) -> str:
    """A model id as a person reads it: ``claude-sonnet-5-5`` is "Sonnet 5.5"."""
    name = re.sub(r"^claude-|-\d{8}$", "", model.strip())
    return " ".join(word.capitalize() for word in re.sub(r"(?<=\d)-(?=\d)", ".", name).split("-"))


def label(cfg: Config, language: str, model: str, effort: str) -> str:
    """The model and effort of a run as the person reads them, such as "Sonnet 5.5, medium effort": the line
    a reply ends with, and what the menu tells of a parallel mind. Empty while
    ``cfg.show_strength`` is off, so nothing shows them."""
    if not cfg.show_strength:
        return ""
    key = f"effort_{effort}"
    named = i18n.t(language, key)
    return i18n.t(language, "strength", model=model_name(model), effort=effort if named == key else named)


def of_turn(conn: sqlite3.Connection, cfg: Config, language: str, chat_id: int, message_id: int) -> str:
    """The label of the model and effort the latest turn of a chat's message ran on."""
    row = conn.execute(
        "SELECT model, effort FROM model_choices WHERE source = 'chat' AND chat_id = ? AND message_id = ? "
        "ORDER BY id DESC LIMIT 1", (chat_id, message_id)).fetchone()
    return label(cfg, language, row["model"], row["effort"]) if row else ""


def of_mind(conn: sqlite3.Connection, cfg: Config, language: str, mind_id: int) -> str:
    """The label of the model and effort a parallel mind runs on, once chosen."""
    row = _of_mind(conn, mind_id)
    return label(cfg, language, row["model"], row["effort"]) if row else ""
