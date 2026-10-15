"""The menu's web server: the Mini App page and the API behind it, on this machine only.

Telegram opens Mini Apps only over HTTPS, so a reverse proxy with a certificate forwards
the address in ZOE_MENU_URL to 127.0.0.1:ZOE_MENU_PORT. The page, its script and its
theme are public; the API answers only requests that carry the data Telegram signed with
the bot's token when it opened the page, signed less than a day before, for an approved
account.

The page has Zoe's look: dark warm surfaces, an orange accent, Sora and Inter, her logo.
It comes with her in ``zoe/theme``; an installation gives the page a look of its own with
``ZOE_HOME/menu/theme.css``, next to the fonts and images it uses. Both are served as
``theme/<file>``, the installation's file first.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import access, i18n
from .config import Config
from .db import connect
from .menu import Menu
from .telegram import TelegramAPI

log = logging.getLogger("zoe.webapp")

# How long the data Telegram signs when the page opens stays good: a menu may stay open for hours.
MAX_AGE = 86400.0
_BODY = 4096
_FILES = {"": ("menu.html", "text/html; charset=utf-8"), "menu.js": ("menu.js", "text/javascript; charset=utf-8")}
_THEME = {".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".png": "image/png", ".jpg": "image/jpeg",
          ".webp": "image/webp"}
_THEME_NAME = re.compile(r"[\w-][\w.-]*")
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self' https://telegram.org; "
                               "style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self'; "
                               "connect-src 'self'; frame-ancestors https://web.telegram.org",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def signer(init_data: str, token: str, now: float) -> int | None:
    """The Telegram id of who opened the page, when Telegram signed ``init_data`` with the bot's
    ``token`` less than MAX_AGE ago; None otherwise."""
    fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    signature = fields.pop("hash", "")
    checked = "\n".join(f"{key}={value}" for key, value in sorted(fields.items())).encode()
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    if not signature or not hmac.compare_digest(hmac.new(secret, checked, hashlib.sha256).hexdigest(), signature):
        return None
    try:
        user_id = int(json.loads(fields["user"])["id"])
        return user_id if now - float(fields["auth_date"]) <= MAX_AGE else None
    except (KeyError, TypeError, ValueError):
        return None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # How often the serving loop looks at the stop signal.
    timeout = 1.0

    def __init__(self, cfg: Config, menu: Menu, token: str) -> None:
        super().__init__(("127.0.0.1", cfg.menu_port), _Handler)
        self.cfg, self.menu, self.token = cfg, menu, token


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:
        *_, folder, name = ("/" + urllib.parse.urlsplit(self.path).path).split("/")
        if folder == "theme":
            self._theme(name)
        elif name in _FILES:
            file, kind = _FILES[name]
            self._send(200, Path(__file__).with_name(file).read_bytes(), kind)
        else:
            self._send(404, b"", "text/plain")

    def _theme(self, name: str) -> None:
        """A file of the theme by its bare name, the installation's before Zoe's own: nothing outside
        the two folders is reachable."""
        kind = _THEME.get(Path(name).suffix.lower())
        if kind and _THEME_NAME.fullmatch(name):
            for folder in (self.server.cfg.data_dir / "menu", Path(__file__).with_name("theme")):
                if (folder / name).is_file():
                    self._send(200, (folder / name).read_bytes(), kind)
                    return
        self._send(404, b"", "text/plain")

    def do_POST(self) -> None:
        if self._leaf() != "api":
            self._send(404, b"", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length)) if 0 < length <= _BODY else None
        except ValueError:
            request = None
        if not isinstance(request, dict):
            self._send(400, b"", "text/plain")
            return
        cfg = self.server.cfg
        user_id = signer(self.headers.get("X-Telegram-Init-Data", ""), self.server.token, time.time())
        conn = connect(cfg.db_path)
        try:
            found = access.resolve(conn, user_id) if user_id else None
            if found is None or found.status != "approved" or found.person is None:
                log.warning("menu request refused: %s", f"account {user_id}" if user_id else "not signed by Telegram")
                self._json(403, {"brand": cfg.bot_name, "title": cfg.bot_name, "back": "", "blocks": [
                    {"text": i18n.t(cfg.default_language, "menu_denied")}]})
                return
            self._json(200, self.server.menu.answer(conn, found.person, request))
        except Exception:
            log.exception("menu request failed")
            self._json(500, {"brand": cfg.bot_name, "title": cfg.bot_name, "back": "home", "blocks": [
                {"text": i18n.t(cfg.default_language, "menu_failed")}]})
        finally:
            conn.close()

    def _leaf(self) -> str:
        """The last part of the path: the proxy forwards the menu's whole address, prefix included."""
        return urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json")

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in _HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # The proxy logs requests already.
        pass


class MenuServer(threading.Thread):
    """Serves the menu until ``stop`` is set. The port is taken when the server is made, so
    a port in use shows at start."""

    def __init__(self, cfg: Config, api: TelegramAPI, token: str, stop: threading.Event) -> None:
        super().__init__(name="menu", daemon=True)
        self._halt = stop
        self._server = _Server(cfg, Menu(cfg, api), token)
        self.port = self._server.server_address[1]

    def run(self) -> None:
        log.info("menu served on 127.0.0.1:%d", self.port)
        with self._server:
            while not self._halt.is_set():
                self._server.handle_request()
        log.info("menu stopped")
