"""Everything a Telegram message can carry, made readable for Zoe.

At intake each message is classified by what it holds, and its details are kept
in its payload: text, photos, files, videos, GIFs, voice notes, audio, round
video messages, stickers, locations and places, contacts, polls, dice. Before a
turn the worker turns it into text Claude can work with:

- files are saved in the person's ``inbox`` folder, where Claude opens them;
  videos, GIFs and video stickers also get one image of frames spread over them,
  left to right, so Claude sees what happens; round videos and animated stickers
  get their preview image;
- voice notes, audio and round video messages are transcribed;
- locations, places, contacts, polls and dice are written out;
- a forwarded message says who it comes from, an edited one which message it changes.

Telegram lets bots download files up to 20 MB: a bigger one is described but not saved.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .telegram import TelegramAPI, TelegramError

# Kinds whose words are in their sound: transcribed before the turn.
SPOKEN = ("voice", "audio", "video_note")
# Kinds that are nothing but their sound: without a transcript there is no message.
SOUND_ONLY = ("voice", "video_note")

_UNSAFE = re.compile(r"[^\w.\-]+")


def _file(obj: dict) -> dict:
    return {
        "file_id": obj.get("file_id"),
        "name": obj.get("file_name"),
        "mime": obj.get("mime_type"),
        "size": obj.get("file_size"),
        "duration": obj.get("duration"),
        "thumb": (obj.get("thumbnail") or obj.get("thumb") or {}).get("file_id"),
    }


def _origin(origin: dict) -> str:
    """Who a forwarded message comes from, with their Telegram id when their privacy shows it."""
    user = origin.get("sender_user") or {}
    chat = origin.get("sender_chat") or origin.get("chat") or {}
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    who = name or origin.get("sender_user_name") or chat.get("title") or "someone"
    return f"{who}, Telegram id {user['id']}" if user.get("id") else who


def classify(msg: dict) -> tuple[str, dict]:
    """Return the kind of a Telegram message and the details Zoe needs to read it."""
    found: dict = {}
    if msg.get("media_group_id"):
        found["group"] = msg["media_group_id"]
    if msg.get("forward_origin"):
        found["forwarded"] = _origin(msg["forward_origin"])

    if "text" in msg or "rich_message" in msg:
        return "text", found
    if "photo" in msg:
        best = msg["photo"][-1]
        return "photo", {**found, "file_id": best["file_id"], "size": best.get("file_size")}
    for kind in ("animation", "video", "video_note", "voice", "audio", "document"):
        if kind in msg:
            item = msg[kind]
            extra = {"title": item.get("title"), "performer": item.get("performer")} if kind == "audio" else {}
            return kind, {**found, **_file(item), **extra}
    if "sticker" in msg:
        sticker = msg["sticker"]
        moving = sticker.get("is_animated") or sticker.get("is_video")
        image = _file(sticker)["thumb"] if moving else sticker.get("file_id")
        return "sticker", {**found, "file_id": image, "emoji": sticker.get("emoji", ""), "set": sticker.get("set_name", "")}
    if "venue" in msg:
        venue = msg["venue"]
        place = venue.get("location") or {}
        return "venue", {**found, "lat": place.get("latitude"), "lon": place.get("longitude"),
                         "title": venue.get("title", ""), "address": venue.get("address", "")}
    if "location" in msg:
        place = msg["location"]
        return "location", {**found, "lat": place.get("latitude"), "lon": place.get("longitude"),
                            "live": bool(place.get("live_period"))}
    if "contact" in msg:
        contact = msg["contact"]
        name = " ".join(x for x in (contact.get("first_name"), contact.get("last_name")) if x)
        return "contact", {**found, "name": name, "phone": contact.get("phone_number", "")}
    if "poll" in msg:
        poll = msg["poll"]
        return "poll", {**found, "question": poll.get("question", ""),
                        "options": [o.get("text", "") for o in poll.get("options", [])]}
    if "dice" in msg:
        return "dice", {**found, "emoji": msg["dice"].get("emoji", ""), "value": msg["dice"].get("value")}
    known = {"message_id", "from", "chat", "date", "edit_date", "caption", "caption_entities", "entities",
             "media_group_id", "forward_origin", "reply_to_message", "quote", "has_protected_content"}
    return "other", {**found, "type": next((key for key in msg if key not in known), "unknown")}


_MARKS = {
    "bold": ("**", "**"),
    "italic": ("_", "_"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("~~", "~~"),
    "spoiler": ("||", "||"),
}
_QUOTES = ("blockquote", "expandable_blockquote")


def to_markdown(text: str, entities: list[dict]) -> str:
    """Write a message's formatting back into its text as Markdown.

    Telegram sends plain text plus entities, offsets counted in UTF-16 units, for
    bold, code, links and the rest. Read as plain text, a link such as "the docs"
    loses its address and a pasted code block its edges.
    """
    units = text.encode("utf-16-le")
    size = len(units) // 2

    def piece(start: int, end: int) -> str:
        return units[2 * start:2 * end].decode("utf-16-le", "surrogatepass")

    inserts: list[tuple[int, int, float, int, str]] = []
    for index, entity in enumerate(entities):
        kind = entity.get("type")
        start = entity.get("offset", 0)
        end = start + entity.get("length", 0)
        if kind != "pre" and kind not in _QUOTES:
            # "**bold **" is not Markdown: whitespace stays outside the markers.
            while start < end and piece(start, start + 1).isspace():
                start += 1
            while end > start and piece(end - 1, end).isspace():
                end -= 1
        if start >= end:
            continue
        inner = piece(start, end)
        if kind in _MARKS:
            opening, closing = _MARKS[kind]
        elif kind == "code":
            opening, closing = ("`` ", " ``") if "`" in inner else ("`", "`")
        elif kind == "pre":
            fence = "````" if "```" in inner else "```"
            opening = ("\n" if start > 0 and piece(start - 1, start) != "\n" else "") + fence \
                + (entity.get("language") or "") + "\n"
            closing = ("" if inner.endswith("\n") else "\n") + fence \
                + ("\n" if end < size and piece(end, end + 1) != "\n" else "")
        elif kind == "text_link":
            url = entity.get("url", "")
            if inner == url:
                continue
            opening, closing = "[", "](" + re.sub(r"[()\s]", lambda m: f"%{ord(m.group()):02x}", url) + ")"
        elif kind == "text_mention":
            opening, closing = "[", f"](tg://user?id={(entity.get('user') or {}).get('id')})"
        elif kind in _QUOTES:
            opening, closing = "> ", ""
            inserts += [(i + 1, 1, float("-inf"), index, "> ") for i in range(start, end - 1) if piece(i, i + 1) == "\n"]
        else:
            continue
        # At one position closings come before openings, outer openings first, inner closings first.
        inserts.append((start, 1, start - end, index, opening))
        if closing:
            inserts.append((end, 0, end - start, -index, closing))

    if not inserts:
        return text
    inserts.sort(key=lambda insert: insert[:4])
    out, last = [], 0
    for position, *_, mark in inserts:
        out += [piece(last, position), mark]
        last = position
    out.append(piece(last, size))
    return "".join(out)


_RICH_MEDIA = {"photo": "photo", "video": "video", "animation": "GIF", "audio": "audio", "voice_note": "voice note",
               "document": "file"}


def rich_text(value: object) -> str:
    """A piece of rich text as Markdown: a string, a list of pieces, or one formatted piece."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(rich_text(piece) for piece in value)
    if not isinstance(value, dict):
        return ""
    kind, inner = value.get("type"), rich_text(value.get("text"))
    if kind in _MARKS and inner.strip():
        opening, closing = _MARKS[kind]
        return f"{opening}{inner}{closing}"
    if kind == "code":
        return f"`{inner}`"
    if kind == "url":
        url = value.get("url", "")
        return inner if inner in ("", url) else f"[{inner}]({url})"
    if kind == "text_mention":
        return f"[{inner}](tg://user?id={(value.get('user') or {}).get('id')})"
    if kind == "mention":
        return inner or f"@{value.get('username', '')}"
    if kind == "custom_emoji":
        return value.get("alternative_text", "")
    if kind == "mathematical_expression":
        return f"${value.get('expression', '')}$"
    if kind == "button":
        return f"[{rich_text((value.get('button') or {}).get('text'))}]"
    return inner


def rich_blocks(blocks: object, depth: int = 0) -> str:
    """The blocks of a rich message as Markdown, one paragraph each. Media in it are named,
    with their caption, but not downloaded."""
    return "\n\n".join(text for text in (_rich_block(b, depth) for b in blocks or [] if isinstance(b, dict)) if text)


def _rich_block(block: dict, depth: int) -> str:
    kind, text = block.get("type"), rich_text(block.get("text"))
    caption = rich_text((block.get("caption") or {}).get("text")) if isinstance(block.get("caption"), dict) \
        else rich_text(block.get("caption"))
    if kind == "heading":
        return f"{'#' * min(max(int(block.get('size') or 1), 1), 6)} {text}"
    if kind == "pre":
        return f"```{block.get('language') or ''}\n{text}\n```"
    if kind == "divider":
        return "---"
    if kind == "mathematical_expression":
        return f"$$ {block.get('expression', '')} $$"
    if kind == "list":
        lines = []
        for item in block.get("items") or []:
            mark = ("[x] " if item.get("is_checked") else "[ ] ") if item.get("has_checkbox") else ""
            body = rich_blocks(item.get("blocks"), depth + 1).replace("\n\n", "\n").replace("\n", "\n" + "  " * (depth + 1))
            lines.append(f"{'  ' * depth}{item.get('label') or '-'} {mark}{body}")
        return "\n".join(lines)
    if kind in ("blockquote", "expandable_blockquote", "pullquote"):
        quoted = rich_blocks(block.get("blocks"), depth) if kind == "blockquote" else text
        credit = rich_text(block.get("credit"))
        return "\n".join(f"> {line}" for line in (quoted + (f"\n({credit})" if credit else "")).splitlines())
    if kind in ("collage", "slideshow"):
        return "\n\n".join(part for part in (rich_blocks(block.get("blocks"), depth), caption) if part)
    if kind == "table":
        rows = ["| " + " | ".join(rich_text(cell.get("text")) for cell in row) + " |" for row in block.get("cells") or []]
        return "\n".join(rows + ([caption] if caption else []))
    if kind == "details":
        return f"{rich_text(block.get('summary'))}\n{rich_blocks(block.get('blocks'), depth)}".strip()
    if kind == "map":
        where = block.get("location") or {}
        return f"[map {_map(where.get('latitude'), where.get('longitude'))}{f': {caption}' if caption else ''}]"
    if kind == "buttons":
        return " ".join(f"[{rich_text(button.get('text'))}]" for button in block.get("buttons") or [])
    if kind in _RICH_MEDIA:
        return f"[{_RICH_MEDIA[kind]} inside the message, not saved{f': {caption}' if caption else ''}]"
    if kind in ("anchor", "thinking"):
        return ""
    return text


def text_of(msg: dict) -> str | None:
    """A message's text or caption, with its formatting written in as Markdown."""
    if isinstance(msg.get("rich_message"), dict):
        return rich_blocks(msg["rich_message"].get("blocks"))
    if msg.get("text") is not None:
        return to_markdown(msg["text"], msg.get("entities") or [])
    if msg.get("caption") is not None:
        return to_markdown(msg["caption"], msg.get("caption_entities") or [])
    return None


def safe_name(name: str) -> str:
    """A file name that stays inside its folder, whatever the sender called the file."""
    cleaned = _UNSAFE.sub("_", Path(name.replace("\\", "/")).name).strip("._")
    return cleaned[:80] or "file"


def save(api: TelegramAPI, file_id: str | None, folder: Path, stem: str, name: str | None = None) -> Path | None:
    """Download a file into ``folder``; None when Telegram does not give it, as for files over 20 MB."""
    if not file_id:
        return None
    try:
        data, remote = api.fetch(file_id)
    except TelegramError:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (f"{stem}-{safe_name(name)}" if name else safe_name(f"{stem}{Path(remote).suffix}"))
    path.write_bytes(data)
    return path


FRAMES = 4


def frames(video: Path) -> Path | None:
    """One image of ``FRAMES`` frames spread over a moving file, left to right, saved next to it,
    so Claude sees what happens in it; None when it cannot be made (no ffmpeg, not a video)."""
    strip = video.with_name(f"{video.stem}-frames.jpg")
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(probe.stdout.strip() or 0)
        if duration <= 0:
            return None
        made = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
             f"fps={FRAMES / duration},scale=320:-2,tile={FRAMES}x1", "-frames:v", "1", str(strip)],
            capture_output=True, timeout=120,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return strip if made.returncode == 0 and strip.exists() else None


def _size(size: int | None) -> str:
    if not size:
        return ""
    return f", {size / 1048576:.1f} MB" if size >= 1048576 else f", {max(1, size // 1024)} KB"


def _duration(seconds: int | None) -> str:
    return f" {seconds // 60}:{seconds % 60:02d}" if seconds else ""


def _map(lat: float | None, lon: float | None) -> str:
    return f"{lat}, {lon}: https://maps.google.com/?q={lat},{lon}"


def describe(
    kind: str,
    payload: dict,
    caption: str,
    saved: Path | None = None,
    preview: Path | None = None,
    heard: str | None = None,
    telegram_id: int | None = None,
    strip: Path | None = None,
) -> str:
    """The text Claude reads for one message: what it holds, where its file was saved, its words."""
    at = f", saved at {saved}" if saved else ", which could not be downloaded (Telegram gives bots files up to 20 MB)"
    if strip:
        shown = f", {FRAMES} frames spread over it, left to right, in one image at {strip}"
    else:
        shown = f", preview image at {preview}" if preview else ""
    spoken = f", what is said in it: «{heard}»" if heard else ""
    p = payload
    body = {
        "text": "",
        "photo": f"[photo{at}]",
        "document": f"[file «{p.get('name') or 'without a name'}»{_size(p.get('size'))}{at}]",
        "video": f"[video{_duration(p.get('duration'))}{_size(p.get('size'))}{at}{shown}]",
        "animation": f"[GIF{at}{shown}]",
        "audio": f"[audio «{p.get('title') or p.get('name') or 'untitled'}»{_duration(p.get('duration'))}{at}{spoken}]",
        "voice": "",
        "video_note": f"[round video message{_duration(p.get('duration'))}{shown}]",
        "sticker": f"[sticker {p.get('emoji', '')} from the set «{p.get('set', '')}»"
                   f"{shown or (f', image at {saved}' if saved else '')}]",
        "location": f"[{'live ' if p.get('live') else ''}location {_map(p.get('lat'), p.get('lon'))}]",
        "venue": f"[place «{p.get('title', '')}», {p.get('address', '')}, {_map(p.get('lat'), p.get('lon'))}]",
        "contact": f"[contact {p.get('name', '')}, {p.get('phone', '')}]",
        "poll": f"[poll «{p.get('question', '')}»: {' / '.join(p.get('options', []))}]",
        "dice": f"[dice {p.get('emoji', '')}: {p.get('value')}]",
    }.get(kind, f"[a Telegram message of type {p.get('type', 'unknown')}, which cannot be opened]")

    words = heard if kind in SOUND_ONLY else caption
    parts = [part for part in (body, words) if part]
    if p.get("forwarded"):
        parts.insert(0, f"[forwarded from {p['forwarded']}]")
    if p.get("edited"):
        parts.insert(0, f"[edited their message #{telegram_id}, which now reads]" if telegram_id else "[edited]")
    return " ".join(parts)
