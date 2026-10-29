"""Stand-ins for the Telegram client and for Claude, recording what the service sends and asks."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path

from zoe import access
from zoe.claude_runner import Result
from zoe.config import Config
from zoe.db import apply_schema, connect
from zoe.telegram import TelegramError
from zoe.worker import Worker

OWNER = 111
OTHER_OWNER_ACCOUNTS = (55, 77)


FAKE_FAILURE = "Something broke on the way"


@contextlib.contextmanager
def logged(name: str):
    """The lines a logger writes while the block runs."""
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)


class FakeRunner:
    """Answers each turn with the next prepared reply, or fails with Claude's own words
    ``FAKE_FAILURE`` when the reply is None.
    Every turn belongs to the same session. A reply writes its directives with {k},
    which becomes the key the prompt gave, as Claude would write them."""

    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.profiles: list[object] = []

    def run(self, prompt_text: str, profile: object, on_step: object = None, cancel: object = None,
            interrupt: object = None, follow_for: float = 0.0) -> Result:
        self.prompts.append(prompt_text)
        self.profiles.append(profile)
        reply = self.replies.pop(0)
        key = re.search(r"Your key for this turn is (\w+)\.", prompt_text)
        if reply and key:
            reply = reply.replace("{k}", key.group(1))
        return Result(reply is not None, reply or "", False, False, 0 if reply is not None else 1, 1.0,
                      session_id="session-1", failure="" if reply is not None else FAKE_FAILURE)


def worker_setup(
    home: Path, *replies: str | None, on_disk: bool = False, others: tuple[int, ...] = ()
) -> tuple[sqlite3.Connection, access.Person, Worker, FakeRunner, FakeAPI]:
    """A database with one owner, and a worker whose Claude gives the prepared replies. The
    owner writes from ``OWNER``, their main chat, and from the ``others`` accounts too. The
    database lives in ``home`` when other threads must open it too."""
    if on_disk:
        conn = connect(home / "zoe.db")
    else:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
    apply_schema(conn)
    owner = access.bootstrap_owner(conn, frozenset({OWNER, *others}), "Alex", "it")
    cfg = Config(
        bot_token="", owner_ids=frozenset({OWNER, *others}), data_dir=home, db_path=home / "zoe.db",
        vault_path=home / "v.db", vault_key_path=home / "v.key", workdir=home, default_language="it",
        owner_chat=OWNER,
    )
    api, runner = FakeAPI(), FakeRunner(*replies)
    worker = Worker(cfg, api, threading.Event())
    worker._runner = runner
    return conn, owner, worker, runner, api


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, list | None]] = []
        self.sent_ids: list[int] = []
        self.edits: list[tuple[int, int, str | None, list | None]] = []
        self.edited: dict[int, str] = {}
        self.edits_fail = False
        self.notices: list[str | None] = []
        self.deleted: list[tuple[int, int]] = []
        self.calls: list[tuple[str, dict]] = []
        self.actions: list[str] = []
        self.replies_to: list[int | None] = []
        self.fetched: list[str] = []
        self.files: list[tuple] = []
        self.file_names: list[str] = []
        self.albums: list[tuple[int, list[str], list[str], str]] = []
        self.voices: list[tuple[int, bytes, str]] = []
        self.reactions: list[tuple[int, int, str]] = []
        self._next_id = 100

    def send_message(
        self, chat_id: int, text: str, parse_mode: str = "HTML", buttons: list | None = None,
        reply_to: int | None = None, silent: bool = False,
    ) -> dict:
        self._next_id += 1
        self.sent.append((chat_id, text, buttons))
        self.sent_ids.append(self._next_id)
        self.replies_to.append(reply_to)
        return {"message_id": self._next_id}

    def notify(self, chat_id: int, text: str, buttons: list | None = None) -> bool:
        self.send_message(chat_id, text, buttons=buttons)
        return True

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        self.fetched.append(file_id)
        if file_id.startswith("big"):
            raise TelegramError("getFile rejected: Bad Request: file is too big")
        return f"data of {file_id}".encode(), f"files/{file_id}.jpg"

    def download(self, file_id: str) -> bytes:
        return self.fetch(file_id)[0]

    def send_file(self, chat_id: int, kind: str, caption: str = "", file_id: str | None = None,
                  path: Path | None = None, silent: bool = False, name: str = "") -> dict:
        self._next_id += 1
        self.files.append((chat_id, kind, caption, file_id, path))
        self.file_names.append(name or (path.name if path else ""))
        return {"message_id": self._next_id}

    def send_album(self, chat_id: int, items: list[tuple[str, Path]], caption: str = "") -> list[dict]:
        self.albums.append((chat_id, [kind for kind, _ in items], [path.name for _, path in items], caption))
        first, self._next_id = self._next_id + 1, self._next_id + len(items)
        return [{"message_id": n} for n in range(first, self._next_id + 1)]

    def edit_message(self, chat_id: int, message_id: int, text: str, buttons: list | None = None) -> bool:
        if self.edits_fail:
            return False
        self.edits.append((chat_id, message_id, text, buttons))
        self.edited[message_id] = text
        return True

    def send_voice(self, chat_id: int, audio: bytes, filename: str = "voice.ogg", reply_to: int | None = None,
                   caption: str = "") -> dict:
        self._next_id += 1
        self.voices.append((chat_id, audio, caption))
        return {"message_id": self._next_id}

    def react(self, chat_id: int, message_id: int, emoji: str) -> str:
        self.reactions.append((chat_id, message_id, emoji))
        return emoji

    def edit_buttons(self, chat_id: int, message_id: int, buttons: list) -> None:
        self.edits.append((chat_id, message_id, None, buttons))

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.notices.append(text)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append(action)

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        return True

    def to(self, chat_id: int) -> list[str]:
        """The texts sent to a chat, each as it reads after its latest edit."""
        return [self.edited.get(mid, text) for (cid, text, _), mid in zip(self.sent, self.sent_ids) if cid == chat_id]

    def last_buttons_to(self, chat_id: int) -> list:
        return next(buttons for cid, _, buttons in reversed(self.sent) if cid == chat_id)


def shown(view: dict) -> str:
    """Everything a menu view shows, as one text to search."""
    return json.dumps(view, ensure_ascii=False)


def targets(view: dict) -> list[str]:
    """The views and actions a menu view's rows and buttons lead to, in order."""
    found = []
    for block in view["blocks"]:
        found += [item["to"] or item["act"] for item in block.get("items", []) if item["to"] or item["act"]]
        found += [block["button"]["act"]] if "button" in block else []
    return found


def button_data(keyboard: list | None) -> list[str]:
    return [button["callback_data"] for row in keyboard or [] for button in row]


def press(update_id: int, user_id: int, data: str, message_id: int = 500, chat_id: int | None = None) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"press-{update_id}",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id or user_id}},
        },
    }


def message(update_id: int, user_id: int, text: str, chat_type: str = "private", **user: object) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": user_id, "type": chat_type},
            "from": {"id": user_id, **user},
            "text": text,
        },
    }
