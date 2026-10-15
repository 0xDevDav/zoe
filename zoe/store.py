"""Message queue and runtime values over SQLite.

Inbound messages are persisted the moment they arrive, so nothing is lost when
a component restarts. Their ``state`` column drives the work queue:
``received`` -> ``processing`` -> ``answered`` / ``failed``.

Each chat is served one message at a time, in arrival order, while different
chats are served in parallel by separate workers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass

from . import content


@dataclass(frozen=True)
class Message:
    id: int
    chat_id: int
    person_id: int | None
    telegram_id: int | None
    text: str | None
    kind: str
    payload: dict | None
    attempts: int


# --- runtime state ---------------------------------------------------------

def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_offset(conn: sqlite3.Connection) -> int:
    return int(get_state(conn, "telegram_offset", "0"))


def set_offset(conn: sqlite3.Connection, offset: int) -> None:
    set_state(conn, "telegram_offset", str(offset))


# --- inbound queue ---------------------------------------------------------

# Workers sleep on this between looks at the queue, and whatever queues work wakes them at once.
_queued = threading.Condition()


def _wake() -> None:
    with _queued:
        _queued.notify_all()


def wait_for_work(timeout: float) -> None:
    """Sleep until work is queued in this process, or for ``timeout`` seconds at most."""
    with _queued:
        _queued.wait(timeout)


def _reply(msg: dict) -> dict | None:
    """Describe the message this one replies to, and the part of it quoted, if any."""
    replied = msg.get("reply_to_message")
    if not replied:
        return None
    return {
        "id": replied.get("message_id"),
        "from_bot": bool(replied.get("from", {}).get("is_bot")),
        "text": content.text_of(replied) or "",
        "quote": (msg.get("quote") or {}).get("text") or "",
    }


def record_inbound(conn: sqlite3.Connection, update: dict, person_id: int) -> int | None:
    """Queue an inbound message from an approved person; None if it was already queued or needs no turn.

    An edited message is queued again as an edit. A live location moving is an edit too,
    every few seconds, and needs no turn.
    """
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    kind, payload = content.classify(msg)
    if "edited_message" in update:
        if kind in ("location", "venue"):
            return None
        payload["edited"] = True
    reply = _reply(msg)
    if reply:
        payload["reply"] = reply
    now = time.time()
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(update_id, chat_id, person_id, sender_id, telegram_id, direction, kind, text, payload, state, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'in', ?, ?, ?, 'received', ?, ?)",
        (
            update["update_id"],
            msg["chat"]["id"],
            person_id,
            msg.get("from", {}).get("id"),
            msg.get("message_id"),
            kind,
            content.text_of(msg),
            json.dumps(payload) if payload else None,
            now,
            now,
        ),
    )
    if not cur.rowcount:
        return None
    _wake()
    return cur.lastrowid


def record_reaction(conn: sqlite3.Connection, update: dict, person_id: int) -> int | None:
    """Log a reaction a person put on one of Zoe's messages, as a message that needs no turn: the
    observer and the next turns read how that message landed. None when the reaction is taken
    away or is on a message that is not Zoe's."""
    reaction = update["message_reaction"]
    chat_id, target = reaction["chat"]["id"], reaction.get("message_id")
    shown = [r.get("emoji") or r.get("type", "") for r in reaction.get("new_reaction") or []]
    found = find_message(conn, chat_id, target) if target else None
    if not shown or found is None or not found["mine"]:
        return None
    said = " ".join(found["text"].split())
    said = said if len(said) <= 120 else said[:119] + "…"
    now = time.time()
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages (update_id, chat_id, person_id, sender_id, direction, kind, text, state, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'in', 'reaction', ?, 'answered', ?, ?)",
        (update["update_id"], chat_id, person_id, (reaction.get("user") or {}).get("id"),
         f"[reaction {' '.join(shown)} to Zoe's message #{target}: «{said}»]", now, now),
    )
    return cur.lastrowid if cur.rowcount else None


def enqueue(
    conn: sqlite3.Connection, chat_id: int, person_id: int, text: str, kind: str, payload: dict | None = None
) -> int:
    """Queue work for a chat that did not arrive as a Telegram message, such as a tapped option or a due plan."""
    now = time.time()
    cur = conn.execute(
        "INSERT INTO messages (chat_id, person_id, direction, kind, text, payload, state, created_at, updated_at) "
        "VALUES (?, ?, 'in', ?, ?, ?, 'received', ?, ?)",
        (chat_id, person_id, kind, text, json.dumps(payload) if payload else None, now, now),
    )
    _wake()
    return int(cur.lastrowid)


_RETURNING = "RETURNING id, chat_id, person_id, telegram_id, text, kind, payload, attempts"
_FOLLOWUP = (
    "chat_id = ? AND direction = 'in' AND state = 'received' AND id > ? AND kind NOT IN ('plan', 'mind')"
)


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        chat_id=row["chat_id"],
        person_id=row["person_id"],
        telegram_id=row["telegram_id"],
        text=row["text"],
        kind=row["kind"],
        payload=json.loads(row["payload"]) if row["payload"] else None,
        attempts=row["attempts"],
    )


def claim_next(conn: sqlite3.Connection) -> Message | None:
    """Atomically take the oldest pending message of a chat that has no turn in progress."""
    row = conn.execute(
        "UPDATE messages SET state = 'processing', attempts = attempts + 1, updated_at = ? "
        "WHERE id = ("
        "  SELECT m.id FROM messages m"
        "  WHERE m.direction = 'in' AND m.state = 'received'"
        "    AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.chat_id = m.chat_id AND p.state = 'processing')"
        "  ORDER BY m.id LIMIT 1"
        f") {_RETURNING}",
        (time.time(),),
    ).fetchone()
    return _message(row) if row else None


def waiting(conn: sqlite3.Connection, chat_id: int, after_id: int) -> list[tuple[int, float]]:
    """Return (id, arrival time) of the person's messages waiting in a chat after ``after_id``."""
    rows = conn.execute(
        f"SELECT id, created_at FROM messages WHERE {_FOLLOWUP} ORDER BY id", (chat_id, after_id)
    ).fetchall()
    return [(r["id"], r["created_at"]) for r in rows]


def absorb(conn: sqlite3.Connection, chat_id: int, after_id: int) -> list[Message]:
    """Take the person's messages waiting in a chat after ``after_id`` into the turn in progress."""
    rows = conn.execute(
        f"UPDATE messages SET state = 'processing', attempts = attempts + 1, updated_at = ? WHERE {_FOLLOWUP} "
        f"{_RETURNING}",
        (time.time(), chat_id, after_id),
    ).fetchall()
    return sorted((_message(r) for r in rows), key=lambda m: m.id)


def release(conn: sqlite3.Connection, chat_id: int, keep: int) -> None:
    """Return to the queue the messages a chat's turn took in, all but ``keep``.

    A chat has one turn at a time, so every message of the chat in progress belongs to it.
    """
    conn.execute(
        "UPDATE messages SET state = 'received', updated_at = ? WHERE chat_id = ? AND state = 'processing' AND id != ?",
        (time.time(), chat_id, keep),
    )


def mark(conn: sqlite3.Connection, message_id: int, state: str) -> None:
    conn.execute(
        "UPDATE messages SET state = ?, updated_at = ? WHERE id = ?",
        (state, time.time(), message_id),
    )


def note(conn: sqlite3.Connection, message_id: int, key: str, value: object) -> None:
    """Keep something about a message in its payload, for the next try of its turn."""
    conn.execute(
        "UPDATE messages SET payload = json_set(COALESCE(payload, '{}'), ?, json(?)), updated_at = ? WHERE id = ?",
        (f"$.{key}", json.dumps(value), time.time(), message_id),
    )


def set_read(conn: sqlite3.Connection, message_id: int, text: str) -> None:
    """Store the text Claude reads for a message, its files saved and its sound transcribed, once for all."""
    conn.execute(
        "UPDATE messages SET text = ?, payload = json_set(COALESCE(payload, '{}'), '$.read', 1), updated_at = ? "
        "WHERE id = ?",
        (text, time.time(), message_id),
    )


def requeue(conn: sqlite3.Connection, message_id: int, counted: bool = True) -> None:
    """Put a message back in the queue; one that is not ``counted`` gets its attempt back."""
    conn.execute(
        "UPDATE messages SET state = 'received', attempts = attempts - ?, updated_at = ? WHERE id = ?",
        (0 if counted else 1, time.time(), message_id),
    )


def reset_stale_processing(conn: sqlite3.Connection) -> int:
    """Return any message left in ``processing`` by an interrupted run to the queue."""
    cur = conn.execute(
        "UPDATE messages SET state = 'received', updated_at = ? WHERE state = 'processing'",
        (time.time(),),
    )
    return cur.rowcount


def record_outbound(
    conn: sqlite3.Connection,
    chat_id: int,
    telegram_id: int | None,
    text: str,
    person_id: int | None = None,
    payload: dict | None = None,
) -> None:
    """Log what Zoe sent; ``payload`` notes where it came from, such as the plan it carried out."""
    now = time.time()
    conn.execute(
        "INSERT INTO messages "
        "(chat_id, person_id, telegram_id, direction, kind, text, payload, state, created_at, updated_at) "
        "VALUES (?, ?, ?, 'out', 'text', ?, ?, 'answered', ?, ?)",
        (chat_id, person_id, telegram_id, text, json.dumps(payload) if payload else None, now, now),
    )


def revise_outbound(conn: sqlite3.Connection, chat_id: int, telegram_id: int, text: str) -> None:
    """Log the new text of a message Zoe rewrote."""
    conn.execute(
        "UPDATE messages SET text = ?, updated_at = ? WHERE chat_id = ? AND telegram_id = ? AND direction = 'out'",
        (text, time.time(), chat_id, telegram_id),
    )


def find_message(conn: sqlite3.Connection, chat_id: int, telegram_id: int) -> dict | None:
    """Return a message of a chat, sent by Zoe or by the person, found by its Telegram id."""
    row = conn.execute(
        "SELECT direction, text, payload, created_at FROM messages WHERE chat_id = ? AND telegram_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (chat_id, telegram_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "mine": row["direction"] == "out",
        "text": row["text"] or "",
        "payload": json.loads(row["payload"]) if row["payload"] else {},
        "created_at": row["created_at"],
    }


def recent_history(conn: sqlite3.Connection, chat_id: int, limit: int = 10, before: int | None = None) -> list[dict]:
    """Return the last completed exchanges of a chat, oldest first, for conversation context; with
    ``before``, those that came before that message.

    Plans Zoe ran and news from her parallel minds are left out: they are her own
    business, and only what she sent about them is part of the chat. A message sent
    as a reply says which message it answered.
    """
    rows = conn.execute(
        "SELECT direction, text, telegram_id, json_extract(payload, '$.reply.id') AS reply_to FROM messages "
        "WHERE chat_id = ? AND text IS NOT NULL AND state = 'answered' AND kind NOT IN ('plan', 'mind') "
        "AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
        (chat_id, before, before, limit),
    ).fetchall()
    return [
        {"direction": r["direction"], "text": r["text"], "telegram_id": r["telegram_id"], "reply_to": r["reply_to"]}
        for r in reversed(rows)
    ]
