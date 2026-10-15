"""Checks for every kind of Telegram content: how it is classified, saved, written out, and read in a turn."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeAPI, worker_setup
from zoe import content, store


def _message(update_id: int, **fields: object) -> dict:
    return {"update_id": update_id, "message": {
        "message_id": update_id, "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, **fields}}


def test_every_kind_is_recognized():
    cases = {
        "photo": {"photo": [{"file_id": "small"}, {"file_id": "big-photo", "file_size": 900}]},
        "animation": {"animation": {"file_id": "gif", "thumbnail": {"file_id": "gif-thumb"}},
                      "document": {"file_id": "gif"}},
        "video_note": {"video_note": {"file_id": "round", "duration": 12}},
        "sticker": {"sticker": {"file_id": "webp", "emoji": "😂", "set_name": "Cats"}},
        "venue": {"venue": {"location": {"latitude": 45.4, "longitude": 9.1}, "title": "Bar Aurora", "address": "Via Roma 1"},
                  "location": {"latitude": 45.4, "longitude": 9.1}},
        "contact": {"contact": {"first_name": "Sara", "last_name": "Neri", "phone_number": "+39 333"}},
        "poll": {"poll": {"question": "Pizza?", "options": [{"text": "Sì"}, {"text": "No"}]}},
        "dice": {"dice": {"emoji": "🎲", "value": 4}},
        "other": {"story": {"id": 1}},
    }
    for kind, fields in cases.items():
        assert content.classify(fields)[0] == kind, kind
    assert content.classify(cases["photo"])[1]["file_id"] == "big-photo"
    moving = {"sticker": {"file_id": "tgs", "is_animated": True, "thumbnail": {"file_id": "tgs-thumb"}}}
    assert content.classify(moving)[1]["file_id"] == "tgs-thumb"
    forwarded = {"text": "ciao", "media_group_id": "g1",
                 "forward_origin": {"type": "user", "sender_user": {"first_name": "Mario", "last_name": "Rossi"}}}
    assert content.classify(forwarded) == ("text", {"group": "g1", "forwarded": "Mario Rossi"})
    shown = {"text": "ciao", "forward_origin": {"type": "user", "sender_user": {"id": 333, "first_name": "Mia"}}}
    assert content.classify(shown)[1]["forwarded"] == "Mia, Telegram id 333"


def test_formatting_comes_back_as_markdown():
    def md(text: str, *entities: dict) -> str:
        return content.to_markdown(text, list(entities))

    assert md("ciao mondo ", {"type": "bold", "offset": 5, "length": 6}) == "ciao **mondo** "
    assert md("😀 guarda la doc qui", {"type": "text_link", "offset": 13, "length": 3, "url": "https://ex.com/a (b)"}) \
        == "😀 guarda la [doc](https://ex.com/a%20%28b%29) qui"
    assert md("molto importante", {"type": "bold", "offset": 0, "length": 16},
              {"type": "italic", "offset": 6, "length": 10}) == "**molto _importante_**"
    assert md("usa x = `y`", {"type": "code", "offset": 4, "length": 7}) == "usa `` x = `y` ``"
    assert md("vedi:\nprint(1)\nfine", {"type": "pre", "offset": 6, "length": 8, "language": "python"}) == \
        "vedi:\n```python\nprint(1)\n```\nfine"
    assert md("riga uno\nriga due", {"type": "blockquote", "offset": 0, "length": 17}) == "> riga uno\n> riga due"
    assert md("https://a.it", {"type": "text_link", "offset": 0, "length": 12, "url": "https://a.it"}) == "https://a.it"
    assert md("ciao @mario", {"type": "mention", "offset": 5, "length": 6}) == "ciao @mario"


def test_received_text_and_replies_keep_their_formatting():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        update = _message(5, text="leggi qui", entities=[{"type": "text_link", "offset": 6, "length": 3, "url": "https://x.it"}],
                          reply_to_message={"message_id": 2, "from": {"is_bot": True}, "caption": "foto vecchia",
                                            "caption_entities": [{"type": "bold", "offset": 0, "length": 4}]})
        store.record_inbound(conn, update, owner.id)
        queued = store.claim_next(conn)
        assert queued.text == "leggi [qui](https://x.it)" and queued.payload["reply"]["text"] == "**foto** vecchia"


def test_file_names_from_senders_stay_inside_their_folder():
    assert content.safe_name("../../etc/passwd") == "passwd"
    assert content.safe_name("..\\..\\Windows\\evil.exe") == "evil.exe"
    assert content.safe_name("fattura marzo (1).pdf") == "fattura_marzo_1_.pdf"
    assert content.safe_name("..") == "file"


def test_content_is_written_out_for_claude():
    assert content.describe("location", {"lat": 45.4, "lon": 9.1, "live": True}, "") == \
        "[live location 45.4, 9.1: https://maps.google.com/?q=45.4,9.1]"
    assert content.describe("contact", {"name": "Sara Neri", "phone": "+39 333"}, "") == "[contact Sara Neri, +39 333]"
    assert content.describe("document", {"name": "big.zip", "size": 30 * 1048576}, "eccolo").startswith(
        "[file «big.zip», 30.0 MB, which could not be downloaded")
    assert content.describe("voice", {}, "", heard="arrivo tra dieci minuti") == "arrivo tra dieci minuti"
    assert content.describe("text", {"forwarded": "Mario", "edited": True}, "domani", telegram_id=9) == \
        "[edited their message #9, which now reads] [forwarded from Mario] domani"


def test_files_are_saved_in_the_inbox_and_read_once():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp), "Bella foto!", None, "Ok")
        store.record_inbound(conn, _message(5, photo=[{"file_id": "p1"}], caption="il mio cane"), owner.id)
        worker._handle(conn, store.claim_next(conn))
        [saved] = list((Path(tmp) / "inbox").rglob("5.jpg"))
        assert saved.read_bytes() == b"data of p1"
        assert f"[photo, saved at {saved} (file #1)] il mio cane" in runner.prompts[-1]

        store.record_inbound(conn, _message(6, document={"file_id": "d1", "file_name": "../contratto.pdf"}), owner.id)
        worker._handle(conn, store.claim_next(conn))
        worker._handle(conn, store.claim_next(conn))
        assert api.fetched.count("d1") == 1 and list((Path(tmp) / "inbox").rglob("6-contratto.pdf"))


def test_an_album_is_one_turn_and_a_moving_live_location_is_no_turn():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = worker_setup(Path(tmp), "Tre foto ricevute.")
        for update_id, file_id in ((5, "a"), (6, "b"), (7, "c")):
            store.record_inbound(conn, _message(update_id, photo=[{"file_id": file_id}], media_group_id="g"), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert len(runner.prompts) == 1 and runner.prompts[0].count("[photo, saved at") == 3

        live = {"update_id": 8, "edited_message": _message(8, location={"latitude": 1, "longitude": 2})["message"]}
        assert store.record_inbound(conn, live, owner.id) is None


def test_a_voice_note_that_cannot_be_heard_is_set_aside():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = worker_setup(Path(tmp))
        store.record_inbound(conn, _message(5, voice={"file_id": "v1"}), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert runner.prompts == [] and "vocale" in api.to(OWNER)[-1]


def test_a_rich_message_is_read_as_markdown():
    rich = {"blocks": [
        {"type": "heading", "size": 2, "text": ["Ricerca approfondita su ", {"type": "mention", "text": "@grafana",
                                                                             "username": "grafana"}]},
        {"type": "paragraph", "text": ["Guarda ", {"type": "bold", "text": "bene"}, " il ",
                                       {"type": "url", "text": "sito", "url": "https://example.com"}, " ",
                                       {"type": "custom_emoji", "custom_emoji_id": "1", "alternative_text": "🔥"}]},
        {"type": "list", "items": [{"label": "1.", "blocks": [{"type": "paragraph", "text": "primo"}]},
                                   {"label": "•", "has_checkbox": True, "is_checked": True,
                                    "blocks": [{"type": "paragraph", "text": "fatto"}]}]},
        {"type": "table", "cells": [[{"text": "Nome", "is_header": True}, {"text": "Ruolo"}], [{"text": "Mia"}, {}]]},
        {"type": "photo", "photo": [{"file_id": "p"}], "caption": {"text": "la sede"}},
        {"type": "thinking", "text": "..."},
    ]}
    assert content.classify({"rich_message": rich})[0] == "text"
    assert content.text_of({"rich_message": rich}) == (
        "## Ricerca approfondita su @grafana\n\nGuarda **bene** il [sito](https://example.com) 🔥\n\n"
        "1. primo\n• [x] fatto\n\n| Nome | Ruolo |\n| Mia |  |\n\n[photo inside the message, not saved: la sede]"
    )
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, _ = worker_setup(Path(tmp), "Ci guardo.")
        store.record_inbound(conn, _message(5, rich_message=rich), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert "## Ricerca approfondita su @grafana" in runner.prompts[-1] and "cannot be opened" not in runner.prompts[-1]


def test_a_video_reaches_claude_as_frames_spread_over_it():
    if not shutil.which("ffmpeg"):
        print("skipped: no ffmpeg")
        return
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2.3:size=320x240:rate=12",
                        "-pix_fmt", "yuv420p", str(clip)], check=True)
        width = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0",
                                str(content.frames(clip))], capture_output=True, text=True).stdout.strip()
        assert width == str(320 * content.FRAMES) and content.frames(Path(tmp) / "nothing.mp4") is None

        conn, owner, worker, runner, api = worker_setup(Path(tmp), "Bello.", "Ok.")
        fetch = api.fetch
        api.fetch = lambda file_id: (clip.read_bytes(), "videos/clip.mp4") if file_id == "v1" else fetch(file_id)
        store.record_inbound(conn, _message(5, animation={"file_id": "v1", "thumbnail": {"file_id": "t1"}}), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert "4 frames spread over it, left to right, in one image at" in runner.prompts[-1] and "t1" not in api.fetched
        store.record_inbound(conn, _message(6, animation={"file_id": "g2", "thumbnail": {"file_id": "t2"}}), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert "preview image at" in runner.prompts[-1] and "t2" in api.fetched


def test_zoe_shows_at_once_that_she_listens_or_downloads():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp), "Ricevuto.", "Ricevuto.")
        shown, fetch = [], api.fetch

        class Ears:
            def transcribe(self, audio, language, glossary=None):
                shown.append(api.to(OWNER)[-1])
                return "arrivo tra dieci minuti"

        worker._transcriber = Ears()
        api.fetch = lambda file_id: shown.append(api.to(OWNER)[-1]) or fetch(file_id)
        store.record_inbound(conn, _message(5, voice={"file_id": "v1"}), owner.id)
        worker._handle(conn, store.claim_next(conn))
        store.record_inbound(conn, _message(6, photo=[{"file_id": "p1"}]), owner.id)
        worker._handle(conn, store.claim_next(conn))
        assert [s.split("</b>")[0] for s in shown] == ["<b>Ascolto…", "<b>Ascolto…", "<b>Scarico…"]
        assert api.to(OWNER) == ["Ricevuto.", "Ricevuto."]


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
