"""Checks for zoe-say: programs queue messages, the service delivers them and logs them in the chat."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeAPI, press, worker_setup
from zoe import choices, formatting, say, store
from zoe.db import connect
from zoe.telegram import TelegramError


def test_a_program_speaks_in_zoes_name_and_she_knows_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        api = FakeAPI()
        say.queue(conn, Path(tmp), OWNER, "📨 <b>PEC</b> da Agenzia Entrate", origin="pec", silent=True)
        assert api.sent == [] and say.deliver(conn, api) == 1
        assert api.to(OWNER) == ["📨 <b>PEC</b> da Agenzia Entrate"]
        assert store.recent_history(conn, OWNER)[-1] == {
            "direction": "out", "text": "[pec] 📨 <b>PEC</b> da Agenzia Entrate", "telegram_id": 101, "reply_to": None}
        assert say.deliver(conn, api) == 0


def test_a_file_is_copied_so_the_program_may_delete_its_own():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        api = FakeAPI()
        note = Path(tmp) / "a.ogg"
        note.write_bytes(b"ogg")
        say.queue(conn, Path(tmp), OWNER, "", file=note, kind="voice", origin="tts")
        note.unlink()
        say.deliver(conn, api)
        chat, kind, caption, file_id, path = api.files[-1]
        assert (chat, kind, file_id) == (OWNER, "voice", None) and not path.exists()
        assert api.file_names[-1] == "a.ogg"
        assert store.recent_history(conn, OWNER)[-1]["text"] == "[tts] [file «a.ogg»]"


def test_a_message_that_keeps_failing_is_set_aside():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        api = FakeAPI()

        def down(*args, **kwargs):
            raise TelegramError("sendMessage rejected: Bad Gateway")

        api.send_message = down
        say.queue(conn, Path(tmp), OWNER, "ciao")
        for _ in range(say.TRIES):
            say.deliver(conn, api)
        assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "failed"


def test_options_put_buttons_under_the_last_part_and_a_tap_becomes_the_next_message():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        api = FakeAPI()
        text = "Riunione con Mia. " * 400
        say.queue(conn, Path(tmp), OWNER, text, origin="turni", options=["Sposta", "Lascia così"])
        say.deliver(conn, api)
        parts = formatting.split(text)
        assert len(parts) > 1 and [buttons for _, _, buttons in api.sent[:-1]] == [None] * (len(parts) - 1)
        choice = conn.execute("SELECT id, telegram_id, state FROM choices").fetchone()
        assert api.sent[-1][2] == choices.keyboard(choice["id"], ["Sposta", "Lascia così"])
        assert choice["telegram_id"] == api.sent_ids[-1] and choice["state"] == "open"
        assert store.recent_history(conn, OWNER)[-1]["text"].endswith("\n[options: Sposta | Lascia così]")

        choices.handle(conn, api, owner, press(1, OWNER, f"c:{choice['id']}:1")["callback_query"])
        queued = store.claim_next(conn)
        assert (queued.text, queued.kind, queued.person_id) == ("Lascia così", "choice", owner.id)


def test_without_options_no_buttons_and_no_choice():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        api = FakeAPI()
        say.queue(conn, Path(tmp), OWNER, "ciao")
        say.deliver(conn, api)
        assert api.sent == [(OWNER, "ciao", None)]
        assert conn.execute("SELECT COUNT(*) FROM choices").fetchone()[0] == 0
        assert store.recent_history(conn, OWNER)[-1]["text"] == "ciao"


def test_options_to_an_unknown_chat_are_sent_without_buttons():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        api = FakeAPI()
        say.queue(conn, Path(tmp), 555, "ciao", options=["Sì", "No"])
        say.deliver(conn, api)
        assert api.sent == [(555, "ciao", None)]
        assert conn.execute("SELECT COUNT(*) FROM choices").fetchone()[0] == 0


def test_a_failed_send_leaves_no_open_choice_behind():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        api = FakeAPI()

        def down(*args, **kwargs):
            raise TelegramError("sendMessage rejected: Bad Gateway")

        api.send_message = down
        say.queue(conn, Path(tmp), OWNER, "ciao", options=["Sì", "No"])
        say.deliver(conn, api)
        assert conn.execute("SELECT COUNT(*) FROM choices").fetchone()[0] == 0


def test_the_command_line_splits_options_and_refuses_them_with_a_file_or_beyond_eight():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        conn, *_ = worker_setup(home, on_disk=True)
        conn.close()
        env = {"ZOE_BOT_TOKEN": "x", "ZOE_OWNER_IDS": str(OWNER), "ZOE_HOME": str(home)}
        saved = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            assert say.main(["--options", " Sì | | No ", "ciao"]) == 0
            conn = connect(home / "zoe.db")
            assert json.loads(conn.execute("SELECT options FROM outbox").fetchone()[0]) == ["Sì", "No"]
            note = home / "a.pdf"
            note.write_bytes(b"pdf")
            for argv in (["--options", "Sì|No", "--file", str(note), "ciao"],
                         ["--options", "|".join(str(n) for n in range(say.MAX_OPTIONS + 1)), "ciao"]):
                try:
                    say.main(argv)
                except SystemExit as exit:
                    assert exit.code == 2
                else:
                    raise AssertionError(f"{argv} was accepted")
            assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
            conn.close()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
