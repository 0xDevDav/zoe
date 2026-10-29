"""The observer: learns about each person from finished conversations, beside the chat.

It wakes every minute and, for each person it observes, reads the messages that
came after the last ones it read, once the conversation has paused or enough of
them have piled up. A separate Claude run with no tools reads them together with
what is already known, and answers with what they show:

- signals: how the person seems (mood, energy, stress), kept as readings over time;
- memories: lasting facts, preferences, habits and goals, and feedback on how Zoe
  should behave with them, kept as the person's neurons and wired to the
  conversations they come from, to the known words they name, and to each other
  when one conversation teaches them together. A memory that corrects a known one
  revises it instead of adding a copy.

The observer cannot talk to anyone or touch anything: its answer is data that
this module reads. Owners are observed unless they turn it off; anyone else only
once it is turned on. Once a night its thread also runs the review (``review.py``),
which sorts what was learned.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

from . import access, i18n, review, signals, vocabulary
from .access import Person
from .brain import Brain, Recalled
from .claude_runner import ClaudeRunner, Profile
from .config import Config
from .db import connect
from .planner import stamp

log = logging.getLogger("zoe.observer")

KINDS = ("fact", "preference", "habit", "goal", "feedback")
SOURCE = "observer"
BATCH = 30
_TEXT = 500
_KNOWN = 40
_CONTENT = 300
_RETRY_AFTER = 900.0
ARGS = (
    "--tools", "",
    "--permission-mode", "dontAsk",
    "--strict-mcp-config",
    "--setting-sources", "project",
    "--disable-slash-commands",
    "--no-session-persistence",
)


def enabled(conn: sqlite3.Connection, person: Person) -> bool:
    row = conn.execute("SELECT enabled FROM observation WHERE person_id = ?", (person.id,)).fetchone()
    return bool(row["enabled"]) if row else person.role == "owner"


def set_enabled(conn: sqlite3.Connection, person_id: int, on: bool) -> None:
    conn.execute(
        "INSERT INTO observation (person_id, enabled) VALUES (?, ?) "
        "ON CONFLICT(person_id) DO UPDATE SET enabled = excluded.enabled",
        (person_id, int(on)),
    )


def _advance(conn: sqlite3.Connection, person: Person, message_id: int) -> None:
    conn.execute(
        "INSERT INTO observation (person_id, enabled, cursor) VALUES (?, ?, ?) "
        "ON CONFLICT(person_id) DO UPDATE SET cursor = excluded.cursor",
        (person.id, int(enabled(conn, person)), message_id),
    )


def counts(conn: sqlite3.Connection, person_id: int) -> dict[str, int]:
    """How many things of each kind the observer has learned about a person."""
    rows = conn.execute(
        f"SELECT kind, COUNT(*) AS n FROM neurons WHERE owner_id = ? AND kind IN ({','.join('?' * len(KINDS))}) "
        "GROUP BY kind",
        (person_id, *KINDS),
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


def unread(conn: sqlite3.Connection, person_id: int, limit: int = BATCH) -> list[sqlite3.Row]:
    """Return the finished messages of a person not read yet, oldest first, stopping
    before any message of theirs still waiting for its turn."""
    return conn.execute(
        "SELECT id, telegram_id, direction, text, created_at FROM messages "
        "WHERE person_id = ? AND state = 'answered' AND text IS NOT NULL AND kind NOT IN ('plan', 'mind') "
        "AND id > COALESCE((SELECT cursor FROM observation WHERE person_id = ?), 0) "
        "AND id < COALESCE((SELECT MIN(id) FROM messages WHERE person_id = ? "
        "                   AND state IN ('received', 'processing')), 9e18) "
        "ORDER BY id LIMIT ?",
        (person_id, person_id, person_id, limit),
    ).fetchall()


def ready(rows: list[sqlite3.Row], now: float, idle: float) -> bool:
    """A batch is read once the conversation has paused, or when it is full."""
    return bool(rows) and (len(rows) >= BATCH or now - rows[-1]["created_at"] >= idle)


def _known(brain: Brain, rows: list[sqlite3.Row]) -> list[Recalled]:
    """What is already known that the batch may touch: the most salient items and those it recalls."""
    seeds = brain.search(" ".join(r["text"] for r in rows if r["direction"] == "in"))
    found = {r.id: r for r in brain.top(KINDS, _KNOWN)}
    for r in brain.recall(seeds, limit=_KNOWN):
        if r.kind in KINDS:
            found.setdefault(r.id, r)
    return list(found.values())[:_KNOWN]


def build_prompt(person: Person, rows: list[sqlite3.Row], known: list[Recalled], trends: list[signals.Trend]) -> str:
    language = i18n.language_name(person.language)
    lines = [
        f"You observe conversations between Zoe, a personal assistant, and {person.name}, to learn about "
        f"{person.name} and about how Zoe should behave with them. You never talk to anyone: a program reads "
        "your answer.",
    ]
    if known:
        lines.append("\nWhat is already known, each with its number:")
        lines += [f"- #{r.id} [{r.kind}] {r.content}" for r in known]
    if trends:
        lines.append("\nRecent readings, from 1 (very low) to 5 (very high):")
        lines += [f"- {t.kind}: average {t.average:.1f} over {t.count}, last {t.last:g} ({t.last_note})" for t in trends]
    lines.append("\nNew messages:")
    for r in rows:
        who = "Zoe" if r["direction"] == "out" else person.name
        text = r["text"] if len(r["text"]) <= _TEXT else r["text"][:_TEXT] + "…"
        lines.append(f"[#{r['telegram_id'] or '-'}] {stamp(r['created_at'])} {who}: {text}")
    lines.append(
        "\nAnswer with one JSON object and nothing else:\n"
        '{"signals": [{"kind": "mood", "value": 2, "note": "...", "message": 4521}], '
        '"memories": [{"kind": "preference", "content": "...", "replaces": null, "messages": [4521]}]}\n'
        f"- signals: how {person.name} seems, only where the messages show it. kind is mood, energy or stress; "
        "value goes from 1 (very low) to 5 (very high); note says why in a few words; message is the number "
        "of the message it shows in.\n"
        f"- memories: lasting things worth remembering about {person.name}. kind is fact (people, places, work, "
        "health, events), preference, habit or goal, or feedback for what the messages show about how Zoe "
        "should behave with them: what helped, what bothered them, what to do differently. Their reactions "
        "to Zoe's messages, written as [reaction ...], show how each one landed. content is one "
        f"short sentence in {language}. replaces is the number of a known item it corrects or updates, else "
        "null. messages are the numbers it comes from.\n"
        "Leave out what is already known and unchanged, small talk, and what is true only for a moment. "
        "Never guess beyond what the messages show. Empty lists are a fine answer."
    )
    return "\n".join(lines)


def json_object(answer: str) -> dict:
    """The JSON object of an answer, even with words around it; ValueError when it holds none."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the answer")
    data = json.loads(answer[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    return data


def parse(answer: str) -> tuple[list[dict], list[dict]]:
    """Return the signals and memories of the observer's answer; raise ValueError if it holds no JSON object."""
    data = json_object(answer)
    found_signals = [s for s in data.get("signals") or [] if isinstance(s, dict)]
    found_memories = [m for m in data.get("memories") or [] if isinstance(m, dict)]
    return found_signals, found_memories


def numbers(value: object) -> list[int]:
    items = value if isinstance(value, list) else [value]
    return [int(v) for v in items if isinstance(v, int) or (isinstance(v, str) and v.isdigit())]


def apply(
    conn: sqlite3.Connection, person: Person, rows: list[sqlite3.Row], found_signals: list[dict], found_memories: list[dict]
) -> tuple[int, int]:
    """Store what the observer found; return how many signals and memories were kept."""
    brain = Brain(conn, person.id)
    when = {r["telegram_id"]: r["created_at"] for r in rows if r["telegram_id"]}
    kept_signals = kept_memories = 0

    for item in found_signals:
        kind, value = item.get("kind"), item.get("value")
        if kind not in signals.OBSERVED or not isinstance(value, (int, float)) or not 1 <= value <= 5:
            continue
        at = next((when[n] for n in numbers(item.get("message")) if n in when), rows[-1]["created_at"])
        signals.record(conn, person.id, kind, float(value), SOURCE, str(item.get("note") or "")[:_CONTENT], at)
        kept_signals += 1

    learned: list[int] = []
    for item in found_memories:
        kind, content = item.get("kind"), str(item.get("content") or "").strip()[:_CONTENT]
        if kind not in KINDS or not content:
            continue
        replaced = next(iter(numbers(item.get("replaces"))), None)
        target = brain.get(replaced) if replaced else None
        if target and target.kind in KINDS and brain.revise(target.id, content):
            brain.boost(target.id, 0.3)
            neuron = target.id
        elif (same := brain.find(kind, content)) is not None:
            brain.boost(same, 0.3)
            neuron = same
        else:
            neuron = brain.learn(kind, content, related=brain.anchored(numbers(item.get("messages"))))
        link_words(brain, neuron, content)
        learned.append(neuron)
        kept_memories += 1
    # What one conversation taught fires together, so it is wired together.
    brain.fire(learned)
    return kept_signals, kept_memories


def link_words(brain: Brain, neuron: int, content: str) -> None:
    """Wire a memory to the known words it names, so a message about them reaches it."""
    for word in vocabulary.extract_terms(content):
        if (term := brain.find(vocabulary.TERM, word)) is not None:
            brain.connect(neuron, term)


class Observer(threading.Thread):
    def __init__(self, cfg: Config, stop: threading.Event, interval: float = 60.0) -> None:
        super().__init__(name="observer", daemon=True)
        self._cfg = cfg
        self._halt = stop
        self._interval = interval
        self._runner = ClaudeRunner(cfg.claude_bin, cfg.observer_model, 900.0, cfg.claude_inactivity_timeout)
        self._profile = Profile(ARGS, cfg.data_dir / "observer")
        self._retry_at: dict[int, float] = {}
        self._reviewer = review.Reviewer(cfg)

    def run(self) -> None:
        self._profile.cwd.mkdir(parents=True, exist_ok=True)
        conn = connect(self._cfg.db_path)
        log.info("observer started")
        while not self._halt.wait(self._interval):
            try:
                people = [person for person, _ in access.people(conn)]
            except Exception:
                log.exception("people to observe not read")
                continue
            for person in people:
                if self._halt.is_set():
                    break
                try:
                    self.observe(conn, person, time.time())
                except Exception:
                    log.exception("observation of person %d failed", person.id)
                    self._retry_at[person.id] = time.time() + _RETRY_AFTER
            try:
                if not self._halt.is_set() and review.due(conn, self._cfg.review_at, time.time()):
                    self._reviewer.run(conn, time.time())
            except Exception:
                log.exception("nightly review failed")
        conn.close()
        log.info("observer stopped")

    def observe(self, conn: sqlite3.Connection, person: Person, now: float) -> bool:
        """Read one batch of a person's messages if it is time; return whether one was read."""
        if not enabled(conn, person) or now < self._retry_at.get(person.id, 0.0):
            return False
        rows = unread(conn, person.id)
        if not ready(rows, now, self._cfg.observer_idle):
            return False
        brain = Brain(conn, person.id)
        prompt = build_prompt(person, rows, _known(brain, rows), signals.trends(conn, person.id, now=now))
        result = self._runner.run(prompt, self._profile)
        if not result.ok:
            log.warning("observer run for person %d failed (rc=%s): %s", person.id, result.returncode,
                        result.failure[:200])
            self._retry_at[person.id] = now + _RETRY_AFTER
            return False
        try:
            kept = apply(conn, person, rows, *parse(result.text))
            log.info("observed %d message(s) of person %d: %d signal(s), %d memory(ies)",
                     len(rows), person.id, *kept)
        except ValueError as exc:
            log.warning("observer answer for person %d unreadable, batch skipped: %s", person.id, exc)
        _advance(conn, person, rows[-1]["id"])
        return True
