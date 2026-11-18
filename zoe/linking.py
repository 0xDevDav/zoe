"""Notices from programs that join the conversation and the work they belong to.

A program that writes through ``zoe-say`` sees neither the chats nor Zoe's work,
so its notice may land in the middle of a conversation about the very same thing,
or report what one of Zoe's parallel minds is doing, and ask what the person has
just said. Before such a notice goes out, a short Claude run with no tools reads
it with the last hours of every chat of the person (one person may write from
several accounts, with the same memory) and with the minds at work or ended in
those hours, each with its folder and last steps.

- When the notice comes from Zoe's own work and gives the person nothing to know
  or do, such as a test a mind sent to itself, it is not sent: it stays in the
  chat log, marked as not sent, so Zoe still knows of it.
- When it is about a conversation or Zoe's work and does matter, it becomes Zoe's
  next message in its chat: starting from what was being said or done, saying so
  when the work is hers, with every fact of the notice and no question the chats
  or the minds already answer, and sent as a reply to the person's message it
  answers when that message is in the same chat.
- Otherwise it goes out exactly as the program wrote it.

Only text notices of a program are read, and only while the person wrote in one
of their chats in the last ``WINDOW`` seconds or one of their minds is at work or
ended in them: a quiet person costs nothing, and a file, a voice note or a message
with buttons always goes as written, since its text belongs to what it carries.
A notice is never lost or held up by a fault: a run that fails, answers something
unreadable, drops a link of the notice or takes longer than ``LIMIT`` seconds lets
the notice go as written, and while it runs only the later messages of the same
chat wait behind it.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from . import access, formatting, minds, observer, prompt, store
from .claude_runner import DOWN, ClaudeRunner, Profile
from .config import Config

log = logging.getLogger("zoe.linking")

# Seconds back a notice is read with: the chats of these hours and the minds at work or ended in them.
WINDOW = 3 * 3600.0
# Seconds a notice may wait for its run before it goes as written.
LIMIT = 20.0
_HISTORY = 12
_MINDS = 8
_TURN = 600
_LINK = re.compile(r"""https?://[^\s"'<>]+""")

_PROMPT = """\
You are {bot}, the assistant of {name}. A program on this machine, "{origin}", wrote the notice below \
for {name}, to be sent in your name to one of your chats with them. The program sees neither your chats \
nor your work. Before the notice goes out, decide whether it is about something you and {name} are talking \
about or something you are doing yourself.

{context}

The notice:
{notice}

The notice is about your conversation when it is about the very thing one of the chats above is about: the \
same object, app, site, person, order or task, or the outcome of something {name} said they are doing. It \
comes from your own work when it is caused by what one of your parallel minds is doing or has just done, or \
by what you said in a chat that you are doing: a test of it, or an effect of it, such as a message or mail \
it sent, a code or confirmation it triggered, an account, record or setting it created or changed. Sharing \
a broad area (work, email, money) is not enough. When in doubt, it is about neither.

When it is about neither, answer {{"joins": false}}.

When it comes from your own work and gives {name} nothing to know or do beyond what the chats already say, \
as with a test a mind sent to itself, it is not sent: answer
{{"joins": true, "send": false, "why": "<the work of yours it comes from, in a few words>"}}
It is sent whenever it carries something {name} would act on or want to know: an error, a cost, a deadline, \
a step left to them, or a security warning your work does not plainly explain.

Otherwise write it as your next message in the chat it goes to, and answer
{{"joins": true, "reply_to": <the number of {name}'s message it answers in that chat, or null>, "text": "<your message>"}}
Your message:
- starts from what you were talking about or doing, so {name} sees at once how it connects, and says first \
when it comes from your own work;
- keeps every fact of the notice: figures, names, times, codes and links, and adds nothing that neither the \
notice, the chats nor your minds say: no cause, link to a mind, plan or promise they do not plainly state;
- states as known what the chats or your minds already answer, such as whether it was {name} or you and \
whether it is being done: never as a question or a condition (such as "if it was not you"), and with no \
advice they make pointless;
- is short, in the language of the conversation, in Telegram HTML like the notice (<b>, <i>, <code>, \
<a href="...">), with no long dash or en dash, and an emoji only at the end of a line.

Answer with the JSON object alone."""


@dataclass(frozen=True)
class Linked:
    """A notice rewritten into the conversation, and the Telegram id of the message it replies to."""

    text: str
    reply_to: int | None


@dataclass(frozen=True)
class Held:
    """A notice about Zoe's own work that is not sent, and the work it comes from."""

    why: str

    def logged(self, text: str) -> str:
        """The notice as the chat log keeps it, marked as not sent."""
        return f"[not sent: {self.why}] {text}" if self.why else f"[not sent] {text}"


@dataclass
class _Draft:
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    linked: Linked | Held | None = None


class Linker:
    """Reads the notices of the outbox with the person's chats and minds before they go. Used by the outbox
    thread only; each run works on a thread of its own and never touches the database."""

    def __init__(self, cfg: Config, runner: ClaudeRunner | None = None, limit: float = LIMIT) -> None:
        self._bot = cfg.bot_name
        self._limit = limit
        self._runner = runner or ClaudeRunner(cfg.claude_bin, cfg.observer_model, limit, limit)
        self._profile = Profile(observer.ARGS, cfg.data_dir / "observer")
        self._drafts: dict[int, _Draft] = {}

    def ready(self, conn: sqlite3.Connection, row: sqlite3.Row,
              now: float | None = None) -> tuple[bool, Linked | Held | None]:
        """Whether the outbox message may go now and, when it joins the conversation or Zoe's work, how:
        rewritten, or held back. The first call for a message starts its run when it has one to make."""
        now = time.time() if now is None else now
        draft = self._drafts.get(row["id"])
        if draft is None:
            try:
                asked = self._prompt(conn, row, now)
            except Exception:
                log.exception("chat for outbox message %d not read: sent as written", row["id"])
                asked = None
            if asked is None:
                return True, None
            draft = self._drafts[row["id"]] = _Draft(now + self._limit)
            self._profile.cwd.mkdir(parents=True, exist_ok=True)
            threading.Thread(target=self._write, args=(draft, *asked, row["text"]),
                             name=f"linking-{row['id']}", daemon=True).start()
        if draft.done.is_set():
            return True, draft.linked
        if now < draft.deadline:
            return False, None
        draft.cancel.set()
        self._drafts[row["id"]] = _Draft(draft.deadline)
        self._drafts[row["id"]].done.set()
        log.warning("outbox message %d not read with its chat within %gs: sent as written", row["id"], self._limit)
        return True, None

    def forget(self, outbox_id: int) -> None:
        """Drop what was prepared for a message that left the outbox."""
        self._drafts.pop(outbox_id, None)

    def _prompt(self, conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> tuple[str, set[int]] | None:
        """The prompt for a notice to read with the person's chats and minds, with the numbers of the person's
        messages it may answer in its own chat; None when it goes as written without a run."""
        if not row["origin"] or row["file"] or row["options"] or not (row["text"] or "").strip():
            return None
        if store.get_state(conn, DOWN):
            return None
        person = access.resolve(conn, row["chat_id"]).person
        if person is None:
            return None
        since = now - WINDOW
        chats = _chats(conn, person.id, row["chat_id"])
        work = minds.active(conn, person.id) + [
            mind for mind in minds.recent(conn, person.id, _MINDS) if (mind.finished_at or 0) >= since]
        if not work and not conn.execute(
            f"SELECT 1 FROM messages WHERE chat_id IN ({','.join('?' * len(chats))}) AND direction = 'in' "
            f"AND {store.NOT_OWN_WORK} AND created_at >= ? LIMIT 1",
            (*chats, since),
        ).fetchone():
            return None
        here, *elsewhere = [store.recent_history(conn, chat, limit=_HISTORY, since=since) for chat in chats]
        theirs = {turn["telegram_id"] for turn in here if turn["direction"] == "in" and turn["telegram_id"]}
        context = ["The chat the notice goes to, last hours, oldest first, each message with its number:\n"
                   + (_lines(here, person.name, numbered=True) or "(no message)")]
        context += [f"Another chat of yours with {person.name}, from another of their accounts (the same person "
                    f"and the same memory), last hours, oldest first:\n{_lines(turns, person.name)}"
                    for turns in elsewhere if turns]
        if work:
            context.append("Your parallel minds at work or ended in the last hours, each with its folder:\n"
                           + "\n".join(f"- #{mind.id} «{mind.title}» in {mind.workdir}, "
                                       f"{minds.status(mind, now, steps=True)}" for mind in work))
        asked = _PROMPT.format(bot=self._bot, name=person.name, origin=row["origin"],
                               context="\n\n".join(context), notice=row["text"])
        return asked, theirs

    def _write(self, draft: _Draft, asked: str, theirs: set[int], notice: str) -> None:
        try:
            result = self._runner.run(asked, self._profile, cancel=draft.cancel, effort="low")
            if result.ok:
                draft.linked = parse(result.text, theirs, notice)
            elif not result.cancelled:
                log.warning("notice not read with its chat (rc=%s): %s", result.returncode, result.failure[:200])
        except Exception:
            log.exception("notice not read with its chat")
        finally:
            draft.done.set()


def parse(answer: str, theirs: set[int], notice: str) -> Linked | Held | None:
    """The rewritten or held notice in a run's answer, or None when it stays as written: the notice joins
    neither a conversation nor Zoe's work, the answer is unreadable, or the new text lost a link of the notice."""
    start = answer.find("{")
    try:
        found, _ = json.JSONDecoder().raw_decode(answer[start:]) if start >= 0 else (None, 0)
    except ValueError:
        found = None
    if not isinstance(found, dict) or found.get("joins") is not True:
        return None
    if found.get("send") is False:
        why = found.get("why")
        return Held(" ".join(why.split()) if isinstance(why, str) else "")
    text = found.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    text = formatting.to_telegram(text.strip())
    if not _links(notice) <= _links(text):
        log.warning("rewritten notice lost a link: sent as written")
        return None
    reply_to = str(found.get("reply_to") or "").lstrip("#")
    return Linked(text, int(reply_to) if reply_to.isdigit() and int(reply_to) in theirs else None)


def _chats(conn: sqlite3.Connection, person_id: int, chat_id: int) -> list[int]:
    """The chats of a person: the one a notice goes to first, then those of their other approved accounts."""
    accounts = [account.telegram_user_id for person, owned in access.people(conn) if person.id == person_id
                for account in owned]
    return [chat_id] + [account for account in accounts if account != chat_id]


def _lines(turns: list[dict], name: str, numbered: bool = False) -> str:
    """A chat as the run reads it; with ``numbered``, each message with the number a reply quotes."""
    return "\n".join(f"{prompt.anchor(turn['telegram_id']) if numbered else ''}"
                     f"{'You' if turn['direction'] == 'out' else name}: {_cut(turn['text'])}" for turn in turns)


def _links(text: str) -> set[str]:
    return set(_LINK.findall(html.unescape(text)))


def _cut(text: str) -> str:
    return text if len(text) <= _TURN else text[:_TURN] + "…"
