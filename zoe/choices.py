"""Questions Zoe asks with buttons, and the answers that come back to her.

When a reply carries an options directive, the question is sent with one button
per option. Tapping one closes the question, leaves the pick shown under it, and
queues the pick as the person's next message, so Zoe continues the conversation
knowing what was chosen. The person can still answer by writing instead.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import i18n, store
from .access import Person
from .telegram import TelegramAPI

_SHORT_LABEL = 14
NOOP = "x"


def keyboard(choice_id: int, options: list[str]) -> list[list[dict]]:
    """Lay out one blue button per option: two per row when every label is short, one otherwise."""
    buttons = [{"text": label, "callback_data": f"c:{choice_id}:{i}", "style": "primary"} for i, label in enumerate(options)]
    width = 2 if all(len(label) <= _SHORT_LABEL for label in options) else 1
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


def create(conn: sqlite3.Connection, chat_id: int, person_id: int, options: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO choices (chat_id, person_id, options, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, person_id, json.dumps(options, ensure_ascii=False), time.time()),
    )
    return int(cur.lastrowid)


def attach(conn: sqlite3.Connection, choice_id: int, telegram_id: int | None) -> None:
    conn.execute("UPDATE choices SET telegram_id = ? WHERE id = ?", (telegram_id, choice_id))


def resolve(conn: sqlite3.Connection, choice_id: int, index: int, person_id: int) -> str | None:
    """Record the tapped option and return its label, or None if the choice is not open to this person."""
    row = conn.execute(
        "SELECT options, state, person_id FROM choices WHERE id = ?", (choice_id,)
    ).fetchone()
    if row is None or row["state"] != "open" or row["person_id"] != person_id:
        return None
    labels = json.loads(row["options"])
    if not 0 <= index < len(labels):
        return None
    cur = conn.execute(
        "UPDATE choices SET state = 'answered', answer = ? WHERE id = ? AND state = 'open'",
        (labels[index], choice_id),
    )
    return labels[index] if cur.rowcount else None


def handle(conn: sqlite3.Connection, api: TelegramAPI, person: Person, query: dict) -> None:
    """Turn a tap on an option into the person's next message."""
    parts = (query.get("data") or "").split(":")
    label = None
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        label = resolve(conn, int(parts[1]), int(parts[2]), person.id)
    if label is None:
        api.answer_callback(query["id"], i18n.t(person.language, "choice_closed"))
        return
    message = query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    api.edit_buttons(chat_id, message.get("message_id"),
                     [[{"text": f"✅ {label}", "callback_data": NOOP, "style": "success"}]])
    store.enqueue(conn, chat_id, person.id, label, "choice")
    api.answer_callback(query["id"])
