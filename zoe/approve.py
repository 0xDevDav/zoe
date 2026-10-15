"""The owner's yes on Telegram before an action their rules mark as asking.

``ZOE_HOME/ask.json`` lists Claude Code permission rules, for example
``["Bash(git push *)", "mcp__whatsapp__send_message"]``, for the actions that need the
owner's yes: writing to other people, publishing, spending. Owner turns and parallel
minds run with them as ``permissions.ask`` and with this module as their
PermissionRequest hook, so Claude Code's own rule matching decides what asks. When
Claude is about to take such an action, the hook asks in the chat, with Allow and
Deny buttons, and waits up to WAIT for the tap; no answer, or nobody to ask, means
no. The hook runs as ``python -m zoe.approve``; the service sends its question and
closes what nobody answered (``deliver``), and takes the tap (``answer``).

A guard of the owner's own, a PreToolUse hook that decides by itself what needs a yes,
asks the same way: it passes its input to ``python -m zoe.approve`` and gets the
answer as a PreToolUse decision. Claude Code does not hand a PreToolUse "ask" on to
the PermissionRequest hook in a run without a terminal, so such a guard asks here.
"""

from __future__ import annotations

import html
import json
import logging
import os
import shlex
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import access, i18n
from .claude_runner import Profile
from .config import Config
from .db import connect
from .telegram import TelegramAPI

log = logging.getLogger("zoe.approve")

RULES = "ask.json"
CHAT = "ZOE_CHAT"
MIND = "ZOE_MIND"
WAIT = 600.0
_POLL = 1.0
_SHOWN = 600


def rules(home: Path) -> list[str]:
    """The rules in ``home/ask.json``; none when the file is missing or is not a list of rules."""
    path = home / RULES
    if not path.exists():
        return []
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("%s not read: %s", path, exc)
        return []
    if not isinstance(found, list) or not all(isinstance(rule, str) for rule in found):
        log.warning("%s is not a list of rules", path)
        return []
    return found


def asking(profile: Profile, home: Path, chat_id: int | None, mind: str = "") -> Profile:
    """An owner's profile whose actions marked as asking wait for a yes in ``chat_id``, naming the
    parallel mind that asks, if one does; without a chat they are refused."""
    hook = {"type": "command", "command": f"{shlex.quote(sys.executable)} -m zoe.approve", "timeout": int(WAIT) + 60}
    settings = json.dumps({
        "permissions": {"ask": rules(home)},
        "hooks": {"PermissionRequest": [{"matcher": "*", "hooks": [hook]}]},
    })
    return replace(profile, args=profile.args + ("--settings", settings),
                   env={**profile.env, CHAT: str(chat_id or ""), MIND: mind})


# --- the hook ------------------------------------------------------------------

def _decision(event: str, allowed: bool, message: str = "") -> str:
    """The hook's answer in the form its event expects."""
    if event == "PreToolUse":
        decision = {"permissionDecision": "allow"} if allowed else {
            "permissionDecision": "deny", "permissionDecisionReason": message}
        return json.dumps({"hookSpecificOutput": {"hookEventName": event, **decision}})
    # An allow carries no updatedInput: Claude Code checks a changed input against the rules
    # again, and the same input would ask once more.
    decision = {"behavior": "allow"} if allowed else {"behavior": "deny", "message": message}
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}})


def wait(conn: sqlite3.Connection, chat_id: int, mind: str, tool: str, args: dict,
         timeout: float = WAIT, poll: float = _POLL) -> str:
    """Ask in ``chat_id`` and wait for the answer; return allowed, denied or expired."""
    cur = conn.execute(
        "INSERT INTO approvals (chat_id, asker, tool, args, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, mind, tool, json.dumps(args, ensure_ascii=False), time.time()),
    )
    approval, deadline = cur.lastrowid, time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = conn.execute("SELECT state FROM approvals WHERE id = ?", (approval,)).fetchone()["state"]
        if state != "waiting":
            return state
        time.sleep(poll)
    expired = conn.execute(
        "UPDATE approvals SET state = 'expired', answered_at = ? WHERE id = ? AND state = 'waiting'",
        (time.time(), approval),
    ).rowcount
    return "expired" if expired else conn.execute("SELECT state FROM approvals WHERE id = ?", (approval,)).fetchone()[0]


def decide(request: dict, cfg: Config, env: dict[str, str]) -> tuple[bool, str]:
    """Whether the owner allows one request, and what Claude is told when they do not."""
    owner = cfg.owner_name
    chat = env.get(CHAT, "")
    if not chat.lstrip("-").isdigit():
        return False, (f"This action needs {owner}'s yes on Telegram, and nobody can be asked now. "
                       "Do not do it another way; say what you wanted to do.")
    conn = connect(cfg.db_path)
    try:
        outcome = wait(conn, int(chat), env.get(MIND, ""), str(request.get("tool_name", "?")),
                       request.get("tool_input") or {})
    finally:
        conn.close()
    if outcome == "allowed":
        return True, ""
    if outcome == "denied":
        return False, f"{owner} said no on Telegram. Do not try it another way; tell them in your reply."
    return False, (f"{owner} did not answer within {int(WAIT // 60)} minutes, so it was not done. Do not retry "
                   "it; say in your reply what you wanted to do.")


# --- the service -----------------------------------------------------------------

def describe(tool: str, args: dict) -> str:
    """An action as the owner reads it: what Claude says it does and the command, or the tool and what it gets."""
    def cut(value: object) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return html.escape(text if len(text) <= _SHOWN else text[:_SHOWN] + "…")

    if tool == "Bash":
        about = f"{cut(args['description'])}\n" if args.get("description") else ""
        return f"{about}<code>{cut(args.get('command', ''))}</code>"
    server, _, name = tool.removeprefix("mcp__").partition("__")
    head = f"<b>{html.escape(server)}</b>: {html.escape(name)}" if tool.startswith("mcp__") else f"<b>{html.escape(tool)}</b>"
    return "\n".join([head, *(f"{html.escape(str(key))}: {cut(value)}" for key, value in args.items())])


def _language(conn: sqlite3.Connection, chat_id: int) -> str:
    found = access.resolve(conn, chat_id).person
    return found.language if found else i18n.DEFAULT


def _text(conn: sqlite3.Connection, row: sqlite3.Row, outcome: str = "") -> str:
    """The question, with how long it waits, or with its outcome once it has one."""
    lang = _language(conn, row["chat_id"])
    who = i18n.t(lang, "approval_mind", mind=html.escape(row["asker"])) if row["asker"] else ""
    asked = i18n.t(lang, "approval_ask", who=who, what=describe(row["tool"], json.loads(row["args"])))
    ending = i18n.t(lang, f"approval_{outcome}") if outcome else i18n.t(lang, "approval_hint", minutes=int(WAIT // 60))
    return f"{asked}\n\n{ending}"


def deliver(conn: sqlite3.Connection, api: TelegramAPI, now: float | None = None) -> None:
    """Ask the questions not asked yet, and close the ones nobody answered in time, even when
    the hook that waited for them is gone."""
    now = time.time() if now is None else now
    conn.execute("UPDATE approvals SET state = 'expired', answered_at = ? WHERE state = 'waiting' AND created_at < ?",
                 (now, now - WAIT - 30))
    for row in conn.execute("SELECT * FROM approvals WHERE state = 'waiting' AND message_id IS NULL").fetchall():
        lang = _language(conn, row["chat_id"])
        buttons = [[{"text": i18n.t(lang, "btn_allow"), "callback_data": f"a:allow:{row['id']}", "style": "success"},
                    {"text": i18n.t(lang, "btn_deny"), "callback_data": f"a:deny:{row['id']}", "style": "danger"}]]
        sent = api.send_message(row["chat_id"], _text(conn, row), buttons=buttons)
        conn.execute("UPDATE approvals SET message_id = ? WHERE id = ?", (sent.get("message_id"), row["id"]))
    for row in conn.execute(
        "SELECT * FROM approvals WHERE state = 'expired' AND settled = 0 AND message_id IS NOT NULL"
    ).fetchall():
        api.edit_message(row["chat_id"], row["message_id"], _text(conn, row, "expired"))
        conn.execute("UPDATE approvals SET settled = 1 WHERE id = ?", (row["id"],))


def asked(conn: sqlite3.Connection, chat_id: int, since: float) -> int:
    """How many questions reached the chat since ``since``."""
    return conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE chat_id = ? AND created_at >= ? AND message_id IS NOT NULL",
        (chat_id, since),
    ).fetchone()[0]


def answer(conn: sqlite3.Connection, api: TelegramAPI, person: access.Person, query: dict) -> None:
    """Take an owner's tap on Allow or Deny; a question already closed only says so."""
    parts = (query.get("data") or "").split(":")
    verdict, number = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
    state = {"allow": "allowed", "deny": "denied"}.get(verdict)
    taken = state and number.isdigit() and conn.execute(
        "UPDATE approvals SET state = ?, answered_at = ?, settled = 1 WHERE id = ? AND state = 'waiting'",
        (state, time.time(), int(number)),
    ).rowcount
    if not taken:
        api.answer_callback(query["id"], i18n.t(person.language, "approval_closed"))
        return
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (int(number),)).fetchone()
    api.edit_message(row["chat_id"], row["message_id"], _text(conn, row, state))
    api.answer_callback(query["id"])


def main() -> None:
    """The hook: read the request, ask and wait, print the decision. Anything that goes wrong means no."""
    event = "PermissionRequest"
    try:
        request = json.load(sys.stdin)
        event = request.get("hook_event_name") or event
        print(_decision(event, *decide(request, Config.from_env(), dict(os.environ))))
    except Exception as exc:
        print(f"zoe.approve: {exc}", file=sys.stderr)
        print(_decision(event, False, f"The yes on Telegram could not be asked ({exc}). Do not do it; say why."))


if __name__ == "__main__":
    main()
