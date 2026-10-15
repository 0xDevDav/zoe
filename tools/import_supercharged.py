"""Bring a claude-telegram-supercharged installation into Zoe: its chat history and its speech glossary.

    python tools/import_supercharged.py --messages /path/to/telegram/data/messages.db \\
        --glossary /path/to/glossary.txt

Run it once, with the service stopped, after the owners and the members are set up:
messages of chats that belong to nobody are left out. Messages already imported are
skipped, so running it again is harmless. The observer then reads the imported history
in the background and learns from it.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import access, vocabulary
from zoe.brain import Brain
from zoe.config import Config
from zoe.db import apply_schema, connect


def import_messages(conn: sqlite3.Connection, old: Path) -> tuple[int, int]:
    """Copy the old chat history in, oldest first; return how many were added and how many left out."""
    source = sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute("SELECT * FROM messages ORDER BY date, id").fetchall()
    finally:
        source.close()
    added = left_out = 0
    for row in rows:
        chat = int(row["chat_id"])
        person = access.resolve(conn, chat).person
        text = row["text"] or row["caption"] or (f"[{row['media_type']}]" if row["media_type"] else None)
        number = row["message_id"] if row["message_id"] and row["message_id"] > 0 else None
        known = conn.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? AND telegram_id = ?", (chat, number)
        ).fetchone() if number is not None else conn.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? AND created_at = ? AND text = ?", (chat, row["date"], text)
        ).fetchone()
        if person is None or text is None or known:
            left_out += 1
            continue
        conn.execute(
            "INSERT INTO messages (chat_id, person_id, sender_id, telegram_id, direction, kind, text, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'text', ?, 'answered', ?, ?)",
            (chat, person.id, int(row["user_id"]) if row["user_id"] else None, number,
             "out" if row["is_outgoing"] else "in", text, row["date"], row["date"]),
        )
        added += 1
    return added, left_out


def import_glossary(conn: sqlite3.Connection, glossary: Path) -> int:
    """Teach the owner's speech glossary the terms of a comma separated list; return how many."""
    owner = conn.execute("SELECT id FROM persons WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    if owner is None:
        raise SystemExit("no owner yet: start Zoe once with ZOE_OWNER_IDS set, then import")
    brain = Brain(conn, owner["id"])
    terms = [term.strip() for term in glossary.read_text(encoding="utf-8").replace("\n", ",").split(",")]
    for term in filter(None, terms):
        brain.boost(brain.ensure(vocabulary.TERM, term), 0.5)
    return len([t for t in terms if t])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--messages", type=Path, help="the old messages.db")
    parser.add_argument("--glossary", type=Path, help="the old comma separated glossary")
    args = parser.parse_args()
    conn = connect(Config.from_env().db_path)
    apply_schema(conn)
    if args.messages:
        added, left_out = import_messages(conn, args.messages)
        print(f"messages: {added} imported, {left_out} left out")
    if args.glossary:
        print(f"glossary: {import_glossary(conn, args.glossary)} terms")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
