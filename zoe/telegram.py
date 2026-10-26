"""Minimal Telegram Bot API client over the standard library."""

from __future__ import annotations

import http.client
import json
import logging
import mimetypes
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

log = logging.getLogger("zoe.telegram")

# The Bot API method and field that send each kind of file.
_SENDERS = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "animation": ("sendAnimation", "animation"),
    "audio": ("sendAudio", "audio"),
    "voice": ("sendVoice", "voice"),
    "video_note": ("sendVideoNote", "video_note"),
    "sticker": ("sendSticker", "sticker"),
    "document": ("sendDocument", "document"),
}

# Seconds to wait before each new try of a request that failed for a passing reason.
RETRY_WAITS = (2.0, 5.0, 10.0)

# The longest caption Telegram puts under a file or a voice note.
CAPTION_LIMIT = 1024

# Seconds to reach one address of the server. Telegram answers on IPv6 and IPv4, and a route
# that drops a connection costs this before the next address is tried, not the request's timeout.
CONNECT_TIMEOUT = 4.0

# Seconds a kept connection may sit idle. A route can drop an idle connection without a word,
# and a request sent on it would wait its whole timeout before it is tried again.
IDLE_LIMIT = 60.0

# Reactions Zoe uses, the same list her instructions offer: a friendly subset of the emoji
# Telegram accepts from bots (ReactionTypeEmoji), written without variation selectors.
REACTIONS = tuple(
    "👍 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 😢 🎉 🤩 🙏 👌 😍 💯 🤣 ⚡ 🏆 💔 🤨 😐 😴 😭 🤓 👀 🙈 😇 🤝 ✍ 🤗 🫡 😘 😎".split()
)


def _connect(address: tuple[str, int], timeout: float, *args: object) -> socket.socket:
    """Reach the first address of the host that answers within CONNECT_TIMEOUT, then wait for
    the answer as long as the request allows."""
    sock = socket.create_connection(address, min(timeout, CONNECT_TIMEOUT), *args)
    sock.settimeout(timeout)
    return sock


class _Connection(http.client.HTTPSConnection):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _connect


_HOST = "api.telegram.org"
# Telegram's certificate and host name are checked on every connection.
_TLS = ssl.create_default_context()
_kept = threading.local()


def _exchange(path: str, body: bytes | None, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """Make one request to Telegram and return its status and body.

    Each thread keeps its own connection open between requests, so one thread never reads
    another's answer, and a connection the server closed while it was idle is opened again at
    once. One idle longer than IDLE_LIMIT is opened again before it is used. Connections are not
    inherited by the processes Zoe starts."""
    while True:
        conn = getattr(_kept, "conn", None)
        if conn is None:
            conn = _kept.conn = _Connection(_HOST, timeout=timeout, context=_TLS)
        elif time.monotonic() - getattr(_kept, "used", 0.0) > IDLE_LIMIT:
            conn.close()
        kept = conn.sock is not None
        conn.timeout = timeout
        if kept:
            conn.sock.settimeout(timeout)
        try:
            conn.request("GET" if body is None else "POST", path, body=body, headers=headers)
            response = conn.getresponse()
            answer = response.status, response.read()
            _kept.used = time.monotonic()
            return answer
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            if not (kept and isinstance(exc, ConnectionError)):
                raise


def _mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _reply_parameters(message_id: int) -> str:
    """Answer a message; if it no longer exists, the message is sent without the reply."""
    return json.dumps({"message_id": message_id, "allow_sending_without_reply": True})


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self._base = f"/bot{token}"
        self._file_base = f"/file/bot{token}"

    def call(self, method: str, **params: object) -> object:
        """Call any Bot API method; dict and list parameters are sent as JSON, None ones are dropped."""
        encoded = {
            key: json.dumps(value) if isinstance(value, (dict, list)) else value
            for key, value in params.items()
            if value is not None
        }
        return self._call(method, encoded, timeout=30)

    def set_profile_photo(self, image: bytes, filename: str) -> None:
        mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        self._upload(
            "setMyProfilePhoto",
            {"photo": json.dumps({"type": "static", "photo": "attach://avatar"})},
            [("avatar", filename, image, mime)],
        )

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        allowed = json.dumps(["message", "edited_message", "callback_query", "message_reaction"])
        return self._call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": allowed},
                          timeout=timeout + 10)

    def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
        buttons: list[list[dict]] | None = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> dict:
        """Send text as HTML, with inline buttons, as a reply to a message, and without a
        notification sound if asked; falls back to plain text if the markup is rejected."""
        params = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": "true"}
        if silent:
            params["disable_notification"] = "true"
        if buttons is not None:
            params["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        if reply_to:
            params["reply_parameters"] = _reply_parameters(reply_to)
        try:
            return self._call("sendMessage", {**params, "parse_mode": parse_mode}, timeout=30)
        except TelegramError as exc:
            if "can't parse entities" not in str(exc):
                raise
            return self._call("sendMessage", params, timeout=30)

    def notify(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> bool:
        """Send a service message; a failure is logged and never interrupts the caller."""
        try:
            self.send_message(chat_id, text, buttons=buttons)
            return True
        except TelegramError as exc:
            log.warning("notice to %s not delivered: %s", chat_id, exc)
            return False

    def edit_message(self, chat_id: int, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> bool:
        """Replace a message's text and buttons; return whether it now shows them. An unchanged
        message is not an error."""
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                      disable_web_page_preview="true", reply_markup={"inline_keyboard": buttons or []})
        except TelegramError as exc:
            if "message is not modified" in str(exc):
                return True
            log.warning("message %s not edited: %s", message_id, exc)
            return False
        return True

    def edit_buttons(self, chat_id: int, message_id: int, buttons: list[list[dict]]) -> None:
        try:
            self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                      reply_markup={"inline_keyboard": buttons})
        except TelegramError as exc:
            log.warning("buttons of message %s not edited: %s", message_id, exc)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            log.warning("message %s not deleted: %s", message_id, exc)

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        """Acknowledge a button press, optionally with a short notice shown to the person."""
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as exc:
            log.warning("button press %s not acknowledged: %s", callback_id, exc)

    def send_file(
        self, chat_id: int, kind: str, caption: str = "", file_id: str | None = None, path: Path | None = None,
        silent: bool = False, name: str = "",
    ) -> dict:
        """Send a file as its kind (photo, video, document...): by a Telegram file id, or uploaded from
        ``path`` under ``name``, its own name when not given."""
        method, field = _SENDERS.get(kind, _SENDERS["document"])
        fields: dict[str, object] = {"chat_id": chat_id}
        if silent:
            fields["disable_notification"] = "true"
        if caption and kind != "sticker":
            fields["caption"] = caption[:CAPTION_LIMIT]
        if file_id:
            return self.call(method, **fields, **{field: file_id})
        name = name or path.name
        return self._upload(method, fields, [(field, name, path.read_bytes(), _mime(name))])

    def send_album(self, chat_id: int, items: list[tuple[str, Path]], caption: str = "") -> list[dict]:
        """Send 2 to 10 files as one album, each uploaded as its kind (photos and videos together,
        or documents together), the caption under the first; return the messages it took."""
        media = [{"type": kind, "media": f"attach://f{n}"} for n, (kind, _) in enumerate(items)]
        if caption:
            media[0]["caption"] = caption[:CAPTION_LIMIT]
        uploads = [(f"f{n}", path.name, path.read_bytes(), _mime(path.name)) for n, (_, path) in enumerate(items)]
        return self._upload("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)}, uploads)

    def send_voice(
        self, chat_id: int, audio: bytes, filename: str = "voice.ogg", reply_to: int | None = None, caption: str = ""
    ) -> dict:
        """Send OGG/Opus audio as a voice note, with an HTML caption if given, or as an audio file
        if the user blocks voice notes."""
        fields: dict[str, object] = {"chat_id": chat_id}
        if reply_to:
            fields["reply_parameters"] = _reply_parameters(reply_to)
        if caption:
            fields.update(caption=caption[:CAPTION_LIMIT], parse_mode="HTML")
        try:
            return self._upload("sendVoice", fields, [("voice", filename, audio, "audio/ogg")])
        except TelegramError as exc:
            if "VOICE_MESSAGES_FORBIDDEN" not in str(exc):
                raise
            return self._upload("sendAudio", fields, [("audio", filename, audio, "audio/ogg")])

    def react(self, chat_id: int, message_id: int, emoji: str) -> str:
        """Put a reaction on a message and return the emoji used; unsupported ones become 👍."""
        emoji = emoji.replace("️", "").strip()
        if emoji not in REACTIONS:
            emoji = "👍"
        self.call(
            "setMessageReaction", chat_id=chat_id, message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}],
        )
        return emoji

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=15)
        except TelegramError:
            pass

    def download(self, file_id: str) -> bytes:
        return self.fetch(file_id)[0]

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        """Download a file sent to the bot; return its bytes and its path on Telegram, which carries the extension."""
        info = self._call("getFile", {"file_id": file_id}, timeout=30)
        try:
            status, data = _exchange(f"{self._file_base}/{info['file_path']}", None, {}, 120)
        except (OSError, http.client.HTTPException) as exc:
            raise TelegramError(f"download failed: {exc}") from exc
        if status != 200:
            raise TelegramError(f"download failed: HTTP {status}")
        return data, info["file_path"]

    def _call(self, method: str, params: dict, timeout: float) -> object:
        body = urllib.parse.urlencode(params).encode()
        return self._send(f"{self._base}/{method}", body,
                          {"Content-Type": "application/x-www-form-urlencoded"}, method, timeout)

    def _upload(self, method: str, fields: dict, files: list[tuple[str, str, bytes, str]]) -> object:
        """Send fields and files, each file as (field, file name, bytes, MIME type), in one request."""
        boundary = uuid.uuid4().hex
        body = bytearray()
        for name, value in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for field, filename, data, mime in files:
            body += (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        return self._send(f"{self._base}/{method}", bytes(body),
                          {"Content-Type": f"multipart/form-data; boundary={boundary}"}, method, timeout=120)

    @staticmethod
    def _send(path: str, body: bytes, headers: dict[str, str], method: str, timeout: float) -> object:
        """Send a request, retrying network failures, server errors and rate limits a few times."""
        waits = list(RETRY_WAITS)
        while True:
            try:
                status, raw = _exchange(path, body, headers, timeout)
                answer = json.loads(raw.decode())
            except (OSError, http.client.HTTPException) as exc:
                # The network failed, or the answer arrived cut short: worth another try.
                answer = {"ok": False, "error_code": 0, "description": f"{type(exc).__name__}: {exc}"}
            except ValueError:
                # A garbled answer is worth another try; an error page counts as its status.
                answer = {"ok": False, "error_code": 0 if status < 400 else status, "description": f"HTTP {status}"}
            if not isinstance(answer, dict):
                answer = {"ok": False, "error_code": 0, "description": "unexpected answer"}
            if answer.get("ok"):
                return answer["result"]
            code = answer.get("error_code", 0)
            if not waits or not (code == 0 or code == 429 or code >= 500):
                raise TelegramError(f"{method} rejected: {answer.get('description')}")
            wait = waits.pop(0)
            time.sleep((answer.get("parameters") or {}).get("retry_after", wait))
