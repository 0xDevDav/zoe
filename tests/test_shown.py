"""Checks for what shows the model and effort of each run while ``show_strength`` is on: the line a
reply ends with, and the label of a parallel mind in the menu, which its news never carries."""

import dataclasses
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeAPI, FakeRunner, message, shown, worker_setup
from test_minds import _engine, _run_mind
from test_worker import _done, _say
from zoe import formatting, minds, say, store, strength
from zoe.brain import Brain
from zoe.claude_runner import Result
from zoe.config import Config
from zoe.menu import Menu
from zoe.telegram import CAPTION_LIMIT, TelegramError

MODEL = "claude-opus-5-5"


def _showing(home: Path, *replies: str | None, on_disk: bool = False):
    """A worker that shows the model and effort of each run."""
    conn, owner, worker, runner, api = worker_setup(home, *replies, on_disk=on_disk)
    worker._cfg = dataclasses.replace(worker._cfg, show_strength=True, claude_model=MODEL)
    return conn, owner, worker, runner, api


def _line(worker, runner) -> str:
    """The line that closes a reply of the latest turn, from the model and effort Claude was started with."""
    return f"\n\n<i>{strength.label(worker._cfg, 'it', *runner.models[-1])}</i>"


def _visible(api) -> list[str]:
    """The texts the chat still shows, as each reads now."""
    return [text for text, sent in zip(api.to(OWNER), api.sent_ids) if (OWNER, sent) not in api.deleted]


def test_a_model_and_its_effort_read_as_a_person_says_them():
    ids = ("claude-sonnet-5-5", "claude-fable-5-2", "claude-opus-4-1-20250805", "claude-3-5-sonnet-20241022", "opus")
    assert [strength.model_name(model) for model in ids] == [
        "Sonnet 5.5", "Fable 5.2", "Opus 4.1", "3.5 Sonnet", "Opus"]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = dataclasses.replace(worker_setup(Path(tmp))[2]._cfg, show_strength=True)
    assert [strength.label(cfg, "it", MODEL, effort) for effort in ("low", "medium", "high", "xhigh")] == [
        "Opus 5.5, sforzo basso", "Opus 5.5, sforzo medio", "Opus 5.5, sforzo alto", "Opus 5.5, sforzo xhigh"]
    assert strength.label(cfg, "en", "claude-sonnet-5-5", "low") == "Sonnet 5.5, low effort"


def test_a_written_reply_ends_with_the_model_and_effort_its_turn_ran_on():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _showing(Path(tmp), "Figurati!", "Fatto: <b>corretto</b>.", "Sì.")
        _say(conn, worker, owner, 5, "Grazie")
        _say(conn, worker, owner, 6, "Sistemami il codice del sito di Mia")
        assert runner.models == [(MODEL, "xhigh")] * 2, runner.models
        assert api.to(OWNER) == ["Figurati!\n\n<i>Opus 5.5, sforzo xhigh</i>",
                                 "Fatto: <b>corretto</b>.\n\n<i>Opus 5.5, sforzo xhigh</i>"]
        # The line is on what is sent only: the log, the next prompt and memory keep what Zoe wrote.
        assert [turn["text"] for turn in store.recent_history(conn, OWNER) if turn["direction"] == "out"] == [
            "Figurati!", "Fatto: <b>corretto</b>."]
        _say(conn, worker, owner, 7, "Chiaro?")
        assert "You: Figurati!\n" in runner.prompts[-1] and "sforzo" not in runner.prompts[-1]
        assert not any("sforzo" in episode.content for episode in Brain(conn, owner.id).top("episode"))


def test_a_reaction_alone_carries_no_line():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = _showing(Path(tmp), "[[{k} react: 👍]]", "[[{k} react: 🙏]]")
        _say(conn, worker, owner, 5, "Grazie")
        assert api.reactions == [(OWNER, 5, "👍")] and _visible(api) == []
        # A reaction that cannot be set, on a message with no number, is written instead and stays bare.
        store.enqueue(conn, OWNER, owner.id, "A dopo", "text")
        worker._handle(conn, store.claim_next(conn))
        assert _visible(api) == ["🙏"]


def test_a_text_to_forward_never_carries_the_line():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _showing(
            Path(tmp),
            "Ecco il messaggio per Mia 👇\n[[{k} forward]]\nCiao Mia, domani alle 10?\n[[{k} /forward]]",
            "[[{k} forward]]Ciao Mia, domani alle 11?[[/forward]]\n[[{k} edit: 102]]",
            "Ciao Mia, domani?\n[[{k} edit: 102]]",
            "[[{k} forward]]Ciao Mia, ci sentiamo dopo.[[/forward]]",
            "[[{k} forward]]Mia, ecco il messaggio.[[/forward]]\n[[{k} edit: 101]]",
            "Mia, ecco il messaggio nuovo.\n[[{k} edit: 101]]",
        )
        _say(conn, worker, owner, 5, "Scrivimi un messaggio per Mia")
        assert _visible(api) == [f"Ecco il messaggio per Mia 👇{_line(worker, runner)}", "Ciao Mia, domani alle 10?"]
        _say(conn, worker, owner, 6, "Fai alle 11")
        assert api.edited[102] == "Ciao Mia, domani alle 11?"
        assert _visible(api)[-1] == f"✏️ Aggiornato.{_line(worker, runner)}"
        # A new draft written as a plain reply still lands on a text to forward: bare.
        _say(conn, worker, owner, 7, "Togli l'orario")
        assert api.edited[102] == "Ciao Mia, domani?"
        _say(conn, worker, owner, 8, "Un altro, corto")
        assert _visible(api)[-1] == "Ciao Mia, ci sentiamo dopo."
        # A reply that becomes a text to forward loses its line, and has none when rewritten again.
        _say(conn, worker, owner, 9, "Fai del primo un messaggio per Mia")
        assert api.edited[101] == "Mia, ecco il messaggio."
        _say(conn, worker, owner, 10, "Aggiungi che è nuovo")
        assert api.edited[101] == "Mia, ecco il messaggio nuovo."


def test_a_message_rewritten_in_place_keeps_one_line():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _showing(
            Path(tmp), "Spesa: pane, latte", "Spesa: pane, latte, uova\n[[{k} edit: 101]]",
            "Spesa: pane\n[[{k} edit: 101]]")
        _say(conn, worker, owner, 5, "Fammi la lista della spesa")
        _say(conn, worker, owner, 6, "Aggiungi le uova")
        assert api.edited[101] == f"Spesa: pane, latte, uova{_line(worker, runner)}"
        assert store.find_message(conn, OWNER, 101)["text"] == "Spesa: pane, latte, uova"
        _say(conn, worker, owner, 7, "Togli latte e uova")
        assert api.edited[101] == f"Spesa: pane{_line(worker, runner)}" and api.edited[101].count("<i>") == 1
        # A program's message never had the line, and has none when Zoe rewrites it.
        say.queue(conn, Path(tmp), OWNER, "Backup fatto")
        say.deliver(conn, api)
        notice = api.sent_ids[-1]
        assert api.to(OWNER)[-1] == "Backup fatto"
        runner.replies.append(f"Backup fatto e verificato\n[[{{k}} edit: {notice}]]")
        _say(conn, worker, owner, 8, "Correggi l'avviso")
        assert api.edited[notice] == "Backup fatto e verificato"


def test_a_voice_note_has_the_line_written_under_it_and_never_spoken():
    fits_alone = ("parola " * 200)[:CAPTION_LIMIT].strip()
    too_long = "parola " * 170
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _showing(
            Path(tmp), "Domani piove.\n[[{k} voice]]", f"{fits_alone}\n[[{{k}} voice]]", f"{too_long}\n[[{{k}} voice]]")
        said = []

        class Voice:
            def synthesize(self, text):
                said.append(text)
                return b"ogg"

        worker._speaker = Voice()
        _say(conn, worker, owner, 5, "Dimmelo a voce: che tempo fa domani?")
        line = _line(worker, runner)
        assert api.voices == [(OWNER, b"ogg", f"Domani piove.{line}")] and said == ["Domani piove."]
        # A caption holds 1024 characters: the text comes first, and the line only where it still fits.
        assert len(fits_alone) <= CAPTION_LIMIT < len(fits_alone + line) and len(too_long.strip()) > CAPTION_LIMIT
        _say(conn, worker, owner, 6, "E dopodomani?")
        _say(conn, worker, owner, 7, "E tra tre giorni?")
        assert [caption for _, _, caption in api.voices[1:]] == [fits_alone, _line(worker, runner).strip()]
        assert said[1:] == [fits_alone, too_long.strip()] and not any("sforzo" in spoken for spoken in said)


def test_a_long_reply_stays_within_telegrams_limit_with_the_line_on_its_last_part():
    paragraphs = "\n\n".join(f"Paragrafo {i}: " + "parola " * 80 for i in range(12))
    one_message = ("parola " * 600)[:formatting.MESSAGE_LIMIT - 2].strip()
    assert len(formatting.split(formatting.to_telegram(one_message))) == 1
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _showing(Path(tmp), paragraphs, one_message)
        _say(conn, worker, owner, 5, "Raccontami tutto")
        line, parts = _line(worker, runner), _visible(api)
        assert len(parts) > 1 and all(len(part) <= formatting.MESSAGE_LIMIT for part in parts)
        assert [part.endswith(line) for part in parts] == [False] * (len(parts) - 1) + [True]
        # A reply that fills one message alone goes in two rather than past the limit.
        _say(conn, worker, owner, 6, "Ancora")
        first, last = _visible(api)[len(parts):]
        assert max(len(first), len(last)) <= formatting.MESSAGE_LIMIT and last.endswith(_line(worker, runner))
        assert "<i>" not in first and (first + last).count("<i>") == 1
        logged = [row["text"] for row in conn.execute("SELECT text FROM messages WHERE direction = 'out'")]
        assert len(logged) == len(parts) + 2 and not any("sforzo" in text for text in logged)


def test_a_reply_of_files_alone_has_the_line_under_its_first_caption():
    with tempfile.TemporaryDirectory() as tmp:
        photo, note = Path(tmp) / "foto.jpg", Path(tmp) / "nota.txt"
        photo.write_bytes(b"data")
        note.write_text("nota", encoding="utf-8")
        long_caption = ("parola " * 200)[:CAPTION_LIMIT - 10].strip()
        conn, owner, worker, runner, api = _showing(
            Path(tmp),
            f"[[{{k}} send: {photo} | Ecco la foto]]\n[[{{k}} send: {note} | E la nota]]",
            f"Eccola 👇\n[[{{k}} send: {photo} | La foto]]",
            f"[[{{k}} send: {photo} | {long_caption}]]",
        )
        _say(conn, worker, owner, 5, "mandami la foto e la nota")
        assert [sent[2] for sent in api.files] == [f"Ecco la foto{_line(worker, runner)}", "E la nota"]
        assert store.find_message(conn, OWNER, api.sent_ids[0] + 1)["text"] == "[file «foto.jpg»] Ecco la foto"
        # With a written reply the line is under the text, once.
        _say(conn, worker, owner, 6, "rimandami la foto")
        assert _visible(api)[-1] == f"Eccola 👇{_line(worker, runner)}" and api.files[-1][2] == "La foto"
        # A caption with no room for the line stays whole.
        _say(conn, worker, owner, 7, "ancora, con la descrizione")
        assert api.files[-1][2] == long_caption


def test_a_reply_delivered_again_ends_with_the_line_of_the_turn_that_wrote_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _showing(Path(tmp), "Prenotato 🍝")
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
        assert len(runner.prompts) == 1 and api.to(OWNER)[-1] == f"Prenotato 🍝{_line(worker, runner)}"


def test_what_a_wait_ends_with_takes_the_place_of_its_notice_with_one_line():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = _showing(Path(tmp), on_disk=True)
        ended = threading.Event()

        class Later:
            def answers(self, cancel=None):
                ended.wait(5)
                yield Result(True, "Render pronto: <b>video.mp4</b>", False, False, 0, 9.0)

        class StartsARender:
            def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0,
                    model="", effort=""):
                self.ran = strength.label(worker._cfg, "it", model, effort)
                return Result(True, "Avvio il render, circa 20 min", False, False, None, 1.0, session_id="s-6",
                              later=Later())

        worker._runner = runner = StartsARender()
        store.record_inbound(conn, message(5, OWNER, "fai il render"), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert api.to(OWNER) == [f"Avvio il render, circa 20 min\n\n<i>{runner.ran}</i>"]
        ended.set()
        _done(conn)
        assert api.to(OWNER) == [f"Render pronto: <b>video.mp4</b>\n\n<i>{runner.ran}</i>"] and not api.deleted


def test_the_menu_alone_tells_the_model_and_effort_of_a_mind():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _showing(Path(tmp), "Il sito è pronto ✅", on_disk=True)
        brain, menu = Brain(conn, owner.id), Menu(worker._cfg, FakeAPI())
        mind_id = minds.start(conn, brain, owner, OWNER, "Sito pizzeria", "Crea il sito", "", [], Path(tmp))
        waiting = minds.start(conn, brain, owner, OWNER, "Ricerca", "Cerca", "", [], Path(tmp))
        assert "sforzo" not in shown(menu.answer(conn, owner, {"view": "minds"})), "nothing is chosen yet"

        engine = _engine(worker, FakeRunner("Fatto: il sito è in /srv/pizzeria."))
        _run_mind(conn, engine)
        assert engine._runner.models == [(MODEL, "xhigh")]
        ran = " · Opus 5.5, sforzo xhigh"
        view = menu.answer(conn, owner, {"view": "minds"})
        details = {row["to"]: row["detail"] for block in view["blocks"] for row in block.get("items", [])}
        assert details == {f"mind:{waiting}": "{elapsed}, in partenza", f"mind:{mind_id}": f"✅ Finita 0 min fa{ran}"}

        worker._handle(conn, store.claim_next(conn))
        assert "has finished" in runner.prompts[-1]
        assert "sforzo xhigh" not in runner.prompts[-1], "the news of a mind names no model or effort to pass on"
        assert api.to(OWNER)[-1] == "Il sito è pronto ✅\n\n<i>Opus 5.5, sforzo alto</i>", "the news turn's own line"

        started = time.time() - 600
        conn.execute("UPDATE minds SET state = 'running', started_at = ?, steps = ? WHERE id = ?",
                     (started, '["Scrivo: index.html"]', mind_id))
        [row] = [row for block in menu.answer(conn, owner, {"view": "minds"})["blocks"]
                 for row in block.get("items", []) if row["to"] == f"mind:{mind_id}"]
        assert row["detail"] == "{elapsed}, Scrivo: index.html" + ran
        page = menu.answer(conn, owner, {"view": f"mind:{mind_id}"})
        assert page["blocks"][0] == {"text": "⚙️ Al lavoro da {elapsed}" + ran, "since": started}
        conn.execute("UPDATE minds SET phases = ?, phase = 2 WHERE id = ?", ('["Leggo", "Scrivo", "Test"]', mind_id))
        [meter] = menu.answer(conn, owner, {"view": "minds"})["blocks"][1]["meters"]
        assert meter["detail"] == "{elapsed}, Fase 2 di 3: Scrivo" + ran
        conn.close()


def test_the_news_of_a_mind_that_asks_or_fails_names_no_model_or_effort():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = _showing(Path(tmp), "Quale dominio vuoi?", "La ricerca si è fermata.",
                                                  on_disk=True)
        brain = Brain(conn, owner.id)
        minds.start(conn, brain, owner, OWNER, "Sito pizzeria", "Crea il sito", "", [], Path(tmp))
        _run_mind(conn, _engine(worker, FakeRunner("[[{k} ask: Quale dominio?]]")))
        worker._handle(conn, store.claim_next(conn))
        assert "needs a decision to go on: Quale dominio?" in runner.prompts[-1]
        assert "sforzo xhigh" not in runner.prompts[-1]
        minds.start(conn, brain, owner, OWNER, "Ricerca", "Cerca", "", [], Path(tmp) / "ricerca")
        _run_mind(conn, _engine(worker, FakeRunner(None, None)))
        worker._handle(conn, store.claim_next(conn))
        assert "stopped without finishing" in runner.prompts[-1]
        assert "sforzo xhigh" not in runner.prompts[-1]
        conn.close()


def test_with_the_switch_off_nothing_shows_the_model_and_effort():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), "Figurati!", "Il sito è pronto ✅", on_disk=True)
        assert not worker._cfg.show_strength and not Config.show_strength
        assert strength.label(worker._cfg, "it", MODEL, "xhigh") == ""
        _say(conn, worker, owner, 5, "Grazie")
        assert api.to(OWNER) == ["Figurati!"] and store.find_message(conn, OWNER, api.sent_ids[0])["payload"] == {}
        mind_id = minds.start(conn, Brain(conn, owner.id), owner, OWNER, "Sito pizzeria", "Crea il sito", "", [],
                              Path(tmp))
        _run_mind(conn, _engine(worker, FakeRunner("Fatto: il sito è in /srv/pizzeria.")))
        menu = Menu(worker._cfg, FakeAPI())
        seen = shown(menu.answer(conn, owner, {"view": "minds"})) + shown(
            menu.answer(conn, owner, {"view": f"mind:{mind_id}"}))
        assert "✅ Finita 0 min fa" in seen and "sforzo" not in seen and " · " not in seen
        worker._handle(conn, store.claim_next(conn))
        assert "has finished" in runner.prompts[-1]
        assert api.to(OWNER)[-1] == "Il sito è pronto ✅"
        conn.close()

        before = os.environ.get("ZOE_SHOW_STRENGTH")
        try:
            read = {}
            for value in (None, "0", "1"):
                os.environ.pop("ZOE_SHOW_STRENGTH", None)
                if value is not None:
                    os.environ["ZOE_SHOW_STRENGTH"] = value
                read[value] = Config.from_env(Path(tmp) / "none.env").show_strength
            assert read == {None: False, "0": False, "1": True}, read
        finally:
            os.environ.pop("ZOE_SHOW_STRENGTH", None)
            if before is not None:
                os.environ["ZOE_SHOW_STRENGTH"] = before


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
