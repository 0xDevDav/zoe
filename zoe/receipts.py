"""Receipts for what a reply set in motion.

Directives leave the reply before it is shown, so on their own they would act
unseen. After every reply that made, cancelled or changed something, the service
(not Claude) sends one silent message listing it: plans made and cancelled, files
kept or deleted, people admitted. Whatever keeps running comes with a button that
undoes it at once, so a directive that should not have acted is caught and
reversed in one tap. Parallel minds are followed and stopped from the menu instead.

Button data: ``r:unplan:<plan id>``, ``r:halt:<mind id>``, ``r:discard:<file id>`` and
``r:revoke:<Telegram id>``, the last for owners only; ``r:halt`` serves the buttons
of earlier receipts that started minds.
"""

from __future__ import annotations

import html
import logging
import sqlite3

from . import access, files, i18n, minds, planner
from .access import Person
from .brain import Brain
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("zoe.receipts")

_LABEL = 40
_SHOWN = 160


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Receipt:
    """The actions of one reply, collected as they happen and sent together."""

    def __init__(self, language: str) -> None:
        self._language = language
        self.lines: list[str] = []
        self.buttons: list[list[dict[str, str]]] = []

    def _t(self, key: str, **kwargs: object) -> str:
        return i18n.t(self._language, key, **kwargs)

    def planned(self, plan: planner.Plan) -> None:
        when = planner.label(plan.due_at, self._language) + (f" 🔁 <code>{html.escape(plan.repeat)}</code>"
                                                             if plan.repeat else "")
        self.lines.append(self._t("receipt_plan", what=html.escape(_cut(plan.instruction, _SHOWN)), when=when))
        self._button(self._t("btn_undo_action", what=plan.instruction), f"r:unplan:{plan.id}")

    def unplanned(self, plan: planner.Plan) -> None:
        self.lines.append(self._t("receipt_unplan", what=html.escape(_cut(plan.instruction, _SHOWN))))

    def kept(self, kept: files.File) -> None:
        self.lines.append(self._t("receipt_keep", name=html.escape(kept.name), path=html.escape(str(kept.path))))
        self._button(self._t("btn_discard", name=kept.name), f"r:discard:{kept.id}")

    def discarded(self, gone: files.File) -> None:
        self.lines.append(self._t("receipt_discard", name=html.escape(gone.name)))

    def shared(self, person: Person, folder: str, given: bool) -> None:
        key = "receipt_share" if given else "receipt_unshare"
        self.lines.append(self._t(key, name=html.escape(person.name), folder=html.escape(folder)))

    def admitted(self, person: Person, user_id: int, new: bool) -> None:
        key = "receipt_admit" if new else "receipt_linked"
        self.lines.append(self._t(key, name=html.escape(person.name), id=user_id))
        self._button(self._t("btn_block_account", name=person.name), f"r:revoke:{user_id}")

    def _button(self, text: str, data: str) -> None:
        # Every receipt button takes something back, so each is red.
        self.buttons.append([{"text": _cut(text, _LABEL), "callback_data": data, "style": "danger"}])

    def send(self, api: TelegramAPI, chat_id: int) -> None:
        if not self.lines:
            return
        try:
            api.send_message(chat_id, "\n\n".join(self.lines), buttons=self.buttons or None, silent=True)
        except TelegramError as exc:
            log.warning("receipt not sent to %s: %s", chat_id, exc)


def handle(conn: sqlite3.Connection, api: TelegramAPI, person: Person, query: dict) -> None:
    """Undo what a receipt's button names, if it is still this person's and still running."""
    _, action, number = ((query.get("data") or "") + "::").split(":")[:3]
    done = False
    if number.isdigit():
        if action == "unplan":
            done = planner.cancel(conn, person.id, int(number))
        elif action == "halt":
            done = minds.stop(conn, person.id, int(number))
        elif action == "revoke" and person.role == "owner":
            done = access.revoke(conn, int(number)) is True
        elif action == "discard":
            done = files.discard(conn, Brain(conn, person.id), person, int(number)) is not None
    message = query.get("message") or {}
    if done:
        mark = i18n.t(person.language, {"unplan": "receipt_undone", "halt": "receipt_halted",
                                        "revoke": "receipt_blocked", "discard": "receipt_deleted"}[action])
        api.edit_buttons(message.get("chat", {}).get("id"), message.get("message_id"),
                         [[{"text": mark, "callback_data": "x"}]])
    api.answer_callback(query["id"], None if done else i18n.t(person.language, "receipt_gone"))
