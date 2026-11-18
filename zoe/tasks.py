"""Tasks programs hand to Zoe: ``zoe-say --task``.

A program on the machine sometimes finds work for Zoe herself rather than news
for the person: an alert whose fix she starts at once, without asking. It hands
the work over as a task, which runs as a turn of Zoe's in the person's chat,
like a plan that came due: she does it with the person's powers, in order with
the chat, and writes to the person what is worth telling, or nothing.

A task gives no power a program lacks: only a program that can write Zoe's
database hands one, and such a program can already queue a plan due at once. Its
text is the program's own words, and Zoe does what it says.

A task names what it is about with keys, such as the rules an alert found
broken. A parallel mind started in a task's turn takes on the task's keys
(``minds.py``), and while it works, a later task sharing one of them goes to
that mind as a note, with its keys: no second mind and no second message for
the same thing. Once the mind ends, the next task on it is a turn again. A task
waits for the turn of another task sharing one of its keys, so it is passed on
only once the minds of that turn have started.

Only an owner's chat takes tasks: a task's turn has every power.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterator

from . import minds, store

log = logging.getLogger("zoe.tasks")

TASK = "task"
_guard = threading.Lock()
# One lock per person and key, held through the turn of a task and the start of its minds.
_keys: dict[tuple[int | None, str], threading.Lock] = {}


def queue(conn: sqlite3.Connection, chat_id: int, person_id: int, text: str, origin: str, keys: list[str]) -> int:
    """Queue a task in a chat, as the turn of the person it is for."""
    return store.enqueue(conn, chat_id, person_id, text, TASK, {"origin": origin, "keys": list(dict.fromkeys(keys))})


def keys(msg: store.Message) -> list[str]:
    """The keys of a task, none for any other message."""
    return list((msg.payload or {}).get("keys") or []) if msg.kind == TASK else []


def origin(payload: dict | None) -> str:
    """The program that handed a task, as Zoe and the person read it."""
    return (payload or {}).get("origin") or "a program"


@contextlib.contextmanager
def deciding(msg: store.Message) -> Iterator[None]:
    """Hold the keys of a task while it is passed on or its turn runs, so a task sharing one of them waits for
    the minds this one starts. Keys are taken in order, so two tasks never wait for each other."""
    with _guard:
        held = [_keys.setdefault((msg.person_id, key), threading.Lock()) for key in sorted(keys(msg))]
    with contextlib.ExitStack() as stack:
        for lock in held:
            stack.enter_context(lock)
        yield


def pass_on(conn: sqlite3.Connection, msg: store.Message) -> int | None:
    """Give a task to the mind at work that took on one of its keys, and close it with no turn; return that
    mind, or None when no mind holds them and the task needs its turn. A mind waiting for the person's answer
    keeps waiting, and reads the task with the answer."""
    held = keys(msg)
    mind_id = minds.holding(conn, msg.person_id, held)
    if mind_id is None:
        return None
    sender = origin(msg.payload)
    if not minds.tell(conn, msg.person_id, mind_id, f"The program «{sender}» sent a new task on your work: {msg.text}",
                      answer=False):
        return None
    minds.take_on(conn, mind_id, held)
    store.note(conn, msg.id, "mind", mind_id)
    store.mark(conn, msg.id, "answered")
    log.info("task %d from %s goes to parallel mind %d", msg.id, sender, mind_id)
    return mind_id
