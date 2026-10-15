"""Messages programs on the machine send in Zoe's name: ``zoe-say``.

A script that has something to tell the owner (a certified email arrived, a shift
changes in 15 minutes, last night's backup failed) does not talk to Telegram on
its own. It hands the message to Zoe's outbox, and the service delivers it and
logs it in the chat, so Zoe knows what was said in her name and the person can
answer it or quote it. A message waits in the outbox while the service is down
and is delivered as soon as it is back.

Command line:

    zoe-say "text"                               # to the owner's first account in ZOE_OWNER_IDS
    zoe-say --chat 123456789 --from pec "text"   # to a chat, saying which program wrote it
    echo "text" | zoe-say -                      # text from stdin
    zoe-say --file report.pdf "caption"          # a file, with an optional caption
    zoe-say --file note.ogg --kind voice         # a voice note
    zoe-say --silent "text"                      # without a notification sound

Text is Telegram HTML (<b>, <i>, <code>...); a message that is not valid HTML
is sent as plain text.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from . import access, approve, formatting, store
from .config import Config
from .db import apply_schema, connect
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("zoe.say")

KINDS = ("document", "photo", "voice", "audio", "video", "animation")
# Tries before a message that keeps failing is set aside.
TRIES = 5


def queue(
    conn: sqlite3.Connection,
    home: Path,
    chat_id: int,
    text: str = "",
    file: Path | None = None,
    kind: str = "document",
    origin: str = "",
    silent: bool = False,
) -> int:
    """Put a message in the outbox. A file is copied into ``home/outbox`` first, so the program
    that asked may delete its own copy at once."""
    kept = None
    if file is not None:
        folder = home / "outbox"
        folder.mkdir(parents=True, exist_ok=True)
        kept = folder / f"{uuid.uuid4().hex}-{file.name}"
        shutil.copyfile(file, kept)
    cur = conn.execute(
        "INSERT INTO outbox (chat_id, text, file, kind, origin, silent, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, text, str(kept) if kept else None, kind if kept else None, origin, int(silent), time.time()),
    )
    return int(cur.lastrowid)


def deliver(conn: sqlite3.Connection, api: TelegramAPI) -> int:
    """Send what waits in the outbox and log it in its chat; return how many were sent."""
    sent = 0
    for row in conn.execute("SELECT * FROM outbox WHERE state = 'waiting' ORDER BY id").fetchall():
        try:
            ids = _send(api, row)
        except (TelegramError, OSError) as exc:
            attempts = row["attempts"] + 1
            conn.execute("UPDATE outbox SET attempts = ?, state = ? WHERE id = ?",
                         (attempts, "failed" if attempts >= TRIES else "waiting", row["id"]))
            log.warning("outbox message %d not sent (try %d): %s", row["id"], attempts, exc)
            continue
        person = access.resolve(conn, row["chat_id"]).person
        label = f"[{row['origin']}] " if row["origin"] else ""
        shown = row["text"] or ""
        if row["file"]:
            shown = f"[file «{_name(row['file'])}»] {shown}".strip()
        for number, part in ids:
            store.record_outbound(conn, row["chat_id"], number, label + (part if not row["file"] else shown),
                                  person.id if person else None, {"origin": row["origin"]} if row["origin"] else None)
        conn.execute("UPDATE outbox SET state = 'sent' WHERE id = ?", (row["id"],))
        if row["file"]:
            Path(row["file"]).unlink(missing_ok=True)
        sent += 1
    return sent


def _name(kept: str) -> str:
    """The name a file had before it was copied into the outbox."""
    return Path(kept).name.split("-", 1)[-1]


def _send(api: TelegramAPI, row: sqlite3.Row) -> list[tuple[int | None, str]]:
    """Send one outbox message; return the Telegram id and text of each message it took."""
    if row["file"]:
        result = api.send_file(row["chat_id"], row["kind"], row["text"] or "", path=Path(row["file"]),
                               silent=bool(row["silent"]), name=_name(row["file"]))
        return [(result.get("message_id"), row["text"] or "")]
    parts = []
    for part in formatting.split(row["text"] or ""):
        result = api.send_message(row["chat_id"], part, silent=bool(row["silent"]))
        parts.append((result.get("message_id"), part))
    return parts


class Outbox(threading.Thread):
    """Delivers the outbox every couple of seconds, with the questions of actions waiting for
    an owner's yes (``approve.py``)."""

    def __init__(self, cfg: Config, api: TelegramAPI, stop: threading.Event, interval: float = 2.0) -> None:
        super().__init__(name="outbox", daemon=True)
        self._cfg = cfg
        self._api = api
        self._halt = stop
        self._interval = interval

    def run(self) -> None:
        conn = connect(self._cfg.db_path)
        log.info("outbox started")
        while not self._halt.wait(self._interval):
            try:
                deliver(conn, self._api)
                approve.deliver(conn, self._api)
            except Exception:
                log.exception("outbox not delivered")
        conn.close()
        log.info("outbox stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zoe-say", description="Send a message in Zoe's name.")
    parser.add_argument("text", nargs="?", default="", help="the message, or - to read it from stdin")
    parser.add_argument("--chat", type=int, help="the chat; the owner's first account when left out")
    parser.add_argument("--from", dest="origin", default="", help="the program that writes, shown to Zoe")
    parser.add_argument("--file", type=Path, help="a file to send, with the text as its caption")
    parser.add_argument("--kind", default="document", choices=KINDS, help="how to send the file")
    parser.add_argument("--silent", action="store_true", help="no notification sound")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    text = sys.stdin.read().strip() if args.text == "-" else args.text
    if not text and args.file is None:
        parser.error("nothing to send: give a text or a file")
    if args.file is not None and not args.file.is_file():
        parser.error(f"{args.file} is not a file")
    chat = args.chat or cfg.owner_chat
    if chat is None:
        parser.error("no chat: give --chat, or set ZOE_OWNER_IDS")
    conn = connect(cfg.db_path)
    apply_schema(conn)
    number = queue(conn, cfg.data_dir, chat, text, args.file, args.kind, args.origin, args.silent)
    conn.close()
    print(f"queued {number}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
