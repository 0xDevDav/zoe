"""Messages for the owners left by steps that cannot send them, delivered at the next start.

A self-update, a rollback or an undo can happen while the service is down; they
leave a notice here and the service sends it to every owner, in the owner's
language, as soon as it is running again.
"""

from __future__ import annotations

import json
from pathlib import Path


def _file(home: Path) -> Path:
    return home / "notices.jsonl"


def post(home: Path, key: str, **params: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    with _file(home).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "params": params}, ensure_ascii=False) + "\n")


def drain(home: Path) -> list[dict]:
    """Return the pending notices and remove them."""
    path = _file(home)
    if not path.exists():
        return []
    notices = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    path.unlink()
    return notices


def discard(home: Path, key: str) -> None:
    """Drop pending notices of one kind, such as an update announcement that a rollback undid."""
    keep = [n for n in drain(home) if n["key"] != key]
    for notice in keep:
        post(home, notice["key"], **notice["params"])
