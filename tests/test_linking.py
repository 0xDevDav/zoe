"""Checks for notices that join the conversation they belong to before the outbox sends them."""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OTHER_OWNER_ACCOUNTS, OWNER, FakeAPI, FakeRunner, message, worker_setup
from zoe import formatting, linking, minds, say, store
from zoe.brain import Brain
from zoe.claude_runner import DOWN, Result

NOTICE = ("📧 Nuovo accesso al tuo account Google\n• App: <b>Orbit Bike</b>\n"
          "Sei stato tu? Se no, togli l'accesso da <a href=\"https://myaccount.example.com/perms\">qui</a>")


WORK = OTHER_OWNER_ACCOUNTS[0]
TRIAL = ("📬 Una richiesta di firma di prova\n• Da office@example.com a sé stessa, con <b>Quill</b>\n"
         "• Link su <code>sign.example.com</code>, codice <code>482913</code>\nLa stavi provando tu?")


def _said(conn, owner, telegram_id: int, text: str, ago: float = 120.0, chat: int = OWNER) -> None:
    """The owner wrote ``text`` ``ago`` seconds back in ``chat``, and Zoe has already answered it."""
    row = store.record_inbound(conn, message(telegram_id, chat, text), owner.id)
    conn.execute("UPDATE messages SET state = 'answered', created_at = ? WHERE id = ?", (time.time() - ago, row))


def _answered(conn, owner, telegram_id: int, text: str, chat: int) -> None:
    store.record_outbound(conn, chat, telegram_id, text, owner.id)


def _setup(tmp: str, *replies, limit: float = 5.0):
    conn, owner, worker, _, _ = worker_setup(Path(tmp), others=OTHER_OWNER_ACCOUNTS)
    runner = FakeRunner(*replies)
    return conn, owner, FakeAPI(), runner, linking.Linker(worker._cfg, runner, limit=limit)


def _deliver_all(conn, api, linker, within: float = 5.0) -> None:
    """Deliver until nothing waits in the outbox."""
    end = time.monotonic() + within
    while conn.execute("SELECT 1 FROM outbox WHERE state = 'waiting'").fetchone():
        assert time.monotonic() < end, "the outbox kept a message waiting"
        say.deliver(conn, api, linker)
        time.sleep(0.02)


def test_a_notice_about_something_else_goes_out_exactly_as_written():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, api, runner, linker = _setup(tmp, '{"joins": false}')
        _said(conn, owner, 40, "Domani mi ricordi la riunione con Mia?")
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        _deliver_all(conn, api, linker)
        assert api.sent == [(OWNER, NOTICE, None)] and api.replies_to == [None]
        assert "[#40] Alex: Domani mi ricordi la riunione con Mia?" in runner.prompts[0]
        assert NOTICE in runner.prompts[0] and '"gmail"' in runner.prompts[0]
        assert store.recent_history(conn, OWNER)[-1]["text"] == f"[gmail] {NOTICE}"


def test_a_notice_about_the_conversation_answers_the_message_it_belongs_to():
    with tempfile.TemporaryDirectory() as tmp:
        written = ("Ecco, la bike Orbit si è appena collegata al tuo Google 🚲 Ora tocca all'app\n"
                   "• App: <b>Orbit Bike</b> — accesso dato\n"
                   "<a href=\"https://myaccount.example.com/perms\">Gestisci accessi</a>")
        conn, owner, api, runner, linker = _setup(
            tmp, json.dumps({"joins": True, "reply_to": "#40", "text": written}))
        _said(conn, owner, 40, "Sto collegando la bike Orbit al telefono, mi segue i giri?")
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        _deliver_all(conn, api, linker)
        shown = formatting.to_telegram(written)
        assert "—" not in shown and "🚲\nOra" in shown
        assert api.sent == [(OWNER, shown, None)] and api.replies_to == [40]
        last = store.recent_history(conn, OWNER)[-1]
        assert last["text"] == f"[gmail] {shown}" and last["reply"] == {"id": 40}
        assert tuple(conn.execute("SELECT text, state FROM outbox").fetchone()) == (NOTICE, "sent")


def test_a_rewritten_notice_reaches_the_person_without_the_label_it_opens_with():
    with tempfile.TemporaryDirectory() as tmp:
        written = ("[gmail] Ecco, la bike Orbit si è appena collegata al tuo Google 🚲\n"
                   "<a href=\"https://myaccount.example.com/perms\">Gestisci accessi</a>")
        conn, owner, api, runner, linker = _setup(
            tmp, json.dumps({"joins": True, "reply_to": 40, "text": written}))
        _said(conn, owner, 40, "Sto collegando la bike Orbit al telefono, mi segue i giri?")
        say.queue(conn, Path(tmp), OWNER, f"[gmail alert] {NOTICE}", origin="gmail")
        _deliver_all(conn, api, linker)
        shown = formatting.to_telegram(written)
        assert api.sent == [(OWNER, shown.removeprefix("[gmail] "), None)] and api.replies_to == [40]
        assert store.recent_history(conn, OWNER)[-1]["text"] == f"[gmail] {shown}"


def _signing_at_work(tmp: str, conn, owner) -> int:
    """The scene of a mind installing a signing app: decided in the work chat while the main one talked of
    something else, and the mind at work on it. Returns the mind."""
    _said(conn, owner, 60, "Il grafico ora va, grazie", ago=300)
    _said(conn, owner, 70, "Che ne dici di Quill per firmare i contratti?", ago=2700, chat=WORK)
    _answered(conn, owner, 71, "Mi piace: lo metterei sulla nostra macchina, su sign.example.com", WORK)
    _said(conn, owner, 72, "Sì, installalo", ago=1800, chat=WORK)
    _answered(conn, owner, 73, "Ho avviato una mente che installa Quill su sign.example.com", WORK)
    mind = minds.start(conn, Brain(conn, owner.id), owner, WORK, "Quill: firma dei contratti su sign.example.com",
                       "Installa Quill", "", [], Path(tmp))
    conn.execute("UPDATE minds SET state = 'running', started_at = ?, steps = ? WHERE id = ?",
                 (time.time() - 1700, json.dumps(["Scrivo: Caddyfile", "Eseguo: Manda una firma di prova"]), mind))
    return mind


def test_a_test_of_a_mind_at_work_decided_in_another_chat_is_kept_in_the_log_and_not_sent():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, api, runner, linker = _setup(
            tmp, json.dumps({"joins": True, "send": False, "why": "prova di firma della mente di Quill"}))
        mind = minds.get(conn, _signing_at_work(tmp, conn, owner))
        say.queue(conn, Path(tmp), OWNER, TRIAL, origin="avviso mail")
        _deliver_all(conn, api, linker)
        asked = runner.prompts[0]
        assert "[#60] Alex: Il grafico ora va, grazie" in asked
        assert "Alex: Sì, installalo" in asked and "You: Ho avviato una mente che installa Quill" in asked
        assert "[#72]" not in asked, "only the messages of the chat the notice goes to can be answered"
        assert (f"#{mind.id} «Quill: firma dei contratti su sign.example.com» in {mind.workdir}, working for 28 min, "
                "last steps: Scrivo: Caddyfile; Eseguo: Manda una firma di prova") in asked
        assert api.sent == [] and conn.execute("SELECT state FROM outbox").fetchone()[0] == "sent"
        kept = conn.execute("SELECT telegram_id, text, payload FROM messages ORDER BY id DESC").fetchone()
        assert tuple(kept)[:2] == (None, f"[avviso mail] [not sent: prova di firma della mente di Quill] {TRIAL}")
        assert json.loads(kept["payload"]) == {"origin": "avviso mail", "held": True}
        assert store.recent_history(conn, OWNER)[-1]["text"] == kept["text"], "Zoe knows of it"


def test_a_notice_of_our_work_that_matters_goes_saying_so_and_replies_to_no_message_of_another_chat():
    with tempfile.TemporaryDirectory() as tmp:
        written = ("Sulla firma di Quill che sta installando la mente: è arrivata la sua prova ✍️\n"
                   "• Codice <code>482913</code>, link su <code>sign.example.com</code>")
        conn, owner, api, runner, linker = _setup(tmp, json.dumps({"joins": True, "reply_to": 72, "text": written}))
        _said(conn, owner, 72, "Sì, installalo", ago=1800, chat=WORK)
        say.queue(conn, Path(tmp), OWNER, TRIAL, origin="avviso mail")
        _deliver_all(conn, api, linker)
        assert "The chat the notice goes to" in runner.prompts[0] and "Alex: Sì, installalo" in runner.prompts[0]
        assert api.sent == [(OWNER, written, None)] and api.replies_to == [None]


def test_a_notice_about_nothing_of_the_chats_or_the_minds_goes_exactly_as_written():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, api, runner, linker = _setup(tmp, '{"joins": false}')
        _signing_at_work(tmp, conn, owner)
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        _deliver_all(conn, api, linker)
        assert len(runner.prompts) == 1 and api.sent == [(OWNER, NOTICE, None)] and api.replies_to == [None]
        assert store.recent_history(conn, OWNER)[-1]["text"] == f"[gmail] {NOTICE}"


def test_a_mind_at_work_or_just_ended_opens_the_reading_while_the_person_is_quiet():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, api, runner, linker = _setup(tmp, '{"joins": false}', '{"joins": false}')
        brain = Brain(conn, owner.id)
        older = minds.start(conn, brain, owner, OWNER, "Backup delle foto", "Copia le foto", "", [], Path(tmp))
        conn.execute("UPDATE minds SET state = 'done', finished_at = ?, steps = ? WHERE id = ?",
                     (time.time() - linking.WINDOW - 60, '["Eseguo: Copia le foto"]', older))
        plan = store.enqueue(conn, OWNER, owner.id, "Ricorda ad Alex la palestra", "plan")
        conn.execute("UPDATE messages SET state = 'answered' WHERE id = ?", (plan,))
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        _deliver_all(conn, api, linker)
        assert runner.prompts == [], "neither a mind ended long ago nor Zoe's own plan open it"

        mind = minds.start(conn, brain, owner, OWNER, "Sito della palestra", "Fai il sito", "", [], Path(tmp))
        conn.execute("UPDATE minds SET state = 'done', finished_at = ?, steps = ? WHERE id = ?",
                     (time.time() - 1200, '["Eseguo: Pubblica il sito"]', mind))
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        working = minds.start(conn, brain, owner, WORK, "Ricerca sulle bici", "Cerca", "", [], Path(tmp))
        say.queue(conn, Path(tmp), WORK, NOTICE, origin="gmail")
        _deliver_all(conn, api, linker)
        assert len(runner.prompts) == 2
        assert "«Sito della palestra» in " in runner.prompts[0]
        assert "done 20 min ago, last steps: Eseguo: Pubblica il sito" in runner.prompts[0]
        assert "Backup delle foto" not in runner.prompts[0] and f"#{working} «Ricerca sulle bici»" in runner.prompts[1]
        assert "(no message)" in runner.prompts[1] and "Another chat of yours with Alex" in runner.prompts[1]
        assert api.sent == [(OWNER, NOTICE, None), (OWNER, NOTICE, None), (WORK, NOTICE, None)]


def test_a_failing_or_unreadable_run_lets_the_notice_go_as_written():
    lost_link = json.dumps({"joins": True, "reply_to": 40, "text": "La bike Orbit è collegata ✅"})
    for answer in (None, "Non saprei.", '{"joins": true, "reply_to": 40}', lost_link,
                   '{"joins": false, "send": false}', '{"joins": true, "send": "no"}'):
        with tempfile.TemporaryDirectory() as tmp:
            conn, owner, api, runner, linker = _setup(tmp, answer)
            _said(conn, owner, 40, "Sto collegando la bike Orbit al telefono")
            say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
            _deliver_all(conn, api, linker)
            assert len(runner.prompts) == 1 and api.sent == [(OWNER, NOTICE, None)], answer


class _Stuck:
    """A run that answers only when it is cancelled."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0,
            model="", effort="") -> Result:
        cancel.wait(10)
        self.cancelled.set()
        return Result(False, "", False, False, None, 10.0, cancelled=True)


def test_a_slow_run_holds_only_its_chat_and_never_past_the_limit():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, _ = worker_setup(Path(tmp))
        api, stuck = FakeAPI(), _Stuck()
        linker = linking.Linker(worker._cfg, stuck, limit=0.3)
        _said(conn, owner, 40, "Sto collegando la bike Orbit al telefono")
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        say.queue(conn, Path(tmp), OWNER, "Backup fatto", origin="backup")
        say.queue(conn, Path(tmp), 555, "Turno di Sam alle 15", origin="turni")
        started = time.monotonic()
        say.deliver(conn, api, linker)
        assert api.sent == [(555, "Turno di Sam alle 15", None)]
        _deliver_all(conn, api, linker)
        assert time.monotonic() - started < 2.0 and stuck.cancelled.wait(1)
        assert [text for _, text, _ in api.sent[1:]] == [NOTICE, "Backup fatto"]


def test_no_run_for_a_quiet_chat_files_buttons_or_messages_without_a_program():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, api, runner, linker = _setup(tmp)
        _said(conn, owner, 40, "Sto collegando la bike Orbit al telefono", ago=linking.WINDOW + 60)
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        _deliver_all(conn, api, linker)
        _said(conn, owner, 41, "Eccomi di nuovo")
        note = Path(tmp) / "giri.pdf"
        note.write_bytes(b"pdf")
        say.queue(conn, Path(tmp), OWNER, "I giri di oggi", file=note, origin="report")
        say.queue(conn, Path(tmp), OWNER, "Attivo il turno?", origin="turni", options=["Sì", "No"])
        say.queue(conn, Path(tmp), OWNER, "Ciao Alex")
        store.set_state(conn, DOWN, "usage limit")
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        _deliver_all(conn, api, linker)
        assert runner.prompts == []
        assert [text for _, text, _ in api.sent] == [NOTICE, "Attivo il turno?", "Ciao Alex", NOTICE]


def test_the_chat_shown_is_only_the_last_hours():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, api, runner, linker = _setup(tmp, '{"joins": false}')
        _said(conn, owner, 39, "Ieri ho cenato da Mia", ago=linking.WINDOW + 60)
        _said(conn, owner, 40, "Sto collegando la bike Orbit al telefono")
        say.queue(conn, Path(tmp), OWNER, NOTICE, origin="gmail")
        _deliver_all(conn, api, linker)
        assert "Mia" not in runner.prompts[0] and "[#40]" in runner.prompts[0]


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
