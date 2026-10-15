"""Measurements about a person over time.

A signal is one reading: a kind (``mood``, ``energy``, ``stress``, and later what
devices measure), a value, an optional note saying why, its source and when it
was true. The observer writes the moods it sees in conversations; devices will
write their readings the same way, so everything known about a person over time
sits in one place and can be compared.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

# Kinds read from conversations, on a scale from 1 (very low) to 5 (very high).
OBSERVED = ("mood", "energy", "stress")


@dataclass(frozen=True)
class Trend:
    kind: str
    average: float
    count: int
    last: float
    last_note: str
    last_at: float


def record(
    conn: sqlite3.Connection,
    person_id: int,
    kind: str,
    value: float,
    source: str,
    note: str = "",
    at: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO signals (person_id, kind, value, note, source, at) VALUES (?, ?, ?, ?, ?, ?)",
        (person_id, kind, value, note or None, source, at or time.time()),
    )


def trends(conn: sqlite3.Connection, person_id: int, days: float = 7, now: float | None = None) -> list[Trend]:
    """Return, for each kind measured in the last ``days``, its average and its latest reading."""
    since = (now or time.time()) - days * 86400
    rows = conn.execute(
        "SELECT kind, value, note, at FROM signals WHERE person_id = ? AND at >= ? ORDER BY at",
        (person_id, since),
    ).fetchall()
    readings: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        readings.setdefault(row["kind"], []).append(row)
    return [
        Trend(kind, sum(r["value"] for r in found) / len(found), len(found),
              found[-1]["value"], found[-1]["note"] or "", found[-1]["at"])
        for kind, found in readings.items()
    ]
