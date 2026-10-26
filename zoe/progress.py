"""A live Telegram message that shows what a turn is doing.

It appears the moment work starts, refreshes every few seconds with the elapsed
time and the latest steps, keeps the chat action indicator alive, moves below
any message the person writes meanwhile and any question that waits for their
yes, and becomes the answer when it is
ready, so the answer appears where the person is already looking. An answer it
cannot become (a voice note, a reply to a quoted message) arrives as a new
message, and the progress message is removed.
"""

from __future__ import annotations

import html
import posixpath
import threading
import time
import urllib.parse

from . import i18n
from .claude_runner import Step
from .telegram import TelegramAPI, TelegramError

_READ_TOOLS = {"Read", "NotebookRead"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_COMMAND_TOOLS = {"Bash", "PowerShell"}
_SEARCH_TOOLS = {"Grep", "Glob"}
_DELEGATE_TOOLS = {"Task", "Agent"}


def _file_name(path: str) -> str:
    return posixpath.basename(path.replace("\\", "/"))


def _tool_label(tool: str, args: dict) -> tuple[str, str]:
    if tool == "WebSearch":
        return "step_web_search", str(args.get("query", ""))
    if tool == "WebFetch":
        return "step_web_fetch", urllib.parse.urlparse(str(args.get("url", ""))).netloc
    if tool in _READ_TOOLS:
        return "step_read", _file_name(str(args.get("file_path") or args.get("notebook_path") or ""))
    if tool in _WRITE_TOOLS:
        return "step_write", _file_name(str(args.get("file_path") or args.get("notebook_path") or ""))
    if tool in _COMMAND_TOOLS:
        # A command shows what it does when it says so; a raw command line tells the person nothing.
        return ("step_command", str(args["description"])) if args.get("description") else ("step_command_plain", "")
    if tool in _SEARCH_TOOLS:
        return "step_search_files", str(args.get("pattern", ""))
    if tool in _DELEGATE_TOOLS:
        return "step_delegate", str(args.get("description", ""))
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return "step_service", parts[1] if len(parts) > 1 else tool
    return "step_tool", tool


_ICONS = {
    "step_web_search": "🔎", "step_web_fetch": "🌐", "step_read": "📄", "step_write": "✏️", "step_command": "⚙️",
    "step_command_plain": "⚙️",
    "step_search_files": "🗂️", "step_delegate": "👥", "step_service": "🔌", "step_tool": "🛠️",
    "step_recall": "🧠",
}
# What the header says while Zoe hears a voice note, downloads what was sent, Claude thinks,
# writes the answer, or does anything else.
_STATES = {
    "listening": ("progress_listening", "🎧"), "downloading": ("progress_downloading", "📥"),
    "thinking": ("step_thinking", "💭"), "writing": ("step_writing", "✍️"), "working": ("progress_working", "⏳"),
    "recording": ("progress_recording", "🎙️"),
}


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def describe(step: Step, language: str, limit: int = 60) -> str:
    """Return a short localized line for one step, as plain text for views that show text as
    it is, such as the steps of a parallel mind; the HTML progress message uses action()."""
    if step.kind in ("thinking", "writing", "update"):
        return i18n.t(language, f"step_{step.kind}")
    key, detail = _tool_label(step.tool, step.args)
    return i18n.t(language, key, detail=_short(detail, limit)).rstrip(": ")


def action(step: Step, language: str, limit: int = 44) -> str:
    """Return what a tool step does as one line that fits a phone screen, led by an icon for
    its kind. A command with a description shows only that: it already says what it does."""
    if step.kind == "recall":
        key, detail = "step_recall", str(step.args.get("memory", ""))
    else:
        key, detail = _tool_label(step.tool, step.args)
    if key == "step_command" and step.args.get("description"):
        text = detail
    else:
        text = i18n.t(language, key, detail=" ".join(detail.split())).rstrip(": ")
    return f"{_ICONS[key]} {html.escape(_short(text, limit))}"


def elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


class Progress:
    def __init__(
        self,
        api: TelegramAPI,
        chat_id: int,
        language: str,
        action: str = "typing",
        interval: float = 3.0,
    ) -> None:
        self._api = api
        self._chat_id = chat_id
        self._language = language
        self._action = action
        self._interval = interval
        self._state = "working"
        self._doing = ""
        self._lock = threading.Lock()
        self._message_lock = threading.Lock()
        self._done = threading.Event()
        self._started = time.monotonic()
        self._message_id: int | None = None
        self._shown = ""
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._started = time.monotonic()
        self._api.send_chat_action(self._chat_id, self._action)
        text = self.render()
        try:
            self._message_id = self._api.send_message(self._chat_id, text)["message_id"]
            self._shown = text
        except TelegramError:
            self._message_id = None
        self._thread.start()

    def step(self, step: Step) -> None:
        """Show only what is happening now: thinking keeps the last action in view, any other
        state clears it, and each new action replaces the one before."""
        if step.kind == "said":
            return
        with self._lock:
            if step.kind in _STATES:
                self._state = step.kind
                if step.kind != "thinking":
                    self._doing = ""
                if step.kind == "recording":
                    self._action = "record_voice"
            else:
                self._state = "working"
                self._doing = (
                    f"💬 {i18n.t(self._language, 'step_update')}" if step.kind == "update"
                    else action(step, self._language)
                )

    def render(self) -> str:
        with self._lock:
            state, doing = self._state, self._doing
        # An answer that will be a voice note is recorded, not written.
        if state == "writing" and self._action == "record_voice":
            state = "recording"
        label, icon = _STATES[state]
        header = f"<b>{i18n.t(self._language, label)}</b> {elapsed(time.monotonic() - self._started)} {icon}"
        return f"{header}\n\n{doing}" if doing else header

    def tick(self) -> None:
        """Refresh the message if anything visible changed, and renew the chat action."""
        self._api.send_chat_action(self._chat_id, self._action)
        text = self.render()
        with self._message_lock:
            if self._message_id is not None and text != self._shown:
                self._api.edit_message(self._chat_id, self._message_id, text)
                self._shown = text

    def renew(self) -> None:
        """Move the message below the person's latest ones: send it again, then delete the old one."""
        text = self.render()
        with self._message_lock:
            old = self._message_id
            try:
                self._message_id = self._api.send_message(self._chat_id, text)["message_id"]
                self._shown = text
            except TelegramError:
                return
            if old is not None:
                self._api.delete_message(self._chat_id, old)

    def become(self, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        """Stop refreshing and turn the message into ``text``; return its id, or None when
        there is no message to turn, so the caller sends ``text`` itself."""
        self._stop_refreshing()
        with self._message_lock:
            if self._message_id is None or not self._api.edit_message(self._chat_id, self._message_id, text, buttons):
                return None
            became, self._message_id = self._message_id, None
        return became

    def finish(self) -> None:
        """Stop refreshing and remove the message, unless it became the answer."""
        self._stop_refreshing()
        with self._message_lock:
            if self._message_id is not None:
                self._api.delete_message(self._chat_id, self._message_id)
                self._message_id = None

    def _stop_refreshing(self) -> None:
        self._done.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._done.wait(self._interval):
            self.tick()
