"""Who may talk to Zoe, as whom, and with what powers.

A person is someone Zoe knows; an account is one Telegram identity. A person can
use several accounts and remains one person to Zoe, with one memory. The role
decides what a turn may do:

- owner: everything Zoe can do on the machine, in the owner's working directory;
- member: conversation and web search, in a private directory, with no access to
  files, commands or connected services.

Unknown accounts receive a request code. An owner approves the code as a new
person, links it to an existing person, or denies it.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import i18n, plugins
from .claude_runner import Profile
from .config import Config

ROLES = ("owner", "member")
# How many of the strangers who wrote are remembered for the owner to admit.
STRANGERS_KEPT = 50

_OWNER_ARGS: tuple[str, ...] = ("--dangerously-skip-permissions",)

# Members get web tools and reading of the files they send in their own directory, and
# nothing of the machine owner: no settings, instructions (not even a CLAUDE.md in a parent
# directory), skills, hooks, plugins, MCP servers or memory. Restricted mode confines
# reading to their directory, and reading is not pre-approved, so nothing outside it
# can be read even if a flag changes meaning. Each turn also checks, as it starts, that
# Claude came up with these tools only (MEMBER_TOOLS).
_MEMBER_ARGS: tuple[str, ...] = (
    "--safe-mode",
    "--restricted",
    "--tools", "WebSearch", "WebFetch", "Read",
    "--allowedTools", "WebSearch", "WebFetch",
    "--permission-mode", "dontAsk",
    "--strict-mcp-config",
    "--setting-sources", "project",
)
MEMBER_TOOLS = frozenset({"WebSearch", "WebFetch", "Read"})


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    role: str
    language: str
    folders: tuple[str, ...] = ()


@dataclass(frozen=True)
class Account:
    telegram_user_id: int
    person_id: int | None
    status: str
    username: str | None
    display_name: str | None
    language_code: str | None
    request_code: str | None

    def label(self) -> str:
        return describe(self.display_name, self.username, self.telegram_user_id)


@dataclass(frozen=True)
class Access:
    status: str
    person: Person | None


def describe(display_name: str | None, username: str | None, user_id: int) -> str:
    parts = [display_name or "", f"@{username}" if username else "", f"({user_id})"]
    return " ".join(p for p in parts if p)


def _person(row: sqlite3.Row) -> Person:
    return Person(row["id"], row["name"], row["role"], row["language"], tuple(json.loads(row["folders"] or "[]")))


def _shareable(folder: Path, home: Path) -> bool:
    """Whether a folder may be opened to a member: an existing one that is not the root of a disk,
    the home folder itself, Zoe's code, or her home, where every person's memory and the keys live."""
    try:
        resolved = folder.resolve()
    except OSError:
        return False
    return (resolved.is_dir() and resolved.parent != resolved and resolved != Path.home().resolve()
            and not resolved.is_relative_to(home.resolve()) and not home.resolve().is_relative_to(resolved)
            and not resolved.is_relative_to(plugins.CORE_ROOT) and not plugins.CORE_ROOT.is_relative_to(resolved))


def share(conn: sqlite3.Connection, person: Person, folder: Path, home: Path) -> Person | None:
    """Let a member read a folder of the owner's; None when they are not a member or the folder cannot be shared."""
    if person.role != "member" or not _shareable(folder, home):
        return None
    folders = tuple(dict.fromkeys([*person.folders, str(folder.resolve())]))
    conn.execute("UPDATE persons SET folders = ? WHERE id = ?", (json.dumps(folders), person.id))
    return get_person(conn, person.id)


def unshare(conn: sqlite3.Connection, person: Person, folder: Path) -> Person | None:
    """Take back a folder from a member; None when it was not theirs."""
    target = str(folder.resolve())
    if target not in person.folders:
        return None
    folders = [f for f in person.folders if f != target]
    conn.execute("UPDATE persons SET folders = ? WHERE id = ?", (json.dumps(folders), person.id))
    return get_person(conn, person.id)


def _account(row: sqlite3.Row) -> Account:
    return Account(
        row["telegram_user_id"], row["person_id"], row["status"], row["username"],
        row["display_name"], row["language_code"], row["request_code"],
    )


# --- persons -----------------------------------------------------------------

def create_person(conn: sqlite3.Connection, name: str, role: str, language: str) -> Person:
    cur = conn.execute(
        "INSERT INTO persons (name, role, language, created_at) VALUES (?, ?, ?, ?)",
        (name, role, language, time.time()),
    )
    return Person(int(cur.lastrowid), name, role, language)


def get_person(conn: sqlite3.Connection, person_id: int | None) -> Person | None:
    if person_id is None:
        return None
    row = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
    return _person(row) if row else None


def find_person(conn: sqlite3.Connection, ref: str) -> Person | None:
    """Find a person by numeric id or, ignoring case, by name."""
    if ref.isdigit():
        return get_person(conn, int(ref))
    row = conn.execute("SELECT * FROM persons WHERE lower(name) = lower(?)", (ref,)).fetchone()
    return _person(row) if row else None


def set_language(conn: sqlite3.Connection, person_id: int, language: str) -> None:
    conn.execute("UPDATE persons SET language = ? WHERE id = ?", (language, person_id))


# --- accounts ----------------------------------------------------------------

def _upsert_account(
    conn: sqlite3.Connection,
    user_id: int,
    person_id: int | None,
    status: str,
    user: dict | None = None,
    code: str | None = None,
) -> None:
    user = user or {}
    display = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or None
    now = time.time()
    conn.execute(
        "INSERT INTO accounts (telegram_user_id, person_id, status, username, display_name, language_code, "
        "request_code, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(telegram_user_id) DO UPDATE SET person_id = excluded.person_id, status = excluded.status, "
        "request_code = excluded.request_code, updated_at = excluded.updated_at, "
        "username = coalesce(excluded.username, accounts.username), "
        "display_name = coalesce(excluded.display_name, accounts.display_name), "
        "language_code = coalesce(excluded.language_code, accounts.language_code)",
        (user_id, person_id, status, user.get("username"), display, user.get("language_code"), code, now, now),
    )


def _set_account(conn: sqlite3.Connection, user_id: int, person_id: int | None, status: str) -> None:
    conn.execute(
        "UPDATE accounts SET person_id = ?, status = ?, request_code = NULL, updated_at = ? "
        "WHERE telegram_user_id = ?",
        (person_id, status, time.time(), user_id),
    )


def bootstrap_owner(conn: sqlite3.Connection, user_ids: frozenset[int], name: str, language: str) -> Person | None:
    """Make the configured accounts belong to the owner, creating the owner once."""
    if not user_ids:
        return None
    row = conn.execute("SELECT * FROM persons WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    owner = _person(row) if row else create_person(conn, name, "owner", language)
    for user_id in sorted(user_ids):
        _upsert_account(conn, user_id, owner.id, "approved")
    return owner


def resolve(conn: sqlite3.Connection, user_id: int) -> Access:
    """Tell whether a Telegram account may talk to Zoe, and as which person."""
    row = conn.execute(
        "SELECT a.status, p.id, p.name, p.role, p.language, p.folders FROM accounts a "
        "LEFT JOIN persons p ON p.id = a.person_id WHERE a.telegram_user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return Access("unknown", None)
    person = _person(row) if row["id"] is not None else None
    if row["status"] == "approved" and person is None:
        return Access("pending", None)
    return Access(row["status"], person)


def note_stranger(conn: sqlite3.Connection, user: dict) -> str:
    """Remember, without answering anyone, an unknown account that wrote; return its code.

    Zoe never answers a stranger and never tells the owners: the owner finds the last
    ``STRANGERS_KEPT`` who tried in the menu and admits them there or through Zoe.
    """
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            _upsert_account(conn, user["id"], None, "pending", user, code)
            break
        except sqlite3.IntegrityError:
            continue
    conn.execute(
        "DELETE FROM accounts WHERE status = 'pending' AND telegram_user_id NOT IN ("
        "  SELECT telegram_user_id FROM accounts WHERE status = 'pending' ORDER BY updated_at DESC LIMIT ?)",
        (STRANGERS_KEPT,),
    )
    return code


def admit(conn: sqlite3.Connection, user_id: int, name: str, default_language: str) -> tuple[Person, bool] | None:
    """Let a Telegram account write to Zoe, as the owner asked; return its person and whether it is new.

    With the name of a member already known the account becomes another of theirs; otherwise
    a new member is made, named after the account when no name is given. Never an owner:
    an owner's accounts come only from ZOE_OWNER_IDS.
    """
    current = resolve(conn, user_id)
    if current.person is not None and current.person.role == "owner":
        return None
    row = conn.execute("SELECT * FROM accounts WHERE telegram_user_id = ?", (user_id,)).fetchone()
    known = _account(row) if row else None
    name = name.strip() or (known and (known.display_name or known.username)) or str(user_id)
    person = find_person(conn, name) if not name.isdigit() else None
    if person is not None and person.role == "owner":
        return None
    created = person is None
    if created:
        language = i18n.normalize(known.language_code if known else None, default_language)
        person = create_person(conn, name, "member", language)
    _upsert_account(conn, user_id, person.id, "approved")
    return person, created


def add_account(conn: sqlite3.Connection, user_id: int, person_id: int) -> Person | None:
    """Let one more Telegram account write to Zoe as a member who exists, as the owner asked in
    the menu; an account removed before comes back. None for an owner, for an owner's
    account and for a person who is not there: an owner's accounts come only from ZOE_OWNER_IDS.
    """
    person = get_person(conn, person_id)
    current = resolve(conn, user_id).person
    if person is None or person.role == "owner" or (current is not None and current.role == "owner"):
        return None
    _upsert_account(conn, user_id, person.id, "approved")
    return person


def pending_account(conn: sqlite3.Connection, code: str) -> Account | None:
    row = conn.execute(
        "SELECT * FROM accounts WHERE request_code = ? AND status = 'pending'", (code,)
    ).fetchone()
    return _account(row) if row else None


def approve(
    conn: sqlite3.Connection, code: str, name: str | None, default_language: str
) -> tuple[Person, Account] | None:
    """Accept a pending account as a new member."""
    account = pending_account(conn, code)
    if account is None:
        return None
    person = create_person(
        conn,
        name or account.display_name or account.username or str(account.telegram_user_id),
        "member",
        i18n.normalize(account.language_code, default_language),
    )
    _set_account(conn, account.telegram_user_id, person.id, "approved")
    return person, account


def link(conn: sqlite3.Connection, code: str, person_ref: str) -> tuple[Person, Account] | None:
    """Attach a pending account to a member who already exists.

    Never to an owner: an owner's accounts come only from ZOE_OWNER_IDS, so one tap on a
    request from someone posing as the owner cannot hand them the owner's powers.
    """
    account = pending_account(conn, code)
    person = find_person(conn, person_ref)
    if account is None or person is None or person.role == "owner":
        return None
    _set_account(conn, account.telegram_user_id, person.id, "approved")
    return person, account


def deny(conn: sqlite3.Connection, code: str) -> Account | None:
    account = pending_account(conn, code)
    if account is None:
        return None
    _set_account(conn, account.telegram_user_id, None, "blocked")
    return account


def revoke(conn: sqlite3.Connection, user_id: int) -> bool | None:
    """Block an account: None if it is unknown, False if it belongs to an owner.

    Its chat goes silent at once: the plans set for it are cancelled and the
    messages it left waiting are set aside.
    """
    row = conn.execute(
        "SELECT a.person_id, p.role FROM accounts a LEFT JOIN persons p ON p.id = a.person_id "
        "WHERE a.telegram_user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    if row["role"] == "owner":
        return False
    _set_account(conn, user_id, row["person_id"], "blocked")
    conn.execute("DELETE FROM neurons WHERE id IN (SELECT neuron_id FROM plans WHERE chat_id = ?)", (user_id,))
    conn.execute(
        "UPDATE messages SET state = 'skipped' WHERE chat_id = ? AND direction = 'in' AND state = 'received'",
        (user_id,),
    )
    return True


def people(conn: sqlite3.Connection) -> list[tuple[Person, list[Account]]]:
    persons = [_person(r) for r in conn.execute("SELECT * FROM persons ORDER BY id").fetchall()]
    accounts = [_account(r) for r in conn.execute(
        "SELECT * FROM accounts WHERE status = 'approved' ORDER BY telegram_user_id"
    ).fetchall()]
    return [(p, [a for a in accounts if a.person_id == p.id]) for p in persons]


def pending(conn: sqlite3.Connection) -> list[Account]:
    rows = conn.execute("SELECT * FROM accounts WHERE status = 'pending' ORDER BY created_at").fetchall()
    return [_account(r) for r in rows]


def owner_recipients(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return (Telegram user id, language) for every approved owner account."""
    rows = conn.execute(
        "SELECT a.telegram_user_id, p.language FROM accounts a JOIN persons p ON p.id = a.person_id "
        "WHERE p.role = 'owner' AND a.status = 'approved' ORDER BY a.telegram_user_id"
    ).fetchall()
    return [(r["telegram_user_id"], r["language"]) for r in rows]


# --- powers ------------------------------------------------------------------

def _owner_env(cfg: Config) -> dict[str, str]:
    """Let owner turns run Zoe's own tools, such as the change pipeline, with this installation's home.
    Her commands come after the system's, so ``python3`` stays the system's, with its libraries."""
    python_dir = str(Path(sys.executable).parent)
    path = os.environ.get("PATH", "")
    pythonpath = os.environ.get("PYTHONPATH", "")
    return {
        "ZOE_HOME": str(cfg.data_dir),
        "PATH": (path + os.pathsep if path else "") + python_dir,
        "PYTHONPATH": str(plugins.CORE_ROOT) + (os.pathsep + pythonpath if pythonpath else ""),
    }


def turn_profile(person: Person, cfg: Config) -> Profile:
    """Return what a Claude turn may do on behalf of this person."""
    if person.role == "owner":
        return Profile(_OWNER_ARGS + plugins.plugin_args(cfg.data_dir), cfg.workdir, _owner_env(cfg))
    home = cfg.data_dir / "people" / str(person.id)
    home.mkdir(parents=True, exist_ok=True)
    shared = tuple(arg for folder in person.folders if Path(folder).is_dir() for arg in ("--add-dir", folder))
    return Profile(_MEMBER_ARGS + shared, home, tools=MEMBER_TOOLS)
