"""Checks for people, accounts, access requests, and the powers of each role."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import access
from zoe.claude_runner import beyond
from zoe.config import Config
from zoe.db import apply_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return conn


def test_owner_with_two_accounts_is_one_person():
    conn = _conn()
    owner = access.bootstrap_owner(conn, frozenset({111, 222}), "Alex", "it")
    first, second = access.resolve(conn, 111), access.resolve(conn, 222)
    assert first.status == second.status == "approved"
    assert first.person.id == second.person.id == owner.id and first.person.role == "owner"
    assert access.bootstrap_owner(conn, frozenset({111, 222}), "Alex", "it").id == owner.id


def test_unknown_account_goes_through_approval():
    conn = _conn()
    access.bootstrap_owner(conn, frozenset({111}), "Alex", "it")
    assert access.resolve(conn, 333).status == "unknown"

    code = access.note_stranger(conn, {"id": 333, "first_name": "Mia", "username": "mia", "language_code": "en"})
    assert access.resolve(conn, 333).status == "pending"

    person, account = access.approve(conn, code, None, "it")
    assert (person.name, person.role, person.language) == ("Mia", "member", "en")
    assert account.telegram_user_id == 333
    assert access.resolve(conn, 333).person.id == person.id
    assert access.approve(conn, code, None, "it") is None


def test_second_account_links_to_the_same_person():
    conn = _conn()
    code = access.note_stranger(conn, {"id": 333, "first_name": "Mia"})
    mia, _ = access.approve(conn, code, "Mia", "it")
    code = access.note_stranger(conn, {"id": 444, "first_name": "Mia work"})
    linked, _ = access.link(conn, code, "mia")
    assert linked.id == mia.id
    assert access.resolve(conn, 444).person.id == mia.id


def test_deny_and_revoke():
    conn = _conn()
    access.bootstrap_owner(conn, frozenset({111}), "Alex", "it")
    code = access.note_stranger(conn, {"id": 555})
    assert access.deny(conn, code) is not None
    assert access.resolve(conn, 555).status == "blocked"

    code = access.note_stranger(conn, {"id": 666})
    access.approve(conn, code, "Guest", "it")
    assert access.revoke(conn, 666) is True and access.resolve(conn, 666).status == "blocked"
    assert access.revoke(conn, 111) is False
    assert access.revoke(conn, 999) is None


def test_member_turns_are_restricted():
    conn = _conn()
    owner = access.bootstrap_owner(conn, frozenset({111}), "Alex", "it")
    code = access.note_stranger(conn, {"id": 333})
    member, _ = access.approve(conn, code, "Mia", "it")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg = Config(
            bot_token="", owner_ids=frozenset(), data_dir=tmp, db_path=tmp / "zoe.db",
            vault_path=tmp / "vault.db", vault_key_path=tmp / "vault.key", workdir=tmp / "owner",
        )
        own = access.turn_profile(owner, cfg)
        guest = access.turn_profile(member, cfg)

        assert "--dangerously-skip-permissions" in own.args and own.cwd == tmp / "owner"
        assert own.env["PATH"].split(os.pathsep)[-1] == str(Path(sys.executable).parent)
        assert own.env["PATH"].startswith(os.environ["PATH"])
        assert "--dangerously-skip-permissions" not in guest.args
        assert {"--safe-mode", "--restricted", "--strict-mcp-config", "dontAsk", "WebSearch", "WebFetch", "Read"}             <= set(guest.args)
        approved = guest.args[guest.args.index("--allowedTools") + 1:guest.args.index("--permission-mode")]
        assert "Read" not in approved and guest.tools == access.MEMBER_TOOLS and own.tools is None
        assert guest.cwd == tmp / "people" / str(member.id) and guest.cwd.is_dir()


def test_a_session_with_more_than_its_tools_is_caught():
    builtin = [{"name": "cc-plugin-telemetry", "path": "builtin"}]
    fine = {"tools": ["Read", "WebFetch", "WebSearch"], "mcp_servers": [], "plugins": builtin}
    assert beyond(fine, access.MEMBER_TOOLS) == ""
    assert beyond({**fine, "tools": ["Read", "Bash"]}, access.MEMBER_TOOLS) == "tools ['Bash']"
    assert beyond({**fine, "mcp_servers": [{"name": "gmail"}]}, access.MEMBER_TOOLS) == "MCP servers ['gmail']"
    assert beyond({**fine, "plugins": builtin + [{"name": "evil", "path": "/x"}]}, access.MEMBER_TOOLS) ==         "plugins ['evil']"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
