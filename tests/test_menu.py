"""Checks for the menu: how it opens, what owners and members see, and what it lets owners do."""

import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeAPI, message, targets
from zoe import access, claude_runner, health, store
from zoe.config import Config
from zoe.db import apply_schema
from zoe.ingest import Poller
from zoe.menu import Menu
from zoe.usage import Limit, Usage

OWNER = 111
URL = "https://zoe.example.org/zoe/app/"


def _setup(home: Path, url: str = URL) -> tuple[sqlite3.Connection, Poller, FakeAPI, Menu]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    access.bootstrap_owner(conn, frozenset({OWNER}), "Alex", "it")
    cfg = Config(
        bot_token="", owner_ids=frozenset({OWNER}), data_dir=home, db_path=home / "zoe.db",
        vault_path=home / "v.db", vault_key_path=home / "v.key", workdir=home, menu_url=url,
    )
    api = FakeAPI()
    return conn, Poller(cfg, api, threading.Event()), api, Menu(cfg, api, Usage(cfg, lambda: []))


def _ask(conn: sqlite3.Connection, menu: Menu, user_id: int, **request: str) -> dict:
    return menu.answer(conn, access.resolve(conn, user_id).person, request)


def _member(conn: sqlite3.Connection, poller: Poller, user_id: int, name: str) -> int:
    poller.process(conn, message(user_id, user_id, "hi", first_name=name))
    return access.approve(conn, access.pending(conn)[0].request_code, name, "it")[0].id


def _opens(api: FakeAPI, chat_id: int) -> str:
    [[button]] = api.last_buttons_to(chat_id)
    return button["web_app"]["url"]


def test_the_menu_opens_from_a_button_on_its_view():
    with tempfile.TemporaryDirectory() as tmp:
        conn, poller, api, _ = _setup(Path(tmp))
        poller.process(conn, message(1, OWNER, "/menu"))
        assert _opens(api, OWNER) == URL and "Menu accanto al campo" in api.to(OWNER)[-1]
        poller.process(conn, message(2, OWNER, "/stato"))
        assert _opens(api, OWNER) == URL + "?view=status"

        _member(conn, poller, 333, "Mia")
        poller.process(conn, message(4, 333, "/start"))
        assert _opens(api, 333) == URL
        poller.process(conn, message(5, 333, "/stato"))
        assert store.claim_next(conn).text == "/stato"

    with tempfile.TemporaryDirectory() as tmp:
        conn, poller, api, _ = _setup(Path(tmp), url="")
        poller.process(conn, message(1, OWNER, "/menu"))
        assert "ZOE_MENU_URL" in api.to(OWNER)[-1] and api.last_buttons_to(OWNER) is None


def test_members_see_their_own_things_and_owners_everything():
    with tempfile.TemporaryDirectory() as tmp:
        conn, poller, api, menu = _setup(Path(tmp))
        home = _ask(conn, menu, OWNER, view="home")
        assert targets(home) == ["minds", "people", "improve", "plans", "known", "lang", "status", "undo"]
        assert home["back"] == "" and home["brand"] == "Zoe" and home["title"] == "Cosa vuoi fare?"

        _member(conn, poller, 333, "Mia")
        assert targets(_ask(conn, menu, 333, view="home")) == ["plans", "known", "lang"]
        for request in ({"view": "people"}, {"view": "status"}, {"action": "deny:x"}, {"action": "undo"}):
            refused = _ask(conn, menu, 333, **request)
            assert refused["notice"] == "Questa funzione è riservata al proprietario." and refused["at"] == "home"


def test_the_language_changes_at_once():
    with tempfile.TemporaryDirectory() as tmp:
        conn, _, _, menu = _setup(Path(tmp))
        assert targets(_ask(conn, menu, OWNER, view="lang")) == ["lang:it", "lang:en"]
        view = _ask(conn, menu, OWNER, action="lang:en")
        assert access.resolve(conn, OWNER).person.language == "en"
        assert view["title"] == "What would you like to do?" and view["notice"] == "From now on I'll talk to you in English."


def test_a_second_account_is_linked_to_a_known_person_and_never_to_an_owner():
    with tempfile.TemporaryDirectory() as tmp:
        conn, poller, api, menu = _setup(Path(tmp))
        mia = _member(conn, poller, 333, "Mia")
        poller.process(conn, message(9, 444, "hi again", first_name="Mia work"))
        code = access.pending(conn)[0].request_code
        owner = access.resolve(conn, OWNER).person

        assert f"request:{code}" in targets(_ask(conn, menu, OWNER, view="people"))
        assert targets(_ask(conn, menu, OWNER, view=f"request:{code}")) == [f"approve:{code}", f"link:{code}", f"deny:{code}"]
        assert targets(_ask(conn, menu, OWNER, view=f"link:{code}")) == [f"link:{code}:{mia}"]
        _ask(conn, menu, OWNER, action=f"link:{code}:{owner.id}")
        assert access.resolve(conn, 444).status == "pending"
        view = _ask(conn, menu, OWNER, action=f"link:{code}:{mia}")
        assert access.resolve(conn, 444).person.id == mia and view["notice"] == "Fatto: l'account ora appartiene a Mia."
        assert api.to(444)[-1] == "Ciao Mia, ora puoi scrivermi." and view["at"] == f"people:{mia}"


def _sections(view: dict) -> list[dict]:
    return next(block["sections"] for block in view["blocks"] if "sections" in block)


def test_people_open_one_at_a_time_the_owner_first():
    with tempfile.TemporaryDirectory() as tmp:
        conn, poller, _, menu = _setup(Path(tmp))
        mia = _member(conn, poller, 333, "Mia")
        owner, mine = _sections(_ask(conn, menu, OWNER, view="people"))
        assert (owner["title"], owner["detail"], owner["open"], mine["open"]) == ("Alex", "proprietario, 1 account", True, False)
        assert targets(owner) == [] and "ZOE_OWNER_IDS" in str(owner["blocks"])
        assert targets(mine) == ["revoke:333", f"observe:{mia}", f"remove:{mia}"]
        assert [s["open"] for s in _sections(_ask(conn, menu, OWNER, view=f"people:{mia}"))] == [False, True]


def test_a_members_accounts_are_added_and_removed_but_never_an_owners():
    with tempfile.TemporaryDirectory() as tmp:
        conn, poller, api, menu = _setup(Path(tmp))
        mia = _member(conn, poller, 333, "Mia")
        owner = access.resolve(conn, OWNER).person

        view = _ask(conn, menu, OWNER, action=f"add:{mia}:444")
        assert access.resolve(conn, 444).person.id == mia and api.to(444)[-1] == "Ciao Mia, ora puoi scrivermi."
        assert view["at"] == f"people:{mia}" and _sections(view)[1]["detail"] == "membro, 2 account"
        for refused in (f"add:{mia}:{OWNER}", f"add:{owner.id}:555", f"add:{mia}:@mia"):
            assert _ask(conn, menu, OWNER, action=refused)["notice"].startswith("Non ho aggiunto")
        assert access.resolve(conn, OWNER).person.role == "owner" and access.resolve(conn, 555).status == "unknown"

        [row] = [r for r in _sections(view)[1]["blocks"][0]["items"] if r["act"] == "revoke:444"]
        assert (row["side"], row["confirm"]) == ("Togli", "Tolgo l'account 444? Non potrà più scrivermi.")
        _ask(conn, menu, OWNER, action="revoke:444")
        assert access.resolve(conn, 444).status == "blocked" and access.resolve(conn, 333).status == "approved"
        assert _ask(conn, menu, OWNER, action=f"revoke:{OWNER}")["notice"] == "Non posso bloccare un account del proprietario."
        assert _ask(conn, menu, OWNER, action=f"remove:{owner.id}")["notice"] == "Non posso bloccare un account del proprietario."
        assert access.resolve(conn, OWNER).status == "approved"

        view = _ask(conn, menu, OWNER, action=f"remove:{mia}")
        assert view["notice"] == "Mia non può più scrivermi. Quello che so resta, se la riaggiungi."
        assert access.resolve(conn, 333).status == "blocked" and access.get_person(conn, mia) is not None
        assert [s["title"] for s in _sections(view)] == ["Alex"]


def test_undo_view_when_there_is_nothing_to_undo():
    with tempfile.TemporaryDirectory() as tmp:
        conn, _, _, menu = _setup(Path(tmp))
        view = _ask(conn, menu, OWNER, view="undo")
        assert view["blocks"] == [{"text": "Non ci sono modifiche da annullare."}] and targets(view) == []


def test_owners_see_how_zoe_is_doing_and_it_keeps_itself_up_to_date():
    with tempfile.TemporaryDirectory() as tmp:
        conn, _, _, menu = _setup(Path(tmp))
        started = time.time() - 3 * 3600 - 120
        store.set_state(conn, health.STARTED, str(started))
        view = _ask(conn, menu, OWNER, view="status")
        cards = {card["label"]: card for card in view["blocks"][0]["stats"]}
        claude = cards["Claude"]
        assert (claude["value"], claude["detail"], claude["tone"], claude["wide"]) == (
            "Disponibile", "claude-opus-5-5", "accent", True)
        assert (cards["Attiva da"]["value"], cards["Attiva da"]["since"]) == ("{elapsed}", started)
        assert cards["Ultimo ripasso"]["value"] == "mai" and "since" not in cards["Ultimo ripasso"]
        assert (cards["Turni in corso"]["value"], cards["Turni in corso"]["detail"]) == ("0", "in coda: 0")
        assert view["title"] == "Stato" and view["at"] == "status" and abs(view["now"] - time.time()) < 5

        store.set_state(conn, claude_runner.DOWN, "Not logged in <now>")
        claude = _ask(conn, menu, OWNER, view="status")["blocks"][0]["stats"][0]
        assert (claude["value"], claude["detail"], claude["tone"]) == ("Non disponibile", "Not logged in <now>", "danger")


def test_the_status_shows_claudes_limits_once_read():
    with tempfile.TemporaryDirectory() as tmp:
        conn, _, api, plain = _setup(Path(tmp))
        soon = time.time() + 3600
        limits = [Limit("Current session", 42.0, soon), Limit("Current week (all models)", 91.0, None),
                  Limit("Current week (Fable)", 104.0, time.time() + 3 * 86400)]
        menu = Menu(plain._cfg, api, Usage(plain._cfg, lambda: limits))
        assert len(_ask(conn, menu, OWNER, view="status")["blocks"]) == 1
        deadline = time.time() + 5
        while len(blocks := _ask(conn, menu, OWNER, view="status")["blocks"]) == 1 and time.time() < deadline:
            time.sleep(0.01)
        heading, card = blocks[1:]
        assert heading == {"heading": "Limiti di Claude"}
        session, week, fable = card["meters"]
        assert (session["label"], session["value"], session["fill"], session["tone"]) == ("Sessione", "42%", 42.0, "")
        assert re.fullmatch(r"si azzera ([a-z]{3} )?\d\d:\d\d", session["detail"])
        assert (week["label"], week["tone"], week["detail"]) == ("Settimana", "danger", "")
        assert (fable["label"], fable["value"], fable["fill"], fable["tone"]) == ("Settimana Fable", "104%", 100, "danger")
        assert re.fullmatch(r"si azzera [a-z]{3} \d\d:\d\d", fable["detail"])


def test_text_from_people_and_memories_is_shown_as_it_is():
    with tempfile.TemporaryDirectory() as tmp:
        conn, poller, _, menu = _setup(Path(tmp))
        mia = _member(conn, poller, 333, "Mia <b>&</b>")
        assert _sections(_ask(conn, menu, OWNER, view="people"))[1]["title"] == "Mia <b>&</b>" and mia


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
