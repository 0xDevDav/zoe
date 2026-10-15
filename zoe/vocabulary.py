"""Teach the speech recognizer the user's own vocabulary.

A generic recognizer mishears what it has never seen: names, brands, jargon.
Zoe keeps those words as ``term`` neurons, learned two ways:

- from what the user types, where unusual words (mixed case, acronyms, proper
  nouns, words with digits) are reinforced each time they appear and wired to
  the words they appear with;
- from corrections Claude reports after reading a voice transcript, which also
  store the misheard form as an ``alias`` neuron linked to its term.

Before each transcription the glossary is built from the terms wired to the
current conversation first, then from the most salient ones, and known aliases
are fixed in every transcript.
"""

from __future__ import annotations

import re

from . import i18n
from .brain import Brain

TERM = "term"
ALIAS = "alias"

_EDGE_PUNCT = ".,;:!?()[]{}\"'«»“”…*_`~|"
_STOPWORDS = {"ok", "ah", "eh", "oh", "uh", "lol", "ahah", "ahahah", "boh", "mah"}
MAX_TERMS = 15
_MAX_LINES = 30


def _is_term(word: str, sentence_start: bool) -> bool:
    if len(word) < 2 or word.lower() in _STOPWORDS or word.startswith(("http", "@", "/", "#", "<")) or "](" in word:
        return False
    if not any(c.isalpha() for c in word):
        return False
    if any(c.isdigit() for c in word):
        return True
    if word.isupper():
        return len(word) <= 6
    if any(c.isupper() for c in word[1:]):
        return True
    return word[0].isupper() and not sentence_start


def extract_terms(text: str) -> list[str]:
    """Return the words in ``text`` that a generic recognizer is likely to get wrong."""
    terms: list[str] = []
    sentence_start = True
    for raw in text.split():
        word = raw.strip(_EDGE_PUNCT)
        if word and _is_term(word, sentence_start) and word not in terms:
            terms.append(word)
        sentence_start = raw.endswith((".", "!", "?", "…"))
    return terms


def learn_from_text(brain: Brain, text: str) -> list[int]:
    """Reinforce the unusual words of a typed message and wire them together.

    Pasted code and logs are skipped and at most ``MAX_TERMS`` words are taken, so a
    long message cannot spend minutes wiring every pair of its words.
    """
    if "```" in text or text.count("\n") > _MAX_LINES:
        return []
    ids = []
    for term in extract_terms(text)[:MAX_TERMS]:
        nid = brain.ensure(TERM, term)
        brain.boost(nid, 0.1)
        ids.append(nid)
    brain.fire(ids)
    return ids


def learn_correction(brain: Brain, term: str, heard: str) -> None:
    """Record that ``heard`` in a transcript stands for ``term``."""
    term, heard = term.strip(), heard.strip()
    if not term or not heard or term.lower() == heard.lower():
        return
    term_id = brain.ensure(TERM, term)
    brain.boost(term_id, 0.5)
    brain.connect(brain.ensure(ALIAS, heard.lower()), term_id, delta=0.5)


def apply_corrections(brain: Brain, transcript: str, min_weight: float = 0.3) -> str:
    """Replace known mishearings with the terms they stand for."""
    seen: set[str] = set()
    for heard, term, _ in brain.pairs(ALIAS, TERM, min_weight):
        if heard in seen:
            continue
        seen.add(heard)
        transcript = re.sub(
            rf"(?<!\w){re.escape(heard)}(?!\w)", lambda _m, t=term: t, transcript, flags=re.IGNORECASE
        )
    return transcript


def glossary(brain: Brain, context: str, language: str, max_chars: int = 700) -> str:
    """Build the recognizer prompt: terms tied to the conversation first, then the most salient."""
    ordered: list[str] = []
    seeds = [nid for term in extract_terms(context) if (nid := brain.find(TERM, term))]
    for r in brain.recall(seeds, limit=60):
        if r.kind == TERM and r.content not in ordered:
            ordered.append(r.content)
    for r in brain.top(TERM, limit=200):
        if r.content not in ordered:
            ordered.append(r.content)

    prefix = i18n.t(language, "glossary_prefix") + ": "
    chosen: list[str] = []
    size = len(prefix)
    for term in ordered:
        if size + len(term) + 2 > max_chars:
            break
        chosen.append(term)
        size += len(term) + 2
    return prefix + ", ".join(chosen) if chosen else ""
