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

from . import access, directives, files, i18n, minds, observer, planner, signals, store, strength, style, tasks
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
    "A short brief: the result expected, where to work, the constraints that are real, who tries the result, "
    "and the one or two checks that matter.\n"
    "[[/mind]]\n"
    "The mind does not see this chat: tell it what it cannot find out alone, and leave to it how to do the "
    "work. It studies and tests by what a mistake costs, so list no "
    "cases to test for what the person tries for themselves in seconds, such as a screen or a text, and ask "
    "for full tests only where a mistake costs: " + strength.COSTLY + ". "
    "To pass a note or an answer to a mind, add [[{k} tell: 3 | text]]; to stop one, [[{k} stop: 3]]. "
    "A mind does something small asked about its work first and sends you news of it within minutes, while it "
    "goes on: when you pass such a request on, tell the person so, never that it comes with the rest of the "
    "work. "
    "A mind never writes to the chat itself: it reports to you, so ask it for the full path of every file to "
    "show, and send them when its news arrives."
)

_WAITING = (
    "Work that ends within about five minutes, such as a change over several accounts or a short download, is "
    "done in this turn, and the person gets one message: the result. A command in the foreground holds the turn "
    "for at most two minutes and is then moved to the background, so for a longer step start it in the "
    "background and wait for its end in this same turn, with checks in the foreground that each last less "
    "than two minutes, and reply once it is done. Only what takes longer, such as a print, a render or a long "
    "download, is left running in the background: reply right away with a single line saying what started and "
    "about how long it takes, nothing more. The chat stays free; when the task ends you are woken in this same "
    "session, and what you write then takes the place of your last message about it, options included, edited "
    "in place, or sent anew below when other messages came after it. Write it whole, as that message should read "
    "now, and never repeat in a result what an earlier update already told the person. If the chat went on "
    "after your last message, you are shown what it said before anything goes out. When there is nothing new "
    "to tell, as when another turn already closed the work or stopped your wait, reply only with "
    "[[{k} skip: the reason]]: nothing is sent and your last message stays as it is."
)

# The measure of a turn's own checks, the one a parallel mind works by.
_MEASURE = (
    "A touch-up the person sees at once and one command undoes, such as a text, a graphic, an alignment or a "
    "figure, you do in this turn, without a parallel mind: run the checks that already exist, write no new "
    "tests, and say what you did not try. Full tests are for where a mistake costs: " + strength.COSTLY + ". "
    "Your own code and skills are never such a touch-up: they change only through your pipeline, with its tests."
)

_SELF = (
    "When {name} asks you to learn something or to work differently, or something you do falls short, change "
    "yourself: your self-improvement skill changes your own code or skills through your pipeline, in a parallel "
    "mind when it takes more than a few minutes. Never work around yourself instead, with a separate script or a "
    "written rule standing in for a change to your code."
)

_MEMORY_CHANGES = (
    "When {name} asks you to merge, correct or forget what you have learned, write the changes in a JSON file "
    "shaped like the nightly review's answer, shown by python -m zoe.review apply --help, and run python -m "
    "zoe.review apply FILE, first with --dry-run. Never change memories in zoe.db with SQL, which loses the "
    "links a merge moves and the record of what changed."
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

# What a copy of a woken session is told when the chat went on after its last message, before its answer goes.
_CAUGHT_UP = (
    "Your reply above has not gone out. While you waited in the background, the chat went on without you, and "
    "you did not see it; what is marked You was written in your other turns:\n{news}\n"
    "Write now, whole, the reply that should go out after all this: it takes the place of your last message "
    "about this work{replaced}. Never tell again what those messages already told {name}, nor ask what they "
    "already answered. When there is nothing new to tell, as when another turn already closed the work or "
    "stopped your wait, reply only with [[{k} skip: the reason]]: nothing is sent and your last message stays "
    "as it is. Do not redo work that is done, and start nothing new."
)

STOPPED_WAITS = (
    "The service restarted while you waited in the background, after your answer to «{asked}», for:\n{waits}\n"
    "Those waits were stopped before they ended. Check whether their work is done. Start again in the "
    "background what is still needed, or tell the person what stopped and what it needs from them. Do not "
    "repeat what is already done or sent."
)

# What a plan's or a mind's news turn is told about messages the person wrote during it.
_OWED = (
    "{name} is waiting for your reply: finish the task above, then write one reply that covers both it and "
    "what they wrote, even if the task alone would send nothing."
)

_LISTED = 120
_REPLIED = 1000
# What the conversation shows of a message replied to that is not in full above.
_QUOTED = 300
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
    lines += [f"- #{mind.id} «{mind.title}» in {mind.workdir}, {minds.status(mind, now)}" for mind in working]
    lines += [f"- #{mind.id} «{mind.title}» {minds.status(mind, now)}" for mind in ended]
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


@dataclass(frozen=True)
class _Quoted:
    """A message being replied to: whose it is, and lines telling what it held."""

    found: dict | None
    text: str
    whose: str
    when: str
    about: list[str]


def _quoted(conn: sqlite3.Connection, chat_id: int, person: Person, reply: dict, limit: int,
            seen: bool = False) -> _Quoted:
    """Describe the message replied to, from the chat log when it is there: its text cut to ``limit``,
    the files it held with their paths and the part quoted; when it is ``seen`` in full in the prompt
    already, only the part quoted. When nothing of it is known, say so, so the person is asked."""
    found = store.find_message(conn, chat_id, reply["id"]) if reply.get("id") else None
    mine = found["mine"] if found else reply.get("from_bot", False)
    text = (found["text"] if found else "") or reply.get("text", "")
    held = (found["payload"] if found else {}).get("files") or []
    when = f", sent {planner.stamp(found['created_at'])}" if found else ""
    about = []
    if not seen:
        if text:
            about.append(f"«{_cut(text, limit)}»")
        if held:
            about.append(f"Files it held: {', '.join(held)}")
        if not about:
            about.append(f"its content is not available: ask {person.name} which message they mean")
    if reply.get("quote"):
        about.append(f"They quote this part of it: «{reply['quote']}»")
    return _Quoted(found, text, "a message of yours" if mine else "their own message", when, about)


def _replied(conn: sqlite3.Connection, chat_id: int, person: Person, reply: dict) -> _Replied:
    """Describe the message replied to with the neuron behind it: the plan it carried out or the
    parallel mind it was news of."""
    quoted = _quoted(conn, chat_id, person, reply, _REPLIED)
    number = f" #{reply['id']}" if reply.get("id") else ""
    line = f"\n{person.name} is replying to {quoted.whose}{number}{quoted.when}: " + "\n".join(quoted.about)
    found = quoted.found
    origin = found["payload"] if found and found["mine"] else {}
    return _Replied(quoted.text, line, origin.get("plan") or origin.get("neuron"))


def _said(conn: sqlite3.Connection, chat_id: int, person: Person, who: str, telegram_id: int | None, text: str,
          reply: dict | None, seen: Iterable[int] = ()) -> str:
    """A message as shown in the conversation, with what the message it replies to held, unless that
    one is ``seen`` in full above it."""
    if not reply:
        return f"{anchor(telegram_id)}{who}: {text}"
    quoted = _quoted(conn, chat_id, person, reply, _QUOTED, reply.get("id") in seen)
    said = f"{anchor(telegram_id)}{who}, replying to #{reply.get('id')}: {text}"
    if not quoted.about:
        return said
    heading = f"  (#{reply.get('id')} is {quoted.whose}{quoted.when}:"
    return "\n".join([said, heading, *(f"  {line}" for line in quoted.about)]) + ")"


_MIND_NEWS = {
    "done": "has finished. Its report:\n{text}\nTell {name} in your words what was done, where the result is, "
            "and anything left open, and send the files it gives for them to see.",
    "ask": "needs a decision to go on: {text}\nAsk {name}. When they answer, pass the answer on with "
           "[[{k} tell: {id} | the answer]].",
    "failed": "stopped without finishing: {text}\nTell {name}, and offer to start it again or to change approach.",
    "news": "is still at work and sends this news:\n{text}\nTell {name} now, in your own words and as briefly "
            "as it allows, what waits for them included, and send the files it gives for them to see: news of a "
            "mind at work is never kept for its final report.",
}
# What Zoe is told when a mind finishes after sending news: only what its report adds is left to tell.
_MIND_ADDS = (
    "has finished. Its report:\n{text}\nWhile it worked you already passed on this news of it:\n{told}\n"
    "Tell {name} in your words only what the report adds to that news: what is new, anything left open, and "
    "the files it gives that they have not seen. Never repeat what the news already told them."
)


def update(conn: sqlite3.Connection, person: Person, chat_id: int,
           news: list[tuple[int | None, str, dict | None]], own_work: bool = False) -> str:
    """What resumes a turn interrupted by messages the person wrote while it worked, each with what
    the message it replies to held. A turn of Zoe's ``own_work``, a plan, a mind's news or a task, owes
    them a reply even when that work alone would send nothing."""
    lines = [f"While you were working, {person.name} wrote:"]
    lines += [_said(conn, chat_id, person, person.name, telegram_id, text, reply) for telegram_id, text, reply in news]
    lines.append(_OWED.format(name=person.name) if own_work else (
        "It may correct or add to what they asked. Take it into account, change what you were doing if needed, "
        "and then write one reply that covers everything, following the same rules as before."
    ))
    lines.append(_reply_in(person))
    return "\n".join(lines)


def caught_up(conn: sqlite3.Connection, person: Person, chat_id: int, unseen: list[dict], replaced: int | None,
              key: str) -> str:
    """What a copy of a woken session is told before its answer goes: the messages of the chat it never saw
    (``store.unseen``) and that its answer, which takes the place of message ``replaced``, may be sent anew or
    not at all."""
    news = "\n".join(_said(conn, chat_id, person, "You" if turn["direction"] == "out" else person.name,
                           turn["telegram_id"], _cut(turn["text"], _TURN), turn["reply"]) for turn in unseen)
    asked = directives.keyed(_CAUGHT_UP, key).format(news=news, name=person.name,
                                                     replaced=f" (#{replaced})" if replaced else "")
    return f"{asked}\n{_reply_in(person)}"


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
    followups: list[str],
    key: str,
    task: dict | None = None,
) -> str:
    if mind is not None:
        # format() fills the template only: the mind's text is never searched for {k}.
        told = "\n".join(f"- {_cut(line, _MEMORY)}" for line in mind.get("told") or [])
        news = (_MIND_ADDS if told else _MIND_NEWS[mind["event"]]).format(
            text=text, name=person.name, id=mind["mind"], k=key, told=told)
        said = f"\nYour parallel mind #{mind['mind']} «{mind['title']}» {news}"
    elif plan is not None:
        said = (
            f"\nIt is time for something you planned for {planner.stamp(plan['due_at'])}: {text}\n"
            f"Do it now. What you write is sent to {person.name} as a message from you, right after the "
            "conversation above: if you were talking about something else, bring it in naturally. "
            f"If nothing needs to be sent, reply only with [[{key} skip: the reason]]: the reason is remembered, "
            "never shown."
        )
    elif task is not None:
        taken = (
            "When it needs a parallel mind, start one: the mind takes the task on, and while it works a later "
            f"task on the same thing goes to it as a note and reaches neither you nor {person.name}. "
        ) if task.get("keys") else ""
        said = (
            f"\nThe program «{tasks.origin(task)}» on this machine hands you a task: {text}\n"
            f"Do it now: {person.name}'s own programs ask it of you. {taken}What you write is sent to "
            f"{person.name} as a message from you, right after the conversation above: if you were talking about "
            f"something else, bring it in naturally. If nothing needs to be sent, reply only with "
            f"[[{key} skip: the reason]]: the reason is remembered, never shown."
        )
    else:
        said = "\n" + "\n".join([f"{anchor(telegram_id)}{person.name}: {text}", *followups])
    if followups and (mind is not None or plan is not None or task is not None):
        said += "\n" + "\n".join([f"{person.name} also wrote:", *followups, _OWED.format(name=person.name)])
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
    followups: list[tuple[int | None, str, dict | None]] = (),
    key: str = "",
    temp_file_days: float = 7.0,
    archive: Path | None = None,
    task: dict | None = None,
) -> tuple[str, Recall]:
    """Return the prompt string and what it brought back from memory.

    A due plan, news from a parallel mind and a program's task are given as their queued payloads,
    a reply as the description of the message it answers, and the messages the
    person sent right after the first as (Telegram id, text, the reply's description). Recall starts from
    the neuron of the plan or of the mind's task, from the one behind a message of
    Zoe's being replied to, from every text, and from the latest messages of the chat. ``key`` is the turn's
    directive key: it goes into the fixed instructions only, never into text Zoe reads.
    """
    replied = _replied(conn, chat_id, person, reply) if reply else None
    origins = ((plan or {}).get("plan"), (mind or {}).get("neuron"), replied and replied.seed)
    history = store.recent_history(conn, chat_id)
    written = (text, *(words for _, words, _ in followups), replied and replied.text)
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
    parts.append(directives.keyed(_WAITING, key))
    parts.append(_MEASURE)
    parts.extend(_minds(conn, person, key))
    if person.role == "owner":
        parts.append(_SELF.format(name=person.name))
    parts.extend(_people(conn, person, key, archive))
    parts.append(directives.keyed(_FILES, key).replace("{days}", f"{temp_file_days:g}").replace("{chat}", str(chat_id)))
    parts.extend(_portrait(conn, brain, person, {r.id for r in memories}))
    if person.role == "owner":
        parts.append(_MEMORY_CHANGES.format(name=person.name))

    if memories:
        parts.append("\nWhat you remember that may be relevant, each with its number:")
        kept = files.by_neurons(conn, [r.id for r in memories if r.kind == files.FILE])
        for r in memories:
            where = f" (file #{kept[r.id].id}, at {kept[r.id].path})" if r.id in kept else ""
            parts.append(f"- {r.id}: {_cut(r.content, _MEMORY)}{where}")
        parts.append(directives.keyed(_USED, key))

    if history:
        parts.append("\nRecent conversation:")
        seen: set[int] = set()
        for turn in history:
            who = "You" if turn["direction"] == "out" else person.name
            parts.append(_said(conn, chat_id, person, who, turn["telegram_id"], _cut(turn["text"], _TURN),
                               turn["reply"], seen))
            if turn["telegram_id"] and len(turn["text"]) <= _TURN:
                seen.add(turn["telegram_id"])

    if replied:
        parts.append(replied.line)
    shown = [_said(conn, chat_id, person, person.name, number, words, quoted) for number, words, quoted in followups]
    parts.append(_ending(person, text, telegram_id, plan, mind, shown, key, task))
    return "\n".join(parts), Recall([nid for nid in origins if nid], memories)
