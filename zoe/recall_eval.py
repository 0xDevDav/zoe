"""Measure how well recall brings back what a message needs.

Once, a judge (Claude) reads a sample of the owner's real messages, with every lasting
memory and the past conversations recall might bring, and names what each message
needed. The set holds personal text, so it lives under ZOE_HOME, in ``recall-set.json``.

Scoring replays each message against memory as it was then, leaving out the neurons
made later, and tells which share of what was needed comes back among the memories a
turn shows: for recall as it runs, and for search alone (words and meaning), without
the links, so the part the links play can be seen. The nightly study reads the score and can measure a
change to recall against it.

The nightly study judges a new set every two weeks, so it covers the memories made since.
``python -m zoe.recall_eval build`` judges one now; ``python -m zoe.recall_eval`` scores it.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import access, meaning, observer, planner, prompt, store
from .access import Person
from .brain import Brain, Recalled
from .claude_runner import ClaudeRunner, Profile
from .config import Config
from .db import apply_schema, connect

log = logging.getLogger("zoe.recall_eval")

# The seeds recall starts from, as prompt.build takes them, and how far each replay looks.
SEEDS = 10
_CANDIDATES = 40
_SAMPLE = 80
# How old a judged set gets before the nightly study judges a new one.
RENEW = 14 * 86400.0
# The fewest messages, written once memory existed, that a set is judged on.
ENOUGH = 40
_BATCH = 10
_CUT = 240
_JUDGE_TIMEOUT = 900.0


@dataclass(frozen=True)
class Score:
    cases: int
    found: float
    useful: float
    alone: float


def path(cfg: Config) -> Path:
    return cfg.data_dir / "recall-set.json"


def _neurons(conn: sqlite3.Connection) -> dict[int, tuple[float, str]]:
    return {r["id"]: (r["created_at"], r["kind"]) for r in conn.execute("SELECT id, created_at, kind FROM neurons")}


def _found(brain: Brain, neurons: dict[int, tuple[float, str]], words: str, at: float) -> dict[int, float]:
    """What search finds for ``words`` among the neurons made before ``at``, as a turn's search would then."""
    hits = [(nid, energy) for nid, energy in brain.search(words, limit=_CANDIDATES).items()
            if neurons.get(nid, (at,))[0] < at] if words else []
    return {nid: energy / hits[0][1] for nid, energy in hits[:SEEDS]}


def _context(conn: sqlite3.Connection, message_id: int | None) -> str:
    """The latest messages before a judged one, as its turn searched with them."""
    row = conn.execute("SELECT chat_id FROM messages WHERE id = ?", (message_id,)).fetchone() if message_id else None
    return prompt.context(store.recent_history(conn, row["chat_id"], before=message_id)) if row else ""


def replay(brain: Brain, neurons: dict[int, tuple[float, str]], text: str, at: float, links: bool = True,
           context: str = "") -> list[int]:
    """The memories a turn would have shown for ``text``, with the chat's latest messages ``context``, at time
    ``at``: by recall as it runs, or by search alone, without the links."""
    seeds = prompt.seeds((), [_found(brain, neurons, text, at)], _found(brain, neurons, context, at))
    if links:
        reached = brain.recall(seeds, limit=_CANDIDATES)
    else:
        reached = [Recalled(r.id, r.kind, r.content, seeds[r.id]) for r in map(brain.get, seeds) if r]
    return [r.id for r in prompt.select([r for r in reached if neurons.get(r.id, (at,))[0] < at])]


def score(conn: sqlite3.Connection, person: Person, cases: list[dict]) -> Score | None:
    """How recall does on the judged cases: the share of what was needed that it brings back, the share of
    what it brings back that was needed, and the share search alone brings back; None with nothing to score."""
    brain, neurons = Brain(conn, person.id), _neurons(conn)
    found = useful = alone = 0.0
    counted = 0
    for case in cases:
        # A memory the nightly review merged since the judging counts as the one that kept it.
        needed = {nid for nid in map(brain.kept, case["needed"]) if nid in neurons and neurons[nid][0] < case["at"]}
        if not needed:
            continue
        context = _context(conn, case.get("message"))
        shown = set(replay(brain, neurons, case["text"], case["at"], context=context))
        found += len(needed & shown) / len(needed)
        useful += len(needed & shown) / len(shown) if shown else 0.0
        unlinked = replay(brain, neurons, case["text"], case["at"], links=False, context=context)
        alone += len(needed & set(unlinked)) / len(needed)
        counted += 1
    return Score(counted, found / counted, useful / counted, alone / counted) if counted else None


def _load(cfg: Config) -> dict | None:
    return json.loads(path(cfg).read_text(encoding="utf-8")) if path(cfg).exists() else None


def _save(cfg: Config, cases: list[dict], now: float) -> None:
    path(cfg).write_text(json.dumps({"made_at": now, "cases": cases}, ensure_ascii=False), encoding="utf-8")


def renew(conn: sqlite3.Connection, cfg: Config, person: Person, now: float, runner: ClaudeRunner | None = None) -> bool:
    """Judge a fresh set when there is none or it is two weeks old, so it covers the memories made since; a set
    renewed every night would be no yardstick, and one judged on a handful of messages would be a poor one, so
    the first waits for enough of them. Return whether a set was judged."""
    judged = _load(cfg)
    if (judged and now - judged["made_at"] < RENEW) or len(_eligible(conn, person)) < ENOUGH:
        return False
    runner = runner or ClaudeRunner(cfg.claude_bin, cfg.observer_model, _JUDGE_TIMEOUT, cfg.claude_inactivity_timeout)
    profile = Profile(observer.ARGS, cfg.data_dir / "observer")
    profile.cwd.mkdir(parents=True, exist_ok=True)
    cases = build(conn, person, runner, profile)
    if cases:
        _save(cfg, cases, now)
    return bool(cases)


def summary(conn: sqlite3.Connection, cfg: Config, person: Person) -> str:
    """One line on how well recall works, for the nightly study; empty without a judged set."""
    judged = _load(cfg)
    result = score(conn, person, judged["cases"]) if judged else None
    if result is None:
        return ""
    return (
        f"\nYour memory, measured on {result.cases} of {person.name}'s real messages a judge labelled on "
        f"{datetime.fromtimestamp(judged['made_at']):%Y-%m-%d}: recall brings back {result.found:.0%} of what they "
        f"needed (search alone, without the links, {result.alone:.0%}), and {result.useful:.0%} of the memories it "
        "shows were needed. `python -m zoe.recall_eval` measures it again after a change to recall."
    )


def _eligible(conn: sqlite3.Connection, person: Person) -> list[sqlite3.Row]:
    """Messages written once memory existed: one from before has nothing to be recalled."""
    return conn.execute(
        "SELECT id, chat_id, text, created_at FROM messages WHERE person_id = ? AND direction = 'in' "
        "AND telegram_id IS NOT NULL AND length(text) >= 20 "
        "AND created_at > (SELECT min(created_at) FROM neurons WHERE owner_id = ?) ORDER BY id",
        (person.id, person.id),
    ).fetchall()


def _sample(conn: sqlite3.Connection, person: Person, size: int) -> list[sqlite3.Row]:
    rows = _eligible(conn, person)
    return sorted(random.sample(rows, min(size, len(rows))), key=lambda r: r["id"])


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _CUT else text[:_CUT] + "…"


def judge_prompt(conn: sqlite3.Connection, person: Person, rows: list[sqlite3.Row]) -> str:
    brain, neurons = Brain(conn, person.id), _neurons(conn)
    lines = [
        f"You judge the memory of Zoe, a personal assistant who talks with {person.name} over Telegram. Before "
        f"answering, Zoe is shown up to {prompt.SHOWN} things she remembers. For each message below, name the memories she "
        "needed to answer it well: what she must know or keep in mind there, not what is merely on the same topic. "
        "Many messages need none. A program reads your answer.",
        "\nLasting memories, each with its number:",
        *[f"- {r.id} [{r.kind}] {_cut(r.content)}" for r in brain.top(prompt.LASTING, limit=2000)],
        "\nMessages:",
    ]
    for number, row in enumerate(rows, 1):
        before = conn.execute(
            "SELECT direction, text FROM messages WHERE chat_id = ? AND id < ? AND text IS NOT NULL "
            "ORDER BY id DESC LIMIT 2",
            (row["chat_id"], row["id"]),
        ).fetchall()
        at = row["created_at"]
        found = [n for n in brain.search(row["text"], limit=_CANDIDATES)
                 if n in neurons and neurons[n][0] < at and neurons[n][1] == "episode"]
        talks = set(found[:6]) | {n for n in replay(brain, neurons, row["text"], at) if neurons[n][1] == "episode"}
        lines.append(f"\nMessage {number}, {planner.stamp(row['created_at'])}")
        lines += [f"{'Zoe' if b['direction'] == 'out' else person.name} before: {_cut(b['text'])}"
                  for b in reversed(before)]
        lines.append(f"{person.name}: {_cut(row['text'])}")
        if talks:
            lines.append("Past conversations recall might bring for it:")
            lines += [f"- {nid} {_cut(brain.get(nid).content)}" for nid in sorted(talks)]
    lines.append(
        '\nAnswer with one JSON object and nothing else, each message number with the numbers of the memories and '
        'conversations it needed: {"1": [12, 345], "2": []}'
    )
    return "\n".join(lines)


def build(conn: sqlite3.Connection, person: Person, runner: ClaudeRunner, profile: Profile,
          size: int = _SAMPLE) -> list[dict]:
    """Judge a fresh sample of the person's messages; return the cases, each with what it needed."""
    cases = []
    rows = _sample(conn, person, size)
    for start in range(0, len(rows), _BATCH):
        batch = rows[start:start + _BATCH]
        result = runner.run(judge_prompt(conn, person, batch), profile)
        try:
            answer = observer.json_object(result.text) if result.ok else {}
        except ValueError:
            answer = {}
        if not answer:
            log.warning("batch from message %d not judged (rc=%s): %s", batch[0]["id"], result.returncode,
                        result.text[-300:] or result.failure[-300:])
            continue
        for number, row in enumerate(batch, 1):
            needed = observer.numbers(answer.get(str(number)) or [])
            cases.append({"message": row["id"], "at": row["created_at"], "text": row["text"], "needed": needed})
    return cases


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    Brain.meaning = meaning.load(cfg.data_dir / "models")
    conn = connect(cfg.db_path)
    apply_schema(conn)
    owner = next((person for person, _ in access.people(conn) if person.role == "owner"), None)
    if owner is None:
        print("no owner yet: Zoe has not run")
        return
    if sys.argv[1:2] == ["build"]:
        runner = ClaudeRunner(cfg.claude_bin, cfg.observer_model, _JUDGE_TIMEOUT, cfg.claude_inactivity_timeout)
        profile = Profile(observer.ARGS, cfg.data_dir / "observer")
        profile.cwd.mkdir(parents=True, exist_ok=True)
        cases = build(conn, owner, runner, profile)
        _save(cfg, cases, time.time())
        print(f"{len(cases)} messages judged")
    judged = _load(cfg)
    result = score(conn, owner, judged["cases"]) if judged else None
    if result is None:
        print("nothing to score")
        return
    print(f"messages scored    {result.cases}")
    print(f"needed, found      {result.found:.0%}")
    print(f"found, needed      {result.useful:.0%}")
    print(f"search alone found {result.alone:.0%}")


if __name__ == "__main__":
    main()
