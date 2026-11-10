"""Checks for the neuron graph: recall by spreading activation, Hebbian wiring, and nothing left
pointing to a deleted neuron."""

import sqlite3
import sys
import tempfile
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe.access import Person, create_person
from zoe.brain import Brain
from zoe.db import apply_schema, connect


def _brain() -> Brain:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return Brain(conn)


def test_recall_follows_connections():
    brain = _brain()
    coffee = brain.add("fact", "the user drinks coffee in the morning")
    morning = brain.add("fact", "the user starts work early")
    unrelated = brain.add("fact", "the capital of France is Paris")
    brain.connect(coffee, morning, delta=0.8)

    reached = {r.id for r in brain.recall([coffee])}
    assert morning in reached
    assert unrelated not in reached


def test_fire_wires_cofiring_neurons():
    brain = _brain()
    a = brain.add("fact", "alpha")
    b = brain.add("fact", "beta")
    assert not brain._neighbors(a)
    brain.fire([a, b])
    assert b in {t for t, *_ in brain._neighbors(a)}


def test_repeated_firing_strengthens():
    brain = _brain()
    a = brain.add("fact", "alpha")
    b = brain.add("fact", "beta")
    brain.fire([a, b])
    first = {t: w for t, w, _ in brain._neighbors(a)}[b]
    brain.fire([a, b])
    assert {t: w for t, w, _ in brain._neighbors(a)}[b] > first


def test_search_then_recall():
    brain = _brain()
    gym = brain.add("fact", "the user wants to walk fifteen minutes every day")
    sleep = brain.add("fact", "the user sleeps badly and goes to bed late")
    brain.connect(gym, sleep, delta=0.6)
    hits = brain.recall_text("tell me about walking every day")
    assert gym in {r.id for r in hits}


def test_search_ranks_rare_words_above_common_ones():
    brain = _brain()
    for i in range(20):
        brain.add("fact", f"the user said something about the weather {i}", salience=5.0)
    boat = brain.add("fact", "the user keeps a sailing boat in the harbour")
    assert list(brain.search("the boats in the harbour"))[0] == boat
    assert not brain.search("Telescope")


def test_search_reads_the_persons_words_not_the_programs_notes():
    brain = _brain()
    for i in range(5):
        brain.add("episode", f"[#{i}] Alex: [photo, saved at /inbox/{i}.jpg (file #{i})] look")
    lemon = brain.add("fact", "Alex grows a lemon tree")
    found = brain.search("[photo, saved at /inbox/9.jpg (file #9)] how is my lemon tree")
    assert list(found) == [lemon] and found[lemon] == 1.0


class FakeMeaning:
    """Places a text by the topics it names, so texts on one topic sit together whatever their words."""

    name = "fake"
    topics = (("dollar", "usd"), ("euro", "eur"), ("lemon",))

    def _place(self, text: str) -> array:
        return array("f", [float(any(word in text.lower() for word in topic)) for topic in self.topics])

    def documents(self, texts: list[str]) -> list[bytes]:
        return [self._place(text).tobytes() for text in texts]

    def rank(self, query: str, vectors: list[bytes], limit: int) -> list[int]:
        asked = self._place(query)
        close = [sum(a * b for a, b in zip(asked, array("f", v))) for v in vectors]
        return sorted(range(len(vectors)), key=lambda i: -close[i])[:limit]


def test_search_finds_by_meaning_what_shares_no_word_and_places_a_changed_neuron_again():
    brain = _brain()
    Brain.meaning = FakeMeaning()
    try:
        rates = brain.add("fact", "For USD/EUR rates Zoe runs the cambio command")
        brain.add("fact", "Alex grows a lemon tree")
        assert list(brain.search("how much is a dollar in euro?"))[0] == rates
        placed = brain._conn.execute("SELECT COUNT(*) FROM neuron_vectors").fetchone()[0]
        brain.revise(rates, "Alex keeps the lemons in the fridge")
        assert brain._conn.execute("SELECT COUNT(*) FROM neuron_vectors").fetchone()[0] == placed - 1
        assert brain.fill() == 1
        stored = brain._conn.execute("SELECT vector FROM neuron_vectors WHERE neuron_id = ?", (rates,)).fetchone()[0]
        assert stored == Brain.meaning.documents(["Alex keeps the lemons in the fridge"])[0]
    finally:
        Brain.meaning = None


def test_search_follows_revised_and_forgotten_neurons():
    brain = _brain()
    conn = brain._conn
    mia = Brain(conn, owner=create_person(conn, "Mia", "member", "en").id)
    fact = mia.add("fact", "Mia plays the violin")
    mia.revise(fact, "Mia plays the cello")
    assert list(mia.search("cello")) == [fact] and not mia.search("violin")
    mia.forget(fact)
    assert not mia.search("cello")


def test_learning_does_not_wire_the_related_neurons_together():
    brain = _brain()
    a, b = brain.add("fact", "alpha"), brain.add("fact", "beta")
    episode = brain.learn("episode", "a talk about both", related=[a, b])
    assert {t for t, *_ in brain._neighbors(a)} == {episode}


def test_what_a_reply_used_wires_together_and_what_came_up_for_nothing_drifts_away():
    brain = _brain()
    a, b, noise = brain.add("fact", "alpha"), brain.add("fact", "beta"), brain.add("fact", "noise")
    brain.connect(a, noise, delta=0.5)
    brain.reinforce([a, b], [noise])
    weights = {t: w for t, w, _ in brain._neighbors(a)}
    assert b in weights and weights[noise] < 0.5
    brain.reinforce([], [a, b])
    assert {t: w for t, w, _ in brain._neighbors(a)} == weights


def test_a_neuron_wired_to_everything_is_not_recalled_for_everything():
    brain = _brain()
    hub = brain.add("feedback", "answer once")
    seeds = [brain.add("episode", f"talk {i}") for i in range(30)]
    for s in seeds:
        brain.connect(s, hub, delta=0.6)
    boat = brain.add("fact", "the user keeps a boat")
    brain.connect(seeds[0], boat, delta=0.6)
    ranked = [r.id for r in brain.recall(seeds[:3])]
    assert boat in ranked and (hub not in ranked or ranked.index(boat) < ranked.index(hub))


def test_consolidate_prunes_weak_links():
    brain = _brain()
    a = brain.add("fact", "alpha")
    b = brain.add("fact", "beta")
    brain.connect(a, b, delta=0.01)
    assert brain.consolidate(min_weight=0.02)["pruned_connections"] >= 1
    assert not brain._neighbors(a)


def test_memories_stay_private_between_people():
    brain = _brain()
    conn = brain._conn
    alice = create_person(conn, "Alice", "member", "en").id
    bob = create_person(conn, "Bob", "member", "en").id
    shared, mind_a, mind_b = Brain(conn), Brain(conn, owner=alice), Brain(conn, owner=bob)

    office = shared.add("fact", "the office opens at nine")
    secret = mind_a.add("fact", "alice is planning a surprise party")
    mind_a.connect(secret, office, delta=0.9)

    assert secret in {r.id for r in mind_a.recall([office])}
    assert secret not in {r.id for r in mind_b.recall([office])}
    assert not mind_b.search("surprise party")
    assert office in {r.id for r in mind_b.recall_text("when does the office open")}


def test_person_writes_into_own_memory():
    brain = _brain()
    conn = brain._conn
    alice = create_person(conn, "Alice", "member", "en").id
    shared_term = Brain(conn).ensure("term", "Kubernetes")
    mind = Brain(conn, owner=alice)
    assert mind.ensure("term", "Kubernetes") == shared_term
    own = mind.ensure("term", "Gianduia")
    assert Brain(conn).find("term", "Gianduia") is None
    assert mind.find("term", "Gianduia") == own


def _on_disk(folder: Path) -> tuple[sqlite3.Connection, Person]:
    conn = connect(folder / "zoe.db")
    apply_schema(conn)
    return conn, create_person(conn, "Alex", "owner", "it")


def _delete_by_hand(db: Path, neuron: int) -> None:
    """Delete a neuron as a bare sqlite3 connection does, with foreign keys off."""
    bare = sqlite3.connect(db)
    bare.execute("DELETE FROM neurons WHERE id = ?", (neuron,))
    bare.commit()
    bare.close()


def test_a_neuron_deleted_with_foreign_keys_off_leaves_nothing_pointing_to_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, alex = _on_disk(Path(tmp))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        pointing = {(table, key["from"], key["on_delete"]) for table in tables
                    for key in conn.execute(f"PRAGMA foreign_key_list({table})") if key["table"] == "neurons"}
        assert pointing == {("connections", "source_id", "CASCADE"), ("connections", "target_id", "CASCADE"),
                            ("neuron_vectors", "neuron_id", "CASCADE"), ("plans", "neuron_id", "CASCADE"),
                            ("files", "neuron_id", "SET NULL"), ("minds", "neuron_id", "SET NULL")}
        brain = Brain(conn, alex.id)
        call, cup = brain.learn("plan", "Chiama Mia"), brain.ensure("term", "Tazza")
        brain.connect(call, cup)
        conn.execute("INSERT INTO neuron_vectors (neuron_id, model, vector) VALUES (?, 'm', x'00')", (call,))
        conn.execute("INSERT INTO plans (neuron_id, chat_id, due_at) VALUES (?, 1, 0)", (call,))
        conn.execute("INSERT INTO files (neuron_id, person_id, kind, path, name, created_at) "
                     "VALUES (?, ?, 'document', '/notes/mia.pdf', 'mia.pdf', 0)", (call, alex.id))
        conn.execute("INSERT INTO minds (neuron_id, person_id, chat_id, title, brief, workdir, created_at) "
                     "VALUES (?, ?, 1, 'Chiamata', 'Prepara la chiamata', '/work', 0)", (call, alex.id))
        _delete_by_hand(Path(tmp) / "zoe.db", call)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [tuple(r) for r in conn.execute("SELECT source_id, target_id FROM connections")] == []
        assert conn.execute("SELECT COUNT(*) FROM neuron_vectors").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
        assert conn.execute("SELECT neuron_id FROM files").fetchone()[0] is None
        assert conn.execute("SELECT neuron_id FROM minds").fetchone()[0] is None


def test_a_merge_works_after_a_neuron_it_was_linked_to_was_deleted_by_hand():
    with tempfile.TemporaryDirectory() as tmp:
        conn, alex = _on_disk(Path(tmp))
        brain = Brain(conn, alex.id)
        tea, green = brain.learn("preference", "Ama il tè"), brain.learn("preference", "Beve tè verde")
        cup, market = brain.ensure("term", "Tazza"), brain.learn("fact", "Compra il tè al mercato")
        brain.connect(green, cup)
        brain.connect(green, market)
        _delete_by_hand(Path(tmp) / "zoe.db", market)
        assert brain.merge(tea, green) and brain.kept(green) == tea
        assert cup in {r.id for r in brain.recall([tea])}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
