"""Checks for vocabulary learning: term extraction, glossary building, and corrections."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe.brain import Brain
from zoe.db import apply_schema
from zoe.vocabulary import (
    TERM,
    apply_corrections,
    extract_terms,
    glossary,
    learn_correction,
    learn_from_text,
)


def _brain() -> Brain:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return Brain(conn)


def test_extract_terms_picks_unusual_words():
    terms = extract_terms("Domani apro GitLab e controllo OpenShift sulla VPS con Marco. Poi pausa con 3Scale.")
    assert {"GitLab", "OpenShift", "VPS", "Marco", "3Scale"} <= set(terms)
    assert "Domani" not in terms and "Poi" not in terms and "pausa" not in terms
    assert extract_terms("Uso **Grafana** e [la Doc](https://ex.com/Path) con `Kubernetes` <u>Sara</u>") == [
        "Grafana", "Kubernetes"]


def test_typed_terms_reach_glossary():
    brain = _brain()
    for _ in range(3):
        learn_from_text(brain, "ricordati di aprire Grafana e OpenShift")
    text = glossary(brain, "", "it", 200)
    assert text.startswith("Glossario: ")
    assert "Grafana" in text and "OpenShift" in text


def test_long_messages_and_pasted_code_cannot_flood_the_graph():
    brain = _brain()
    assert len(learn_from_text(brain, " ".join(f"Termine{i}" for i in range(500)))) == 15
    assert learn_from_text(brain, "guarda:\n```\nKubernetes OpenShift Grafana\n```") == []
    assert learn_from_text(brain, "\n".join(f"ERROR Pod{i}" for i in range(40))) == []


def test_a_pasted_key_never_becomes_a_term_but_job_ids_and_names_do():
    brain = _brain()
    key = "sk-or-v1-" + "0123456789abcdef" * 4
    job = "3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f"
    learn_from_text(brain, f"la chiave di Zylo: {key} e poi il job {job} di Mia3")
    terms = {r.content for r in brain.top(TERM)}
    assert key not in terms and not any("0123456789abcdef" in t for t in terms)
    assert {job, "Mia3", "Zylo"} <= terms
    assert "0123456789abcdef" not in glossary(brain, "", "it")


def test_glossary_respects_budget():
    brain = _brain()
    for i in range(100):
        brain.ensure(TERM, f"Termine{i:03d}")
    assert len(glossary(brain, "", "en", 120)) <= 120


def test_conversation_terms_come_first():
    brain = _brain()
    for i in range(50):
        brain.boost(brain.ensure(TERM, f"Popolare{i}"), 5.0)
    learn_from_text(brain, "Abito tra Valdoria e Montelago")
    text = glossary(brain, "andiamo a Valdoria stasera", "it", 60)
    assert "Valdoria" in text and "Montelago" in text


def test_correction_fixes_future_transcripts():
    brain = _brain()
    learn_correction(brain, "Grafana", "gravana")
    assert apply_corrections(brain, "apri Gravana adesso") == "apri Grafana adesso"
    assert apply_corrections(brain, "gravanalizzare") == "gravanalizzare"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
