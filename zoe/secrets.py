"""Recognize keys and tokens pasted into a chat, so memory keeps only a placeholder.

Before a key leaves an answered message or its episode, it is stored in the vault, so the
placeholder that takes its place ("key saved in credentials") is always true and the key
is never lost; what Zoe remembers of the conversation (episodes, terms, observed memories,
the message log) holds only the placeholder.

A key is recognized by the published prefix of a common API key or token format, or as a
bare run of 32, or 48 and more, hex digits: app secrets and API keys share that shape with
MD5 and SHA-256 digests, which are masked too, since in a chat they are far rarer than keys.
Plain UUIDs and 40-digit commit hashes are left alone: job ids and commits get quoted on purpose.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import closing

from . import i18n
from .config import Config
from .vault import open_vault

SECRET = re.compile(
    r"(?<![\w-])(sk-[A-Za-z0-9_-]{16,}"
    r"|EAA[A-Za-z0-9]{40,}"
    r"|KGAT_[0-9a-f]{16,}"
    r"|rpa_[A-Za-z0-9]{20,}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}(\.[A-Za-z0-9_-]+)?"
    r"|\b(?:[0-9a-f]{32}|[0-9a-f]{48,})\b"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{20,})"
)


def mask(text: str, language: str) -> str:
    """Return ``text`` with every key in it replaced by the placeholder."""
    placeholder = i18n.t(language, "key_saved")
    return SECRET.sub(lambda _m: placeholder, text)


def found(*texts: str | None) -> list[str]:
    """Return the keys in ``texts``, each once."""
    return list(dict.fromkeys(m.group(0) for text in texts if text for m in SECRET.finditer(text)))


def keep(cfg: Config, keys: list[str], where: str) -> None:
    """Store each key in the vault, named by its digest so the same key is stored once."""
    if not keys:
        return
    with closing(open_vault(cfg)) as vault:
        for key in keys:
            vault.set("chat", hashlib.sha256(key.encode()).hexdigest()[:16], key, note=f"pasted in {where}")


def is_secret(word: str) -> bool:
    """Whether ``word`` holds a key."""
    return SECRET.search(word) is not None
