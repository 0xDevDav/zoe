"""Checks for the credential vault: encryption at rest, metadata listing, key handling."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.fernet import Fernet

from zoe import claude_runner
from zoe.vault import Vault, VaultError, load_key

# Run by the service, as Zoe's own change pipeline does, the checks inherit its vault-key
# credential, which would stand in for the keys they make.
os.environ.pop("CREDENTIALS_DIRECTORY", None)


def test_roundtrip_and_encryption_at_rest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        vault = Vault(tmp / "vault.db", load_key(tmp / "vault.key"))
        vault.set("elevenlabs", "api_key", "sk-very-secret", kind="api_key", note="voice replies")
        assert vault.get("elevenlabs", "api_key") == "sk-very-secret"
        vault.close()
        assert b"sk-very-secret" not in (tmp / "vault.db").read_bytes()


def test_listing_shows_metadata_only():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        vault = Vault(tmp / "vault.db", load_key(tmp / "vault.key"))
        vault.set("telegram", "bot_token", "123:abc", kind="token")
        vault.set("mail", "imap_password", "hunter2", kind="password", expires_at="2030-01-01")
        entries = vault.entries()
        vault.close()
        assert [(e.service, e.name, e.kind) for e in entries] == [
            ("mail", "imap_password", "password"),
            ("telegram", "bot_token", "token"),
        ]
        assert not hasattr(entries[0], "value")


def test_wrong_key_cannot_read():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        vault = Vault(tmp / "vault.db", load_key(tmp / "vault.key"))
        vault.set("svc", "name", "value")
        vault.close()
        intruder = Vault(tmp / "vault.db", Fernet.generate_key())
        try:
            intruder.get("svc", "name")
            raise AssertionError("decryption with a foreign key must fail")
        except VaultError:
            pass
        finally:
            intruder.close()


def test_key_file_is_created_once():
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "vault.key"
        assert load_key(key_path) == load_key(key_path)


def test_a_vault_with_secrets_never_gets_a_new_key():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        vault = Vault(tmp / "vault.db", load_key(tmp / "vault.key", vault_path=tmp / "vault.db"))
        vault.set("telegram", "bot_token", "123:abc", kind="token")
        vault.close()
        (tmp / "vault.key").unlink()
        try:
            load_key(tmp / "vault.key", vault_path=tmp / "vault.db")
        except VaultError as exc:
            assert "key is missing" in str(exc) and not (tmp / "vault.key").exists()
        else:
            raise AssertionError("a new key was made for a vault holding secrets")


def test_the_key_can_come_from_a_systemd_credential():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "vault-key").write_bytes(b"credential-key\n")
        os.environ["CREDENTIALS_DIRECTORY"] = str(tmp)
        try:
            assert load_key(tmp / "missing.key") == b"credential-key"
        finally:
            del os.environ["CREDENTIALS_DIRECTORY"]


def test_claude_runs_never_inherit_the_services_secrets():
    os.environ["ZOE_VAULT_KEY"], os.environ["ZOE_BOT_TOKEN"] = "k", "t"
    try:
        inherited = claude_runner._inherited()
        assert "ZOE_VAULT_KEY" not in inherited and "ZOE_BOT_TOKEN" not in inherited and "PATH" in inherited
    finally:
        del os.environ["ZOE_VAULT_KEY"], os.environ["ZOE_BOT_TOKEN"]


def test_unknown_kind_and_delete():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        vault = Vault(tmp / "vault.db", load_key(tmp / "vault.key"))
        try:
            vault.set("svc", "name", "value", kind="banana")
            raise AssertionError("unknown kinds must be rejected")
        except VaultError:
            pass
        vault.set("svc", "name", "value")
        assert vault.delete("svc", "name") and not vault.delete("svc", "name")
        vault.close()


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
