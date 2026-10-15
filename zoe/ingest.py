"""Poller thread: long-poll Telegram, check who is writing, and route each update.

Messages from approved people go to the queue, /menu (and /start once approved)
answers with the button that opens the menu, and button presses go to the
question they answer or to the receipt they undo. An unknown account gets no
answer at all and the owners get no notice: it is only remembered, for the owner
to admit from the menu or by asking Zoe. Only private chats are handled.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from . import access, approve, choices, i18n, receipts, store
from .config import Config
from .db import connect
from .menu import Menu
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("zoe.ingest")

_MENU_COMMANDS = {"/menu", "/start"}
_STATUS_COMMANDS = {"/stato", "/status"}
# How many times an update that fails is tried before it is set aside.
UPDATE_TRIES = 3


def _command(text: str | None) -> str:
    """Return the command a message starts with, without the bot name, or an empty string."""
    if not text or not text.startswith("/"):
        return ""
    return text.split()[0].split("@", 1)[0].lower()


class Poller(threading.Thread):
    def __init__(self, cfg: Config, api: TelegramAPI, stop: threading.Event) -> None:
        super().__init__(name="poller", daemon=True)
        self._cfg = cfg
        self._api = api
        self._halt = stop
        self._menu = Menu(cfg, api)
        self._conflict_told = float("-inf")
        self.beat = time.time()
        self.polled = 0.0
        self._failures: dict[int, int] = {}

    def run(self) -> None:
        conn = connect(self._cfg.db_path)
        offset = store.get_offset(conn)
        backoff = 1.0
        log.info("poller started at offset %d", offset)

        while not self._halt.is_set():
            self.beat = time.time()
            try:
                offset = self._poll(conn, offset)
                backoff = 1.0
            except Exception as exc:
                if isinstance(exc, TelegramError):
                    log.warning("getUpdates failed: %s", exc)
                    if "Conflict" in str(exc):
                        self.conflict(conn)
                else:
                    log.exception("polling failed")
                self._halt.wait(backoff)
                backoff = min(backoff * 2, 30.0)

        conn.close()
        log.info("poller stopped")

    def _poll(self, conn: sqlite3.Connection, offset: int) -> int:
        """Handle one batch of updates and return the offset after the last one handled.

        An update that fails is tried again with the next batch, so a passing error loses
        nothing; after ``UPDATE_TRIES`` failures it is set aside and the owners are told.
        """
        updates = self._api.get_updates(offset, self._cfg.poll_timeout)
        self.polled = time.time()
        for update in updates:
            update_id = update["update_id"]
            try:
                self.process(conn, update)
            except Exception:
                log.exception("update %s not processed", update_id)
                self._failures[update_id] = self._failures.get(update_id, 0) + 1
                if self._failures[update_id] < UPDATE_TRIES:
                    self._halt.wait(2.0)
                    return offset
                self._lost(conn)
            self._failures.pop(update_id, None)
            offset = update_id + 1
            store.set_offset(conn, offset)
        return offset

    def _lost(self, conn: sqlite3.Connection) -> None:
        try:
            for owner_id, language in access.owner_recipients(conn):
                self._api.notify(owner_id, i18n.t(language, "update_lost"))
        except Exception:
            log.exception("owners not told about a lost update")

    def turning(self, now: float) -> bool:
        """Whether the polling loop still goes round: a long poll, a backoff and a batch fit in the margin."""
        return now - self.beat < self._cfg.poll_timeout + 120

    def conflict(self, conn: sqlite3.Connection) -> None:
        """Tell the owners, at most once an hour, that another program reads this bot's messages."""
        if time.monotonic() - self._conflict_told < 3600:
            return
        self._conflict_told = time.monotonic()
        log.error("another program is reading this bot's updates: some messages may reach it instead")
        for owner_id, language in access.owner_recipients(conn):
            self._api.notify(owner_id, i18n.t(language, "poll_conflict"))

    def process(self, conn: sqlite3.Connection, update: dict) -> None:
        if "callback_query" in update:
            self._press(conn, update["callback_query"])
            return
        if "message_reaction" in update:
            reaction = update["message_reaction"]
            found = access.resolve(conn, (reaction.get("user") or {}).get("id", 0))
            if (reaction.get("chat") or {}).get("type") == "private" and found.status == "approved" and found.person:
                store.record_reaction(conn, update, found.person.id)
            return

        msg = update.get("message") or update.get("edited_message")
        if not msg or "from" not in msg or msg["from"].get("is_bot"):
            return
        if msg["chat"].get("type") != "private":
            return

        user = msg["from"]
        chat_id = msg["chat"]["id"]
        found = access.resolve(conn, user["id"])

        if found.status == "unknown":
            access.note_stranger(conn, user)
            log.info("message from unknown account %s left unanswered", user["id"])
            return
        if found.status != "approved" or found.person is None:
            return

        command = _command(msg.get("text"))
        if command in _MENU_COMMANDS:
            self._menu.invite(chat_id, found.person)
            return
        if command in _STATUS_COMMANDS and found.person.role == "owner":
            self._menu.invite(chat_id, found.person, "status")
            return
        store.record_inbound(conn, update, found.person.id)

    def _press(self, conn: sqlite3.Connection, query: dict) -> None:
        found = access.resolve(conn, (query.get("from") or {}).get("id", 0))
        data = query.get("data") or ""
        person = found.person if found.status == "approved" else None
        if person and data.startswith("c:"):
            choices.handle(conn, self._api, person, query)
        elif person and data.startswith("r:"):
            receipts.handle(conn, self._api, person, query)
        elif person and person.role == "owner" and data.startswith("a:"):
            approve.answer(conn, self._api, person, query)
        else:
            # Strangers, options already chosen and buttons nothing answers any more: only acknowledged.
            self._api.answer_callback(query["id"])
