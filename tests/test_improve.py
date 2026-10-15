"""Checks for self-improvement: the nightly study's proposals, kept, decided in the menu and made by a mind."""

import json
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import OWNER, FakeRunner, shown, targets, worker_setup
from zoe import access, improve, minds, store
from zoe.claude_runner import Result
from zoe.menu import Menu

ANSWER = {
    "proposals": [
        {"title": "Album per i caroselli", "benefit": "Le foto arrivano insieme.", "plan": "Directive send.",
         "checked": "Provato su 3 foto.", "careful": False},
        {"title": "Avviso spese", "benefit": "Ti avviso prima di spendere.", "plan": "Hook.", "careful": True},
        {"title": "Terza", "benefit": "Troppe.", "plan": "Mai."},
    ],
    "summary": "Ho imparato come usi le liste.",
}


def _setup(tmp: Path, *replies: str):
    conn, owner, worker, runner, api = worker_setup(tmp, *replies)
    cfg = replace(worker._cfg, owner_chat=OWNER)
    return conn, owner, cfg, runner, Menu(cfg, api)


def _ask(conn, menu: Menu, **request: str) -> dict:
    return menu.answer(conn, access.resolve(conn, OWNER).person, request)


def test_a_study_keeps_at_most_two_complete_proposals_and_what_it_learned():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, cfg, runner, _ = _setup(Path(tmp), json.dumps(ANSWER), "no JSON here")
        kept = improve.study(conn, cfg, runner, 1000.0)
        assert [improve.get(conn, n).title for n in kept] == ["Album per i caroselli", "Avviso spese"]
        assert improve.get(conn, kept[1]).careful and store.get_state(conn, improve.SUMMARY) == ANSWER["summary"]
        assert store.get_state(conn, improve.STATE) == "1000.0" and runner.profiles[0].cwd == cfg.workdir

        improve.drop(conn, kept[1])
        assert improve.study(conn, cfg, runner, 2000.0) == []
        prompt = runner.prompts[-1]
        assert "still waiting for an answer (do not repeat them):\n- Album per i caroselli" in prompt
        assert "set aside: never again, unless something changed, and then say what:\n- Avviso spese" in prompt


def test_a_study_cut_short_runs_again():
    class Cut(FakeRunner):
        def run(self, *args, **kwargs) -> Result:
            return Result(False, "", False, False, None, 1.0, cancelled=True)

    with tempfile.TemporaryDirectory() as tmp:
        conn, _, cfg, _, _ = _setup(Path(tmp))
        assert improve.study(conn, cfg, Cut(), 1000.0) == [] and store.get_state(conn, improve.STATE) is None


def test_the_owner_makes_or_sets_aside_a_proposal_in_the_menu():
    with tempfile.TemporaryDirectory() as tmp:
        conn, owner, cfg, _, menu = _setup(Path(tmp))
        make, drop = improve.record(conn, ANSWER, time.time())
        [entry] = [item for item in _ask(conn, menu, view="home")["blocks"][0]["items"] if item["to"] == "improve"]
        assert entry["count"] == 2

        view = _ask(conn, menu, view="improve")
        sections = next(block["sections"] for block in view["blocks"] if "sections" in block)
        assert [s["open"] for s in sections] == [True, False] and "da fare con attenzione" in sections[1]["detail"]
        assert targets(sections[0]) == [f"make:{make}", f"drop:{make}"]
        assert "Ho imparato come usi le liste." in shown(view)

        view = _ask(conn, menu, action=f"make:{make}")
        [mind] = minds.active(conn, owner.id)
        assert view["notice"].startswith("Ci lavora una mente parallela") and mind.chat_id == OWNER
        assert "Directive send." in mind.brief and "self-improvement skill" in mind.brief
        assert _ask(conn, menu, action=f"make:{make}")["notice"] == "Questa proposta è già stata decisa."

        view = _ask(conn, menu, action=f"drop:{drop}")
        assert view["notice"] == "Scartata." and improve.waiting(conn) == []
        decided = next(b["items"] for b in view["blocks"] if "items" in b)
        assert [(row["label"], row["to"]) for row in decided] == [
            ("Avviso spese", ""), ("Album per i caroselli", f"mind:{mind.id}")]

        mia = access.create_person(conn, "Mia", "member", "it")
        refused = menu.answer(conn, mia, {"action": f"make:{drop}"})
        assert refused["notice"] == "Questa funzione è riservata al proprietario." and len(minds.active(conn, owner.id)) == 1


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
