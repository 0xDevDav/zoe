"""Encrypted store for every credential Zoe uses: tokens, API keys, passwords, cookies.

All secrets live in one SQLite file, organized by service and name, with values
encrypted by a key kept outside the database. Values leave the vault only
through ``get``; listing returns metadata.

Command line:

    zoe-vault set elevenlabs api_key --kind api_key      # value read from stdin
    zoe-vault get elevenlabs api_key
    zoe-vault list [service]
    zoe-vault delete elevenlabs api_key
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import Config

KINDS = ("token", "api_key", "password", "oauth", "cookie", "certificate", "other")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    service    TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    value      BLOB NOT NULL,
    note       TEXT,
    expires_at TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (service, name)
);
"""


class VaultError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    service: str
    name: str
    kind: str
    note: str | None
    expires_at: str | None
    updated_at: float


def load_key(key_path: Path, env_key: str | None = None, vault_path: Path | None = None) -> bytes:
    """Return the vault key: from the environment, from the ``vault-key`` credential systemd
    hands the service (outside ZOE_HOME, so a copy of the home alone does not open the
    vault), or from its key file, created once. A vault that already holds secrets never
    gets a new key: that would leave every secret unreadable, so a missing key is an error."""
    if env_key:
        return env_key.encode()
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials and (Path(credentials) / "vault-key").is_file():
        return (Path(credentials) / "vault-key").read_bytes().strip()
    if key_path.exists():
        return key_path.read_bytes().strip()
    if vault_path is not None and _holds_secrets(vault_path):
        raise VaultError(
            f"{vault_path} holds secrets but its key is missing: restore {key_path.name}, "
            "or give the key through ZOE_VAULT_KEY or the vault-key credential"
        )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def _holds_secrets(path: Path) -> bool:
    if not path.exists():
        return False
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return bool(conn.execute("SELECT count(*) FROM secrets").fetchone()[0])
    except sqlite3.Error:
        return False
    finally:
        conn.close()


class Vault:
    def __init__(self, path: Path, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except ValueError as exc:
            raise VaultError("invalid vault key") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def set(
        self,
        service: str,
        name: str,
        value: str,
        kind: str = "other",
        note: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        if kind not in KINDS:
            raise VaultError(f"unknown kind {kind!r}, expected one of: {', '.join(KINDS)}")
        now = time.time()
        self._conn.execute(
            "INSERT INTO secrets (service, name, kind, value, note, expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(service, name) DO UPDATE SET kind = excluded.kind, value = excluded.value, "
            "note = excluded.note, expires_at = excluded.expires_at, updated_at = excluded.updated_at",
            (service, name, kind, self._fernet.encrypt(value.encode()), note, expires_at, now, now),
        )

    def get(self, service: str, name: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM secrets WHERE service = ? AND name = ?", (service, name)
        ).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row["value"]).decode()
        except InvalidToken as exc:
            raise VaultError(f"cannot decrypt {service}/{name}: wrong vault key") from exc

    def require(self, service: str, name: str) -> str:
        value = self.get(service, name)
        if value is None:
            raise VaultError(f"missing credential {service}/{name}")
        return value

    def delete(self, service: str, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM secrets WHERE service = ? AND name = ?", (service, name))
        return cur.rowcount > 0

    def entries(self, service: str | None = None) -> list[Entry]:
        sql = "SELECT service, name, kind, note, expires_at, updated_at FROM secrets"
        params: tuple = ()
        if service:
            sql += " WHERE service = ?"
            params = (service,)
        rows = self._conn.execute(sql + " ORDER BY service, name", params).fetchall()
        return [Entry(**dict(row)) for row in rows]

    def close(self) -> None:
        self._conn.close()


def open_vault(cfg: Config) -> Vault:
    return Vault(cfg.vault_path, load_key(cfg.vault_key_path, os.environ.get("ZOE_VAULT_KEY"), cfg.vault_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zoe-vault", description="Manage Zoe's credentials.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="store a credential; the value is read from stdin")
    p_set.add_argument("service")
    p_set.add_argument("name")
    p_set.add_argument("--kind", default="other", choices=KINDS)
    p_set.add_argument("--note")
    p_set.add_argument("--expires", help="expiry date, ISO format")

    for command, help_text in (("get", "print a credential value"), ("delete", "remove a credential")):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("service")
        p.add_argument("name")

    p_list = sub.add_parser("list", help="list credentials without their values")
    p_list.add_argument("service", nargs="?")

    args = parser.parse_args(argv)
    vault = open_vault(Config.from_env())
    try:
        if args.command == "set":
            value = getpass.getpass("value: ") if sys.stdin.isatty() else sys.stdin.read().rstrip("\n")
            vault.set(args.service, args.name, value, args.kind, args.note, args.expires)
        elif args.command == "get":
            print(vault.require(args.service, args.name))
        elif args.command == "delete":
            if not vault.delete(args.service, args.name):
                print(f"no credential {args.service}/{args.name}", file=sys.stderr)
                return 1
        else:
            for e in vault.entries(args.service):
                expiry = f"  expires {e.expires_at}" if e.expires_at else ""
                note = f"  {e.note}" if e.note else ""
                print(f"{e.service}/{e.name}  [{e.kind}]{expiry}{note}")
    except VaultError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        vault.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
