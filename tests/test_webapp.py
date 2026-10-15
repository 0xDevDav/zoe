"""Checks for the menu's web server: only data Telegram signed for an approved account gets an answer."""

import hashlib
import hmac
import http.client
import json
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeAPI
from zoe import access, webapp
from zoe.config import Config
from zoe.db import apply_schema, connect

TOKEN = "123456:test-token"


def _signed(user_id: int, at: float, token: str = TOKEN) -> str:
    """Init data as Telegram signs it when it opens a Mini App."""
    fields = {"auth_date": str(int(at)), "query_id": "AAQ", "user": json.dumps({"id": user_id, "first_name": "Alex"})}
    checked = "\n".join(f"{key}={value}" for key, value in sorted(fields.items())).encode()
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urllib.parse.urlencode({**fields, "hash": hmac.new(secret, checked, hashlib.sha256).hexdigest()})


def test_only_fresh_data_signed_with_the_bots_token_names_someone():
    now = time.time()
    assert webapp.signer(_signed(OWNER, now - 60), TOKEN, now) == OWNER
    assert webapp.signer(_signed(OWNER, now - 60, "999:other"), TOKEN, now) is None
    assert webapp.signer(_signed(OWNER, now - webapp.MAX_AGE - 60), TOKEN, now) is None
    forged = _signed(OWNER, now).replace(f"%22id%22%3A+{OWNER}", "%22id%22%3A+222")
    assert forged != _signed(OWNER, now) and webapp.signer(forged, TOKEN, now) is None
    assert webapp.signer("", TOKEN, now) is None and webapp.signer("user=x&hash=00", TOKEN, now) is None


def test_the_page_is_served_and_the_api_answers_approved_accounts_only():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        conn = connect(home / "zoe.db")
        apply_schema(conn)
        access.bootstrap_owner(conn, frozenset({OWNER}), "Alex", "it")
        cfg = Config(
            bot_token="", owner_ids=frozenset({OWNER}), data_dir=home, db_path=home / "zoe.db",
            vault_path=home / "v.db", vault_key_path=home / "v.key", workdir=home,
            menu_url="https://zoe.example.org/zoe/app/", menu_port=0,
        )
        stop = threading.Event()
        server = webapp.MenuServer(cfg, FakeAPI(), TOKEN, stop)
        server.start()

        def get(path: str) -> tuple[int, bytes, str]:
            client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            client.request("GET", path)
            response = client.getresponse()
            return response.status, response.read(), response.getheader("Content-Security-Policy") or ""

        def post(body: bytes, init_data: str = "") -> tuple[int, dict | None]:
            client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            client.request("POST", "/zoe/app/api", body=body, headers={"X-Telegram-Init-Data": init_data})
            response = client.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None

        try:
            status, page, policy = get("/zoe/app/")
            assert status == 200 and b'src="menu.js"' in page and "default-src 'none'" in policy
            assert get("/zoe/app/menu.js")[0] == 200
            status, own = get("/zoe/app/theme/theme.css")[:2]
            assert status == 200 and b"--accent: #e07a52" in own and get("/zoe/app/theme/sora-latin.woff2")[0] == 200
            (home / "menu").mkdir()
            (home / "menu" / "theme.css").write_text(":root { --accent: #e07a52; }")
            (home / ".env").write_text("ZOE_BOT_TOKEN=secret")
            assert get("/zoe/app/theme/theme.css")[1] == b":root { --accent: #e07a52; }"
            for path in ("/zoe/app/theme/..%2F.env", "/zoe/app/theme/.env", "/zoe/app/../.env", "/zoe/app/theme/zoe.db",
                         "/zoe/app/nothing"):
                assert get(path)[0] == 404, path

            request = json.dumps({"view": "home"}).encode()
            assert post(request)[0] == 403
            assert post(request, _signed(999, time.time()))[0] == 403
            status, view = post(request, _signed(OWNER, time.time()))
            assert status == 200 and view["title"] == "Cosa vuoi fare?" and view["brand"] == "Zoe"
            assert post(b"not json", _signed(OWNER, time.time()))[0] == 400
        finally:
            stop.set()
            server.join(timeout=5)
            conn.close()
        assert not server.is_alive()


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
