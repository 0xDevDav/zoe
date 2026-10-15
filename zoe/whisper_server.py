"""Standalone transcription server that keeps a faster-whisper model in memory.

Loading the model takes minutes on a CPU, so it runs as its own long-lived
process and the bot only sends audio over HTTP.

    POST /transcribe?language=it&prompt=...   body: raw audio bytes
         -> {"text": ..., "language": ..., "duration": ...}
    GET  /health
         -> {"ok": true, "model": ...}

It has no authentication, so it refuses to listen on anything but the local
machine, and it refuses audio over 25 MB.

Requires the ``voice`` extra (faster-whisper).
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import env_file, load_dotenv

log = logging.getLogger("zoe.whisper")

_LOCAL = ("127.0.0.1", "::1", "localhost")
# Telegram gives bots files up to 20 MB, so no real voice note is larger.
MAX_AUDIO = 25 * 1048576


def main() -> None:
    from faster_whisper import WhisperModel

    load_dotenv(env_file())
    logging.basicConfig(level=os.environ.get("ZOE_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    host = os.environ.get("ZOE_WHISPER_HOST", "127.0.0.1")
    port = int(os.environ.get("ZOE_WHISPER_PORT", "8790"))
    if host not in _LOCAL:
        raise SystemExit(f"ZOE_WHISPER_HOST is {host}: the server has no authentication, so it listens only locally")
    model_name = os.environ.get("ZOE_WHISPER_MODEL", "large-v3-turbo")
    model = WhisperModel(
        model_name,
        device=os.environ.get("ZOE_WHISPER_DEVICE", "cpu"),
        compute_type=os.environ.get("ZOE_WHISPER_COMPUTE", "int8"),
    )
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, {"ok": True, "model": model_name})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            url = urllib.parse.urlparse(self.path)
            if url.path != "/transcribe":
                self._reply(404, {"error": "not found"})
                return
            params = urllib.parse.parse_qs(url.query)
            size = int(self.headers.get("Content-Length", 0))
            if size > MAX_AUDIO:
                self._reply(413, {"error": f"audio over {MAX_AUDIO // 1048576} MB"})
                return
            audio = self.rfile.read(size)
            try:
                with lock:
                    segments, info = model.transcribe(
                        io.BytesIO(audio),
                        vad_filter=True,
                        language=params.get("language", [None])[0],
                        initial_prompt=params.get("prompt", [None])[0],
                    )
                    text = " ".join(s.text.strip() for s in segments)
                self._reply(200, {"text": text, "language": info.language, "duration": info.duration})
            except Exception as exc:
                log.exception("transcription failed")
                self._reply(500, {"error": str(exc)})

        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    log.info("listening on %s:%d with %s", host, port, model_name)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
