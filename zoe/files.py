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
import struct
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
# The shapes Telegram takes as a photo: width plus height at most this, the long side at most this
# many times the short one.
_PHOTO_SIDES = 10000
_PHOTO_RATIO = 20
# JPEG markers that start a frame and hold its size.
_JPEG_FRAMES = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
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


def _dimensions(path: Path) -> tuple[int, int] | None:
    """Width and height of a PNG, WebP or JPEG image read from its header, which in a JPEG can follow
    long metadata; None when unknown."""
    try:
        with path.open("rb") as handle:
            head = handle.read(1048576)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and len(head) >= 30:
        chunk = head[12:16]
        if chunk == b"VP8X":
            return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
        if chunk == b"VP8 ":
            width, height = struct.unpack("<HH", head[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L":
            bits = int.from_bytes(head[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return None
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 <= len(head) and head[i] == 0xFF:
            marker = head[i + 1]
            if marker in _JPEG_FRAMES:
                height, width = struct.unpack(">HH", head[i + 5:i + 9])
                return width, height
            i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    return None


def _odd_shape(path: Path) -> bool:
    """Whether an image has a shape Telegram refuses as a photo, such as a long thin strip."""
    size = _dimensions(path)
    if not size or min(size) == 0:
        return False
    return sum(size) > _PHOTO_SIDES or max(size) / min(size) > _PHOTO_RATIO


def _kind(path: Path, uncompressed: bool) -> tuple[str, bool]:
    """How an uploaded file is sent: as a photo or a video when Telegram takes it so and it may be
    compressed, otherwise as a document; and whether it is an image going as a document only for its
    shape."""
    size = path.stat().st_size
    if size > SEND_LIMIT:
        raise TelegramError(f"{path.name} is {size // 1048576} MB, over Telegram's 50 MB")
    if uncompressed:
        return "document", False
    if path.suffix.lower() in _IMAGES and size <= _PHOTO_LIMIT:
        odd = _odd_shape(path)
        return ("document" if odd else "photo"), odd
    return ("video" if path.suffix.lower() in _VIDEOS else "document"), False


def send(
    api: TelegramAPI,
    chat_id: int,
    chosen: list[tuple[File | None, Path | None]],
    caption: str,
    uncompressed: bool = False,
) -> list[dict]:
    """Send files to a chat, the caption under the first, and return the messages they took, in the
    order of ``chosen``. Several files are uploaded as albums of up to ten: photos and videos together,
    then the images whose shape Telegram refuses as a photo, as documents. All go as documents when one
    is neither an image nor a video, or when they go ``uncompressed``. Raises TelegramError when they
    cannot be sent, as for files over 50 MB."""
    if len(chosen) == 1:
        return [_send_one(api, chat_id, *chosen[0], caption, uncompressed)]
    if any(path is None for _, path in chosen):
        raise TelegramError("a file is no longer there")
    kinds = [_kind(path, uncompressed) for _, path in chosen]
    if any(kind == "document" and not odd for kind, odd in kinds):
        kinds = [("document", False)] * len(kinds)
    media = [n for n, (_, odd) in enumerate(kinds) if not odd]
    shaped = [n for n, (_, odd) in enumerate(kinds) if odd]
    sent: list[dict] = []
    for group in (media, shaped):
        for start in range(0, len(group), ALBUM_LIMIT):
            items = [(kinds[n][0], chosen[n][1]) for n in group[start:start + ALBUM_LIMIT]]
            sent += _upload(api, chat_id, items, "" if sent else caption)
    messages: list[dict] = [{}] * len(chosen)
    for n, message in zip(media + shaped, sent):
        messages[n] = message
    return messages


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
    return _upload(api, chat_id, [(_kind(path, uncompressed)[0], path)], caption)[0]


def _upload(api: TelegramAPI, chat_id: int, items: list[tuple[str, Path]], caption: str) -> list[dict]:
    """Upload one file, or an album of them, each as its kind. When Telegram still refuses a photo for
    its shape, as when its header could not be read, they go again as documents."""
    try:
        if len(items) == 1:
            return [api.send_file(chat_id, items[0][0], caption, path=items[0][1])]
        return api.send_album(chat_id, items, caption)
    except TelegramError as exc:
        if "PHOTO_INVALID_DIMENSIONS" not in str(exc) or all(kind != "photo" for kind, _ in items):
            raise
        log.info("%s sent as documents: %s", ", ".join(path.name for _, path in items), exc)
        return _upload(api, chat_id, [("document", path) for _, path in items], caption)
