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
    zoe-say --options "Yes|No" "text"            # with one button per option
    zoe-say --task --from alert --key rule-a "text"   # a task for Zoe herself (tasks.py)

Text is Telegram HTML (<b>, <i>, <code>...); a message that is not valid HTML
is sent as plain text. Labels that open the text, such as ``[invoice alert]`` or
``[backup-job]``, name the program to Zoe: the chat log keeps them and the person
reads the message without them. A text notice that comes while the person is talking with
Zoe, or while one of her parallel minds works, is first read with the person's chats and
minds (``linking.py``): it joins the conversation or the work it belongs to, and one that
only reports Zoe's own work is not sent but kept in the chat log, marked as not sent.
Options work like the ones Zoe offers in her replies (``choices.py``): the buttons sit
under the last part of the message, and a tap reaches Zoe as the person's next message,
as if they had written the label.

With ``--task`` the text is no message for the person but work for Zoe: it is
queued as a task in the chat (``tasks.py``) and gives her a turn, or goes to the
parallel mind already at work on one of its ``--key``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from . import access, approve, choices, formatting, linking, store, tasks
from .config import Config
from .db import apply_schema, connect
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("zoe.say")

KINDS = ("document", "photo", "voice", "audio", "video", "animation")
# Tries before a message that keeps failing is set aside.
TRIES = 5
MAX_OPTIONS = 8
# Labels in square brackets at the start of a text: a word, then words, digits, spaces, dots, commas or
# hyphens. A command between double brackets and a link written as [text](address) are never one.
_LABELS = re.compile(r"\A(?:\s*\[[^\W\d_][\w .,'-]{1,59}\](?!\())+\s*")


def notice_recipients(conn: sqlite3.Connection, cfg: Config) -> list[tuple[int, str]]:
    """Return (Telegram user id, language) of the chat that hears Zoe's own notices: the owner's
    main chat, the first in ZOE_OWNER_IDS, or every owner account when that chat is not one."""
    owners = access.owner_recipients(conn)
    main = [owner for owner in owners if owner[0] == cfg.owner_chat]
    return main or owners


def queue(
    conn: sqlite3.Connection,
    home: Path,
    chat_id: int,
    text: str = "",
    file: Path | None = None,
    kind: str = "document",
    origin: str = "",
    silent: bool = False,
    options: list[str] | None = None,
) -> int:
    """Put a message in the outbox. A file is copied into ``home/outbox`` first, so the program
    that asked may delete its own copy at once. Options are the buttons of a text message."""
    if options and file is not None:
        raise ValueError("options go with a text message, not with a file")
    if options and len(options) > MAX_OPTIONS:
        raise ValueError(f"at most {MAX_OPTIONS} options")
    kept = None
    if file is not None:
        folder = home / "outbox"
        folder.mkdir(parents=True, exist_ok=True)
        kept = folder / f"{uuid.uuid4().hex}-{file.name}"
        shutil.copyfile(file, kept)
    cur = conn.execute(
        "INSERT INTO outbox (chat_id, text, file, kind, origin, silent, options, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, text, str(kept) if kept else None, kind if kept else None, origin, int(silent),
         json.dumps(options, ensure_ascii=False) if options else None, time.time()),
    )
    return int(cur.lastrowid)


def deliver(conn: sqlite3.Connection, api: TelegramAPI, linker: linking.Linker | None = None) -> int:
    """Send what waits in the outbox and log it in its chat; return how many were sent. With ``linker``, a
    notice is read with the person's chats and minds first, and the later messages of its chat wait behind it
    meanwhile; a notice it holds back is only logged, as not sent."""
    sent = 0
    held: set[int] = set()
    for row in conn.execute("SELECT * FROM outbox WHERE state = 'waiting' ORDER BY id").fetchall():
        if row["chat_id"] in held:
            continue
        go, linked = linker.ready(conn, row) if linker else (True, None)
        if not go:
            held.add(row["chat_id"])
            continue
        person = access.resolve(conn, row["chat_id"]).person
        label = f"[{row['origin']}] " if row["origin"] else ""
        noted = {"origin": row["origin"]} if row["origin"] else None
        if isinstance(linked, linking.Held):
            store.record_outbound(conn, row["chat_id"], None, label + linked.logged(row["text"]),
                                  person.id if person else None, {**(noted or {}), "held": True})
            conn.execute("UPDATE outbox SET state = 'sent' WHERE id = ?", (row["id"],))
            linker.forget(row["id"])
            log.info("outbox message %d not sent, about Zoe's own work: %s", row["id"], linked.why)
            continue
        options = json.loads(row["options"]) if row["options"] else []
        choice = choices.create(conn, row["chat_id"], person.id, options) if options and person else None
        try:
            ids = _send(api, row, choices.keyboard(choice, options) if choice else None, linked)
        except (TelegramError, OSError) as exc:
            if choice:
                choices.discard(conn, choice)
            attempts = row["attempts"] + 1
            conn.execute("UPDATE outbox SET attempts = ?, state = ? WHERE id = ?",
                         (attempts, "failed" if attempts >= TRIES else "waiting", row["id"]))
            log.warning("outbox message %d not sent (try %d): %s", row["id"], attempts, exc)
            if linker and attempts >= TRIES:
                linker.forget(row["id"])
            continue
        shown = row["text"] or ""
        if row["file"]:
            shown = f"[file «{_name(row['file'])}»] {shown}".strip()
        for index, (number, part) in enumerate(ids):
            logged = label + (part if not row["file"] else shown)
            if choice and index == len(ids) - 1:
                logged = choices.logged(logged, options)
            replied = {"reply": {"id": linked.reply_to}} if linked and linked.reply_to and index == 0 else {}
            store.record_outbound(conn, row["chat_id"], number, logged,
                                  person.id if person else None, {**(noted or {}), **replied} or None)
        if choice:
            choices.attach(conn, choice, ids[-1][0])
        conn.execute("UPDATE outbox SET state = 'sent' WHERE id = ?", (row["id"],))
        if linker:
            linker.forget(row["id"])
        if row["file"]:
            Path(row["file"]).unlink(missing_ok=True)
        sent += 1
    return sent


def unlabeled(text: str) -> str:
    """The text as the person reads it, without the labels that open it; a text of labels alone stays."""
    rest = _LABELS.sub("", text, count=1)
    return rest if rest.strip() else text


def _name(kept: str) -> str:
    """The name a file had before it was copied into the outbox."""
    return Path(kept).name.split("-", 1)[-1]


def _send(api: TelegramAPI, row: sqlite3.Row, buttons: list[list[dict]] | None,
          linked: linking.Linked | None = None) -> list[tuple[int | None, str]]:
    """Send one outbox message, with the buttons under its last part, or as ``linked`` rewrote it, its first
    part replying to the message it answers; return the Telegram id and text of each message it took, as
    logged: with the labels that open it, which the person never sees."""
    if row["file"]:
        result = api.send_file(row["chat_id"], row["kind"], unlabeled(row["text"] or ""), path=Path(row["file"]),
                               silent=bool(row["silent"]), name=_name(row["file"]))
        return [(result.get("message_id"), row["text"] or "")]
    parts = []
    chunks = formatting.split(linked.text if linked else row["text"] or "")
    for index, part in enumerate(chunks):
        last = index == len(chunks) - 1
        result = api.send_message(row["chat_id"], part if parts else unlabeled(part),
                                  buttons=buttons if last else None, silent=bool(row["silent"]),
                                  reply_to=linked.reply_to if linked and not parts else None)
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
        self._linker = linking.Linker(cfg)

    def run(self) -> None:
        conn = connect(self._cfg.db_path)
        log.info("outbox started")
        while not self._halt.wait(self._interval):
            try:
                deliver(conn, self._api, self._linker)
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
    parser.add_argument("--options", default="", help="buttons under the text, separated by |")
    parser.add_argument("--task", action="store_true", help="a task for Zoe herself, not a message for the person")
    parser.add_argument("--key", action="append", default=[], help="what a task is about, one per --key")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    text = sys.stdin.read().strip() if args.text == "-" else args.text
    if not text and args.file is None:
        parser.error("nothing to send: give a text or a file")
    if args.file is not None and not args.file.is_file():
        parser.error(f"{args.file} is not a file")
    options = [label.strip() for label in args.options.split("|") if label.strip()]
    chat = args.chat or cfg.owner_chat
    if chat is None:
        parser.error("no chat: give --chat, or set ZOE_OWNER_IDS")
    if args.task and (args.file is not None or options or args.silent or not text):
        parser.error("a task is a text alone")
    if args.task and not args.origin.strip():
        parser.error("a task names the program that hands it, with --from")
    if args.key and not args.task:
        parser.error("--key goes with --task")
    conn = connect(cfg.db_path)
    apply_schema(conn)
    try:
        if args.task:
            found = access.resolve(conn, chat)
            person = found.person if found.status == "approved" else None
            if person is None or person.role != "owner":
                parser.error(f"a task goes to an owner's chat, and {chat} is not one")
            keys = [key.strip() for key in args.key if key.strip()]
            print(f"task {tasks.queue(conn, chat, person.id, text, args.origin, keys)}")
            return 0
        number = queue(conn, cfg.data_dir, chat, text, args.file, args.kind, args.origin, args.silent, options)
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        conn.close()
    print(f"queued {number}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
