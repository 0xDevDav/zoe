"""Checks for a turn's messages: their Telegram numbers, quoting, replies, and messages written during a turn."""

import re
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, button_data, message, press, worker_setup
from zoe import access, formatting, prompt, store
from zoe import worker as worker_module
from zoe.brain import Brain, Recalled
from zoe.claude_runner import Result
from zoe.ingest import Poller
from zoe.telegram import TelegramError


def _say(conn, worker, owner, update_id: int, text: str, reply_to: int | None = None, quote: str = "") -> None:
    update = message(update_id, OWNER, text)
    if reply_to:
        update["message"]["reply_to_message"] = {"message_id": reply_to, "from": {"id": OWNER}, "text": "?"}
    if quote:
        update["message"]["quote"] = {"text": quote}
    store.record_inbound(conn, update, owner.id)
    worker._handle(conn, store.claim_next(conn))


def test_every_message_is_kept_with_its_number_and_shown_with_it():
    long_reply = "\n\n".join(f"Paragrafo {i}: " + "parola " * 80 for i in range(12))
    parts = len(formatting.split(formatting.to_telegram(long_reply)))
    assert parts > 1
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = worker_setup(Path(tmp), long_reply, "Sì.")
        _say(conn, worker, owner, 7, "Raccontami tutto")
        sent_ids = [r["telegram_id"] for r in conn.execute("SELECT telegram_id FROM messages WHERE direction = 'out'")]
        assert len(sent_ids) == parts and all(store.find_message(conn, OWNER, i)["mine"] for i in sent_ids)
        theirs = store.find_message(conn, OWNER, 7)
        assert not theirs["mine"] and theirs["text"] == "Raccontami tutto"
        _say(conn, worker, owner, 8, "Chiaro?")
        assert "[#7] Alex: Raccontami tutto" in runner.prompts[-1]
        assert f"[#{sent_ids[0]}] You: " in runner.prompts[-1]
        assert "[#8] Alex: Chiaro?" in runner.prompts[-1]
        episodes = [r.content for r in Brain(conn, owner.id).top("episode")]
        assert any(e.startswith("[#7] Alex: Raccontami tutto") and f"[#{sent_ids[0]}] Zoe:" in e for e in episodes)


def test_the_memories_a_reply_used_are_wired_closer_and_the_rest_drift_away():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp))
        brain = Brain(conn, owner.id)
        umbrella = brain.add("fact", "Alex dimentica sempre l'ombrello quando piove")
        rain = brain.add("preference", "Alex vuole sapere se piove prima di uscire")
        garden = brain.add("fact", "Alex annaffia il giardino quando piove poco")
        brain.connect(umbrella, garden, delta=0.3)
        runner.replies = [f"Prendi l'ombrello!\n[[{{k}} used: {umbrella}, {rain}, 999]]"]
        _say(conn, worker, owner, 5, "Oggi piove, esco?")
        assert f"- {umbrella}: Alex dimentica sempre l'ombrello quando piove" in runner.prompts[-1]
        assert api.to(OWNER)[-1] == "Prendi l'ombrello!"
        weight = {t: w for t, w, _ in brain._neighbors(umbrella)}
        assert rain in weight and weight[garden] < 0.3
        episode = brain.top("episode")[0].id
        assert {t for t, *_ in brain._neighbors(episode)} == {umbrella, rain}


def test_a_short_message_recalls_what_the_messages_before_it_were_about():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = worker_setup(Path(tmp), "Mi spiace!", "Annaffialo meno.")
        lemon = Brain(conn, owner.id).add("fact", "Alex ha un limone in giardino che soffre il freddo")
        _say(conn, worker, owner, 5, "Il limone ha le foglie gialle")
        _say(conn, worker, owner, 6, "Cosa faccio?")
        assert f"- {lemon}: Alex ha un limone" in runner.prompts[-1]


def test_a_turn_shows_lasting_memories_and_at_most_two_past_conversations():
    kinds = ["episode", "episode", "term", "episode", "fact", "plan", *["preference"] * 14]
    shown = prompt.select([Recalled(i, kind, f"neuron {i}", 20.0 - i) for i, kind in enumerate(kinds)])
    shown_kinds = [r.kind for r in shown]
    assert len(shown) == prompt.SHOWN + 2 and shown_kinds.count("episode") == 2
    assert not {"term", "plan"} & set(shown_kinds)
    assert [r.score for r in shown] == sorted((r.score for r in shown), reverse=True)


def test_zoe_quotes_only_messages_of_the_chat():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(
            Path(tmp), "Te l'avevo detto qui 👆\n[[{k} quote: 5]]", "Eccolo\n[[{k} quote: 999]]"
        )
        _say(conn, worker, owner, 5, "Il codice del cancello è 4412")
        _say(conn, worker, owner, 6, "E quello del box?")
        assert [r for r in api.replies_to if r is not None] == [5]


def test_the_answer_takes_the_place_of_the_progress_message_unless_it_quotes_one():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp), "Ciao!", "Eccolo\n[[{k} quote: 5]]")
        _say(conn, worker, owner, 5, "Ciao Zoe")
        assert api.to(OWNER) == ["Ciao!"] and api.deleted == []
        assert store.find_message(conn, OWNER, api.sent_ids[0])["mine"]
        _say(conn, worker, owner, 6, "Dov'era?")
        assert api.to(OWNER)[-1] == "Eccolo" and api.replies_to[-1] == 5
        assert api.deleted == [(OWNER, api.sent_ids[1])]


def test_zoe_rewrites_her_own_message_in_place_and_points_to_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(
            Path(tmp), "Spesa: pane, latte", "Spesa: pane, latte, uova\n[[{k} edit: 101]]", "Fatto\n[[{k} edit: 5]]"
        )
        _say(conn, worker, owner, 5, "Fammi la lista della spesa")
        assert api.sent_ids[0] == 101 and api.to(OWNER) == ["Spesa: pane, latte"]
        _say(conn, worker, owner, 6, "Aggiungi le uova")
        assert api.to(OWNER)[0] == "Spesa: pane, latte, uova" and api.to(OWNER)[-1] == "✏️ Aggiornato."
        assert api.replies_to[-1] == 101 and store.find_message(conn, OWNER, 101)["text"] == "Spesa: pane, latte, uova"
        _say(conn, worker, owner, 7, "Cambia il mio messaggio")
        assert api.to(OWNER)[-1] == "Fatto" and store.find_message(conn, OWNER, 5)["text"] == "Fammi la lista della spesa"


def test_a_text_to_forward_comes_bare_as_its_own_message_and_a_correction_rewrites_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(
            Path(tmp),
            "Ecco il messaggio per Mia 👇\n[[{k} forward]]\nCiao Mia, domani alle 10?\n[[{k} /forward]]",
            "[[{k} forward]]Ciao Mia, domani alle 11?[[/forward]]\n[[{k} edit: 102]]",
            "Ho tolto l'orario 👍\n[[{k} forward]]Ciao Mia, domani?[[/forward]]\n[[{k} edit: 102]]",
            "[[{k} forward]]Ciao Mia, ci sentiamo dopo.[[/forward]]",
        )
        _say(conn, worker, owner, 5, "Scrivimi un messaggio per Mia")
        assert api.to(OWNER) == ["Ecco il messaggio per Mia 👇", "Ciao Mia, domani alle 10?"]
        assert api.sent_ids[1] == 102 and api.replies_to[1] is None
        assert store.find_message(conn, OWNER, 102)["mine"]
        _say(conn, worker, owner, 6, "Fai alle 11")
        assert api.edited[102] == "Ciao Mia, domani alle 11?"
        assert api.to(OWNER)[-1] == "✏️ Aggiornato." and api.replies_to[-1] == 102
        _say(conn, worker, owner, 7, "Togli l'orario")
        assert api.edited[102] == "Ciao Mia, domani?" and store.find_message(conn, OWNER, 102)["text"] == "Ciao Mia, domani?"
        assert api.to(OWNER)[-1] == "Ho tolto l'orario 👍" and api.replies_to[-1] == 102
        _say(conn, worker, owner, 8, "Un altro, corto")
        shown = [t for t, i in zip(api.to(OWNER), api.sent_ids) if (OWNER, i) not in api.deleted]
        assert shown == ["Ecco il messaggio per Mia 👇", "Ciao Mia, domani?", "✏️ Aggiornato.",
                         "Ho tolto l'orario 👍", "Ciao Mia, ci sentiamo dopo."]


def test_a_voice_answer_asked_in_writing_comes_with_its_text():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp), "Domani piove.\n[[{k} voice]]", "Va bene.")
        said = []

        class Voice:
            def synthesize(self, text):
                said.append(text)
                return b"ogg"

        worker._speaker = Voice()
        _say(conn, worker, owner, 5, "Dimmelo a voce: che tempo fa domani?")
        assert api.voices == [(OWNER, b"ogg", "Domani piove.")] and said == ["Domani piove."]
        assert "Domani piove." not in api.to(OWNER) and len(api.deleted) == 1
        _say(conn, worker, owner, 6, "Grazie")
        assert len(api.voices) == 1 and api.to(OWNER)[-1] == "Va bene."


class LoggedOut:
    """Claude is logged out for the first runs, then works again."""

    def __init__(self, failing: int) -> None:
        self.failing, self.runs = failing, 0

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None) -> Result:
        self.runs += 1
        if self.runs <= self.failing:
            return Result(False, "", False, False, 1, 0.1, failure="Not logged in · Please run /login")
        return Result(True, "Eccomi", False, False, 0, 1.0)


def test_while_claude_cannot_work_turns_wait_and_the_owner_hears_it_once_and_when_it_is_back():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        worker._runner = LoggedOut(failing=2)
        store.record_inbound(conn, message(5, OWNER, "ciao"), owner.id)
        for _ in range(3):
            msg = store.claim_next(conn)
            assert msg.id == 1 and msg.attempts == 1
            worker._handle(conn, msg)
        assert float(store.get_state(conn, "claude_retry_at")) > time.time()
        down = [t for t in api.to(OWNER) if "Claude non può lavorare" in t]
        assert len(down) == 1 and "Not logged in" in down[0]
        assert sum("Claude funziona di nuovo" in t for t in api.to(OWNER)) == 1 and "Eccomi" in api.to(OWNER)
        assert store.claim_next(conn) is None

    written = Result(False, "L'API di Grafana dice: rate limit reached", False, False, 1, 3.0)
    assert worker_module.unavailable(written) == ""


def test_a_reply_to_a_message_brings_its_text_and_time():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = worker_setup(Path(tmp), "Ok.", "Certo.", "L'appuntamento.")
        _say(conn, worker, owner, 5, "Domani dentista alle 10")
        _say(conn, worker, owner, 6, "spostalo alle 11", reply_to=5, quote="alle 10")
        assert "Alex is replying to their own message #5, sent " in runner.prompts[-1]
        assert "«Domani dentista alle 10»" in runner.prompts[-1]
        assert "They quote this part of it: «alle 10»" in runner.prompts[-1]
        _say(conn, worker, owner, 7, "Cosa ti ho chiesto di spostare?")
        assert "[#6] Alex, replying to #5: spostalo alle 11" in runner.prompts[-1]


def test_messages_waiting_together_are_answered_in_one_turn():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), "Ok, tutto chiaro.")
        for update_id, text in ((5, "Prenota il ristorante"), (6, "per sabato"), (7, "alle 20")):
            store.record_inbound(conn, message(update_id, OWNER, text), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert len(runner.prompts) == 1
        assert "[#5] Alex: Prenota il ristorante\n[#6] Alex: per sabato\n[#7] Alex: alle 20\nReply in Italian" in runner.prompts[0]
        assert runner.prompts[0].endswith("whatever language your work, tools and skills are in.\nYou:")
        assert store.claim_next(conn) is None and api.to(OWNER)[-1] == "Ok, tutto chiaro."


class WritesDuringTheTurn:
    """The person writes while the first run works; the run stops and the session goes on."""

    def __init__(self, conn, person_id: int) -> None:
        self.conn, self.person_id = conn, person_id
        self.prompts, self.profiles = [], []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None) -> Result:
        self.prompts.append(prompt_text)
        self.profiles.append(profile)
        if len(self.prompts) == 1:
            store.record_inbound(self.conn, message(8, OWNER, "anzi, per domenica"), self.person_id)
            interrupt.wait(5)
            return Result(False, "", False, False, None, 1.0, session_id="s-1", cancelled=True)
        return Result(True, "Prenotato per domenica 🍝", False, False, 0, 1.0, session_id="s-1")


def test_a_message_written_during_the_turn_moves_the_progress_and_joins_the_turn():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        worker_module.FOLLOWUP_POLL, worker_module.FOLLOWUP_PAUSE = 0.05, 0.1
        worker._runner = runner = WritesDuringTheTurn(conn, owner.id)
        store.record_inbound(conn, message(7, OWNER, "Prenota per sabato"), owner.id)
        worker._handle(conn, store.claim_next(conn))

        assert len(runner.prompts) == 2 and runner.profiles[1].args[-2:] == ("--resume", "s-1")
        assert "While you were working, Alex wrote:\n[#8] Alex: anzi, per domenica" in runner.prompts[1]
        assert "Al lavoro" in api.sent[0][1] and "Al lavoro" in api.sent[1][1] and (OWNER, 101) in api.deleted
        assert api.to(OWNER)[-1] == "Prenotato per domenica 🍝" and store.claim_next(conn) is None
        episodes = [r.content for r in Brain(conn, owner.id).top("episode")]
        assert any("[#7] Alex: Prenota per sabato\n[#8] Alex: anzi, per domenica" in e for e in episodes)


def test_a_failed_turn_returns_the_messages_it_took_in_to_the_queue():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp), None)
        for update_id, text in ((5, "uno"), (6, "due")):
            store.record_inbound(conn, message(update_id, OWNER, text), owner.id)
        worker._handle(conn, store.claim_next(conn))
        again = store.claim_next(conn)
        assert again.telegram_id == 5 and [m.telegram_id for m in store.absorb(conn, OWNER, again.id)] == [6]


class StuckThenDone:
    """The first run goes silent and is stopped; the second must resume its session."""

    def __init__(self) -> None:
        self.prompts, self.profiles = [], []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None) -> Result:
        self.prompts.append(prompt_text)
        self.profiles.append(profile)
        if len(self.prompts) == 1:
            return Result(False, "", False, True, None, 240.0, session_id="s-9")
        return Result(True, "Fatto, senza rifare quello che era già fatto.", False, False, 0, 1.0, session_id="s-9")


def test_a_stuck_turn_resumes_its_session_instead_of_starting_over():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        worker._runner = runner = StuckThenDone()
        store.record_inbound(conn, message(5, OWNER, "manda la mail a Marco e aggiorna il server"), owner.id)
        worker._handle(conn, store.claim_next(conn))
        worker._handle(conn, store.claim_next(conn))
        assert runner.profiles[1].args[-2:] == ("--resume", "s-9")
        assert "do not repeat what has been done" in runner.prompts[1]
        assert api.to(OWNER)[-1] == "Fatto, senza rifare quello che era già fatto."


class StuckThenSends:
    """The first run goes silent and is stopped; the resumed one sends a file with the key its session was given."""

    def __init__(self, video: Path) -> None:
        self.video, self.prompts = video, []

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None) -> Result:
        self.prompts.append(prompt_text)
        if len(self.prompts) == 1:
            self.key = re.search(r"Your key for this turn is (\w+)\.", prompt_text).group(1)
            return Result(False, "", False, True, None, 310.0, session_id="s-9")
        return Result(True, f"Eccolo 👇\n[[{self.key} send: {self.video}]]", False, False, 0, 1.0, session_id="s-9")


def test_a_resumed_turn_keeps_the_key_its_session_was_given():
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "video.mp4"
        video.write_bytes(b"data")
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        worker._runner = runner = StuckThenSends(video)
        store.record_inbound(conn, message(5, OWNER, "mandami il video"), owner.id)
        worker._handle(conn, store.claim_next(conn))
        worker._handle(conn, store.claim_next(conn))
        assert len(runner.prompts) == 2 and api.files[-1][4] == video
        assert api.to(OWNER)[-1] == "Eccolo 👇"


def test_a_reply_that_could_not_be_delivered_is_delivered_again_without_a_new_turn():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), "Prenotato 🍝")
        real_send, failures = api.send_message, iter([False, True])

        def flaky(chat_id, text, *args, **kwargs):
            if "Prenotato" in text and next(failures, False) is False:
                raise TelegramError("sendMessage rejected: Bad Gateway")
            return real_send(chat_id, text, *args, **kwargs)

        api.send_message, api.edits_fail = flaky, True
        store.record_inbound(conn, message(5, OWNER, "prenota da Gino"), owner.id)
        claimed = store.claim_next(conn)
        try:
            worker._handle(conn, claimed)
        except TelegramError:
            worker._recover(conn, claimed)
        worker._handle(conn, store.claim_next(conn))
        assert len(runner.prompts) == 1 and api.to(OWNER)[-1] == "Prenotato 🍝"


def test_the_owner_admits_someone_by_id_through_zoe():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(
            Path(tmp), "Fatto 👍\n[[{k} admit: 333 | Mia]]", "Ok\n[[{k} admit: 444 | mia]]",
            f"No.\n[[{{k}} admit: {OWNER}]]",
        )
        _say(conn, worker, owner, 5, "aggiungi 333, si chiama Mia")
        mia = access.resolve(conn, 333).person
        assert mia.name == "Mia" and mia.role == "member"
        assert "Ora può scriverti: <b>Mia</b>" in api.to(OWNER)[-1]
        assert button_data(api.last_buttons_to(OWNER)) == ["r:revoke:333"]
        assert any("Ciao Mia" in text for text in api.to(333))

        _say(conn, worker, owner, 6, "aggiungi anche 444, è sempre Mia")
        assert access.resolve(conn, 444).person.id == mia.id and "Nuovo account di <b>Mia</b>" in api.to(OWNER)[-1]
        assert "People who can write to you besides Alex: Mia." in runner.prompts[-1]

        _say(conn, worker, owner, 7, "aggiungi il mio id")
        assert "è un account del proprietario" in api.to(OWNER)[-1] and access.resolve(conn, OWNER).person.role == "owner"

        Poller(worker._cfg, api, threading.Event()).process(conn, press(9, OWNER, "r:revoke:444"))
        assert access.resolve(conn, 444).status == "blocked"


def test_a_member_cannot_admit_anyone():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), "Ok\n[[{k} admit: 555 | Amico]]")
        mia = access.create_person(conn, "Mia", "member", "it")
        store.enqueue(conn, 333, mia.id, "aggiungi il mio amico 555", "text")
        worker._handle(conn, store.claim_next(conn))
        assert access.resolve(conn, 555).status == "unknown"
        assert "admit:" not in runner.prompts[-1]


def test_a_turn_that_starts_with_more_powers_is_stopped_and_reported():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        api = worker._api
        worker._runner = type("Refusing", (), {"run": lambda self, *a, **k: Result(
            False, "", False, False, None, 1.0, refused="tools ['Bash']")})()
        store.record_inbound(conn, message(5, OWNER, "ciao"), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert store.claim_next(conn) is None
        assert any("tools [&#x27;Bash&#x27;]" in text or "tools ['Bash']" in text for text in api.to(OWNER))


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
