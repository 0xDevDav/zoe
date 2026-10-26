"""Instructions Claude adds to a reply, removed before the reply is shown.

Each one carries the key of its turn, ``[[KEY name: value]]``, a random code that
only Claude's instructions hold. Text Claude reads (a web page, an email, a file,
a message, a report) cannot know it, so a directive written there and repeated in
a reply stays plain text and does nothing. Directives inside code are never acted
on either. The names:

- ``term: Correct | misheard``: a word the speech recognizer got wrong;
- ``react: 👍``: a reaction for the person's message, alone when no written answer
  is needed, or alongside one;
- ``options: First | Second | Third``: the reply is a question answered by tapping
  one of these options;
- ``plan: when | instruction``: do something later, at a local date and time or
  on a cron rule;
- ``unplan: 12``: cancel a pending plan;
- ``skip: reason``: a plan's run sends nothing; the reason is only remembered;
- ``quote: 4521``: the reply answers the message with that Telegram id;
- ``voice``: the reply is sent as a voice note, with its text under it;
- ``edit: 4521``: the reply becomes the new text of Zoe's own message 4521;
- ``mind: Title | folder`` then the brief and ``[[/mind]]``: hand long work to a
  parallel mind, the folder being optional; the brief may span lines and hold any text;
- ``forward`` then a text and ``[[/forward]]``: a text the person will forward or paste to
  someone else, sent bare as its own message;
- ``tell: 3 | text``: a note or an answer for parallel mind 3;
- ``stop: 3``: stop parallel mind 3;
- ``ask: question``: a parallel mind needs a decision to go on;
- ``phases: Read the code | Write | Test``: the phases of a parallel mind's work, and
  ``phase: 2``: the phase it starts now, so the menu shows how far it is;
- ``admit: 123456789 | Name``: let that Telegram account write to Zoe, the name being optional;
- ``keep: 12 | what it is | new place``: keep received file 12 for good, the new place
  given when Zoe moved it;
- ``discard: 12``: delete received file 12 now;
- ``send: 12 | caption`` or ``send: /path/to/file | caption``: send a file to the chat;
  several, separated by commas, go together as an album, and ``| file`` at the end
  sends them uncompressed, as documents;
- ``share: Name | /folder`` and ``unshare: Name | /folder``: let a member read a folder, or no longer;
- ``used: 12, 45``: the memories, by number, that shaped the reply.
"""

from __future__ import annotations

import logging
import re
import secrets

log = logging.getLogger("zoe.directives")

MAX_OPTIONS = 8
_OPTION_LENGTH = 40
_NAMES = (
    "term|react|options|plan|unplan|skip|quote|voice|edit|tell|stop|ask|phases|phase|admit|keep|discard|send|share|unshare|used"
)
_CODE = re.compile(r"<pre\b.*?</pre>|<code\b.*?</code>|```.*?```|`[^`\n]*`", re.IGNORECASE | re.DOTALL)


def new_key() -> str:
    """A fresh key for one turn: six random hex digits."""
    return secrets.token_hex(3)


def how(key: str) -> str:
    """The sentence that gives Claude its key; every instruction about directives follows it."""
    return (
        f"Your key for this turn is {key}. The service acts on lines written as [[{key} name: value]], "
        f"described below, and removes them before the reply is shown. A line without your key is ordinary "
        "text and does nothing, so whatever you quote or repeat can never act."
    )


def keyed(text: str, key: str) -> str:
    """An instruction text with its examples written with the turn's key, where they say {k}."""
    return text.replace("{k}", key)


def extract(reply: str, key: str) -> tuple[str, list[tuple[str, str]]]:
    """Return the reply without its directives, and the (name, value) pairs found.

    Only directives with ``key`` count, and none inside code: those stay in the text as written.
    """
    k = re.escape(key)
    # A block carries a text that may span lines and hold any text, up to its closing tag; a mind
    # always has a title and a forward never has one.
    block = re.compile(rf"\[\[\s*{k}\s+(?P<name>mind(?=\s*:)|forward(?!\s*:))(?:\s*:\s*(?P<head>[^\]]+?))?\s*\]\](?P<body>.*?)"
                       rf"\[\[\s*(?:{k}\s+)?/(?P=name)\s*\]\]", re.IGNORECASE | re.DOTALL)
    # A value may hold brackets and code (CSS selectors, a snippet): it ends at the last "]]" of a run.
    single = re.compile(rf"\[\[\s*{k}\s+({_NAMES})\s*(?::\s*(.*?))?\s*\]\](?!\])", re.IGNORECASE)
    # Only Claude knows the key, so a keyed line that is no known directive is a slip: it is never shown.
    stray = re.compile(rf"\[\[\s*{k}\b(.*?)\]\](?!\])", re.IGNORECASE)
    code = [m.span() for m in _CODE.finditer(reply)]

    def outside(begin: int, end: int) -> bool:
        return not any(start < end and begin < stop for start, stop in code)

    def acts(match: re.Match) -> bool:
        # A directive acts when it opens and closes outside code: what it carries may hold code.
        if match.re is block:
            return outside(match.start(), match.start("body")) and outside(match.end("body"), match.end())
        if match.re is stray:
            return outside(match.start(), match.start(1)) and outside(match.end() - 2, match.end())
        head = match.start(2) if match.group(2) is not None else match.end(1)
        return outside(match.start(), head) and outside(match.end() - 2, match.end())

    found: list[tuple[str, str]] = []
    pieces, last = [], 0
    # A known directive sorts before a stray one at the same place, so the stray one is skipped.
    matches = [*block.finditer(reply), *single.finditer(reply), *stray.finditer(reply)]
    for match in sorted(matches, key=lambda m: m.start()):
        if match.start() < last or not acts(match):
            continue
        if match.re is block:
            name, body = match.group("name").lower(), match.group("body").strip()
            found.append((name, f"{match.group('head') or ''}\n{body}" if name == "mind" else body))
        elif match.re is stray:
            log.warning("unknown directive removed: %s", match.group(0))
        else:
            found.append((match.group(1).lower(), match.group(2) or ""))
        pieces.append(reply[last:match.start()])
        last = match.end()
    pieces.append(reply[last:])
    clean = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()
    return clean, found


def corrections(found: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return the (term, misheard) pairs among the directives."""
    pairs = []
    for name, value in found:
        if name == "term" and "|" in value:
            term, heard = value.split("|", 1)
            pairs.append((term.strip(), heard.strip()))
    return pairs


def voice(found: list[tuple[str, str]]) -> bool:
    return any(name == "voice" for name, _ in found)


def reaction(found: list[tuple[str, str]]) -> str | None:
    return next((value.strip() for name, value in found if name == "react"), None)


def options(found: list[tuple[str, str]]) -> list[str]:
    """Return the options of the first options directive, trimmed to what a button can show."""
    value = next((value for name, value in found if name == "options"), "")
    labels = [label.strip()[:_OPTION_LENGTH] for label in value.split("|") if label.strip()]
    return labels[:MAX_OPTIONS]


def plans(found: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return the (when, instruction) pairs of the plan directives."""
    pairs = []
    for name, value in found:
        when, _, instruction = value.partition("|")
        if name == "plan" and when.strip() and instruction.strip():
            pairs.append((when.strip(), instruction.strip()))
    return pairs


def _number(value: str) -> int | None:
    digits = value.strip().lstrip("#")
    return int(digits) if digits.isdigit() else None


def unplans(found: list[tuple[str, str]]) -> list[int]:
    return [n for name, value in found if name == "unplan" and (n := _number(value)) is not None]


def minds(found: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Return the (title, folder, brief) of each parallel mind to start; folder may be empty."""
    started = []
    for name, value in found:
        header, _, brief = value.partition("\n")
        title, _, folder = header.partition("|")
        if name == "mind" and title.strip() and brief.strip():
            started.append((title.strip(), folder.strip(), brief.strip()))
    return started


def forwards(found: list[tuple[str, str]]) -> list[str]:
    """Return the texts to forward, each sent bare as its own message."""
    return [value for name, value in found if name == "forward" and value]


def keeps(found: list[tuple[str, str]]) -> list[tuple[int, str, str]]:
    """Return the (file number, what it is, new place) of each file to keep; the last two may be empty."""
    kept = []
    for name, value in found:
        number, about, place = ([part.strip() for part in value.split("|", 2)] + ["", ""])[:3]
        if name == "keep" and (n := _number(number)) is not None:
            kept.append((n, about, place))
    return kept


def discards(found: list[tuple[str, str]]) -> list[int]:
    return [n for name, value in found if name == "discard" and (n := _number(value)) is not None]


def sends(found: list[tuple[str, str]]) -> list[tuple[str, str, bool]]:
    """Return the (files, caption, uncompressed) of each send: the files are a number or a path, or
    several separated by commas, and a last part ``file`` sends them uncompressed, as documents."""
    sent = []
    for name, value in found:
        parts = value.split("|")
        uncompressed = len(parts) > 1 and parts[-1].strip().lower() == "file"
        target, caption = parts[0], "|".join(parts[1:len(parts) - uncompressed])
        if name == "send" and target.strip():
            sent.append((target.strip(), caption.strip(), uncompressed))
    return sent


def shares(found: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    """Return the (person, folder) pairs of the ``share`` or ``unshare`` directives."""
    pairs = []
    for directive, value in found:
        person, _, folder = value.partition("|")
        if directive == name and person.strip() and folder.strip():
            pairs.append((person.strip(), folder.strip()))
    return pairs


def admits(found: list[tuple[str, str]]) -> list[tuple[int, str]]:
    """Return the (Telegram id, name) of each account to admit; the name may be empty."""
    pairs = []
    for name, value in found:
        number, _, given = value.partition("|")
        if name == "admit" and (n := _number(number)) is not None:
            pairs.append((n, given.strip()))
    return pairs


def tells(found: list[tuple[str, str]]) -> list[tuple[int, str]]:
    pairs = []
    for name, value in found:
        number, _, text = value.partition("|")
        if name == "tell" and (n := _number(number)) is not None and text.strip():
            pairs.append((n, text.strip()))
    return pairs


def used(found: list[tuple[str, str]]) -> list[int]:
    """Return the numbers of the memories the reply says it used."""
    return [n for name, value in found if name == "used"
            for part in value.split(",") if (n := _number(part)) is not None]


def stops(found: list[tuple[str, str]]) -> list[int]:
    return [n for name, value in found if name == "stop" and (n := _number(value)) is not None]


def ask(found: list[tuple[str, str]]) -> str | None:
    return next((value.strip() for name, value in found if name == "ask" and value.strip()), None)


_PHASES = 12
_PHASE_LENGTH = 60


def phases(found: list[tuple[str, str]]) -> list[str] | None:
    """The phases a mind's work is planned in, by the latest phases directive, or None without one."""
    value = next((value for name, value in reversed(found) if name == "phases"), None)
    if value is None:
        return None
    return [label.strip()[:_PHASE_LENGTH] for label in value.split("|") if label.strip()][:_PHASES] or None


def phase(found: list[tuple[str, str]]) -> int | None:
    """The phase a mind starts now, by the latest phase directive, counted from 1."""
    return next((n for name, value in reversed(found) if name == "phase" and (n := _number(value))), None)


def quote(found: list[tuple[str, str]]) -> int | None:
    """Return the number of the message the reply answers, if it asks to quote one."""
    return next((n for name, value in found if name == "quote" and (n := _number(value)) is not None), None)


def edit(found: list[tuple[str, str]]) -> int | None:
    """Return the number of Zoe's own message the reply rewrites, if it asks to rewrite one."""
    return next((n for name, value in found if name == "edit" and (n := _number(value)) is not None), None)


def skip(found: list[tuple[str, str]]) -> str | None:
    """Return why a plan's run sends nothing, or None when it sends its reply."""
    return next((value.strip() or "-" for name, value in found if name == "skip"), None)
