"""Files people send: temporary unless worth keeping, remembered when kept, sent back on request.

Every file saved from a message is temporary: after ``ZOE_TEMP_FILE_DAYS`` it is
deleted, so a screenshot shown in passing does not fill the disk. When a file
matters (a document, a contract, anything the person will need again) Zoe keeps
it: it loses its expiry and becomes a ``file`` neuron, wired to the conversation
it came with, holding what it is. When Zoe moves a kept file she gives its new
place, so months later "the lease I sent you in March" is found where it is now.

Deleting is careful: only files still in an ``inbox`` folder are ever deleted
from disk. A file moved anywhere else is only forgotten, never touched.

A kept or received file can be sent back to the chat; one Telegram already has is
sent again by its id, without uploading it. Several files sent together arrive as
an album.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .access import Person
from .brain import Brain
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("zoe.files")

FILE = "file"
INBOX = "inbox"
# Telegram's limit for files a bot sends, and for a file sent as a photo.
SEND_LIMIT = 50 * 1048576
_PHOTO_LIMIT = 10 * 1048576
_IMAGES = (".jpg", ".jpeg", ".png", ".webp")
_VIDEOS = (".mp4",)
# The most files Telegram puts in one album.
ALBUM_LIMIT = 10


@dataclass(frozen=True)
class File:
    id: int
    neuron_id: int | None
    person_id: int
    kind: str
    path: Path
    name: str
    telegram_id: str | None
    size: int | None
    expires_at: float | None

    @property
    def kept(self) -> bool:
        return self.expires_at is None


def _file(row: sqlite3.Row) -> File:
    return File(row["id"], row["neuron_id"], row["person_id"], row["kind"], Path(row["path"]), row["name"],
                row["telegram_id"], row["size"], row["expires_at"])


def register(
    conn: sqlite3.Connection,
    person_id: int,
    kind: str,
    path: Path,
    telegram_id: str | None,
    size: int | None,
    days: float,
) -> int:
    """Record a file just saved from a message, temporary for ``days``."""
    now = time.time()
    cur = conn.execute(
        "INSERT INTO files (person_id, kind, path, name, telegram_id, size, expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, kind, str(path), path.name, telegram_id, size, now + days * 86400, now),
    )
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, person_id: int, file_id: int) -> File | None:
    row = conn.execute("SELECT * FROM files WHERE id = ? AND person_id = ?", (file_id, person_id)).fetchone()
    return _file(row) if row else None


def by_neurons(conn: sqlite3.Connection, neuron_ids: list[int]) -> dict[int, File]:
    """The kept files behind these neurons, by neuron id."""
    if not neuron_ids:
        return {}
    rows = conn.execute(
        f"SELECT * FROM files WHERE neuron_id IN ({','.join('?' * len(neuron_ids))})", neuron_ids
    ).fetchall()
    return {row["neuron_id"]: _file(row) for row in rows}


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _readable(person: Person, path: Path, home: Path) -> bool:
    """Whether a member may use this file: it is in their own folder or in one the owner shared."""
    return any(_inside(path, Path(folder)) for folder in (home, *person.folders))


def _in_inbox(path: Path) -> bool:
    """Whether a file still sits where received files are saved: an inbox folder, one level of days down."""
    return path.parent.parent.name == INBOX


def keep(
    conn: sqlite3.Connection,
    brain: Brain,
    person: Person,
    file_id: int,
    about: str,
    moved_to: str,
    home: Path,
    related: list[int],
) -> File | None:
    """Keep a file for good, remembering what it is and, when Zoe moved it, where it is now.

    A member's file can only be in their own folder ``home``. None when the file is not
    this person's, or the new place is not a file they may keep there.
    """
    found = get(conn, person.id, file_id)
    if found is None:
        return None
    path = Path(moved_to).expanduser() if moved_to else found.path
    if not path.is_file() or (person.role != "owner" and not _readable(person, path, home)):
        return None
    about = about.strip() or found.name
    content = f"File «{path.name}»: {about}"
    if found.neuron_id and brain.get(found.neuron_id):
        brain.revise(found.neuron_id, content)
        neuron = found.neuron_id
    else:
        neuron = brain.learn(FILE, content, related=related)
    conn.execute(
        "UPDATE files SET path = ?, name = ?, expires_at = NULL, neuron_id = ? WHERE id = ?",
        (str(path), path.name, neuron, file_id),
    )
    return get(conn, person.id, file_id)


def discard(conn: sqlite3.Connection, brain: Brain, person: Person, file_id: int) -> File | None:
    """Forget a file now; delete it from disk too when it is still in an inbox."""
    found = get(conn, person.id, file_id)
    if found is None:
        return None
    _remove(conn, found)
    if found.neuron_id:
        brain.forget(found.neuron_id)
    return found


def _remove(conn: sqlite3.Connection, found: File) -> None:
    if _in_inbox(found.path):
        found.path.unlink(missing_ok=True)
        try:
            found.path.parent.rmdir()
        except OSError:
            pass
    conn.execute("DELETE FROM files WHERE id = ?", (found.id,))


def sweep(conn: sqlite3.Connection, now: float) -> int:
    """Delete the temporary files whose time has come; return how many."""
    expired = [_file(r) for r in conn.execute(
        "SELECT * FROM files WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
    ).fetchall()]
    for found in expired:
        _remove(conn, found)
    return len(expired)


def resolve(conn: sqlite3.Connection, person: Person, target: str, home: Path) -> tuple[File | None, Path | None]:
    """What ``[[send]]`` names: a file number of this person, or a path. A member may only
    send what is in their own folder ``home``; nothing that is not a file is sent."""
    target = target.strip().lstrip("#")
    if target.isdigit():
        found = get(conn, person.id, int(target))
        return (found, found.path if found and found.path.is_file() else None) if found else (None, None)
    path = Path(target).expanduser()
    if not path.is_absolute() or not path.is_file() or (person.role != "owner" and not _readable(person, path, home)):
        return None, None
    return None, path


def resolve_all(
    conn: sqlite3.Connection, person: Person, target: str, home: Path
) -> list[tuple[File | None, Path | None]]:
    """What ``[[send]]`` names: one file, or several separated by commas. Empty when any of them
    is not a file this person may send."""
    whole = resolve(conn, person, target, home)
    chosen = [whole] if whole != (None, None) else [resolve(conn, person, part, home) for part in target.split(",")]
    return [] if (None, None) in chosen else chosen


def _kind(path: Path, uncompressed: bool) -> str:
    """How an uploaded file is sent: as a photo or a video when Telegram takes it so and it may be
    compressed, otherwise as a document."""
    size = path.stat().st_size
    if size > SEND_LIMIT:
        raise TelegramError(f"{path.name} is {size // 1048576} MB, over Telegram's 50 MB")
    if uncompressed:
        return "document"
    if path.suffix.lower() in _IMAGES and size <= _PHOTO_LIMIT:
        return "photo"
    return "video" if path.suffix.lower() in _VIDEOS else "document"


def send(
    api: TelegramAPI,
    chat_id: int,
    chosen: list[tuple[File | None, Path | None]],
    caption: str,
    uncompressed: bool = False,
) -> list[dict]:
    """Send files to a chat, the caption under the first, and return the messages they took. Several
    files are uploaded as albums of up to ten, all as documents when one is neither a photo nor a
    video, or when they go ``uncompressed``. Raises TelegramError when they cannot be sent, as for
    files over 50 MB."""
    if len(chosen) == 1:
        return [_send_one(api, chat_id, *chosen[0], caption, uncompressed)]
    if any(path is None for _, path in chosen):
        raise TelegramError("a file is no longer there")
    items = [(_kind(path, uncompressed), path) for _, path in chosen]
    if any(kind == "document" for kind, _ in items):
        items = [("document", path) for _, path in items]
    sent = []
    for start in range(0, len(items), ALBUM_LIMIT):
        group, text = items[start:start + ALBUM_LIMIT], caption if start == 0 else ""
        if len(group) == 1:
            sent.append(api.send_file(chat_id, group[0][0], text, path=group[0][1]))
        else:
            sent += api.send_album(chat_id, group, text)
    return sent


def _send_one(
    api: TelegramAPI, chat_id: int, found: File | None, path: Path | None, caption: str, uncompressed: bool
) -> dict:
    """Send one file: again by its Telegram id when Telegram has it as it should go, otherwise uploaded."""
    if found and found.telegram_id and not (uncompressed and found.kind != "document"):
        try:
            return api.send_file(chat_id, found.kind, caption, file_id=found.telegram_id)
        except TelegramError as exc:
            if path is None:
                raise
            log.info("file %d sent again by upload: %s", found.id, exc)
    if path is None:
        raise TelegramError("the file is no longer there")
    return api.send_file(chat_id, _kind(path, uncompressed), caption, path=path)
