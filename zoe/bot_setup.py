"""Keep the bot's public profile and command menu in line with this installation.

Runs at every start and changes only what differs, so restarts cost nothing and
rate-limited methods such as setMyName are called only after a real change. A
failed step is logged and never stops the service.

- name, set once for every language;
- description and short description, per language;
- one command, /menu, for everyone: everything else lives in the menu, so owners'
  chats carry no command list of their own;
- the chat's Menu button, which opens the menu when ZOE_MENU_URL is set and lists
  the commands otherwise;
- profile photo, uploaded again only when the image file changes.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from . import access, i18n, store
from .config import Config
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("zoe.setup")

# Zoe's own profile photo; an installation shows another with ZOE_HOME/avatar.png (ZOE_BOT_PHOTO).
_PHOTO = Path(__file__).with_name("avatar.png")

_EVERYONE = {"type": "all_private_chats"}


def commands(language: str) -> list[dict[str, str]]:
    return [{"command": "menu", "description": i18n.t(language, "cmd_menu")}]


def configure(api: TelegramAPI, cfg: Config, conn: sqlite3.Connection) -> None:
    _safely(api.call, "deleteWebhook")
    _safely(_ensure_text, api, "MyName", "name", cfg.bot_name, "")

    for code in ("", *i18n.SUPPORTED):
        language = code or cfg.default_language
        _safely(_ensure_text, api, "MyDescription", "description", i18n.t(language, "bot_description"), code)
        _safely(_ensure_text, api, "MyShortDescription", "short_description",
                i18n.t(language, "bot_short_description"), code)
        _safely(_ensure_commands, api, commands(language), _EVERYONE, code)

    for owner_id, _ in access.owner_recipients(conn):
        _safely(_clear_chat_commands, api, owner_id)

    _safely(_ensure_menu_button, api, cfg)
    _safely(_ensure_photo, api, cfg, conn)


def _safely(step: Callable[..., object], *args: object) -> None:
    try:
        step(*args)
    except TelegramError as exc:
        log.warning("bot setup step skipped: %s", exc)


def _ensure_text(api: TelegramAPI, suffix: str, field: str, value: str, language_code: str) -> None:
    current = api.call(f"get{suffix}", language_code=language_code)
    if current.get(field, "") != value:
        api.call(f"set{suffix}", **{field: value, "language_code": language_code})
        log.info("bot %s set for %s", field, language_code or "every language")


def _ensure_commands(api: TelegramAPI, wanted: list[dict], scope: dict, language_code: str) -> None:
    if api.call("getMyCommands", scope=scope, language_code=language_code) != wanted:
        api.call("setMyCommands", commands=wanted, scope=scope, language_code=language_code)
        log.info("command menu set for %s, %s", scope["type"], language_code or "every language")


def _clear_chat_commands(api: TelegramAPI, chat_id: int) -> None:
    scope = {"type": "chat", "chat_id": chat_id}
    try:
        current = api.call("getMyCommands", scope=scope)
    except TelegramError as exc:
        if "chat not found" in str(exc):  # never opened the bot, so it has no menu of its own
            return
        raise
    if current:
        api.call("deleteMyCommands", scope=scope)
        log.info("chat-specific command menu removed for %s", chat_id)


def _ensure_menu_button(api: TelegramAPI, cfg: Config) -> None:
    wanted = {"type": "commands"}
    if cfg.menu_url:
        wanted = {"type": "web_app", "text": i18n.t(cfg.default_language, "menu_button"),
                  "web_app": {"url": cfg.menu_url}}
    if api.call("getChatMenuButton") != wanted:
        api.call("setChatMenuButton", menu_button=wanted)
        log.info("menu button set: %s", wanted["type"])


def _ensure_photo(api: TelegramAPI, cfg: Config, conn: sqlite3.Connection) -> None:
    """The installation's photo when it has one, Zoe's own otherwise."""
    photo = cfg.bot_photo if cfg.bot_photo is not None and cfg.bot_photo.exists() else _PHOTO
    image = photo.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if store.get_state(conn, "bot_photo_sha256") == digest:
        return
    api.set_profile_photo(image, photo.name)
    store.set_state(conn, "bot_photo_sha256", digest)
    log.info("bot profile photo updated")
