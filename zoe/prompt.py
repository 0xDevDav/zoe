"""Build the prompt for one turn from the person, history, recalled neurons, and the message.

Standing instructions are not repeated here: Claude reads ``CLAUDE.md`` from the
directory a turn runs in. A short default persona is added only when that file
is missing.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import access, directives, files, i18n, minds, observer, planner, signals, store, style
from .access import Person
from .brain import Brain, Recalled

_DEFAULT_PERSONA = (
    "You are Zoe, a personal assistant who talks to people over Telegram. "
    "Keep replies short and natural, suited to a phone screen."
)

_TRANSCRIBED = (
    "The message is a voice note, transcribed automatically. Names and jargon may be misheard. "
    "When you are confident a word was misheard, add one line per word at the very end of your reply, "
    "in the form [[{k} term: Correct | misheard]], the word term written as is, for example "
    "[[{k} term: Grafana | gravana]]. Those lines are removed before the reply is shown."
)

_CHOSEN = "The message is the option the person tapped among the ones you offered in your last reply."

_USED = (
    "When some of these shaped your reply, name them once with their numbers, [[{k} used: 12, 45]]: what helps "
    "is wired closer and what came up for nothing drifts away, so name only what you really used."
)

_PLANS = (
    "To do something later, add a line [[{k} plan: 2026-10-14 09:00 | what to do then]] with a local date and time, "
    "or with a cron rule such as 0 8 * * 1 (minute hour day month weekday) for something that repeats. "
    "Write what to do as a short instruction to yourself, in the person's language since they see it in their "
    "menu: when it comes due you receive it, with what you remember about it, and do it then. "
    "To cancel a plan, add [[{k} unplan: 12]] with its number. "
    "These lines are removed before the reply is shown."
)

_MINDS = (
    "For work that takes long, such as building a site or a program, a large piece of research, or anything "
    "beyond a few minutes, do not do it in this turn: start a parallel mind, which works on its own while the "
    "chat stays free, and tell the person you did. Write it as:\n"
    "[[{k} mind: Short title | /optional/folder]]\n"
    "The complete brief: the goal, where to work, constraints, and how to verify it is done. The mind does not "
    "see this chat.\n"
    "[[/mind]]\n"
    "To pass a note or an answer to a mind, add [[{k} tell: 3 | text]]; to stop one, [[{k} stop: 3]]. "
    "A mind never writes to the chat itself: it reports to you, so ask it for the full path of every file to "
    "show, and send them when its news arrives."
)

_SELF = (
    "When {name} asks you to learn something or to work differently, or something you do falls short, change "
    "yourself: your self-improvement skill changes your own code or skills through your pipeline, in a parallel "
    "mind when it takes more than a few minutes. Never work around yourself instead, with a separate script or a "
    "written rule standing in for a change to your code."
)

_FILES = (
    "Files people send are saved where the message says, with a number, and deleted after {days} days unless "
    "you keep them. Keep what they will need again (documents, contracts, receipts, anything they ask you to "
    "keep) with [[{k} keep: 12 | what it is]]; leave screenshots and things shown in passing. If you move a file, "
    "keep it with its new place: [[{k} keep: 12 | what it is | /new/place/name.pdf]]. To delete one now: "
    "[[{k} discard: 12]]. To send a file to the chat: [[{k} send: 12 | caption]], or [[{k} send: /path/to/file | "
    "caption]]. Several files separated by commas arrive together as an album, such as photos of a carousel: "
    "[[{k} send: /path/one.jpg, /path/two.jpg | caption]]. Photos and videos are compressed by Telegram: when "
    "the originals matter, end with | file to send them uncompressed, as files. A program that sends in your name, such as zoe-say or tg-doc, takes this chat's id: {chat}."
)

_ADMIT = (
    "Nobody can write to you unless your owner lets them. When your owner asks you to add someone by their "
    "Telegram id, add a line [[{k} admit: 123456789 | Name]]: the name is optional, and with the name of someone "
    "already listed below the id becomes another account of theirs. A forwarded message shows its sender's id "
    "when the sender allows it. To let a member read one of your owner's folders, add "
    "[[{k} share: Name | /folder]]; [[{k} unshare: Name | /folder]] takes it back. Never do either unless your "
    "owner asks."
)

_QUOTING = (
    "Messages show their number, like [#4521]. To send your reply as an answer to one of them, yours or theirs, "
    "for instance to bring something back to mind, add a line [[{k} quote: 4521]]. To change one of your own "
    "earlier messages instead of sending a new one (a list to update, a figure to correct), write its whole new "
    "text as your reply and add a line [[{k} edit: 4521]]: the person gets a short note pointing to it. "
    "When you write a text the person will forward or paste to someone else (a model, a partner, support), put "
    "only that text inside [[{k} forward]]…[[{k} /forward]]: no intro, no code block, no notes inside it. It "
    "reaches them bare, as its own message. Your own comment goes in the normal reply. To change a draft you "
    "already sent, add edit with its number and put the new draft in a forward block: the draft changes in "
    "place and your comment, if any, answers it."
)

RESUMED = (
    "Your previous run of this turn was stopped: it ran too long or went silent. Check what is already "
    "done before doing anything again, and do not repeat what has been done (messages sent, files changed, "
    "orders placed). Then finish the work and reply."
)

_LISTED = 120
_REPLIED = 1000
_PORTRAIT = 12
# Longest memory and history line in a prompt: enough to read, not enough to flood it.
_MEMORY = 1500
_TURN = 2000
# What a turn shows of what recall reaches: lasting memories, and a few past conversations.
LASTING = (*observer.KINDS, files.FILE)
SHOWN = 12
_TALKS = 2
_REACHED = 40
# The latest messages of the chat that recall also searches with, and their weight against what was just written.
CONTEXT = 4
_CONTEXT_WEIGHT = 0.5


def seeds(origins: Iterable[int | None], now: Iterable[Mapping[int, float]], before: Mapping[int, float]) -> Counter:
    """Where recall starts: the neurons a turn comes from, those found for what was just written and, at half
    the energy, those found for the messages just before it, which give a short message its sense."""
    found = Counter({nid: 1.0 for nid in origins if nid})
    for hits in now:
        found.update(hits)
    for nid, energy in before.items():
        found[nid] += _CONTEXT_WEIGHT * energy
    return found


def context(history: list[dict]) -> str:
    """The text of the latest messages that recall also searches with."""
    return " ".join(turn["text"] for turn in history[-CONTEXT:])


def select(reached: list[Recalled]) -> list[Recalled]:
    """What a turn shows of what recall reached, best first: up to eight lasting memories and two past
    conversations. The rest (words, plans, tasks) carries recall along without being shown."""
    lasting = [r for r in reached if r.kind in LASTING][:SHOWN]
    talks = [r for r in reached if r.kind == "episode"][:_TALKS]
    return sorted(lasting + talks, key=lambda r: r.score, reverse=True)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def anchor(telegram_id: int | None) -> str:
    """The number a message is shown with, which Zoe uses to quote it."""
    return f"[#{telegram_id}] " if telegram_id else ""


def _plans(conn: sqlite3.Connection, person: Person, key: str) -> list[str]:
    lines = [f"\nNow: {datetime.now().astimezone():%a %d %b %Y %H:%M %z}.",
             directives.keyed(_PLANS, key), directives.keyed(_QUOTING, key)]
    pending = planner.pending(conn, person.id)
    if pending:
        lines.append(f"Your plans with {person.name}:")
    for plan in pending:
        repeats = f", repeats {plan.repeat}" if plan.repeat else ""
        lines.append(f"- #{plan.id} {planner.stamp(plan.due_at)}{repeats}: {_cut(plan.instruction, _LISTED)}")
    return lines


def _people(conn: sqlite3.Connection, person: Person, key: str, archive: Path | None) -> list[str]:
    """How the owner lets someone write to Zoe, who already can, and where the whole chat
    history is; owners only."""
    if person.role != "owner":
        return []
    known = [p.name + (f" (reads {', '.join(p.folders)})" if p.folders else "")
             for p, accounts in access.people(conn) if p.role != "owner" and accounts]
    lines = [directives.keyed(_ADMIT, key)]
    if archive is not None:
        lines.append(
            f"The whole history of your chats, beyond the recent conversation shown here, is in the messages "
            f"table of the SQLite database {archive} (chat_id, telegram_id, direction in or out, text, "
            "created_at in Unix seconds): read it when you need an older message."
        )
    if known:
        lines.append(f"People who can write to you besides {person.name}: {', '.join(known)}.")
    return lines


def _minds(conn: sqlite3.Connection, person: Person, key: str) -> list[str]:
    """How to start parallel minds, and what the person's minds are doing; owners only."""
    if person.role != "owner":
        return []
    lines = [directives.keyed(_MINDS, key)]
    working, ended = minds.active(conn, person.id), minds.recent(conn, person.id, 3)
    if working or ended:
        lines.append("Your parallel minds (leave the folder of a working one to it):")
    now = time.time()
    for mind in working:
        if mind.state == "asking":
            status = f"waiting for an answer to: {mind.question}"
        elif mind.state == "queued":
            status = "about to start" + (" again" if mind.session_id else "")
        else:
            status = f"working for {minds.duration(now - (mind.started_at or now))}"
            status += f", last steps: {'; '.join(mind.steps[-3:])}" if mind.steps else ""
        lines.append(f"- #{mind.id} «{mind.title}» in {mind.workdir}, {status}")
    for mind in ended:
        lines.append(f"- #{mind.id} «{mind.title}» {mind.state} {minds.duration(now - (mind.finished_at or now))} ago")
    return lines


def _portrait(conn: sqlite3.Connection, brain: Brain, person: Person, shown: set[int]) -> list[str]:
    """How the person has been lately and the most salient things the observer learned about them."""
    trends = signals.trends(conn, person.id)
    learned = [r for r in brain.top(observer.KINDS, _PORTRAIT) if r.id not in shown]
    if not trends and not learned:
        return []
    lines = [f"\nWhat you have learned about {person.name} by observing your conversations:"]
    for t in trends:
        note = f" ({t.last_note})" if t.last_note else ""
        lines.append(f"- {t.kind} over the last 7 days: {t.average:.1f}/5, last {t.last:g}/5 "
                     f"on {planner.stamp(t.last_at)}{note}")
    lines += [f"- [{r.kind}] {r.content}" for r in learned]
    return lines




@dataclass(frozen=True)
class Recall:
    """What a turn's prompt brought back from memory: the neurons recall started from (a plan, a
    mind's task, the neuron behind a message replied to) and the memories shown, best first."""

    origins: list[int]
    memories: list[Recalled]


@dataclass(frozen=True)
class _Replied:
    text: str
    line: str
    seed: int | None


def _replied(conn: sqlite3.Connection, chat_id: int, person: Person, reply: dict) -> _Replied:
    """Describe the message replied to, from the chat log when it is there, with the neuron behind it:
    the plan it carried out or the parallel mind it was news of."""
    found = store.find_message(conn, chat_id, reply["id"]) if reply.get("id") else None
    mine = found["mine"] if found else reply.get("from_bot", False)
    text = (found["text"] if found else "") or reply.get("text", "")
    whose = "a message of yours" if mine else "their own message"
    number = f" #{reply['id']}" if reply.get("id") else ""
    when = f", sent {planner.stamp(found['created_at'])}" if found else ""
    line = f"\n{person.name} is replying to {whose}{number}{when}: «{_cut(text, _REPLIED)}»"
    if reply.get("quote"):
        line += f"\nThey quote this part of it: «{reply['quote']}»"
    origin = found["payload"] if found and mine else {}
    return _Replied(text, line, origin.get("plan") or origin.get("neuron"))


_MIND_NEWS = {
    "done": "has finished. Its report:\n{text}\nTell {name} in your words what was done, where the result is, "
            "and anything left open, and send the files it gives for them to see.",
    "ask": "needs a decision to go on: {text}\nAsk {name}. When they answer, pass the answer on with "
           "[[{k} tell: {id} | the answer]].",
    "failed": "stopped without finishing: {text}\nTell {name}, and offer to start it again or to change approach.",
}


def update(person: Person, news: list[tuple[int | None, str]]) -> str:
    """What resumes a turn interrupted by messages the person wrote while it worked."""
    lines = [f"While you were working, {person.name} wrote:"]
    lines += [f"{anchor(telegram_id)}{person.name}: {text}" for telegram_id, text in news]
    lines.append(
        "It may correct or add to what they asked. Take it into account, change what you were doing if needed, "
        "and then write one reply that covers everything, following the same rules as before."
    )
    lines.append(_reply_in(person))
    return "\n".join(lines)


def _reply_in(person: Person) -> str:
    """The language of the reply, said last, where it weighs most: work in another language
    (code, tools, skills, a brief) must not carry the reply with it."""
    return f"Reply in {i18n.language_name(person.language)}, whatever language your work, tools and skills are in."


def _ending(
    person: Person,
    text: str,
    telegram_id: int | None,
    plan: dict | None,
    mind: dict | None,
    followups: list[tuple[int | None, str]],
    key: str,
) -> str:
    if mind is not None:
        # format() fills the template only: the mind's text is never searched for {k}.
        news = _MIND_NEWS[mind["event"]].format(text=text, name=person.name, id=mind["mind"], k=key)
        said = f"\nYour parallel mind #{mind['mind']} «{mind['title']}» {news}"
    elif plan is not None:
        said = (
            f"\nIt is time for something you planned for {planner.stamp(plan['due_at'])}: {text}\n"
            f"Do it now. What you write is sent to {person.name} as a message from you, right after the "
            "conversation above: if you were talking about something else, bring it in naturally. "
            f"If nothing needs to be sent, reply only with [[{key} skip: the reason]]: the reason is remembered, "
            "never shown."
        )
    else:
        said = "\n" + "\n".join(f"{anchor(number)}{person.name}: {words}"
                                for number, words in [(telegram_id, text), *followups])
    return f"{said}\n{_reply_in(person)}\nYou:"


def _identity(person: Person) -> str:
    if person.role == "owner":
        return f"You are talking with {person.name}, your owner."
    shared = f" Besides your own folder you can read, and only read: {', '.join(person.folders)}." \
        if person.folders else ""
    return (
        f"You are talking with {person.name}. They are not your owner: never share anything about "
        f"your owner or about the other people you talk to.{shared}"
    )


def build(
    conn: sqlite3.Connection,
    brain: Brain,
    person: Person,
    chat_id: int,
    text: str,
    cwd: Path,
    transcribed: bool = False,
    spoken: bool = False,
    chosen: bool = False,
    telegram_id: int | None = None,
    plan: dict | None = None,
    mind: dict | None = None,
    reply: dict | None = None,
    followups: list[tuple[int | None, str]] = (),
    key: str = "",
    temp_file_days: float = 7.0,
    archive: Path | None = None,
) -> tuple[str, Recall]:
    """Return the prompt string and what it brought back from memory.

    A due plan and news from a parallel mind are given as their queued payloads,
    a reply as the description of the message it answers, and the messages the
    person sent right after the first as (Telegram id, text). Recall starts from
    the neuron of the plan or of the mind's task, from the one behind a message of
    Zoe's being replied to, from every text, and from the latest messages of the chat. ``key`` is the turn's
    directive key: it goes into the fixed instructions only, never into text Zoe reads.
    """
    replied = _replied(conn, chat_id, person, reply) if reply else None
    origins = ((plan or {}).get("plan"), (mind or {}).get("neuron"), replied and replied.seed)
    history = store.recent_history(conn, chat_id)
    written = (text, *(words for _, words in followups), replied and replied.text)
    starts = seeds(origins, [brain.search(words) for words in written if words], brain.search(context(history)))
    memories = select(brain.recall(starts, limit=_REACHED))

    parts: list[str] = []
    if not (cwd / "CLAUDE.md").exists():
        parts.append(_DEFAULT_PERSONA)
    parts.append(_identity(person))
    if transcribed:
        parts.append(directives.keyed(_TRANSCRIBED, key))
    if chosen:
        parts.append(_CHOSEN)
    parts.append(directives.how(key))
    parts.append(directives.keyed(style.SPOKEN if spoken else style.TELEGRAM, key))
    parts.extend(_plans(conn, person, key))
    parts.extend(_minds(conn, person, key))
    if person.role == "owner":
        parts.append(_SELF.format(name=person.name))
    parts.extend(_people(conn, person, key, archive))
    parts.append(directives.keyed(_FILES, key).replace("{days}", f"{temp_file_days:g}").replace("{chat}", str(chat_id)))
    parts.extend(_portrait(conn, brain, person, {r.id for r in memories}))

    if memories:
        parts.append("\nWhat you remember that may be relevant, each with its number:")
        kept = files.by_neurons(conn, [r.id for r in memories if r.kind == files.FILE])
        for r in memories:
            where = f" (file #{kept[r.id].id}, at {kept[r.id].path})" if r.id in kept else ""
            parts.append(f"- {r.id}: {_cut(r.content, _MEMORY)}{where}")
        parts.append(directives.keyed(_USED, key))

    if history:
        parts.append("\nRecent conversation:")
        for turn in history:
            who = "You" if turn["direction"] == "out" else person.name
            replying = f", replying to #{turn['reply_to']}" if turn.get("reply_to") else ""
            parts.append(f"{anchor(turn['telegram_id'])}{who}{replying}: {_cut(turn['text'], _TURN)}")

    if replied:
        parts.append(replied.line)
    parts.append(_ending(person, text, telegram_id, plan, mind, list(followups), key))
    return "\n".join(parts), Recall([nid for nid in origins if nid], memories)
