"""Checks for what came with the move from the old bridge: imported history and glossary,
folders shared with a member, and pronunciations for the voice."""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fakes import OWNER, worker_setup
from import_supercharged import import_glossary, import_messages
from zoe import access, files, store, vocabulary
from zoe.brain import Brain
from zoe.speech import pronounce


def _old_history(path: Path) -> None:
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, message_id INTEGER, chat_id TEXT, user_id TEXT, "
                "text TEXT, media_type TEXT, caption TEXT, date INTEGER, is_outgoing INTEGER)")
    old.executemany("INSERT INTO messages (message_id, chat_id, user_id, text, media_type, caption, date, is_outgoing) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                        (10, str(OWNER), str(OWNER), "Ciao Zoe", None, None, 1000, 0),
                        (11, str(OWNER), None, "Ciao Alex!", None, None, 1001, 1),
                        (-5, str(OWNER), None, "[report del mattino] Buongiorno", None, None, 1002, 1),
                        (12, str(OWNER), str(OWNER), None, "photo", "la ricevuta", 1003, 0),
                        (3, "999", "999", "chi sono?", None, None, 1004, 0),
                    ])
    old.commit()
    old.close()


def test_the_old_history_and_glossary_come_in_once():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = worker_setup(Path(tmp))
        _old_history(Path(tmp) / "old.db")
        assert import_messages(conn, Path(tmp) / "old.db") == (4, 1)
        assert import_messages(conn, Path(tmp) / "old.db") == (0, 5)
        history = store.recent_history(conn, OWNER)
        assert [turn["text"] for turn in history][:4] == ["Ciao Zoe", "Ciao Alex!", "[report del mattino] Buongiorno",
                                                          "la ricevuta"]
        assert history[2]["telegram_id"] is None and store.find_message(conn, OWNER, 10)["text"] == "Ciao Zoe"

        (Path(tmp) / "glossario.txt").write_text("Grafana, OpenShift,  Kubernetes\n", encoding="utf-8")
        assert import_glossary(conn, Path(tmp) / "glossario.txt") == 3
        assert {"Grafana", "OpenShift", "Kubernetes"} <= {r.content for r in Brain(conn, owner.id).top(vocabulary.TERM)}


def test_a_member_reads_only_the_folders_shared_with_them():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as work:
        conn, owner, worker, *_ = worker_setup(Path(tmp))
        mia = access.create_person(conn, "Mia", "member", "it")
        notes = Path(work) / "mia" / "notes"
        notes.mkdir(parents=True)
        (notes / "NOTES.md").write_text("appunti", encoding="utf-8")

        for refused in (Path(tmp), Path(tmp) / "people", Path(Path(tmp).anchor), Path.home(), Path(work) / "manca"):
            assert access.share(conn, mia, refused, Path(tmp)) is None, refused
        assert access.share(conn, owner, notes, Path(tmp)) is None
        mia = access.share(conn, mia, notes, Path(tmp))
        args = access.turn_profile(mia, worker._cfg).args
        assert args[args.index("--add-dir") + 1] == str(notes.resolve())

        home = access.turn_profile(mia, worker._cfg).cwd
        assert files.resolve(conn, mia, str(notes / "NOTES.md"), home) == (None, notes / "NOTES.md")
        assert files.resolve(conn, mia, str(Path(work) / "altro.txt"), home) == (None, None)
        mia = access.unshare(conn, mia, notes)
        assert mia.folders == () and "--add-dir" not in access.turn_profile(mia, worker._cfg).args


def test_the_voice_says_words_the_way_the_dictionary_spells_them():
    with tempfile.TemporaryDirectory() as tmp:
        dictionary = Path(tmp) / "pronuncia.txt"
        dictionary.write_text("# Parola = come va detta\nSQL = esse cu elle\nNginx = engine ics\n", encoding="utf-8")
        assert pronounce("Nginx e SQL", dictionary) == "engine ics e esse cu elle"
        assert pronounce("ciao", Path(tmp) / "manca.txt") == "ciao"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
