"""Checks for the Telegram client: passing failures are retried, refusals are not."""

import http.client
import http.server
import json
import socket
import ssl
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import telegram
from zoe.telegram import TelegramAPI, TelegramError


EXCHANGE = telegram._exchange


def _answers(*outcomes: object) -> list[str]:
    """Make Telegram give these outcomes in turn: an error raised, raw bytes, a (status, answer)
    pair or an answer; return the list the methods called are added to."""
    pending, calls = list(outcomes), []

    def exchange(path, body, headers, timeout):
        calls.append(path.rsplit("/", 1)[1])
        outcome = pending.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, answer = outcome if isinstance(outcome, tuple) else (200, outcome)
        return status, answer if isinstance(answer, bytes) else json.dumps(answer).encode()

    telegram._exchange = exchange
    telegram.time.sleep = lambda seconds: None
    return calls


def _http(code: int, description: str) -> tuple[int, dict]:
    return code, {"ok": False, "error_code": code, "description": description}


def test_network_failures_and_server_errors_are_retried():
    calls = _answers(OSError("connection reset"), _http(502, "Bad Gateway"), {"ok": True, "result": {"message_id": 7}})
    assert TelegramAPI("t").send_message(1, "ciao") == {"message_id": 7}
    assert calls == ["sendMessage"] * 3


def test_answers_cut_short_or_garbled_are_retried():
    calls = _answers(http.client.IncompleteRead(b"{\"ok\""), b"not JSON", (502, b"<html>"), {"ok": True, "result": []})
    assert TelegramAPI("t").get_updates(0, 1) == [] and len(calls) == 4


def test_refusals_are_not_retried_and_retries_end():
    calls = _answers(_http(400, "Bad Request: chat not found"))
    try:
        TelegramAPI("t").call("getChat", chat_id=1)
    except TelegramError as exc:
        assert "chat not found" in str(exc) and calls == ["getChat"]
    else:
        raise AssertionError("no error")

    calls = _answers((404, b"<html>Not Found</html>"))
    try:
        TelegramAPI("t").call("getMe")
    except TelegramError as exc:
        assert "HTTP 404" in str(exc) and calls == ["getMe"]
    else:
        raise AssertionError("no error")

    calls = _answers(*[OSError("down")] * (len(telegram.RETRY_WAITS) + 1))
    try:
        TelegramAPI("t").call("getMe")
    except TelegramError:
        assert len(calls) == len(telegram.RETRY_WAITS) + 1
    else:
        raise AssertionError("no error")


def test_a_reply_answers_the_quoted_message():
    sent = []
    api = TelegramAPI("t")
    api._call = lambda method, params, timeout: sent.append(params) or {"message_id": 2}
    api.send_message(1, "eccolo", reply_to=5)
    assert json.loads(sent[0]["reply_parameters"]) == {"message_id": 5, "allow_sending_without_reply": True}


def test_an_album_uploads_every_file_in_one_request():
    sent = []
    api = TelegramAPI("t")
    api._send = lambda path, body, headers, method, timeout: sent.append((method, body)) or [{}, {}]
    with tempfile.TemporaryDirectory() as tmp:
        shot, clip = Path(tmp) / "shot.png", Path(tmp) / "clip.mp4"
        shot.write_bytes(b"PNG-DATA")
        clip.write_bytes(b"MP4-DATA")
        assert len(api.send_album(5, [("photo", shot), ("video", clip)], "la card")) == 2
    method, body = sent[0]
    assert method == "sendMediaGroup" and b"PNG-DATA" in body and b'name="f1"; filename="clip.mp4"' in body
    media = json.loads(body.split(b'name="media"\r\n\r\n')[1].split(b"\r\n")[0])
    assert media == [{"type": "photo", "media": "attach://f0", "caption": "la card"},
                     {"type": "video", "media": "attach://f1"}]


def test_an_address_that_does_not_answer_costs_seconds_not_the_request_timeout():
    server = socket.create_server(("127.0.0.1", 0))
    port, real = server.getsockname()[1], socket.getaddrinfo
    socket.getaddrinfo = lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.255.255.1", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ]
    telegram.CONNECT_TIMEOUT = 0.3
    try:
        start = time.monotonic()
        sock = telegram._connect(("api.telegram.org", port), 30)
        assert time.monotonic() - start < 3 and sock.getpeername()[0] == "127.0.0.1" and sock.gettimeout() == 30
        sock.close()
    finally:
        socket.getaddrinfo = real
        server.close()


class _Counting(http.server.BaseHTTPRequestHandler):
    """Answers every request and notes the client's port, which tells connections apart."""

    protocol_version = "HTTP/1.1"
    ports: list[int] = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).ports.append(self.client_address[1])
        body = b'{"ok": true, "result": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # Close without saying so, as a server does with a connection left idle.
        self.close_connection = self.path.endswith("/drop")

    def log_message(self, *args: object) -> None:
        pass


def test_each_thread_keeps_its_own_connection_and_reopens_one_closed_or_idle():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Counting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port, real = server.server_address[1], telegram._Connection
    telegram._Connection = lambda host, timeout, context: http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    telegram._kept = threading.local()
    try:
        def ask(path: str) -> None:
            assert EXCHANGE(path, b"x=1", {"Content-Type": "application/x-www-form-urlencoded"}, 5)[0] == 200

        _Counting.ports = []
        ask("/first")
        ask("/drop")
        ask("/after")
        other = threading.Thread(target=ask, args=("/elsewhere",))
        other.start()
        other.join()
        ask("/kept")
        telegram._kept.used -= telegram.IDLE_LIMIT + 1
        ask("/idle")
        first, drop, after, elsewhere, kept, idle = _Counting.ports
        assert first == drop and after != first and elsewhere not in (first, after)
        assert kept == after and idle != after
    finally:
        telegram._Connection = real
        server.shutdown()
        server.server_close()


def test_telegram_is_reached_only_over_a_verified_connection():
    assert telegram._TLS.verify_mode == ssl.CERT_REQUIRED and telegram._TLS.check_hostname


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
