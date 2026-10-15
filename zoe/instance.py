"""One Zoe per home, closed to other users.

Two services reading the same bot would split its messages between them, so the
first one to start holds an exclusive lock on ``ZOE_HOME/zoe.lock`` for as long as
it runs, and any other refuses to start. The launcher holds the lock and passes
it to the service it runs, so the lock outlives the launcher if only the
launcher dies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

# Set in the service's environment when the launcher already holds the lock for it.
HELD = "ZOE_INSTANCE_HELD"


def hold(home: Path) -> IO[str] | None:
    """Take the lock of ``home`` and return its open file, or None if another Zoe holds it."""
    home.mkdir(parents=True, exist_ok=True)
    handle = open(home / "zoe.lock", "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def protect(home: Path) -> None:
    """Close ``home`` to every other user of the machine: memory, messages, files and keys live in it.

    Only the home is closed, not the files the service writes elsewhere: a site a parallel
    mind publishes must stay readable by the web server.
    """
    if os.name == "nt":
        return
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    for item in home.iterdir():
        if item.is_file() and not item.is_symlink():
            item.chmod(0o600)
