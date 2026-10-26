"""Turn whatever Claude writes into clean Telegram HTML that follows the writing standard.

Claude is asked to write Telegram HTML, but the formatter does not depend on it.
It accepts Markdown too, keeps only the tags Telegram renders, and applies the
mechanical rules of ``style`` to every reply:

- Markdown emphasis, code, links, headings, quotes, lists and tables become
  Telegram HTML, titles and lists;
- the long dash and the en dash are replaced;
- an emoji that ends a sentence ends its line, except in section titles;
- a section title gets a blank line before it, and so does a paragraph after a list.

The result is always valid for Telegram's HTML parse mode.
"""

from __future__ import annotations

import html
import re

_ALLOWED = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "a", "blockquote", "tg-spoiler", "span",
}

_EMOJI_CHARS = "⌀-⏿☀-➿⬀-⯿〰〽㊗㊙\U0001f000-\U0001faff"
_EMOJI_TAIL = "[︎️\U0001f3fb-\U0001f3ff]*"
EMOJI = re.compile(
    f"(?:[\U0001f1e6-\U0001f1ff]{{2}}|[{_EMOJI_CHARS}]){_EMOJI_TAIL}(?:‍[{_EMOJI_CHARS}]{_EMOJI_TAIL})*"
)
_EMOJI_CLUSTER = re.compile(f"(?:{EMOJI.pattern})(?:[ \t]*(?:{EMOJI.pattern}))*")

_MID_SENTENCE = ",.;:!?)]}»…"

_HELD = re.compile(r"\x00(\d+)\x00")
_MARKUP = re.compile(r"<[^<>]+>|\x00\d+\x00")
_ENTITY = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)")
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)((?:\s[^<>]*)?)/?>")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")


class _Held:
    """Fragments kept out of the text while it is transformed, restored at the end."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.late: set[int] = set()

    def hold(self, fragment: str, late: bool) -> str:
        self.items.append(fragment)
        if late:
            self.late.add(len(self.items) - 1)
        return f"\x00{len(self.items) - 1}\x00"

    def restore(self, text: str, late: bool) -> str:
        def put_back(match: re.Match) -> str:
            index = int(match.group(1))
            return self.items[index] if (index in self.late) == late else match.group(0)

        return _HELD.sub(put_back, text)


def _escape(text: str) -> str:
    return _ENTITY.sub("&amp;", text).replace("<", "&lt;").replace(">", "&gt;")


def _visible(fragment: str) -> str:
    return _MARKUP.sub("", fragment)


def _clean_tag(match: re.Match) -> str | None:
    closing, name, attrs = match.group(1), match.group(2).lower(), match.group(3) or ""
    if name not in _ALLOWED:
        return None
    if closing:
        return f"</{name}>"
    if name == "a":
        href = re.search(r"href\s*=\s*\"([^\"]*)\"|href\s*=\s*'([^']*)'", attrs)
        if not href:
            return None
        return f'<a href="{html.escape(html.unescape(href.group(1) or href.group(2)), quote=True)}">'
    if name == "blockquote":
        return "<blockquote expandable>" if "expandable" in attrs else "<blockquote>"
    if name == "span":
        return '<span class="tg-spoiler">' if "tg-spoiler" in attrs else None
    if name == "code":
        language = re.search(r"class\s*=\s*\"(language-[\w+-]+)\"", attrs)
        return f'<code class="{language.group(1)}">' if language else "<code>"
    return f"<{name}>"


def _pre(code: str, language: str) -> str:
    body = _escape(code.rstrip("\n"))
    return f'<pre><code class="language-{language}">{body}</code></pre>' if language else f"<pre>{body}</pre>"


def _tables(text: str) -> str:
    out: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if len(block) < 2:
            out.extend(block)
        else:
            rows = [[c.strip() for c in _TABLE_ROW.match(r).group(1).split("|")] for r in block if not _TABLE_RULE.match(r)]
            if _TABLE_RULE.match(block[1]) and rows:
                out.append("<b>" + ", ".join(rows.pop(0)) + "</b>")
            out.extend("• " + (f"{cells[0]}: {', '.join(cells[1:])}" if len(cells) > 1 else cells[0]) for cells in rows)
        block.clear()

    for line in text.split("\n"):
        if _TABLE_ROW.match(line):
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def _lines(text: str) -> str:
    out: list[str] = []
    quote: list[str] = []

    def flush_quote() -> None:
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for line in text.split("\n"):
        quoted = re.match(r"^\s*&gt;\s?(.*)$", line)
        if quoted:
            quote.append(quoted.group(1))
            continue
        flush_quote()
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            out.append(f"<b>{heading.group(1)}</b>")
            continue
        out.append(re.sub(r"^(\s*)[-*+]\s+(?=\S)", r"\1• ", line))
    flush_quote()
    return "\n".join(out)


def _inline(text: str, held: _Held) -> str:
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        lambda m: held.hold(f'<a href="{m.group(2)}">', late=True) + m.group(1) + "</a>",
        text,
    )
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text)
    text = re.sub(r"\|\|(?=\S)(.+?)(?<=\S)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", text)
    return re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<i>\1</i>", text)


def _dashes(text: str) -> str:
    text = re.sub(r"[ \t]+[—–][ \t]+", ", ", text)
    return re.sub(r"[—–]", "-", text)


def _break_after_emoji(line: str) -> list[str]:
    """Split the line after each emoji that ends a sentence, unless the emoji opens the line.

    An emoji followed by a lowercase letter or by punctuation sits inside its sentence and stays.
    """
    for match in _EMOJI_CLUSTER.finditer(line):
        if _visible(line[: match.start()]).strip() in ("", "•"):
            continue
        rest = line[match.end():]
        closing = re.match(r"(?:\s*</[^<>]+>)*", rest).group(0)
        after = rest[len(closing):]
        following = _visible(after).lstrip()
        if not re.search(r"\w", following) or following[0].islower() or following[0] in _MID_SENTENCE:
            continue
        return [line[: match.end()] + closing, *_break_after_emoji(after.lstrip())]
    return [line]


def _is_bullet(line: str) -> bool:
    return _visible(line).lstrip().startswith("•")


def _is_title(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("<b>"):
        return False
    return bool(EMOJI.match(stripped[3:])) or (stripped.endswith("</b>") and "</b>" not in stripped[:-4])


def _space_blocks(lines: list[list[str]]) -> str:
    """Join lines, each given with the pieces it was split into, adding a blank line between blocks.

    The pieces of one line stay together, so a bullet split after an emoji remains one item.
    """
    out: list[str] = []
    previous = ""
    for pieces in lines:
        head = pieces[0]
        if out and out[-1].strip() and head.strip():
            if _is_title(head) or (_is_bullet(previous) and not _is_bullet(head)):
                out.append("")
        out.extend(pieces)
        previous = head
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _balanced(text: str) -> bool:
    stack: list[str] = []
    for match in re.finditer(r"<(/?)([a-z-]+)[^<>]*>", text):
        if not match.group(1):
            stack.append(match.group(2))
        elif not stack or stack.pop() != match.group(2):
            return False
    return not stack


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^<>]+>", "", text)


def to_telegram(text: str) -> str:
    """Return ``text`` as Telegram HTML that follows the writing standard."""
    held = _Held()
    text = text.replace("\r\n", "\n")
    text = re.sub(r"```([\w+-]*)[ \t]*\n?(.*?)```", lambda m: held.hold(_pre(m.group(2), m.group(1)), late=True),
                  text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: held.hold(f"<code>{_escape(m.group(1))}</code>", late=True), text)

    def keep_tag(match: re.Match) -> str:
        tag = _clean_tag(match)
        if tag is None:
            return match.group(0)
        return held.hold(tag, late=tag.startswith(("<a ", "<code", "<pre")))

    text = _escape(_TAG.sub(keep_tag, text))
    text = held.restore(text, late=False)
    text = _inline(_lines(_tables(text)), held)
    text = _dashes(text)
    text = _space_blocks([_break_after_emoji(line) for line in text.split("\n")])
    text = held.restore(text, late=True).strip()
    return text if _balanced(text) else _strip_tags(text)


def split(text: str, limit: int = 4096) -> list[str]:
    """Split formatted text into messages under Telegram's length limit, at block boundaries."""
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        chunks.append(current)
    return [c if _balanced(c) else _strip_tags(c) for c in chunks]


def to_plain(text: str) -> str:
    """Return the words of ``text`` without markup or emoji, for speech."""
    plain = html.unescape(_strip_tags(to_telegram(text)))
    plain = EMOJI.sub("", plain).replace("•", "")
    return "\n".join(" ".join(line.split()) for line in plain.splitlines()).strip()
