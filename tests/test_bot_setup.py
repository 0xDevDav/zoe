"""Checks for the bot's public profile setup: applied once, then left alone."""

import json
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import access, bot_setup
from zoe.config import Config
from zoe.db import apply_schema

_FIELD = {"MyName": "name", "MyDescription": "description", "MyShortDescription": "short_description"}


class FakeBotAPI:
    """Stands in for Telegram, remembering what was set."""

    def __init__(self) -> None:
        self.state: dict[tuple, object] = {}
        self.writes: list[str] = []
        self.photos = 0

    def _key(self, suffix: str, params: dict) -> tuple:
        return suffix, json.dumps(params.get("scope")), params.get("language_code")

    def call(self, method: str, **params: object) -> object:
        if method == "deleteWebhook":
            return True
        if method == "getChatMenuButton":
            return self.state.get("menu button", {"type": "commands"})
        if method == "setChatMenuButton":
            self.writes.append(method)
            self.state["menu button"] = params["menu_button"]
            return True
        if method == "deleteMyCommands":
            self.writes.append(method)
            self.state.pop(self._key("MyCommands", params), None)
            return True
        verb, suffix = method[:3], method[3:]
        if verb == "get":
            return self.state.get(self._key(suffix, params), [] if suffix == "MyCommands" else {_FIELD[suffix]: ""})
        self.writes.append(method)
        value = params["commands"] if suffix == "MyCommands" else {_FIELD[suffix]: params[_FIELD[suffix]]}
        self.state[self._key(suffix, params)] = value
        return True

    def set_profile_photo(self, image: bytes, filename: str) -> None:
        self.photos += 1
        self.photo = image


def _setup(tmp: Path) -> tuple[sqlite3.Connection, Config]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    access.bootstrap_owner(conn, frozenset({111}), "Alex", "it")
    photo = tmp / "avatar.png"
    photo.write_bytes(b"\x89PNG fake image")
    cfg = Config(
        bot_token="", owner_ids=frozenset({111}), data_dir=tmp, db_path=tmp / "zoe.db",
        vault_path=tmp / "v.db", vault_key_path=tmp / "v.key", workdir=tmp, bot_photo=photo,
    )
    return conn, cfg


def test_first_start_applies_everything_and_restarts_change_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        conn, cfg = _setup(Path(tmp))
        api = FakeBotAPI()

        bot_setup.configure(api, cfg, conn)
        assert api.writes.count("setMyName") == 1
        assert api.writes.count("setMyDescription") == 3
        assert api.writes.count("setMyCommands") == 3
        assert api.photos == 1

        api.writes.clear()
        bot_setup.configure(api, cfg, conn)
        assert api.writes == [] and api.photos == 1


def test_everyone_sees_only_the_menu_command():
    with tempfile.TemporaryDirectory() as tmp:
        conn, cfg = _setup(Path(tmp))
        api = FakeBotAPI()
        bot_setup.configure(api, cfg, conn)
        for code, description in (("it", "Apri il menu"), ("en", "Open the menu")):
            menu = api.state[("MyCommands", json.dumps({"type": "all_private_chats"}), code)]
            assert menu == [{"command": "menu", "description": description}]


def test_the_menu_button_opens_the_menu_once_it_has_an_address():
    with tempfile.TemporaryDirectory() as tmp:
        conn, cfg = _setup(Path(tmp))
        api = FakeBotAPI()
        bot_setup.configure(api, cfg, conn)
        assert "setChatMenuButton" not in api.writes

        served = replace(cfg, menu_url="https://zoe.example.org/zoe/app/")
        bot_setup.configure(api, served, conn)
        assert api.state["menu button"] == {"type": "web_app", "text": "Menu",
                                            "web_app": {"url": "https://zoe.example.org/zoe/app/"}}
        api.writes.clear()
        bot_setup.configure(api, served, conn)
        assert api.writes == []
        bot_setup.configure(api, cfg, conn)
        assert api.state["menu button"] == {"type": "commands"}


def test_zoe_shows_her_own_photo_until_the_installation_has_one():
    with tempfile.TemporaryDirectory() as tmp:
        conn, cfg = _setup(Path(tmp))
        cfg.bot_photo.unlink()
        api = FakeBotAPI()
        bot_setup.configure(api, cfg, conn)
        assert api.photo == bot_setup._PHOTO.read_bytes() and api.photo.startswith(bytes([0x89]) + b"PNG")
        cfg.bot_photo.write_bytes(b"the installation's own photo")
        bot_setup.configure(api, cfg, conn)
        assert api.photo == b"the installation's own photo" and api.photos == 2


def test_owner_chats_lose_a_menu_of_their_own():
    with tempfile.TemporaryDirectory() as tmp:
        conn, cfg = _setup(Path(tmp))
        api = FakeBotAPI()
        own_scope = ("MyCommands", json.dumps({"type": "chat", "chat_id": 111}), None)
        api.state[own_scope] = [{"command": "utenti", "description": "x"}]

        bot_setup.configure(api, cfg, conn)
        assert own_scope not in api.state and api.writes.count("deleteMyCommands") == 1
        api.writes.clear()
        bot_setup.configure(api, cfg, conn)
        assert "deleteMyCommands" not in api.writes


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
