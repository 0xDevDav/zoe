"""What Zoe's HTTP servers share: they listen on this machine only, answer each request in a
thread of its own, and stop when told."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LocalServer(ThreadingHTTPServer):
    """A server on 127.0.0.1; the port is taken when it is made, so a port in use shows at once."""

    daemon_threads = True
    # How often the serving loop looks at the stop signal.
    timeout = 1.0

    def __init__(self, port: int, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(("127.0.0.1", port), handler)

    def serve_until(self, stop: threading.Event) -> None:
        with self:
            while not stop.is_set():
                self.handle_request()


class Answering(BaseHTTPRequestHandler):
    """Answers with the headers every answer of its server carries, ``HEADERS``, and logs nothing:
    what calls it, or the proxy in front, does."""

    HEADERS: dict[str, str] = {}

    def _json(self, status: int, value: object, headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json", headers)

    def _send(self, status: int, body: bytes, kind: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**self.HEADERS, **(headers or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass
