"""SQLite connection factory and schema setup."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode, tuned for concurrent threads.

    Each thread opens its own connection; the WAL journal lets the poller and
    the worker read and write at the same time.
    """
    conn = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False, timeout=30.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# Columns added to a table after it first shipped: (table, column, definition). The schema
# file has them too, for new databases; existing ones get them here.
_ADDED_COLUMNS = (
    ("minds", "directive_key", "TEXT"),
    ("persons", "folders", "TEXT"),
    ("outbox", "options", "TEXT"),
    ("minds", "phases", "TEXT"),
    ("minds", "phase", "INTEGER NOT NULL DEFAULT 0"),
)


def apply_schema(conn: sqlite3.Connection) -> None:
    indexed = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'neurons_fts'").fetchone()
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not indexed:
        conn.execute("INSERT INTO neurons_fts (neurons_fts) VALUES ('rebuild')")
    for table, column, definition in _ADDED_COLUMNS:
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
