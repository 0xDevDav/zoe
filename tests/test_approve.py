"""Checks for the owner's yes on Telegram: which runs ask, the question with its buttons, the tap, no answer."""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, button_data, message, press, worker_setup
from zoe import access, approve, say, store
from zoe import worker as worker_module
from zoe.claude_runner import Result
from zoe.db import connect
from zoe.ingest import Poller

PUSH = {"command": "git push origin main", "description": "Publish the site"}


def _settings(args: tuple[str, ...]) -> dict:
    return json.loads(args[args.index("--settings") + 1])


def test_owner_runs_ask_by_the_owners_rules_and_members_never_do():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        conn, owner, worker, runner, _ = worker_setup(home, "Fatto.", "Fatto.")
        (home / approve.RULES).write_text('["Bash(git push *)", "mcp__whatsapp__send_message"]', encoding="utf-8")
        store.enqueue(conn, OWNER, owner.id, "pubblica", "text")
        worker._handle(conn, store.claim_next(conn))
        settings, env = _settings(runner.profiles[0].args), runner.profiles[0].env
        assert settings["permissions"]["ask"] == ["Bash(git push *)", "mcp__whatsapp__send_message"]
        [hook] = settings["hooks"]["PermissionRequest"][0]["hooks"]
        assert hook["command"].endswith("-m zoe.approve") and hook["timeout"] > approve.WAIT
        assert env[approve.CHAT] == str(OWNER) and env[approve.MIND] == ""

        mia = access.create_person(conn, "Mia", "member", "it")
        store.enqueue(conn, 333, mia.id, "ciao", "text")
        worker._handle(conn, store.claim_next(conn))
        assert "--settings" not in runner.profiles[1].args and approve.CHAT not in runner.profiles[1].env

        (home / approve.RULES).write_text('{"not": "a list"}', encoding="utf-8")
        assert approve.rules(home) == []


def test_the_owner_says_yes_or_no_with_a_tap():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp), on_disk=True)
        poller = Poller(worker._cfg, api, threading.Event())
        for verdict, expected, shown in (("allow", "allowed", "✅ Consentito"), ("deny", "denied", "❌ Negato")):
            outcome = {}

            def hook() -> None:
                own = connect(Path(tmp) / "zoe.db")
                try:
                    outcome["state"] = approve.wait(own, OWNER, "Sito", "Bash", PUSH, timeout=10, poll=0.05)
                finally:
                    own.close()

            waiting = threading.Thread(target=hook)
            waiting.start()
            while not conn.execute("SELECT 1 FROM approvals WHERE state = 'waiting'").fetchone():
                time.sleep(0.02)
            approve.deliver(conn, api)
            question = api.to(OWNER)[-1]
            assert "Posso farlo?" in question and "<code>git push origin main</code>" in question
            assert "Publish the site" in question and "«Sito»" in question and "entro 10 minuti" in question
            [number] = {data.rsplit(":", 1)[1] for data in button_data(api.last_buttons_to(OWNER))}
            assert button_data(api.last_buttons_to(OWNER)) == [f"a:allow:{number}", f"a:deny:{number}"]

            poller.process(conn, press(1, OWNER, f"a:{verdict}:{number}"))
            waiting.join()
            assert outcome["state"] == expected and api.edits[-1][2].endswith(shown) and not api.edits[-1][3]
            poller.process(conn, press(2, OWNER, f"a:allow:{number}"))
            assert api.notices[-1] == "Questa richiesta è già chiusa."
        conn.close()


def test_without_an_answer_nothing_is_done_and_the_question_closes():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        assert approve.wait(conn, OWNER, "", "mcp__whatsapp__send_message", {"recipient": "39333", "message": "Ciao"},
                            timeout=0.1, poll=0.02) == "expired"
        approve.deliver(conn, api)
        assert "Nessuna risposta" not in "".join(api.to(OWNER))
        conn.execute("UPDATE approvals SET message_id = 900, settled = 0")
        approve.deliver(conn, api)
        assert api.edits[-1][1] == 900 and api.edits[-1][2].endswith("⌛ Nessuna risposta: non l'ho fatto")
        assert "<b>whatsapp</b>: send_message\nrecipient: 39333\nmessage: Ciao" in api.edits[-1][2]

        # A question whose hook is gone closes on its own.
        conn.execute("INSERT INTO approvals (chat_id, tool, args, created_at) VALUES (?, 'Bash', '{}', ?)",
                     (OWNER, time.time() - approve.WAIT - 60))
        approve.deliver(conn, api)
        assert conn.execute("SELECT state FROM approvals ORDER BY id DESC").fetchone()[0] == "expired"


def test_a_member_cannot_answer_and_without_a_chat_the_answer_is_no():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        conn.execute("INSERT INTO approvals (chat_id, tool, args, created_at, message_id) VALUES (?, 'Bash', '{}', ?, 5)",
                     (OWNER, time.time()))
        access.add_account(conn, 333, access.create_person(conn, "Mia", "member", "it").id)
        Poller(worker._cfg, api, threading.Event()).process(conn, press(1, 333, "a:allow:1"))
        assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "waiting"

        allowed, why = approve.decide({"tool_name": "Bash", "tool_input": PUSH}, worker._cfg, {})
        assert not allowed and "nobody can be asked now" in why


def _with_buttons(api, chat_id: int) -> list[tuple[int, list]]:
    """The messages of a chat still there with buttons, oldest first, as they read after their edits."""
    shown = []
    for (cid, _, buttons), number in zip(api.sent, api.sent_ids):
        for edited_chat, edited, _, new_buttons in api.edits:
            if (edited_chat, edited) == (cid, number):
                buttons = new_buttons
        if cid == chat_id and buttons and (cid, number) not in api.deleted:
            shown.append((number, buttons))
    return shown


def _ask(conn) -> int:
    return conn.execute("INSERT INTO approvals (chat_id, asker, tool, args, created_at) VALUES (?, '', 'Bash', ?, ?)",
                        (OWNER, json.dumps(PUSH), time.time())).lastrowid


def test_a_waiting_question_moves_once_below_what_reaches_the_chat_after_it():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        number = _ask(conn)
        approve.deliver(conn, api)
        first = api.sent_ids[-1]

        # A notice in Zoe's name and a message of the owner come after the question.
        say.queue(conn, Path(tmp), OWNER, "📬 Una mail nuova da Mia", origin="mail")
        say.queue(conn, Path(tmp), OWNER, "📬 Un'altra mail da Mia", origin="mail")
        say.deliver(conn, api)
        api._next_id += 1
        Poller(worker._cfg, api, threading.Event()).process(conn, message(api._next_id, OWNER, "ci sei?"))
        approve.deliver(conn, api)
        assert [n for n, _ in _with_buttons(api, OWNER)] == [first], "it waits for the chat to settle"

        approve.deliver(conn, api, now=time.time() + approve.SETTLE)
        [(moved, buttons)] = _with_buttons(api, OWNER)
        assert moved == api.sent_ids[-1] and (OWNER, first) in api.deleted
        assert button_data(buttons) == [f"a:allow:{number}", f"a:deny:{number}"]
        assert "Posso farlo?" in api.to(OWNER)[-1] and "<code>git push origin main</code>" in api.to(OWNER)[-1]
        assert conn.execute("SELECT message_id FROM approvals WHERE id = ?", (number,)).fetchone()[0] == moved

        sent = len(api.sent)
        approve.deliver(conn, api, now=time.time() + 60)
        assert len(api.sent) == sent, "nothing new came, so it stays"

        Poller(worker._cfg, api, threading.Event()).process(conn, press(9, OWNER, f"a:allow:{number}", moved))
        assert conn.execute("SELECT state FROM approvals WHERE id = ?", (number,)).fetchone()[0] == "allowed"
        assert api.edits[-1][1] == moved and api.edits[-1][2].endswith("✅ Consentito") and not _with_buttons(api, OWNER)


def test_a_question_telegram_does_not_delete_loses_its_buttons_when_it_moves():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        _ask(conn)
        approve.deliver(conn, api)
        first = api.sent_ids[-1]
        say.queue(conn, Path(tmp), OWNER, "📬 Una mail nuova da Mia")
        say.deliver(conn, api)
        api.deletes_fail = True
        approve.deliver(conn, api, now=time.time() + approve.SETTLE)
        assert api.edited[first] == "⬇️ Richiesta spostata in fondo alla chat"
        assert [n for n, _ in _with_buttons(api, OWNER)] == [api.sent_ids[-1]]


def test_a_question_answered_while_it_moves_keeps_no_second_set_of_buttons():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        number = _ask(conn)
        approve.deliver(conn, api)
        say.queue(conn, Path(tmp), OWNER, "📬 Una mail nuova da Mia")
        say.deliver(conn, api)
        ask_again = api.send_message

        def answered_meanwhile(chat_id, text, **kwargs):
            sent = ask_again(chat_id, text, **kwargs)
            approve.answer(conn, api, owner, {"id": "tap", "data": f"a:deny:{number}"})
            return sent

        api.send_message = answered_meanwhile
        approve.deliver(conn, api, now=time.time() + approve.SETTLE)
        assert conn.execute("SELECT state FROM approvals WHERE id = ?", (number,)).fetchone()[0] == "denied"
        assert not _with_buttons(api, OWNER) and (OWNER, api.sent_ids[-1]) in api.deleted


class AsksDuringTheTurn:
    """While the turn works, a question for the owner reaches the chat."""

    def __init__(self, conn, api) -> None:
        self.conn, self.api = conn, api

    def run(self, prompt_text, profile, on_step=None, cancel=None, interrupt=None, follow_for=0.0):
        self.api.send_message(OWNER, "🔐 Posso farlo?")
        self.conn.execute("INSERT INTO approvals (chat_id, tool, args, created_at, message_id) VALUES (?, 'Bash', '{}', ?, ?)",
                          (OWNER, time.time(), self.api.sent_ids[-1]))
        deadline = time.time() + 5
        while sum("Al lavoro" in text for _, text, _ in self.api.sent) < 2 and time.time() < deadline:
            time.sleep(0.02)
        return Result(True, "Fatto ✅", False, False, 0, 1.0, session_id="s-1")


def test_the_answer_comes_after_the_question_asked_during_the_turn():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, worker, _, api = worker_setup(Path(tmp))
        worker_module.FOLLOWUP_POLL = 0.05
        worker._runner = AsksDuringTheTurn(conn, api)
        store.enqueue(conn, OWNER, owner.id, "pubblica", "text")
        worker._handle(conn, store.claim_next(conn))
        texts = [text for _, text, _ in api.sent]
        assert "Al lavoro" in texts[0] and texts[1] == "🔐 Posso farlo?" and "Al lavoro" in texts[2]
        assert (OWNER, api.sent_ids[0]) in api.deleted and api.to(OWNER)[-1] == "Fatto ✅"


def test_the_answer_takes_the_form_of_the_hook_that_asked():
    granted = json.loads(approve._decision("PermissionRequest", True))["hookSpecificOutput"]
    assert granted["decision"] == {"behavior": "allow"}
    refused = json.loads(approve._decision("PermissionRequest", False, "no"))["hookSpecificOutput"]
    assert refused["decision"] == {"behavior": "deny", "message": "no"}
    guard = json.loads(approve._decision("PreToolUse", True))["hookSpecificOutput"]
    assert guard == {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
    guard = json.loads(approve._decision("PreToolUse", False, "no"))["hookSpecificOutput"]
    assert (guard["permissionDecision"], guard["permissionDecisionReason"]) == ("deny", "no")


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
