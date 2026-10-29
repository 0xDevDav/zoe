"""Checks for received files: temporary unless kept, remembered when kept, deleted carefully, sent back."""

import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, button_data, press, worker_setup
from zoe import access, files, store
from zoe.brain import Brain
from zoe.ingest import Poller
from zoe.telegram import TelegramError


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


def _png(path: Path, width: int, height: int) -> Path:
    """A minimal PNG header of this size: enough for its shape to be read."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b""))
    return path


def test_images_telegram_refuses_as_photos_go_as_documents():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        assert files._kind(_png(tmp / "phone.png", 390, 844), False)[0] == "photo"
        assert files._kind(_png(tmp / "strip.png", 1280, 56), False)[0] == "document"
        assert files._kind(_png(tmp / "huge.png", 9000, 2000), False)[0] == "document"
        photo = tmp / "beach.jpg"
        photo.write_bytes(b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
                          + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 600, 800) + bytes(12))
        assert files._dimensions(photo) == (800, 600) and files._kind(photo, False) == ("photo", False)
        assert files._dimensions(tmp / "phone.png") == (390, 844)

        def webp(name: str, chunk: bytes, body: bytes) -> Path:
            path = tmp / name
            path.write_bytes(b"RIFF" + bytes(4) + b"WEBP" + chunk + bytes(4) + body + bytes(8))
            return path
        extended = webp("x.webp", b"VP8X", bytes(4) + (1279).to_bytes(3, "little") + (55).to_bytes(3, "little"))
        lossy = webp("y.webp", b"VP8 ", bytes(6) + struct.pack("<HH", 390, 844))
        lossless = webp("z.webp", b"VP8L", b"\x2f" + (1279 | 55 << 14).to_bytes(4, "little"))
        assert files._dimensions(extended) == (1280, 56) and files._kind(extended, False) == ("document", True)
        assert files._dimensions(lossy) == (390, 844) and files._kind(lossy, False) == ("photo", False)
        assert files._dimensions(lossless) == (1280, 56)


def test_an_odd_image_in_an_album_goes_as_a_document_and_the_rest_as_photos():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn, owner, worker, _, api = worker_setup(tmp, "")
        strip = _png(tmp / "menu.png", 1280, 56)
        shots = [_png(tmp / f"page{n}.png", 1280, 800) for n in range(3)]
        sent = files.send(api, OWNER, [(None, path) for path in (strip, *shots)], "le schermate")
        assert api.albums == [(OWNER, ["photo"] * 3, ["page0.png", "page1.png", "page2.png"], "le schermate")]
        assert api.files[-1] == (OWNER, "document", "", None, strip)
        assert [m["message_id"] for m in sent] == [104, 101, 102, 103]

        sheet = tmp / "sheet.pdf"
        sheet.write_bytes(b"data")
        files.send(api, OWNER, [(None, strip), (None, shots[0]), (None, sheet)], "")
        assert api.albums[-1][1] == ["document"] * 3


def test_photos_telegram_still_refuses_for_their_shape_go_again_as_documents():
    class Refusing:
        def __init__(self) -> None:
            self.sent: list[list[str]] = []

        def send_file(self, chat_id, kind, caption="", file_id=None, path=None, silent=False, name=""):
            return self.send_album(chat_id, [(kind, path)], caption)[0]

        def send_album(self, chat_id, items, caption=""):
            self.sent.append([kind for kind, _ in items])
            if "photo" in self.sent[-1]:
                raise TelegramError('sendMediaGroup rejected: Bad Request: "PHOTO_INVALID_DIMENSIONS"')
            return [{"message_id": n} for n in range(len(items))]

    with tempfile.TemporaryDirectory() as tmp:
        api = Refusing()
        image, clip = Path(tmp) / "odd.png", Path(tmp) / "clip.mp4"
        image.write_bytes(b"png")
        clip.write_bytes(b"mp4")
        assert files.send(api, OWNER, [(None, image)], "eccola") == [{"message_id": 0}]
        assert api.sent == [["photo"], ["document"]]
        assert len(files.send(api, OWNER, [(None, image), (None, clip)], "")) == 2
        assert api.sent[2:] == [["photo", "video"], ["document", "document"]]


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
