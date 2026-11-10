"""The nightly review: each person's memory sorted after the day, the way sleep does it.

Once a night (``ZOE_REVIEW_AT``, 03:00 by default), for each person the observer
watches, a Claude run with no tools reads the conversations since the last review
together with everything learned about the person, and answers with:

- what the observer missed, above all what is easy to miss in a single exchange: a
  correction, something the person confirms or already knew, how a tool or service
  they use really behaves;
- known items that say the same thing, merged into one that keeps the links of all;
- items that are unclear or no longer true, rewritten;
- items that were never lasting or turned out wrong, forgotten.

Memory is knowledge, not behavior, so the changes apply at once. Each review is
written to ``ZOE_HOME/reviews`` with what every change replaced, and its summary
shows in the owner's menu, under Improvements. Then weak links are
pruned and activation fades across the graph.

The observer's thread runs it; ``python -m zoe.review`` runs one now. Changes the
owner asks for, written the way a review answers, apply the same way with
``python -m zoe.review apply FILE``, which ``--dry-run`` only shows.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import access, i18n, observer, store
from .access import Person
from .brain import Brain, Recalled
from .claude_runner import ClaudeRunner, Profile
from .config import Config
from .db import connect
from .planner import Cron, stamp

log = logging.getLogger("zoe.review")

STATE = "review_last"
_MESSAGES = 400
_TEXT = 500
_CONTENT = 300
_FIRST_WINDOW = 86400.0


def due(conn: sqlite3.Connection, rule: str, now: float, key: str = STATE) -> bool:
    """Whether the time of a nightly job, the review unless ``key`` names another, has come since
    its last run; the first start only sets the clock."""
    last = store.get_state(conn, key)
    if last is None:
        store.set_state(conn, key, str(now))
        return False
    return Cron(rule).next_after(datetime.fromtimestamp(float(last))).timestamp() <= now


def memories(conn: sqlite3.Connection, person_id: int) -> list[Recalled]:
    """Everything learned about a person, as the review sees it."""
    rows = conn.execute(
        f"SELECT id, kind, content, salience FROM neurons WHERE owner_id = ? "
        f"AND kind IN ({','.join('?' * len(observer.KINDS))}) ORDER BY kind, id",
        (person_id, *observer.KINDS),
    ).fetchall()
    return [Recalled(r["id"], r["kind"], r["content"], r["salience"]) for r in rows]


def conversations(conn: sqlite3.Connection, person_id: int, since: float) -> list[sqlite3.Row]:
    """The person's finished messages since ``since``, the latest ``_MESSAGES`` of them, oldest first."""
    rows = conn.execute(
        "SELECT telegram_id, direction, text, created_at FROM messages "
        "WHERE person_id = ? AND state = 'answered' AND text IS NOT NULL AND kind NOT IN ('plan', 'mind') "
        "AND created_at > ? ORDER BY id DESC LIMIT ?",
        (person_id, since, _MESSAGES),
    ).fetchall()
    return list(reversed(rows))


def build_prompt(person: Person, known: list[Recalled], rows: list[sqlite3.Row]) -> str:
    language = i18n.language_name(person.language)
    lines = [
        f"You review what Zoe, a personal assistant, has learned about {person.name}, the way sleep sorts a "
        "day's memories. You never talk to anyone: a program reads your answer.",
        "\nEverything learned so far, each with its number:",
        *(f"- #{r.id} [{r.kind}] {r.content}" for r in known),
        "\nConversations since the last review:",
    ]
    for r in rows:
        who = "Zoe" if r["direction"] == "out" else person.name
        text = r["text"] if len(r["text"]) <= _TEXT else r["text"][:_TEXT] + "…"
        lines.append(f"[#{r['telegram_id'] or '-'}] {stamp(r['created_at'])} {who}: {text}")
    lines.append(
        "\nAnswer with one JSON object and nothing else:\n"
        '{"learn": [{"kind": "fact", "content": "...", "messages": [4521]}], '
        '"merge": [{"keep": 12, "drop": [40, 55], "content": "..."}], '
        '"revise": [{"id": 7, "content": "..."}], "forget": [{"id": 9, "why": "..."}], "summary": "..."}\n'
        "- learn: lasting things the conversations show that no known item covers. Look above all for what is "
        f"easy to miss in a single exchange: a correction of Zoe, something {person.name} confirms or says they "
        "already know, how a tool or service they use really behaves, a limit or trap of their data. kind is "
        "fact, preference, habit, goal, or feedback for how Zoe should behave with them; content is one short "
        f"sentence in {language}; messages are the numbers it comes from.\n"
        "- merge: known items that say the same thing. keep is the one to keep, drop the others, content the "
        "one sentence that covers them all.\n"
        "- revise: a known item that is unclear, or that the conversations show is no longer true, with its new "
        "sentence.\n"
        "- forget: a known item that was never lasting (true only for a moment, small talk) or that turned out "
        "wrong, with why.\n"
        f"- summary: one sentence in {language} for {person.name}, saying in plain words what changed, or an "
        "empty string when nothing did.\n"
        "Use each known number at most once. Never guess beyond what the messages and the known items show. "
        "Empty lists are a fine answer."
    )
    return "\n".join(lines)


def _sentence(value: object) -> str:
    return " ".join(str(value or "").split())[:_CONTENT]


def apply(conn: sqlite3.Connection, person: Person, known: list[Recalled], answer: dict) -> dict:
    """Apply a review to the memories it was shown, in one transaction; return what changed and
    what each change replaced. A number it was not shown, or one already used, is ignored."""
    brain = Brain(conn, person.id)
    shown = {r.id: r for r in known}
    used: set[int] = set()
    done: dict = {"learned": [], "merged": [], "revised": [], "forgot": [], "summary": _sentence(answer.get("summary"))}

    def take(value: object) -> int | None:
        number = next(iter(observer.numbers(value)), None)
        if number in shown and number not in used:
            used.add(number)
            return number
        return None

    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        for item in answer.get("merge") or []:
            keep = take(item.get("keep"))
            drop = [n for n in map(take, item.get("drop") or []) if n is not None]
            if keep is None or not drop:
                continue
            content = _sentence(item.get("content")) or shown[keep].content
            brain.revise(keep, content)
            for gone in drop:
                brain.merge(keep, gone)
            observer.link_words(brain, keep, content)
            done["merged"].append({"id": keep, "content": content, "replaced": [shown[n].content for n in (keep, *drop)]})
        for item in answer.get("revise") or []:
            number, content = take(item.get("id")), _sentence(item.get("content"))
            if number is not None and content and brain.revise(number, content):
                observer.link_words(brain, number, content)
                done["revised"].append({"id": number, "content": content, "replaced": shown[number].content})
        for item in answer.get("forget") or []:
            number = take(item.get("id"))
            if number is not None and brain.forget(number):
                done["forgot"].append({"content": shown[number].content, "why": _sentence(item.get("why"))})
        for item in answer.get("learn") or []:
            kind, content = item.get("kind"), _sentence(item.get("content"))
            if kind not in observer.KINDS or not content or brain.find(kind, content) is not None:
                continue
            neuron = brain.learn(kind, content, related=brain.anchored(observer.numbers(item.get("messages"))))
            observer.link_words(brain, neuron, content)
            done["learned"].append({"id": neuron, "kind": kind, "content": content})
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    if own:
        conn.execute("COMMIT")
    return done


def change(conn: sqlite3.Connection, person: Person, answer: dict, folder: Path, now: float,
           dry_run: bool = False) -> tuple[dict, Path | None]:
    """Apply changes asked for, written like a review's answer, to all of the person's memories and
    record in ``folder`` what each replaced; return what changed and the record. A dry run tells what
    would change and changes nothing."""
    known = memories(conn, person.id)
    if dry_run:
        conn.execute("SAVEPOINT dry_run")
        try:
            return apply(conn, person, known, answer), None
        finally:
            conn.execute("ROLLBACK TO dry_run")
            conn.execute("RELEASE dry_run")
    done = apply(conn, person, known, answer)
    return done, _record(folder, f"{datetime.fromtimestamp(now):%Y-%m-%d-%H%M%S}-{person.id}", done)


def _folder(cfg: Config) -> Path:
    return cfg.data_dir / "reviews"


def _record(folder: Path, name: str, done: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    path.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def last_summary(cfg: Config) -> str:
    """What the last review changed, in plain words; empty when it changed nothing."""
    last = _folder(cfg) / "last.txt"
    return last.read_text(encoding="utf-8").strip() if last.exists() else ""


class Reviewer:
    def __init__(self, cfg: Config) -> None:
        self._runner = ClaudeRunner(cfg.claude_bin, cfg.claude_model, 1800.0, cfg.claude_inactivity_timeout)
        self._profile = Profile(observer.ARGS, cfg.data_dir / "observer")
        self.folder = _folder(cfg)

    def run(self, conn: sqlite3.Connection, now: float) -> list[str]:
        """Review every observed person since the last review, then let weak links go; return the summaries."""
        since = float(store.get_state(conn, STATE) or now - _FIRST_WINDOW)
        store.set_state(conn, STATE, str(now))
        self.folder.mkdir(parents=True, exist_ok=True)
        summaries = []
        for person, _ in access.people(conn):
            if not observer.enabled(conn, person):
                continue
            try:
                done = self.review(conn, person, since, now)
            except Exception:
                log.exception("review of person %d failed", person.id)
                continue
            if done and done["summary"]:
                summaries.append(done["summary"])
        pruned = Brain(conn).consolidate()["pruned_connections"]
        (self.folder / "last.txt").write_text("\n".join(summaries), encoding="utf-8")
        log.info("review done: %d summary(ies), %d weak link(s) pruned", len(summaries), pruned)
        return summaries

    def review(self, conn: sqlite3.Connection, person: Person, since: float, now: float) -> dict | None:
        """Review one person's memories against their conversations since ``since``."""
        known, rows = memories(conn, person.id), conversations(conn, person.id, since)
        if not known and not rows:
            return None
        self._profile.cwd.mkdir(parents=True, exist_ok=True)
        result = self._runner.run(build_prompt(person, known, rows), self._profile)
        if not result.ok:
            log.warning("review run for person %d failed (rc=%s)", person.id, result.returncode)
            return None
        done = apply(conn, person, known, observer.json_object(result.text))
        _record(self.folder, f"{datetime.fromtimestamp(now):%Y-%m-%d}-{person.id}", done)
        log.info("reviewed person %d: %d learned, %d merged, %d revised, %d forgotten", person.id,
                 len(done["learned"]), len(done["merged"]), len(done["revised"]), len(done["forgot"]))
        return done


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m zoe.review",
                                     description="Review the observed people's memories now.")
    commands = parser.add_subparsers(dest="command")
    asked = commands.add_parser(
        "apply", help="apply changes asked for, written like a review's answer",
        description="Apply changes to a person's memories, written like the nightly review's answer: "
        '{"merge": [{"keep": 12, "drop": [40, 55], "content": "..."}], "revise": [{"id": 7, "content": "..."}], '
        '"forget": [{"id": 9, "why": "..."}], "learn": [{"kind": "fact", "content": "..."}], "summary": "..."}. '
        "Only the person's facts, preferences, habits, goals and feedback change; any other number is ignored.",
    )
    asked.add_argument("file", type=Path, help="the JSON file with the changes")
    asked.add_argument("--person", help="whose memories, by name or number; the owner's when left out")
    asked.add_argument("--dry-run", action="store_true", help="show what would change and change nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    conn = connect(cfg.db_path)
    if args.command != "apply":
        for summary in Reviewer(cfg).run(conn, time.time()):
            print(summary)
        return
    if args.person:
        person = access.find_person(conn, args.person)
    else:
        person = access.resolve(conn, cfg.owner_chat).person if cfg.owner_chat else None
    if person is None:
        raise SystemExit(f"no such person: {args.person}" if args.person else "no owner configured: use --person")
    answer = observer.json_object(args.file.read_text(encoding="utf-8"))
    done, record = change(conn, person, answer, _folder(cfg), time.time(), args.dry_run)
    print(json.dumps(done, ensure_ascii=False, indent=1))
    print(f"recorded in {record}" if record else "dry run: nothing changed")


if __name__ == "__main__":
    main()
