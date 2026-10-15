"""Checks for plans: timing rules, runs as turns, wiring into memory, and the menu."""

import sqlite3
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeAPI, button_data, message, press, shown, targets, worker_setup as _setup
from zoe import access, planner, store
from zoe.brain import Brain
from zoe.ingest import Poller
from zoe.menu import Menu
from zoe.worker import Worker

TUESDAY = datetime(2026, 10, 13, 14, 5)


def _run_next(conn: sqlite3.Connection, worker: Worker) -> None:
    worker._handle(conn, store.claim_next(conn))


def test_cron_rules_find_their_next_time():
    def after(rule: str) -> datetime:
        return planner.Cron(rule).next_after(TUESDAY)

    assert after("0 8 * * *") == datetime(2026, 10, 14, 8, 0)
    assert after("*/15 * * * *") == datetime(2026, 10, 13, 14, 15)
    assert after("0 10 * * 1") == datetime(2026, 10, 19, 10, 0)
    assert after("30 21 * * 0,6") == datetime(2026, 10, 17, 21, 30)
    assert after("0 10 1 * *") == datetime(2026, 11, 1, 10, 0)
    assert after("0 9 13 * 5") == datetime(2026, 10, 16, 9, 0)
    for wrong in ("0 8 * *", "61 * * * *", "0 9 31 2 *", "a b c d e"):
        try:
            after(wrong)
        except ValueError:
            continue
        raise AssertionError(wrong)


def test_a_time_is_a_date_or_a_cron_rule():
    assert planner.parse_when("2026-10-14 09:00", TUESDAY) == (datetime(2026, 10, 14, 9, 0).timestamp(), None)
    assert planner.parse_when(" 0 8  * * * ", TUESDAY) == (datetime(2026, 10, 14, 8, 0).timestamp(), "0 8 * * *")
    try:
        planner.parse_when("tomorrow morning", TUESDAY)
    except planner.PlanError as exc:
        assert str(exc) == "plan_bad_time"
    else:
        raise AssertionError("accepted")


def test_due_plans_are_queued_once_and_repeating_ones_move_on():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = _setup(Path(tmp))
        brain = Brain(conn, owner.id)
        once = planner.create(conn, brain, owner, OWNER, "2026-10-13 15:00", "Chiedi com'è andata", [], TUESDAY)
        daily = planner.create(conn, brain, owner, OWNER, "0 8 * * *", "Report del mattino", [], TUESDAY)

        assert planner.fire_due(conn, datetime(2026, 10, 13, 14, 59).timestamp()) == 0
        assert planner.fire_due(conn, datetime(2026, 10, 14, 8, 0).timestamp()) == 2
        queued = [store.claim_next(conn)]
        store.mark(conn, queued[0].id, "answered")
        queued.append(store.claim_next(conn))
        assert [(m.kind, m.payload["plan"]) for m in queued] == [("plan", once), ("plan", daily)]
        assert [p.id for p in planner.pending(conn, owner.id)] == [daily]
        assert planner.pending(conn, owner.id)[0].due_at == datetime(2026, 10, 15, 8, 0).timestamp()


def test_members_plan_at_most_hourly_and_only_their_own():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = _setup(Path(tmp))
        mia = access.create_person(conn, "Mia", "member", "it")
        brain = Brain(conn, mia.id)
        try:
            planner.create(conn, brain, mia, 333, "*/30 * * * *", "Controlla", [], TUESDAY)
        except planner.PlanError as exc:
            assert str(exc) == "plan_too_often"
        else:
            raise AssertionError("accepted")
        plan = planner.create(conn, brain, mia, 333, "0 9 * * *", "Buongiorno", [], TUESDAY)
        assert not planner.cancel(conn, owner.id, plan)
        assert planner.cancel(conn, mia.id, plan) and planner.pending(conn, mia.id) == []


def test_repeating_plans_keep_a_minimum_gap_even_for_owners():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, *_ = _setup(Path(tmp))
        brain = Brain(conn, owner.id)
        for rule in ("* * * * *", "*/5 * * * *", "0,5 9 * * *"):
            try:
                planner.create(conn, brain, owner, OWNER, rule, "Controlla", [], TUESDAY)
            except planner.PlanError as exc:
                assert str(exc) == "plan_too_often"
            else:
                raise AssertionError(rule)
        assert planner.create(conn, brain, owner, OWNER, "*/15 * * * *", "Controlla la coda", [], TUESDAY)


def test_a_receipt_shows_what_a_reply_planned_and_undoes_it_in_one_tap():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = _setup(Path(tmp), "Fatto.\n[[{k} plan: 0 8 * * 1 | Pesata del lunedì]]")
        store.enqueue(conn, OWNER, owner.id, "Ricordami la pesata ogni lunedì", "text")
        _run_next(conn, worker)
        [plan] = planner.pending(conn, owner.id)
        assert "⏰ In programma: <b>Pesata del lunedì</b>" in api.to(OWNER)[-1]
        assert button_data(api.last_buttons_to(OWNER)) == [f"r:unplan:{plan.id}"]
        assert api.last_buttons_to(OWNER)[0][0]["style"] == "danger"

        poller = Poller(worker._cfg, api, threading.Event())
        poller.process(conn, press(1, OWNER, f"r:unplan:{plan.id}"))
        assert planner.pending(conn, owner.id) == [] and api.edits[-1][3][0][0]["text"] == "↩️ Annullato"
        poller.process(conn, press(2, OWNER, f"r:unplan:{plan.id}"))
        assert api.notices[-1] == "Non c'è più niente da annullare."


def test_a_blocked_account_loses_its_plans_and_its_waiting_messages():
    with tempfile.TemporaryDirectory() as tmp:
        conn, *_ = _setup(Path(tmp))
        conn.execute("INSERT INTO persons (name, role, language, created_at) VALUES ('Mia', 'member', 'it', 0)")
        mia = access.get_person(conn, conn.execute("SELECT max(id) FROM persons").fetchone()[0])
        conn.execute("INSERT INTO accounts (telegram_user_id, person_id, status, created_at, updated_at) "
                     "VALUES (333, ?, 'approved', 0, 0)", (mia.id,))
        planner.create(conn, Brain(conn, mia.id), mia, 333, "0 9 * * *", "Buongiorno", [], TUESDAY)
        store.enqueue(conn, 333, mia.id, "ancora qui?", "text")
        assert access.revoke(conn, 333) is True
        assert planner.pending(conn, mia.id) == [] and store.claim_next(conn) is None


def test_a_plan_made_in_conversation_runs_with_what_it_was_wired_to():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _setup(
            Path(tmp),
            "Te lo chiedo domani 👍\n[[{k} plan: 2026-10-14 20:00 | Chiedi ad Alex com'è andata la camminata]]",
            "Allora, com'è andata la camminata al lago? 🌲",
        )
        store.enqueue(conn, OWNER, owner.id, "Domani vado a camminare al lago di Bracciano", "text")
        _run_next(conn, worker)
        assert api.to(OWNER)[-2].startswith("Te lo chiedo domani")
        assert api.to(OWNER)[-1].startswith("⏰ In programma: <b>Chiedi ad Alex")
        [plan] = planner.pending(conn, owner.id)
        assert plan.instruction == "Chiedi ad Alex com'è andata la camminata"
        assert any("Bracciano" in r.content for r in Brain(conn, owner.id).recall([plan.id]))

        planner.fire_due(conn, plan.due_at)
        _run_next(conn, worker)
        assert "It is time for something you planned" in runner.prompts[-1]
        assert "Bracciano" in runner.prompts[-1].split("Recent conversation:")[0]
        assert api.to(OWNER)[-1].startswith("Allora, com'è andata")
        history = store.recent_history(conn, OWNER)
        assert not any("Chiedi ad Alex" in turn["text"] for turn in history)


def test_a_run_with_nothing_to_say_sends_nothing_and_remembers_why():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _setup(Path(tmp), "[[{k} skip: ne abbiamo appena parlato]]")
        brain = Brain(conn, owner.id)
        plan = planner.create(conn, brain, owner, OWNER, "0 21 * * *", "Check-in serale", [], TUESDAY)
        planner.fire_due(conn, planner.pending(conn, owner.id)[0].due_at)
        _run_next(conn, worker)
        assert api.sent == []
        assert any("ne abbiamo appena parlato" in r.content for r in brain.recall([plan]))


def test_a_plan_reaches_the_person_even_when_its_turns_fail():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = _setup(Path(tmp), None, None)
        planner.create(conn, Brain(conn, owner.id), owner, OWNER, "2026-10-13 15:00", "Alle 15 entra Sara in turno", [])
        planner.fire_due(conn, datetime(2026, 10, 13, 15, 0).timestamp())
        _run_next(conn, worker)
        assert api.sent == []
        _run_next(conn, worker)
        assert api.to(OWNER) == ["⏰ Alle 15 entra Sara in turno"]
        assert store.recent_history(conn, OWNER)[-1]["text"] == "⏰ Alle 15 entra Sara in turno"


def test_a_reply_to_a_reminder_leads_back_to_the_plan():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, runner, api = _setup(
            Path(tmp),
            "Ok!\n[[{k} plan: 2026-10-13 15:00 | Avvisa Alex che entra Sara in turno al bar Aurora]]",
            "Alex, tra poco entra Sara in turno ⏰",
            "Di niente!",
        )
        store.enqueue(conn, OWNER, owner.id, "Ricordami alle 15 che Sara entra in turno al bar Aurora", "text")
        _run_next(conn, worker)
        planner.fire_due(conn, datetime(2026, 10, 13, 15, 0).timestamp())
        _run_next(conn, worker)
        reminder_id = len(api.sent) + 100

        update = message(900, OWNER, "fatto, grazie")
        update["message"]["reply_to_message"] = {"message_id": reminder_id, "from": {"is_bot": True}}
        store.record_inbound(conn, update, owner.id)
        _run_next(conn, worker)
        assert f"is replying to a message of yours #{reminder_id}" in runner.prompts[-1]
        assert "tra poco entra Sara in turno" in runner.prompts[-1].split("is replying")[1]
        assert "bar Aurora" in runner.prompts[-1].split("Recent conversation:")[0]


def test_a_time_that_cannot_be_scheduled_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = _setup(Path(tmp), "Fatto.\n[[{k} plan: domani mattina | Ricordami il medico]]")
        store.enqueue(conn, OWNER, owner.id, "Ricordami il medico", "text")
        _run_next(conn, worker)
        assert "non è un orario valido" in api.to(OWNER)[-1]
        assert planner.pending(conn, owner.id) == []


def test_the_menu_lists_plans_and_deletes_one():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, *_ = _setup(Path(tmp))
        plan = planner.create(conn, Brain(conn, owner.id), owner, OWNER, "0 8 * * 1", "Pesata del lunedì", [], TUESDAY)
        menu = Menu(worker._cfg, FakeAPI())
        home = menu.answer(conn, owner, {"view": "home"})
        assert [row["count"] for row in home["blocks"][0]["items"] if row["to"] == "plans"] == [1]

        [[row]] = [block["items"] for block in menu.answer(conn, owner, {"view": "plans"})["blocks"]]
        assert (row["label"], row["detail"], row["to"]) == ("🔁 Pesata del lunedì", "lun 19/10 08:00", f"plan:{plan}")
        view = menu.answer(conn, owner, {"view": f"plan:{plan}"})
        assert "🔁 Si ripete: 0 8 * * 1" in shown(view) and targets(view) == [f"unplan:{plan}"]
        view = menu.answer(conn, owner, {"action": f"unplan:{plan}"})
        assert view["notice"] == "Eliminato." and planner.pending(conn, owner.id) == []
        assert view["blocks"] == [{"text": "Non c'è niente in programma."}]


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
