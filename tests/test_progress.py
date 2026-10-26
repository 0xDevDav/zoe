"""Checks for the live progress message and the reading of Claude's event stream."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeAPI
from zoe.claude_runner import Step, to_steps
from zoe.progress import Progress, action, describe, elapsed


def test_stream_events_become_steps():
    tool = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "gold price"}}]}}
    assert to_steps(tool) == [Step("tool", "WebSearch", {"query": "gold price"})]
    thinking = {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "thinking"}}}
    assert to_steps(thinking) == [Step("thinking")]
    writing = {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}}
    assert to_steps(writing) == [Step("writing")]
    assert to_steps({"type": "stream_event", "event": {"type": "content_block_delta"}}) == []
    said = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Leggo il codice."}, {"type": "tool_use", "name": "Read", "input": {}}]}}
    assert to_steps(said) == [Step("said", args={"text": "Leggo il codice."}), Step("tool", "Read", {})]


def test_steps_read_naturally_in_both_languages():
    assert describe(Step("tool", "WebSearch", {"query": "prezzo oro"}), "it") == "Cerco sul web: prezzo oro"
    assert describe(Step("tool", "Read", {"file_path": "C:\\dati\\report.pdf"}), "it") == "Leggo: report.pdf"
    assert describe(Step("tool", "WebFetch", {"url": "https://www.example.com/x"}), "en") == "Reading a page: www.example.com"
    assert describe(Step("tool", "Bash", {"command": "ls", "description": "List files"}), "en") == "Running: List files"
    assert describe(Step("tool", "Bash", {"command": "cd /tmp && cat > t.py"}), "it") == "Eseguo un comando"
    assert describe(Step("tool", "mcp__calendar__list_events", {}), "it") == "Uso calendar"
    assert describe(Step("tool", "WebSearch", {"query": "<b>x</b>"}), "it") == "Cerco sul web: <b>x</b>"
    assert describe(Step("thinking"), "it") == "Ragiono…"


def test_progress_message_lifecycle():
    api = FakeAPI()
    progress = Progress(api, 42, "it", interval=60)
    progress.start()
    assert "Al lavoro…" in api.to(42)[0] and api.actions == ["typing"]

    progress.step(Step("tool", "WebSearch", {"query": "meteo"}))
    progress.step(Step("tool", "WebSearch", {"query": "meteo"}))
    progress.tick()
    assert len(api.edits) == 1 and api.edits[0][2].count("Cerco sul web: meteo") == 1

    progress.finish()
    assert api.deleted == [(42, 101)]


def test_the_message_shows_only_what_is_happening_now():
    progress = Progress(FakeAPI(), 42, "it", interval=60)
    progress.step(Step("listening"))
    progress.start()
    assert progress.render() == "<b>Ascolto…</b> 0:00 🎧"
    progress.step(Step("thinking"))
    assert progress.render() == "<b>Ragiono…</b> 0:00 💭"
    progress.step(Step("tool", "Bash", {"command": "ls", "description": "Controllo gli ordini"}))
    progress.step(Step("tool", "Read", {"file_path": "C:\\dati\\report.pdf"}))
    assert progress.render() == "<b>Al lavoro…</b> 0:00 ⏳\n\n📄 Leggo: report.pdf"
    progress.step(Step("thinking"))
    assert progress.render() == "<b>Ragiono…</b> 0:00 💭\n\n📄 Leggo: report.pdf"
    progress.step(Step("writing"))
    assert progress.render() == "<b>Scrivo la risposta…</b> 0:00 ✍️"
    progress.finish()


def test_what_zoe_remembers_shows_like_a_tool():
    progress = Progress(FakeAPI(), 42, "it", interval=60)
    progress.step(Step("recall", args={"memory": "Alex vuole i messaggi da inoltrare senza commenti intorno"}))
    assert progress.render() == "<b>Al lavoro…</b> 0:00 ⏳\n\n🧠 Ricordo: Alex vuole i messaggi da inoltrare…"
    progress.step(Step("thinking"))
    assert progress.render().endswith("🧠 Ricordo: Alex vuole i messaggi da inoltrare…")
    progress.step(Step("recall", args={"memory": ""}))
    assert progress.render().endswith("\n\n🧠 Ricordo")


def test_an_answer_that_will_be_a_voice_note_is_recorded_not_written():
    progress = Progress(FakeAPI(), 42, "it", "record_voice", interval=60)
    progress.start()
    progress.step(Step("writing"))
    assert progress.render() == "<b>Registro il vocale…</b> 0:00 🎙️"
    progress.finish()


def test_an_action_fits_one_line_and_a_command_says_only_what_it_does():
    long = Step("tool", "Bash", {"command": "ls", "description": "Controllo gli ordini arrivati oggi e le fatture del mese"})
    assert action(long, "it") == "⚙️ Controllo gli ordini arrivati oggi e le fat…"
    assert action(Step("tool", "Bash", {"command": "ls -la"}), "it") == "⚙️ Eseguo un comando"
    assert action(Step("tool", "WebSearch", {"query": "<b>x</b>"}), "it") == "🔎 Cerco sul web: &lt;b&gt;x&lt;/b&gt;"


def test_the_progress_message_becomes_the_answer_or_goes_away():
    api = FakeAPI()
    progress = Progress(api, 42, "it", interval=60)
    progress.start()
    assert progress.become("Fatto ✅") == 101
    progress.finish()
    assert api.to(42) == ["Fatto ✅"] and api.deleted == []

    api.edits_fail = True
    progress = Progress(api, 42, "it", interval=60)
    progress.start()
    assert progress.become("Fatto ✅") is None
    progress.finish()
    assert api.deleted == [(42, 102)]


def test_the_progress_message_moves_below_new_messages():
    api = FakeAPI()
    progress = Progress(api, 42, "it", interval=60)
    progress.start()
    progress.renew()
    progress.tick()
    assert len(api.to(42)) == 2 and api.deleted == [(42, 101)]
    progress.finish()
    assert api.deleted == [(42, 101), (42, 102)]


def test_elapsed_time_format():
    assert elapsed(5) == "0:05" and elapsed(83) == "1:23" and elapsed(725) == "12:05"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
