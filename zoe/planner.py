"""Plans: what Zoe means to do later, once or on a schedule.

A plan is a neuron of kind ``plan`` holding Zoe's instruction to herself,
private to the person it is for and wired to the conversation it came from. Its
timing sits beside it in the ``plans`` table. When a plan comes due, the planner
queues it in the person's chat, so it runs as an ordinary turn: in order with
the chat's messages, with the person's powers, and with recall starting from the
plan neuron, which brings back why it was made. Each run is wired back to the
plan, so a repeating plan gathers the history of its earlier runs.

Times are in the server's local time zone. A repeating plan follows a
five-field cron rule: minute, hour, day of month, month, day of week.

The planner's thread also keeps the service's own schedule: once an hour it
deletes the temporary files whose time has come.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import files, i18n, store
from .access import Person
from .brain import Brain
from .config import Config
from .db import connect

log = logging.getLogger("zoe.planner")

PLAN = "plan"
MEMBER_LIMIT = 20
# Seconds between two sweeps of temporary files.
_SWEEP_EVERY = 3600
OWNER_GAP = 600
MEMBER_GAP = 3600
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_HORIZON = timedelta(days=5 * 366)


class PlanError(ValueError):
    """A plan that cannot be made; the message is the i18n key that explains why."""


@dataclass(frozen=True)
class Plan:
    id: int
    person_id: int
    chat_id: int
    instruction: str
    due_at: float
    repeat: str | None


def _values(spec: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        body, _, step = part.partition("/")
        if body == "*":
            start, end = low, high
        elif "-" in body:
            first, last = body.split("-", 1)
            start, end = int(first), int(last)
        else:
            start = int(body)
            end = high if step else start
        stride = int(step) if step else 1
        if not low <= start <= end <= high or stride < 1:
            raise ValueError(spec)
        values.update(range(start, end + 1, stride))
    return values


class Cron:
    def __init__(self, rule: str) -> None:
        fields = rule.split()
        if len(fields) != 5:
            raise ValueError(rule)
        self.minutes, self.hours, self.days, self.months, weekdays = (
            _values(spec, low, high) for spec, (low, high) in zip(fields, _RANGES)
        )
        self.weekdays = {day % 7 for day in weekdays}
        self._either_day = not fields[2].startswith("*") and not fields[4].startswith("*")

    def _day_matches(self, moment: datetime) -> bool:
        day, weekday = moment.day in self.days, moment.isoweekday() % 7 in self.weekdays
        return (day or weekday) if self._either_day else (day and weekday)

    def next_after(self, moment: datetime) -> datetime:
        """Return the first minute after ``moment`` that the rule matches."""
        t = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + _HORIZON
        while t < limit:
            if t.month not in self.months:
                t = (t.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
            elif not self._day_matches(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minutes:
                t += timedelta(minutes=1)
            else:
                return t
        raise ValueError("the rule never matches")


def parse_when(when: str, now: datetime) -> tuple[float, str | None]:
    """Return the first due time and the repeat rule for a local date and time or a cron rule."""
    when = " ".join(when.split())
    try:
        return datetime.fromisoformat(when).timestamp(), None
    except ValueError:
        pass
    try:
        return Cron(when).next_after(now).timestamp(), when
    except ValueError:
        raise PlanError("plan_bad_time") from None


def min_gap(person: Person) -> int:
    """The shortest time in seconds between two runs of a repeating plan of this person:
    every run is a full turn, and an owner's turn has every power."""
    return OWNER_GAP if person.role == "owner" else MEMBER_GAP


def shortest_gap(cron: Cron, now: datetime, runs: int = 48) -> float:
    """The shortest time in seconds between consecutive runs among the next ``runs``."""
    # ponytail: samples the next 48 runs; a rule whose closest runs come later than that would pass.
    moments = [cron.next_after(now)]
    for _ in range(runs - 1):
        moments.append(cron.next_after(moments[-1]))
    return min((b - a).total_seconds() for a, b in zip(moments, moments[1:]))


def create(
    conn: sqlite3.Connection,
    brain: Brain,
    person: Person,
    chat_id: int,
    when: str,
    instruction: str,
    related: list[int],
    now: datetime | None = None,
) -> int:
    """Store a plan for ``person`` in ``brain``, wired to the neurons it came up with."""
    now = now or datetime.now()
    due_at, repeat = parse_when(when, now)
    if repeat and shortest_gap(Cron(repeat), now) < min_gap(person):
        raise PlanError("plan_too_often")
    if person.role != "owner" and len(pending(conn, person.id)) >= MEMBER_LIMIT:
        raise PlanError("plan_too_many")
    plan_id = brain.learn(PLAN, instruction, related=related)
    conn.execute(
        "INSERT INTO plans (neuron_id, chat_id, due_at, repeat) VALUES (?, ?, ?, ?)",
        (plan_id, chat_id, due_at, repeat),
    )
    return plan_id


_SELECT = (
    "SELECT n.id, n.owner_id, p.chat_id, n.content, p.due_at, p.repeat "
    "FROM plans p JOIN neurons n ON n.id = p.neuron_id "
)


def _plan(row: sqlite3.Row) -> Plan:
    return Plan(row["id"], row["owner_id"], row["chat_id"], row["content"], row["due_at"], row["repeat"])


def pending(conn: sqlite3.Connection, person_id: int) -> list[Plan]:
    rows = conn.execute(_SELECT + "WHERE n.owner_id = ? ORDER BY p.due_at", (person_id,)).fetchall()
    return [_plan(r) for r in rows]


def get(conn: sqlite3.Connection, person_id: int, plan_id: int) -> Plan | None:
    row = conn.execute(_SELECT + "WHERE n.id = ? AND n.owner_id = ?", (plan_id, person_id)).fetchone()
    return _plan(row) if row else None


def cancel(conn: sqlite3.Connection, person_id: int, plan_id: int) -> bool:
    """Forget a pending plan of this person, with its links."""
    cur = conn.execute(
        "DELETE FROM neurons WHERE id = ? AND owner_id = ? AND id IN (SELECT neuron_id FROM plans)",
        (plan_id, person_id),
    )
    return cur.rowcount > 0


def fire_due(conn: sqlite3.Connection, now: float) -> int:
    """Queue every plan that has come due, then move repeating plans to their next time."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        due = _queue_due(conn, now)
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    if own:
        conn.execute("COMMIT")
    return len(due)


def _queue_due(conn: sqlite3.Connection, now: float) -> list[Plan]:
    """Queue and move on the due plans; one transaction, so a plan is never queued twice or lost."""
    due = [_plan(r) for r in conn.execute(_SELECT + "WHERE p.due_at <= ?", (now,)).fetchall()]
    for plan in due:
        store.enqueue(conn, plan.chat_id, plan.person_id, plan.instruction, PLAN,
                      {"plan": plan.id, "due_at": plan.due_at})
        if plan.repeat:
            later = Cron(plan.repeat).next_after(datetime.fromtimestamp(now)).timestamp()
            conn.execute("UPDATE plans SET due_at = ? WHERE neuron_id = ?", (later, plan.id))
        else:
            conn.execute("DELETE FROM plans WHERE neuron_id = ?", (plan.id,))
    return due


def stamp(ts: float) -> str:
    """A time as Claude reads it in prompts."""
    return datetime.fromtimestamp(ts).strftime("%a %d %b %Y %H:%M")


def label(ts: float, lang: str) -> str:
    """A time as a person reads it in the menu."""
    moment = datetime.fromtimestamp(ts)
    weekday = i18n.t(lang, "weekdays").split()[moment.weekday()]
    return f"{weekday} {moment:%d/%m %H:%M}"


class Planner(threading.Thread):
    """Queues due plans and deletes temporary files once an hour."""

    def __init__(self, cfg: Config, stop: threading.Event, interval: float = 20.0) -> None:
        super().__init__(name="planner", daemon=True)
        self._cfg = cfg
        self._halt = stop
        self._interval = interval

    def run(self) -> None:
        conn = connect(self._cfg.db_path)
        log.info("planner started")
        swept = 0.0
        while True:
            try:
                fired = fire_due(conn, time.time())
                if fired:
                    log.info("queued %d due plan(s)", fired)
                if time.time() - swept >= _SWEEP_EVERY:
                    swept = time.time()
                    deleted = files.sweep(conn, swept)
                    if deleted:
                        log.info("deleted %d temporary file(s)", deleted)
            except Exception:
                log.exception("due plans not queued or files not swept")
            if self._halt.wait(self._interval):
                break
        conn.close()
        log.info("planner stopped")
