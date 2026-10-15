"""Checks for zoe-say: programs queue messages, the service delivers them and logs them in the chat."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeAPI, worker_setup
from zoe import say, store
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


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
