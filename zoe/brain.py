"""The neuron graph: Zoe's memory and the engine of her self-learning.

Every piece of knowledge is a neuron. Neurons link to each other with weighted
connections. Recall works by spreading activation: energy starts at the neurons
that share the rarest words with what is being said or come closest to what it
means, and flows across connections,
so a thought pulls in whatever it is wired to. Connections between neurons that
fire together grow stronger, which is how structure emerges from use rather than
from hand-written rules. A neuron's links share what it passes on, in proportion
to their weight, and a neuron wired to everything takes little from each: strong,
specific paths carry far, links to everything carry almost nothing.

A brain is opened on behalf of one person. It reads that person's neurons and
the shared ones, writes new neurons as that person's, and never lets activation
flow into another person's memory. A brain opened with no person works on the
shared space only.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations

_SCOPE = "(owner_id IS NULL OR owner_id = ?)"
# How many neurons each way of searching proposes, and the constant that fuses their rankings.
_CANDIDATES = 40
_FUSION = 60


@dataclass(frozen=True)
class Recalled:
    id: int
    kind: str
    content: str
    score: float


class Brain:
    # The process's meaning model (zoe.meaning), set once at start; recall goes by words alone without it.
    meaning = None

    def __init__(self, conn: sqlite3.Connection, owner: int | None = None) -> None:
        self._conn = conn
        self._owner = owner

    def add(self, kind: str, content: str, summary: str | None = None, salience: float = 1.0) -> int:
        cur = self._conn.execute(
            "INSERT INTO neurons (owner_id, kind, content, summary, salience, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (self._owner, kind, content, summary, salience, time.time()),
        )
        return int(cur.lastrowid)

    def find(self, kind: str, content: str) -> int | None:
        """Return the neuron with exactly this content, preferring the person's own over a shared one."""
        row = self._conn.execute(
            f"SELECT id FROM neurons WHERE kind = ? AND content = ? AND {_SCOPE} "
            "ORDER BY owner_id IS NULL LIMIT 1",
            (kind, content, self._owner),
        ).fetchone()
        return row["id"] if row else None

    def ensure(self, kind: str, content: str, salience: float = 1.0) -> int:
        """Return the neuron holding exactly this content, creating it if needed."""
        return self.find(kind, content) or self.add(kind, content, salience=salience)

    def boost(self, neuron_id: int, amount: float = 0.1, ceiling: float = 10.0) -> None:
        """Raise a neuron's long-term importance."""
        self._conn.execute(
            f"UPDATE neurons SET salience = min(?, salience + ?) WHERE id = ? AND {_SCOPE}",
            (ceiling, amount, neuron_id, self._owner),
        )

    def top(self, kinds: str | tuple[str, ...], limit: int = 50) -> list[Recalled]:
        """Return the most salient neurons of one kind or of several."""
        kinds = (kinds,) if isinstance(kinds, str) else kinds
        rows = self._conn.execute(
            f"SELECT id, kind, content, salience FROM neurons WHERE kind IN ({','.join('?' * len(kinds))}) "
            f"AND {_SCOPE} ORDER BY salience DESC, fire_count DESC, id LIMIT ?",
            (*kinds, self._owner, limit),
        ).fetchall()
        return [Recalled(r["id"], r["kind"], r["content"], r["salience"]) for r in rows]

    def get(self, neuron_id: int) -> Recalled | None:
        row = self._conn.execute(
            f"SELECT id, kind, content, salience FROM neurons WHERE id = ? AND {_SCOPE}", (neuron_id, self._owner)
        ).fetchone()
        return Recalled(row["id"], row["kind"], row["content"], row["salience"]) if row else None

    def revise(self, neuron_id: int, content: str) -> bool:
        """Correct what one of the person's own neurons holds, keeping its links."""
        cur = self._conn.execute(
            "UPDATE neurons SET content = ? WHERE id = ? AND owner_id IS ?", (content, neuron_id, self._owner)
        )
        return cur.rowcount > 0

    def forget(self, neuron_id: int) -> bool:
        """Delete one of the person's own neurons with its links."""
        cur = self._conn.execute("DELETE FROM neurons WHERE id = ? AND owner_id IS ?", (neuron_id, self._owner))
        return cur.rowcount > 0

    def merge(self, keep: int, gone: int) -> bool:
        """Fold one of the person's neurons into another that says the same: its links move
        over, adding up where both had one, its use adds to the other's, the kept one counts as
        known since the older of the two, and the gone one is deleted, leaving a note of where it went."""
        if keep == gone:
            return False
        owned = self._conn.execute(
            "SELECT COUNT(*) FROM neurons WHERE id IN (?, ?) AND owner_id IS ?", (keep, gone, self._owner)
        ).fetchone()[0]
        if owned != 2:
            return False
        now = time.time()
        for moved, other in (("source_id", "target_id"), ("target_id", "source_id")):
            self._conn.execute(
                f"INSERT INTO connections ({moved}, {other}, weight, created_at, updated_at) "
                f"SELECT ?, {other}, weight, created_at, ? FROM connections WHERE {moved} = ? AND {other} NOT IN (?, ?) "
                "ON CONFLICT(source_id, target_id) DO UPDATE SET weight = min(1.0, weight + excluded.weight), "
                "updated_at = excluded.updated_at",
                (keep, now, gone, keep, gone),
            )
        self._conn.execute(
            "UPDATE neurons SET salience = max(salience, (SELECT salience FROM neurons WHERE id = ?)), "
            "fire_count = fire_count + (SELECT fire_count FROM neurons WHERE id = ?), "
            "created_at = min(created_at, (SELECT created_at FROM neurons WHERE id = ?)) WHERE id = ?",
            (gone, gone, gone, keep),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO merged (gone_id, kept_id, merged_at) VALUES (?, ?, ?)", (gone, keep, now)
        )
        return self.forget(gone)

    def kept(self, neuron_id: int) -> int:
        """The neuron that holds what this one held now: itself, or the one it was merged into."""
        seen = set()
        while neuron_id not in seen:
            seen.add(neuron_id)
            row = self._conn.execute("SELECT kept_id FROM merged WHERE gone_id = ?", (neuron_id,)).fetchone()
            if row is None:
                break
            neuron_id = row[0]
        return neuron_id

    def anchored(self, telegram_ids: list[int]) -> list[int]:
        """Return the episodes that hold any of these Telegram messages."""
        ids: list[int] = []
        for telegram_id in telegram_ids:
            ids += [r["id"] for r in self._conn.execute(
                f"SELECT id FROM neurons WHERE kind = 'episode' AND content LIKE ? AND {_SCOPE}",
                (f"%[#{telegram_id}] %", self._owner),
            ).fetchall()]
        return ids

    def pairs(self, source_kind: str, target_kind: str, min_weight: float = 0.0) -> list[tuple[str, str, float]]:
        """Return (source, target, weight) for every visible link from one kind of neuron to another."""
        rows = self._conn.execute(
            "SELECT s.content AS source, t.content AS target, c.weight AS weight "
            "FROM connections c "
            "JOIN neurons s ON s.id = c.source_id "
            "JOIN neurons t ON t.id = c.target_id "
            "WHERE s.kind = ? AND t.kind = ? AND c.weight >= ? "
            "AND (s.owner_id IS NULL OR s.owner_id = ?) AND (t.owner_id IS NULL OR t.owner_id = ?) "
            "ORDER BY c.weight DESC",
            (source_kind, target_kind, min_weight, self._owner, self._owner),
        ).fetchall()
        return [(r["source"], r["target"], r["weight"]) for r in rows]

    def connect(self, a: int, b: int, delta: float = 0.15, max_weight: float = 1.0) -> None:
        """Strengthen the link between two neurons in both directions."""
        if a == b:
            return
        now = time.time()
        for source, target in ((a, b), (b, a)):
            self._conn.execute(
                "INSERT INTO connections (source_id, target_id, weight, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(source_id, target_id) DO UPDATE SET "
                "weight = min(?, weight + ?), updated_at = ?",
                (source, target, delta, now, now, max_weight, delta, now),
            )

    def fire(self, ids: list[int], energy: float = 1.0) -> None:
        """Activate neurons and wire together every pair that fired at once, in one transaction."""
        if not ids:
            return
        now = time.time()
        own = not self._conn.in_transaction
        if own:
            self._conn.execute("BEGIN")
        try:
            self._conn.executemany(
                "UPDATE neurons SET activation = activation + ?, last_fired_at = ?, fire_count = fire_count + 1 "
                "WHERE id = ?",
                [(energy, now, nid) for nid in ids],
            )
            for a, b in combinations(sorted(set(ids)), 2):
                self.connect(a, b)
        except BaseException:
            if own:
                self._conn.execute("ROLLBACK")
            raise
        if own:
            self._conn.execute("COMMIT")

    def search(self, query: str, limit: int = 10) -> dict[int, float]:
        """Find seed neurons by the words they share with the query and by what it means, best first.

        Each comes with how well it matches, 1.0 for the best: a neuron high in both rankings
        beats one found only one way. Bracketed notes such as a saved photo's path or a
        message number are the program's, not the person's, and are left out.
        """
        said = re.sub(r"\[[^\]]*\]", " ", query)
        score: dict[int, float] = defaultdict(float)
        for ranked in (self._by_words(said, _CANDIDATES), self._by_meaning(said, _CANDIDATES)):
            for rank, nid in enumerate(ranked):
                score[nid] += 1.0 / (_FUSION + rank)
        best = sorted(score, key=score.get, reverse=True)[:limit]
        return {nid: score[nid] / score[best[0]] for nid in best}

    def _by_words(self, said: str, limit: int) -> list[int]:
        """The neurons sharing the query's words, the rarest words weighing most; a longer word
        matches its other endings too."""
        words = sorted(set(re.findall(r"\w{3,}", said.lower())), key=len, reverse=True)[:24]
        if not words:
            return []
        # ponytail: dropping the last letter is a crude stem, good for Italian endings;
        # a real stemmer if recall misses forms
        match = " OR ".join(f'"{w[:-1]}"*' if len(w) > 4 else f'"{w}"' for w in words)
        rows = self._conn.execute(
            "SELECT n.id FROM neurons_fts f JOIN neurons n ON n.id = f.rowid "
            "WHERE neurons_fts MATCH ? AND (n.owner_id IS NULL OR n.owner_id = ?) "
            "ORDER BY bm25(neurons_fts) LIMIT ?",
            (match, self._owner, limit),
        ).fetchall()
        return [r["id"] for r in rows]

    def _by_meaning(self, said: str, limit: int) -> list[int]:
        """The neurons closest in meaning to the query; none without a meaning model."""
        if self.meaning is None or not said.strip():
            return []
        self.fill()
        rows = self._conn.execute(
            "SELECT v.neuron_id, v.vector FROM neuron_vectors v JOIN neurons n ON n.id = v.neuron_id "
            "WHERE v.model = ? AND (n.owner_id IS NULL OR n.owner_id = ?)",
            (self.meaning.name, self._owner),
        ).fetchall()
        # ponytail: every place is read and compared on each search;
        # an index such as sqlite-vec past ~50k neurons
        return [rows[i]["neuron_id"] for i in self.meaning.rank(said, [r["vector"] for r in rows], limit)]

    def fill(self, limit: int | None = 16) -> int:
        """Place by meaning the visible neurons the model has not placed yet, newest first; return how many."""
        if self.meaning is None:
            return 0
        rows = self._conn.execute(
            "SELECT n.id, n.content FROM neurons n LEFT JOIN neuron_vectors v ON v.neuron_id = n.id AND v.model = ? "
            "WHERE v.neuron_id IS NULL AND (n.owner_id IS NULL OR n.owner_id = ?) ORDER BY n.id DESC LIMIT ?",
            (self.meaning.name, self._owner, -1 if limit is None else limit),
        ).fetchall()
        if rows:
            places = self.meaning.documents([r["content"] for r in rows])
            self._conn.executemany(
                "INSERT OR REPLACE INTO neuron_vectors (neuron_id, model, vector) VALUES (?, ?, ?)",
                [(r["id"], self.meaning.name, place) for r, place in zip(rows, places)],
            )
        return len(rows)

    def recall(
        self, seeds: Iterable[int] | Mapping[int, float], max_hops: int = 2, decay: float = 0.5, limit: int = 20
    ) -> list[Recalled]:
        """Spread activation from the seed neurons and return the top neurons reached.

        Seeds are given as ids, each starting with full energy, or with the energy each
        starts with, as ``search`` returns them.
        """
        start = seeds if isinstance(seeds, Mapping) else Counter(seeds)
        energy: dict[int, float] = defaultdict(float, start)
        frontier: dict[int, float] = dict(start)

        for _ in range(max_hops):
            nxt: dict[int, float] = defaultdict(float)
            for nid, e in frontier.items():
                links = self._neighbors(nid)
                own = sum(weight for _, weight, _ in links)
                for target, weight, theirs in links:
                    spread = e * decay * weight / math.sqrt(own * max(theirs, weight))
                    if spread > 0.01:
                        nxt[target] += spread
            if not nxt:
                break
            for nid, e in nxt.items():
                energy[nid] += e
            frontier = nxt

        if not energy:
            return []
        rows = {
            r["id"]: r
            for r in self._conn.execute(
                f"SELECT id, kind, content, salience FROM neurons WHERE id IN "
                f"({','.join('?' * len(energy))}) AND {_SCOPE}",
                (*energy, self._owner),
            ).fetchall()
        }
        scored = [
            Recalled(nid, rows[nid]["kind"], rows[nid]["content"], e + 0.1 * rows[nid]["salience"])
            for nid, e in energy.items()
            if nid in rows
        ]
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:limit]

    def recall_text(self, query: str, limit: int = 8) -> list[Recalled]:
        return self.recall(self.search(query), limit=limit)

    def learn(self, kind: str, content: str, related: list[int] | None = None, salience: float = 1.0) -> int:
        """Store a new neuron and wire it to the neurons it came up with.

        The related neurons are not wired to each other: being recalled together for one
        message says nothing about how they belong together, and wiring them would make
        whatever is recalled once be recalled every time after.
        """
        nid = self.add(kind, content, salience=salience)
        for other in related or []:
            self.connect(nid, other)
        self.fire([nid])
        return nid

    def reinforce(self, used: list[int], unused: list[int], fade: float = 0.7) -> None:
        """Learn from what one reply used of what recall brought: what was used together wires
        together, and the links that pulled in what went unused alongside it weaken."""
        if not used:
            return
        self.fire(used)
        if unused:
            u, n = ",".join("?" * len(used)), ",".join("?" * len(unused))
            self._conn.execute(
                f"UPDATE connections SET weight = weight * ? WHERE (source_id IN ({u}) AND target_id IN ({n})) "
                f"OR (source_id IN ({n}) AND target_id IN ({u}))",
                (fade, *used, *unused, *unused, *used),
            )

    def decay(self, rate: float = 0.1) -> None:
        """Let transient activation fade across the whole graph."""
        self._conn.execute("UPDATE neurons SET activation = activation * (1 - ?)", (rate,))

    def consolidate(self, min_weight: float = 0.02) -> dict[str, int]:
        """Prune links that never got reinforced and fade activation, across the whole graph."""
        cur = self._conn.execute("DELETE FROM connections WHERE weight < ?", (min_weight,))
        self.decay(0.5)
        return {"pruned_connections": cur.rowcount}

    def _neighbors(self, neuron_id: int) -> list[tuple[int, float, float]]:
        """Return (target, weight, the target's total link weight) for every visible link."""
        rows = self._conn.execute(
            "SELECT c.target_id, c.weight, "
            "(SELECT COALESCE(SUM(o.weight), 0) FROM connections o WHERE o.source_id = c.target_id) AS strength "
            "FROM connections c JOIN neurons n ON n.id = c.target_id "
            "WHERE c.source_id = ? AND (n.owner_id IS NULL OR n.owner_id = ?)",
            (neuron_id, self._owner),
        ).fetchall()
        return [(r["target_id"], r["weight"], r["strength"]) for r in rows]
