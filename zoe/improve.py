"""Self-improvement: every night Zoe studies how she works and proposes what to make better.

Once a night (``ZOE_IMPROVE_AT``, 03:30 by default) a Claude run with the owner's powers
studies the last days: where the owner had to correct or guide her, what they asked by
hand again and again, what broke. It changes nothing that works: it answers with at most
two proposals, each saying what changes for the owner, how to make it and how it was
checked, and one sentence on what it learned. The owner's standing instructions may say
what to study and which notes it keeps.

Owners find the proposals in the menu: one tap hands a proposal to a parallel mind that
makes it and reports in the chat, another sets it aside, and the study never proposes a
set-aside one again unless something changed.

``python -m zoe.improve`` runs a study now.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import access, approve, i18n, minds, observer, recall_eval, review, store
from .access import Person
from .brain import Brain
from .claude_runner import ClaudeRunner
from .config import Config
from .db import connect

log = logging.getLogger("zoe.improve")

STATE = "improve_last"
SUMMARY = "improve_summary"
# The most proposals one study makes, and the most earlier ones it is shown.
MOST = 2
_SHOWN = 30
_TEXT = 3000
_STUDY_TIMEOUT = 3600.0


@dataclass(frozen=True)
class Proposal:
    id: int
    title: str
    benefit: str
    plan: str
    checked: str
    careful: bool
    state: str
    mind_id: int | None
    created_at: float
    decided_at: float | None


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(row["id"], row["title"], row["benefit"], row["plan"], row["checked"], bool(row["careful"]),
                    row["state"], row["mind_id"], row["created_at"], row["decided_at"])


def get(conn: sqlite3.Connection, proposal_id: int) -> Proposal | None:
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    return _proposal(row) if row else None


def waiting(conn: sqlite3.Connection) -> list[Proposal]:
    """The proposals waiting for the owner, the oldest first."""
    return [_proposal(r) for r in conn.execute("SELECT * FROM proposals WHERE state = 'waiting' ORDER BY id")]


def decided(conn: sqlite3.Connection, limit: int) -> list[Proposal]:
    """The proposals the owner approved or set aside, the latest first."""
    rows = conn.execute(
        "SELECT * FROM proposals WHERE state != 'waiting' ORDER BY decided_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_proposal(r) for r in rows]


def _clean(value: object) -> str:
    return str(value or "").strip()[:_TEXT]


def record(conn: sqlite3.Connection, answer: dict, now: float) -> list[int]:
    """Keep the proposals of a study's answer, at most MOST, each with a title, a benefit and a
    plan, and its summary; return the new proposals' ids."""
    kept = []
    for item in (answer.get("proposals") or [])[:MOST]:
        if not isinstance(item, dict):
            continue
        title, benefit, plan = _clean(item.get("title"))[:120], _clean(item.get("benefit")), _clean(item.get("plan"))
        if not (title and benefit and plan):
            continue
        cur = conn.execute(
            "INSERT INTO proposals (title, benefit, plan, checked, careful, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, benefit, plan, _clean(item.get("checked")), 1 if item.get("careful") else 0, now),
        )
        kept.append(int(cur.lastrowid))
    store.set_state(conn, SUMMARY, _clean(answer.get("summary"))[:500])
    return kept


def _brief(proposal: Proposal, person: Person) -> str:
    return "\n".join(part for part in (
        f"Make this improvement, which {person.name} approved.",
        f"What changes for {person.name}: {proposal.benefit}",
        f"Plan:\n{proposal.plan}",
        f"How it was checked, and how to check it once made:\n{proposal.checked}" if proposal.checked else "",
        "A change to how you work goes through your self-improvement skill. Verify it as the plan says; if it "
        "does not hold, change nothing and say why.",
    ) if part)


def make(conn: sqlite3.Connection, person: Person, chat_id: int, proposal_id: int, home: Path) -> int | None:
    """Hand a waiting proposal to a parallel mind that makes it; return the mind's id, or None
    when the proposal is not waiting."""
    proposal = get(conn, proposal_id)
    if proposal is None or proposal.state != "waiting":
        return None
    mind_id = minds.start(conn, Brain(conn, person.id), person, chat_id, proposal.title, _brief(proposal, person),
                          "", [], home)
    conn.execute("UPDATE proposals SET state = 'approved', mind_id = ?, decided_at = ? WHERE id = ?",
                 (mind_id, time.time(), proposal_id))
    return mind_id


def drop(conn: sqlite3.Connection, proposal_id: int) -> bool:
    return conn.execute(
        "UPDATE proposals SET state = 'rejected', decided_at = ? WHERE id = ? AND state = 'waiting'",
        (time.time(), proposal_id),
    ).rowcount > 0


def build_prompt(conn: sqlite3.Connection, person: Person, db_path: Path, recall: str = "") -> str:
    """The study's prompt; ``recall`` is the line on how well recall works, when it was measured."""
    language = i18n.language_name(person.language)
    earlier = conn.execute("SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (_SHOWN,)).fetchall()
    shown = {"waiting": [], "approved": [], "rejected": []}
    for proposal in map(_proposal, earlier):
        shown[proposal.state].append(f"- {proposal.title}: {proposal.benefit}")
    lines = [
        f"You are Zoe, the personal assistant of {person.name}. Tonight you study how you work, to find what "
        f"would make {person.name}'s life easier, so they do not have to think up and explain every function "
        "themselves. Nobody reads your steps: a program reads your answer, and the proposals in it wait in "
        f"{person.name}'s menu, where they approve or set them aside.",
        "\nTonight you change nothing that works: no code, scripts, schedules, services, settings or messages. "
        "You read, query and try things out in a temporary folder that you delete. Your standing instructions "
        "may say what else to study and which notes you keep up to date.",
        f"\nStudy the last days: the conversations in the messages table of the SQLite database {db_path} "
        "(direction 'out' is you), above all where you had to be corrected or guided, what was asked by hand "
        "again and again, and what you could have done on your own; errors and things that broke "
        "(journalctl -u zoe --since -1d, the logs of the tools you run). Check every idea on the real data.",
    ]
    if recall:
        lines.append(recall)
    for state, heading in (("waiting", "Proposals still waiting for an answer (do not repeat them):"),
                           ("approved", "Proposals approved and made:"),
                           ("rejected", f"Proposals {person.name} set aside: never again, unless something "
                                        "changed, and then say what:")):
        if shown[state]:
            lines += [f"\n{heading}", *shown[state]]
    lines.append(
        "\nAnswer with one JSON object and nothing else:\n"
        '{"proposals": [{"title": "...", "benefit": "...", "plan": "...", "checked": "...", "careful": false}], '
        '"summary": "..."}\n'
        f"- proposals: at most {MOST}, the most useful for {person.name} against the work they take; none is a "
        "fine answer. Prefer small, solid, verifiable changes to large rewrites. A change to how you work goes "
        "through your self-improvement skill; a tool for their work goes where their instructions say.\n"
        f"- title: a few words. benefit: one or two plain sentences for {person.name}, what changes for them and "
        "why, with the example from the chat that suggested it. plan: the files to touch and what to write, "
        "precise enough to make it without thinking it over again. checked: how you verified it tonight, and "
        "how to check it once made. careful: true when it writes to outside services, sends anything to third "
        "parties, touches system services, credentials or security rules, or spends money.\n"
        f"- summary: one sentence for {person.name} on what you learned tonight, or an empty string.\n"
        f"Write title, benefit and summary in {language}; plan and checked may be in English."
    )
    return "\n".join(lines)


def study(
    conn: sqlite3.Connection, cfg: Config, runner: ClaudeRunner, now: float, cancel: threading.Event | None = None,
) -> list[int]:
    """Run one study for the first owner and keep what it proposes; return the new proposals' ids.
    A study cut short by ``cancel`` counts as not run, so it runs again after a restart."""
    owner = next((person for person, _ in access.people(conn) if person.role == "owner"), None)
    if owner is None:
        return []
    # Nobody is asked at night: whatever needs the owner's yes is refused.
    profile = approve.asking(access.turn_profile(owner, cfg), cfg.data_dir, None)
    recall_eval.renew(conn, cfg, owner, now)
    recall = recall_eval.summary(conn, cfg, owner)
    result = runner.run(build_prompt(conn, owner, cfg.db_path, recall), profile, cancel=cancel)
    if result.cancelled:
        return []
    store.set_state(conn, STATE, str(now))
    if not result.ok:
        log.warning("study failed (rc=%s)", result.returncode)
        return []
    try:
        answer = observer.json_object(result.text)
    except ValueError:
        log.warning("study answered no JSON")
        return []
    kept = record(conn, answer, now)
    log.info("study done: %d proposal(s)", len(kept))
    return kept


class Improver(threading.Thread):
    """Runs the study once a night."""

    def __init__(self, cfg: Config, stop: threading.Event, interval: float = 60.0) -> None:
        super().__init__(name="improver", daemon=True)
        self._cfg = cfg
        self._halt = stop
        self._interval = interval
        self._runner = ClaudeRunner(cfg.claude_bin, cfg.claude_model, _STUDY_TIMEOUT, cfg.mind_inactivity_timeout)

    def run(self) -> None:
        conn = connect(self._cfg.db_path)
        log.info("improver started")
        while not self._halt.wait(self._interval):
            try:
                if review.due(conn, self._cfg.improve_at, time.time(), STATE):
                    study(conn, self._cfg, self._runner, time.time(), self._halt)
            except Exception:
                log.exception("study failed")
        conn.close()
        log.info("improver stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    conn = connect(cfg.db_path)
    runner = ClaudeRunner(cfg.claude_bin, cfg.claude_model, _STUDY_TIMEOUT, cfg.mind_inactivity_timeout)
    for proposal_id in study(conn, cfg, runner, time.time()):
        print(get(conn, proposal_id).title)


if __name__ == "__main__":
    main()
