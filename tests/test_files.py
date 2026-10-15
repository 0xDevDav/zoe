"""Checks for received files: temporary unless kept, remembered when kept, deleted carefully, sent back."""

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, button_data, press, worker_setup
from zoe import access, files, store
from zoe.brain import Brain
from zoe.ingest import Poller


def _photo(conn, owner_id: int, update_id: int, file_id: str, caption: str = "") -> None:
    store.record_inbound(conn, {"update_id": update_id, "message": {
        "message_id": update_id, "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
        "document": {"file_id": file_id, "file_name": f"{file_id}.pdf", "file_size": 2048}, "caption": caption,
    }}, owner_id)


def test_a_received_file_is_temporary_and_the_sweep_deletes_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = worker_setup(Path(tmp), "Visto.")
        _photo(conn, owner.id, 5, "screen")
        worker._handle(conn, store.claim_next(conn))
        [saved] = list((Path(tmp) / "inbox").rglob("5-screen.pdf"))
        assert f"saved at {saved} (file #1)" in runner.prompts[-1] and "deleted after 7 days" in runner.prompts[-1]
        assert f"takes this chat's id: {OWNER}." in runner.prompts[-1]

        assert files.sweep(conn, time.time()) == 0 and saved.exists()
        assert files.sweep(conn, time.time() + 8 * 86400) == 1
        assert not saved.exists() and files.get(conn, owner.id, 1) is None


def test_a_kept_file_is_remembered_where_it_is_and_survives_the_sweep():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(
            Path(tmp), "Lo tengo 📁\n[[{k} keep: 1 | Contratto d'affitto di via Roma, firmato a marzo]]",
            "È qui.",
        )
        _photo(conn, owner.id, 5, "contratto", "il contratto")
        worker._handle(conn, store.claim_next(conn))
        kept = files.get(conn, owner.id, 1)
        assert kept.kept and "📁 Tenuto: <b>5-contratto.pdf</b>" in api.to(OWNER)[-1]
        assert button_data(api.last_buttons_to(OWNER)) == ["r:discard:1"]

        archive = Path(tmp) / "archivio" / "casa"
        archive.mkdir(parents=True)
        moved = shutil.move(str(kept.path), archive / "contratto-affitto.pdf")
        assert files.keep(conn, Brain(conn, owner.id), owner, 1, "Contratto d'affitto di via Roma", str(moved),
                          Path(tmp), []).path == Path(moved)
        assert files.sweep(conn, time.time() + 30 * 86400) == 0 and Path(moved).exists()

        store.enqueue(conn, OWNER, owner.id, "dov'è il contratto d'affitto?", "text")
        worker._handle(conn, store.claim_next(conn))
        assert f"(file #1, at {moved})" in runner.prompts[-1]

        files.discard(conn, Brain(conn, owner.id), owner, 1)
        assert Path(moved).exists() and files.get(conn, owner.id, 1) is None


def test_a_member_keeps_and_sends_only_inside_their_folder():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        mia = access.create_person(conn, "Mia", "member", "it")
        home = access.turn_profile(mia, worker._cfg).cwd
        secret = Path(tmp) / "zoe.secret"
        secret.write_text("x", encoding="utf-8")
        mine = home / "inbox" / "2026-10-13" / "9-foto.jpg"
        mine.parent.mkdir(parents=True)
        mine.write_bytes(b"jpg")
        number = files.register(conn, mia.id, "photo", mine, "tg-foto", 3, 7)

        assert files.keep(conn, Brain(conn, mia.id), mia, number, "foto", str(secret), home, []) is None
        assert files.resolve(conn, mia, str(secret), home) == (None, None)
        assert files.resolve(conn, owner, str(secret), home) == (None, secret)
        record, path = files.resolve(conn, mia, f"#{number}", home)
        assert record.id == number and path == mine
        assert files.get(conn, owner.id, number) is None


def test_files_are_sent_back_by_their_telegram_id_or_uploaded():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(
            Path(tmp), "Eccolo 👇\n[[{k} send: 1 | il contratto]]", None, None,
        )
        _photo(conn, owner.id, 5, "contratto")
        worker._handle(conn, store.claim_next(conn))
        assert api.files[-1] == (OWNER, "document", "il contratto", "contratto", None)
        assert store.recent_history(conn, OWNER)[-1]["text"] == "[file «5-contratto.pdf»] il contratto"

        report = Path(tmp) / "report.png"
        report.write_bytes(b"png")
        files.send(api, OWNER, [(None, report)], "")
        assert api.files[-1][1] == "photo" and api.files[-1][4] == report


def test_several_files_arrive_together_as_albums_logged_in_the_chat():
    with tempfile.TemporaryDirectory() as tmp:
        shots = [Path(tmp) / f"shot{n}.png" for n in range(11)]
        clip, sheet = Path(tmp) / "clip.mp4", Path(tmp) / "sheet.pdf"
        for path in (*shots, clip, sheet):
            path.write_bytes(b"data")
        conn, owner, worker, _, api = worker_setup(Path(tmp), "\n".join([
            "Eccole 👇",
            f"[[{{k}} send: {shots[0]}, {clip} | la card]]",
            f"[[{{k}} send: {shots[0]}, {sheet}]]",
            f"[[{{k}} send: {', '.join(map(str, shots))}]]",
            f"[[{{k}} send: {shots[0]}, {Path(tmp) / 'gone.png'}]]",
            f"[[{{k}} send: {shots[1]}, {shots[2]} | gli originali | file]]",
        ]))
        store.enqueue(conn, OWNER, owner.id, "fammi vedere", "text")
        worker._handle(conn, store.claim_next(conn))

        card, mixed, carousel, originals = api.albums
        assert card == (OWNER, ["photo", "video"], ["shot0.png", "clip.mp4"], "la card")
        assert mixed[1] == ["document", "document"] and carousel[1] == ["photo"] * 10
        assert api.files[-1][1:] == ("photo", "", None, shots[10])
        assert originals[1:] == (["document", "document"], ["shot1.png", "shot2.png"], "gli originali")
        assert "Non sono riuscita a mandarti i file" in api.to(OWNER)[-1]
        logged = [turn["text"] for turn in store.recent_history(conn, OWNER, 30)]
        assert "[file «shot0.png»] la card" in logged and "[file «clip.mp4»]" in logged


def test_the_receipt_deletes_a_kept_file_in_one_tap():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp), "Tenuto.\n[[{k} keep: 1 | Ricevuta]]")
        _photo(conn, owner.id, 5, "ricevuta")
        worker._handle(conn, store.claim_next(conn))
        path = files.get(conn, owner.id, 1).path
        Poller(worker._cfg, api, threading.Event()).process(conn, press(9, OWNER, "r:discard:1"))
        assert not path.exists() and api.edits[-1][3][0][0]["text"] == "🗑️ Eliminato"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
