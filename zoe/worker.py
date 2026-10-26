"""Worker thread: turn one pending message into one reply.

Several workers run side by side. Each takes a message, or a plan that came due
or news from a parallel mind, which run like one without a progress message,
opens the memory of the
person who sent it, shows a live progress message while Claude works with that
person's powers, then replaces it with the answer, formatted to the writing
standard, in the chat the message came from, and records what was learned. A
stuck turn is caught here, by the code that runs it.
"""

from __future__ import annotations

import html
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

from . import (
    access, approve, availability, choices, content, directives, files, formatting, i18n, minds, planner, prompt,
    receipts, store, vocabulary,
)
from .access import Person
from .brain import Brain
from .claude_runner import ClaudeRunner, Later, Profile, Result, Step, unavailable
from .config import Config
from .db import connect
from .progress import Progress
from .speech import Speaker, SpeechError, Transcriber
from .telegram import CAPTION_LIMIT, TelegramAPI, TelegramError

log = logging.getLogger("zoe.worker")

# Kinds whose file is saved for Claude to open, and kinds whose preview image is saved.
_SAVED = ("photo", "document", "video", "animation", "audio", "sticker")
_PREVIEWED = ("video", "animation", "video_note")
# Saved kinds that move: they get one image of frames spread over them.
_MOVING = ("video", "animation", "sticker")
# Seconds to wait for the rest of an album, whose files arrive as separate messages.
ALBUM_WAIT = 1.5

# Seconds between looks for messages written during a turn, and the pause after the
# last one before the turn stops to take them in.
FOLLOWUP_POLL = 1.0
FOLLOWUP_PAUSE = 2.5

_INTERNAL = (planner.PLAN, minds.EVENT)


def _taking_in(kind: str) -> str:
    """What the progress message says while a message of this kind is taken in."""
    if kind in content.SPOKEN:
        return "listening"
    return "downloading" if kind in _SAVED or kind in _PREVIEWED else "working"


def _origin(msg: store.Message) -> dict | None:
    """Note on a sent message the plan it carried out or the mind it is news of, so a later
    reply to it leads back there."""
    if msg.kind == planner.PLAN and msg.payload:
        return {"plan": msg.payload["plan"]}
    if msg.kind == minds.EVENT and msg.payload:
        return {"mind": msg.payload["mind"], "neuron": msg.payload.get("neuron")}
    return None


def _speaker(msg: store.Message, person: Person) -> str:
    """Who a message comes from, as remembered in episodes."""
    if msg.kind == planner.PLAN:
        return "Plan"
    if msg.kind == minds.EVENT:
        return f"Parallel mind #{(msg.payload or {}).get('mind')}"
    return person.name


class Worker(threading.Thread):
    def __init__(
        self,
        cfg: Config,
        api: TelegramAPI,
        stop: threading.Event,
        transcriber: Transcriber | None = None,
        speaker: Speaker | None = None,
        name: str = "worker",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._cfg = cfg
        self._api = api
        self._halt = stop
        self._transcriber = transcriber
        self._speaker = speaker
        self._runner = ClaudeRunner(
            cfg.claude_bin, cfg.claude_model, cfg.claude_timeout, cfg.claude_inactivity_timeout
        )

    def run(self) -> None:
        conn = connect(self._cfg.db_path)
        log.info("%s started", self.name)

        while not self._halt.is_set():
            if availability.waiting(conn):
                self._halt.wait(5.0)
                continue
            try:
                msg = store.claim_next(conn)
            except Exception:
                log.exception("%s could not take the next message", self.name)
                self._halt.wait(5.0)
                continue
            if msg is None:
                store.wait_for_work(1.0)
                continue
            try:
                self._handle(conn, msg)
            except Exception:
                log.exception("unhandled error on message %d", msg.id)
                self._recover(conn, msg)

        conn.close()
        log.info("%s stopped", self.name)

    def _recover(self, conn: sqlite3.Connection, msg: store.Message) -> None:
        """After a turn broke: retry it or set it aside, without ever stopping the worker."""
        try:
            person = access.get_person(conn, msg.person_id)
            if person is None:
                store.mark(conn, msg.id, "failed")
            else:
                self._retry_or_give_up(conn, person, msg, "error_notice")
        except Exception:
            log.exception("message %d stays in progress until the next start", msg.id)

    def _handle(self, conn: sqlite3.Connection, msg: store.Message) -> None:
        person = access.get_person(conn, msg.person_id)
        if person is None:
            store.mark(conn, msg.id, "skipped")
            return
        brain = Brain(conn, owner=person.id)

        internal = msg.kind in _INTERNAL
        profile = access.turn_profile(person, self._cfg)
        if person.role == "owner":
            profile = approve.asking(profile, self._cfg.data_dir, msg.chat_id)
        key = directives.new_key()
        # Plans and news from parallel minds show no progress: nobody is waiting for them,
        # and a plan may end up sending nothing. A person sees it at once, before what they
        # sent is downloaded or heard.
        progress = None if internal else Progress(
            self._api, msg.chat_id, person.language,
            "record_voice" if msg.kind == "voice" and self._speaker is not None else "typing",
        )
        try:
            if progress:
                progress.step(Step("working" if (msg.payload or {}).get("read") else _taking_in(msg.kind)))
                progress.start()
            if (msg.payload or {}).get("group"):
                self._halt.wait(ALBUM_WAIT)
            batch = [msg] + ([] if internal else store.absorb(conn, msg.chat_id, msg.id))
            read = self._read(conn, brain, person, batch, profile.cwd / "inbox", progress)
            if read:
                self._answer(conn, brain, person, read, profile, key, progress)
        finally:
            if progress:
                progress.finish()

    def _answer(
        self,
        conn: sqlite3.Connection,
        brain: Brain,
        person: Person,
        read: list[tuple[store.Message, str]],
        profile: Profile,
        key: str,
        progress: Progress | None,
    ) -> None:
        first = read[0][0]
        spoken = first.kind == "voice" and self._speaker is not None

        def fresh() -> tuple[str, prompt.Recall]:
            return prompt.build(
                conn, brain, person, first.chat_id, read[0][1], profile.cwd,
                transcribed=any(m.kind in content.SPOKEN for m, _ in read), spoken=spoken,
                chosen=first.kind == "choice", telegram_id=first.telegram_id,
                plan=first.payload if first.kind == planner.PLAN else None,
                mind=first.payload if first.kind == minds.EVENT else None,
                reply=(first.payload or {}).get("reply"),
                followups=[(m.telegram_id, text) for m, text in read[1:]], key=key,
                temp_file_days=self._cfg.temp_file_days, archive=self._cfg.db_path,
            )

        session = (first.payload or {}).get("resume", "")
        if session:
            # The resumed session still holds the first try's prompt, so its key goes on.
            key = first.payload.get("key", key)
        prompt_text, recalled = fresh()
        if progress and recalled.memories:
            # The progress shows what Zoe remembers, as it shows the tools she uses.
            lasting = next((r.content for r in recalled.memories if r.kind != "episode"), "")
            progress.step(Step("recall", args={"memory": lasting}))
        stored = (first.payload or {}).get("answer")
        if session:
            prompt_text = prompt.RESUMED
        if stored:
            # The turn ran and only its delivery failed: deliver the same answer, with its own key.
            result, key = Result(True, stored["text"], False, False, 0, 0.0), stored["key"]
        else:
            result = self._converse(conn, brain, person, read, prompt_text, profile, progress, fresh, session)
        try:
            self._finish(conn, brain, person, read, recalled, result, spoken, key, progress)
        finally:
            if result.later:
                threading.Thread(target=self._follow, args=(result.later, person.id, read, key),
                                 name=f"{self.name}-later-{first.id}", daemon=True).start()

    def _follow(self, later: Later, person_id: int, read: list[tuple[store.Message, str]], key: str) -> None:
        """Deliver what a turn's session says after its answer, once the background tasks it
        waits for end, as further replies of the same turn: each is delivered, remembered and
        acted on like the first, in writing and with no progress message. Work stopped before
        it ends is told to the person, except when the service stops: then a plan due at once
        takes the waits up again after the start. The chat is free meanwhile: the next message
        starts a turn and a session of its own, since only a stuck turn resumes its session."""
        conn = connect(self._cfg.db_path)
        try:
            with closing(later.answers(self._halt)) as answers:
                for result in answers:
                    person = access.get_person(conn, person_id)
                    if person is None:
                        break
                    if result.ok and result.text:
                        self._finish(conn, Brain(conn, owner=person.id), person, read, prompt.Recall([], []),
                                     result, False, key)
                    elif result.cancelled and self._halt.is_set():
                        self._resume_after_stop(conn, person, read, later.waits(), result.duration)
                    elif not result.ok:
                        log.warning("background work of message %d stopped after %.0fs", read[0][0].id,
                                    result.duration)
                        self._api.notify(read[0][0].chat_id, i18n.t(person.language, "background_stopped",
                                                                    minutes=round(result.duration / 60)))
        except Exception:
            log.exception("background work of message %d not followed to its end", read[0][0].id)
        finally:
            conn.close()

    def _resume_after_stop(self, conn: sqlite3.Connection, person: Person,
                           read: list[tuple[store.Message, str]], waits: list[str], duration: float) -> None:
        """Plan, due at once, the resumption of background waits that a stop of the service ended."""
        msg = read[0][0]
        instruction = prompt.STOPPED_WAITS.format(asked=read[0][1][:300], waits="\n".join(f"- {w}" for w in waits))
        try:
            planner.create(conn, Brain(conn, owner=person.id), person, msg.chat_id,
                           datetime.now().isoformat(timespec="seconds"), instruction, [])
        except planner.PlanError as exc:
            log.warning("background waits of message %d not planned again: %s", msg.id, exc)
            self._api.notify(msg.chat_id, i18n.t(person.language, "background_stopped", minutes=round(duration / 60)))
            return
        log.info("background waits of message %d ended by the stop, planned again: %s", msg.id, "; ".join(waits))

    def _read(
        self,
        conn: sqlite3.Connection,
        brain: Brain,
        person: Person,
        batch: list[store.Message],
        inbox: Path,
        progress: Progress | None = None,
    ) -> list[tuple[store.Message, str]]:
        """The text Claude reads for each message of a turn: files saved in the person's inbox, sound
        transcribed, everything else written out. A message read once keeps its text, so a retried
        turn does not download or transcribe again. A voice message that cannot be heard is set aside.
        The progress message says what is being taken in, then that the work goes on."""
        read = []
        for msg in batch:
            payload = msg.payload or {}
            if msg.kind in _INTERNAL or msg.kind == "choice" or payload.get("read"):
                read.append((msg, msg.text or ""))
                continue
            if progress:
                progress.step(Step(_taking_in(msg.kind)))
            heard = self._transcribe(conn, brain, msg, person.language) if msg.kind in content.SPOKEN else None
            if msg.kind in content.SOUND_ONLY and heard is None:
                self._api.notify(msg.chat_id, i18n.t(person.language, "voice_unavailable"))
                store.mark(conn, msg.id, "failed")
                continue
            folder, stem = inbox / time.strftime("%Y-%m-%d"), str(msg.telegram_id or msg.id)
            saved = content.save(self._api, payload.get("file_id"), folder, stem, payload.get("name")) \
                if msg.kind in _SAVED else None
            strip = content.frames(saved) if saved and msg.kind in _MOVING else None
            preview = content.save(self._api, payload.get("thumb"), folder, f"{stem}-preview") \
                if msg.kind in _PREVIEWED and not strip else None
            days = self._cfg.temp_file_days
            if saved:
                number = files.register(conn, person.id, msg.kind, saved, payload.get("file_id"), payload.get("size"), days)
                saved = f"{saved} (file #{number})"
            for image in (strip, preview):
                if image:
                    files.register(conn, person.id, "photo", image, None, None, days)
            text = content.describe(msg.kind, payload, msg.text or "", saved, preview, heard, msg.telegram_id, strip)
            store.set_read(conn, msg.id, text)
            if msg.kind == "text" and msg.text:
                vocabulary.learn_from_text(brain, msg.text)
            read.append((msg, text))
        if progress:
            progress.step(Step("working"))
        return read

    def _converse(
        self,
        conn: sqlite3.Connection,
        brain: Brain,
        person: Person,
        read: list[tuple[store.Message, str]],
        prompt_text: str,
        profile: Profile,
        progress: Progress | None,
        fresh: Callable[[], tuple[str, prompt.Recall]],
        session: str = "",
    ) -> Result:
        """Run the turn, from the start or resuming ``session``. Messages the person writes
        meanwhile stop it once they pause and no command is running, join ``read``, and the
        same session resumes with them."""
        while True:
            done, interrupt, news = threading.Event(), threading.Event(), {}
            watcher = threading.Thread(
                target=self._watch, args=(conn, read[-1][0], progress, done, interrupt, news),
                daemon=True,
            ) if progress else None
            if watcher:
                watcher.start()
            run_profile = Profile(profile.args + ("--resume", session), profile.cwd, profile.env) if session else profile
            result = self._runner.run(prompt_text, run_profile, on_step=progress.step if progress else None,
                                      interrupt=interrupt,
                                      follow_for=self._cfg.claude_background_timeout)
            done.set()
            if watcher:
                watcher.join()
            if not (result.cancelled and news.get("arrived")):
                return result

            added = self._read(conn, brain, person, store.absorb(conn, read[-1][0].chat_id, read[-1][0].id),
                               profile.cwd / "inbox", progress)
            read.extend(added)
            progress.step(Step("update"))
            session = result.session_id or session
            log.info("turn of message %d resumes with %d new message(s)", read[0][0].id, len(added))
            if not session:
                prompt_text = fresh()[0]
            elif added:
                prompt_text = prompt.update(person, [(m.telegram_id, text) for m, text in added])
            else:
                prompt_text = "Carry on with what you were doing."

    def _watch(
        self,
        conn: sqlite3.Connection,
        last: store.Message,
        progress: Progress,
        done: threading.Event,
        interrupt: threading.Event,
        news: dict,
    ) -> None:
        """Watch for messages the person writes during the turn and for questions that wait for
        their yes: move the progress message below them at once, so the answer it becomes comes
        after them, and ask the turn to stop once the person pauses, so that it resumes with
        their messages as soon as no command is running."""
        seen, since = 0, time.time()
        while not done.wait(FOLLOWUP_POLL):
            arrived = store.waiting(conn, last.chat_id, last.id)
            shown = len(arrived) + approve.asked(conn, last.chat_id, since)
            if shown > seen:
                seen = shown
                progress.renew()
            if arrived and time.time() - arrived[-1][1] >= FOLLOWUP_PAUSE:
                news["arrived"] = True
                interrupt.set()

    def _transcribe(self, conn: sqlite3.Connection, brain: Brain, msg: store.Message, language: str) -> str | None:
        if self._transcriber is None or not msg.payload:
            return None
        context = " ".join(turn["text"] for turn in store.recent_history(conn, msg.chat_id, limit=6))
        glossary = vocabulary.glossary(brain, context, language, self._cfg.glossary_chars)
        try:
            audio = self._api.download(msg.payload["file_id"])
            raw = self._transcriber.transcribe(audio, language, glossary or None)
        except (TelegramError, SpeechError) as exc:
            log.warning("message %d not transcribed: %s", msg.id, exc)
            return None
        return vocabulary.apply_corrections(brain, raw) or "[voice note with no speech]"

    def _finish(
        self,
        conn: sqlite3.Connection,
        brain: Brain,
        person: Person,
        read: list[tuple[store.Message, str]],
        recalled: prompt.Recall,
        result: Result,
        spoken: bool,
        key: str,
        progress: Progress | None = None,
    ) -> None:
        msg, last = read[0][0], read[-1][0]
        reason = unavailable(result)
        if reason:
            self._wait_for_claude(conn, msg, reason)
            return
        availability.works(conn, self._cfg.data_dir)
        if result.ok and result.text:
            reply, found = directives.extract(result.text, key)
            for term, heard in directives.corrections(found):
                vocabulary.learn_correction(brain, term, heard)
            skipped = directives.skip(found) if msg.kind == planner.PLAN else None
            try:
                outcome, sent_id = (f"[sent nothing: {skipped}]", None) if skipped else self._reply(
                    conn, person, last, reply, found, spoken, progress
                )
            except TelegramError:
                store.note(conn, msg.id, "answer", {"text": result.text, "key": key})
                raise
            for done, _ in read:
                store.mark(conn, done.id, "answered")
            said = "\n".join(f"{prompt.anchor(m.telegram_id)}{_speaker(m, person)}: {text}" for m, text in read)
            shown = [r.id for r in recalled.memories]
            used = [n for n in directives.used(found) if n in shown]
            brain.reinforce(used, [n for n in shown if n not in used])
            context = [*recalled.origins, *used]
            episode = brain.learn("episode", f"{said}\n{prompt.anchor(sent_id)}Zoe: {outcome}", related=context)
            receipt = receipts.Receipt(person.language)
            self._plan(conn, brain, person, msg.chat_id, found, [episode, *context], receipt)
            self._files(conn, brain, person, msg.chat_id, found, [episode, *context], receipt)
            if person.role == "owner":
                self._mind(conn, brain, person, last, found, [episode, *context])
                self._admit(conn, person, msg.chat_id, found, receipt)
            receipt.send(self._api, msg.chat_id)
            log.info("answered message %d for person %d in %.1fs", msg.id, person.id, result.duration)
            return

        if result.refused:
            # Claude came up with more than this person may use: never retried, and the owners are told.
            log.error("turn of message %d stopped as it started: it had %s", msg.id, result.refused)
            store.release(conn, msg.chat_id, keep=msg.id)
            store.mark(conn, msg.id, "failed")
            self._api.notify(msg.chat_id, i18n.t(person.language, "error_notice"))
            for owner_id, owner_language in access.owner_recipients(conn):
                self._api.notify(owner_id, i18n.t(owner_language, "turn_refused", name=html.escape(person.name),
                                                  what=html.escape(result.refused)))
            return

        if result.timed_out or result.inactivity:
            log.warning("message %d stuck (%.0fs)", msg.id, result.duration)
            if result.session_id:
                store.note(conn, msg.id, "resume", result.session_id)
                store.note(conn, msg.id, "key", key)
            self._retry_or_give_up(conn, person, msg, "given_up")
            for owner_id, owner_language in access.owner_recipients(conn):
                self._api.notify(owner_id, i18n.t(owner_language, "stuck_notice"))
            return

        log.error("message %d failed (rc=%s)", msg.id, result.returncode)
        self._retry_or_give_up(conn, person, msg, "error_notice")

    def _reply(
        self,
        conn: sqlite3.Connection,
        person: Person,
        msg: store.Message,
        reply: str,
        found: list[tuple[str, str]],
        spoken: bool,
        progress: Progress | None = None,
    ) -> tuple[str, int | None]:
        """Send the reply, its reaction, its options and the texts to forward; return what the
        person got, as remembered, and the Telegram id of the first message sent."""
        wanted = directives.reaction(found)
        reacted = self._react(msg, wanted)
        drafts = directives.forwards(found)
        if wanted and not reacted and not reply and not drafts:
            reply = wanted
        options = directives.options(found)
        if options and not reply:
            reply = i18n.t(person.language, "choose_prompt")
        quoted = directives.quote(found)
        if quoted and not store.find_message(conn, msg.chat_id, quoted):
            quoted = None
        voiced = spoken or directives.voice(found)
        said = []
        target = directives.edit(found)
        # An edit changes the draft to forward when there is one, otherwise the reply is the new text.
        if target and (drafts or (reply and not voiced and not options)):
            text = drafts[0] if drafts else reply
            if self._rewrite(conn, msg, text, target):
                said.append(f"[rewrote #{target}] {text}")
                if drafts:
                    drafts = drafts[1:]
                else:
                    reply = ""
                if not reply:
                    reply, voiced = i18n.t(person.language, "rewritten"), False
                # What else is said answers the rewritten message, so the person finds it.
                quoted = target
        sent_id = None
        if reply:
            said.append(reply)
            sent_id = self._deliver(conn, msg, person, reply, voiced, options, quoted, progress)
        # A text to forward comes bare, as a message of its own the person can forward as it is.
        for draft in drafts:
            said.append(f"[to forward] {draft}")
            forwarded = self._deliver(conn, msg, person, draft, False, [], None, progress)
            sent_id = sent_id or forwarded
        if said:
            return "\n".join(said), sent_id
        if reacted:
            store.record_outbound(conn, msg.chat_id, None, f"[reaction {reacted}]", person.id)
        return reacted or "", None

    def _rewrite(self, conn: sqlite3.Connection, msg: store.Message, text: str, target: int) -> bool:
        """Put the text in place of one of Zoe's own messages of this chat; return False when that
        message cannot take it (not Zoe's, not text, or a text longer than one message)."""
        found = store.find_message(conn, msg.chat_id, target)
        chunks = formatting.split(formatting.to_telegram(text))
        if not found or not found["mine"] or len(chunks) != 1:
            return False
        if not self._api.edit_message(msg.chat_id, target, chunks[0]):
            return False
        store.revise_outbound(conn, msg.chat_id, target, chunks[0])
        return True

    def _plan(
        self,
        conn: sqlite3.Connection,
        brain: Brain,
        person: Person,
        chat_id: int,
        found: list[tuple[str, str]],
        related: list[int],
        receipt: receipts.Receipt,
    ) -> None:
        """Cancel and make the plans the reply asked for, and tell the person when one cannot be made."""
        for plan_id in directives.unplans(found):
            plan = planner.get(conn, person.id, plan_id)
            if plan and planner.cancel(conn, person.id, plan_id):
                receipt.unplanned(plan)
        for when, instruction in directives.plans(found):
            try:
                plan_id = planner.create(conn, brain, person, chat_id, when, instruction, related)
            except planner.PlanError as exc:
                log.warning("plan %r for person %d not made: %s", when, person.id, exc)
                self._api.notify(chat_id, i18n.t(
                    person.language, str(exc), when=html.escape(when), limit=planner.MEMBER_LIMIT,
                    minutes=planner.min_gap(person) // 60,
                ))
                continue
            receipt.planned(planner.get(conn, person.id, plan_id))

    def _mind(
        self,
        conn: sqlite3.Connection,
        brain: Brain,
        person: Person,
        last: store.Message,
        found: list[tuple[str, str]],
        related: list[int],
    ) -> None:
        """Start, instruct and stop the parallel minds the reply asked for. The menu shows
        them, so a note passed on is only a ✍ on the person's message."""
        for mind_id in directives.stops(found):
            minds.stop(conn, person.id, mind_id)
        for mind_id, note in directives.tells(found):
            if minds.tell(conn, person.id, mind_id, note):
                self._react(last, "✍")
            else:
                log.warning("note for parallel mind %d not delivered: it is not working", mind_id)
        for title, folder, brief in directives.minds(found):
            mind_id = minds.start(conn, brain, person, last.chat_id, title, brief, folder, related, self._cfg.data_dir)
            log.info("parallel mind %d started for person %d: %s", mind_id, person.id, title)

    def _files(
        self,
        conn: sqlite3.Connection,
        brain: Brain,
        person: Person,
        chat_id: int,
        found: list[tuple[str, str]],
        related: list[int],
        receipt: receipts.Receipt,
    ) -> None:
        """Keep, delete and send the files the reply asked for."""
        home = access.turn_profile(person, self._cfg).cwd
        for number, about, place in directives.keeps(found):
            kept = files.keep(conn, brain, person, number, about, place, home, related)
            if kept:
                receipt.kept(kept)
            else:
                log.warning("file %d of person %d not kept", number, person.id)
        for number in directives.discards(found):
            gone = files.discard(conn, brain, person, number)
            if gone:
                receipt.discarded(gone)
        for target, caption, uncompressed in directives.sends(found):
            chosen = files.resolve_all(conn, person, target, home)
            try:
                if not chosen:
                    raise TelegramError("no such file of this person")
                sent = files.send(self._api, chat_id, chosen, caption, uncompressed)
            except TelegramError as exc:
                log.warning("file %r not sent to %s: %s", target, chat_id, exc)
                notice = "files_not_sent" if "," in target else "file_not_sent"
                self._api.notify(chat_id, i18n.t(person.language, notice, what=html.escape(target)))
                continue
            for n, ((record, path), message) in enumerate(zip(chosen, sent)):
                name = record.name if record else path.name
                store.record_outbound(conn, chat_id, message.get("message_id"),
                                      f"[file «{name}»] {caption if n == 0 else ''}".strip(), person.id)

    def _admit(
        self, conn: sqlite3.Connection, owner: Person, chat_id: int, found: list[tuple[str, str]],
        receipt: receipts.Receipt,
    ) -> None:
        """Let the accounts the owner named write to Zoe, welcome them, and share folders with members."""
        for given in (True, False):
            for name, folder in directives.shares(found, "share" if given else "unshare"):
                member = access.find_person(conn, name)
                if member is None:
                    changed = None
                elif given:
                    changed = access.share(conn, member, Path(folder), self._cfg.data_dir)
                else:
                    changed = access.unshare(conn, member, Path(folder))
                if changed is None:
                    self._api.notify(chat_id, i18n.t(owner.language, "share_refused", folder=html.escape(folder),
                                                     name=html.escape(name)))
                else:
                    receipt.shared(changed, folder, given)
        for user_id, name in directives.admits(found):
            admitted = access.admit(conn, user_id, name, self._cfg.default_language)
            if admitted is None:
                self._api.notify(chat_id, i18n.t(owner.language, "admit_refused", id=user_id))
                continue
            person, new = admitted
            log.info("account %d admitted as person %d", user_id, person.id)
            receipt.admitted(person, user_id, new)
            self._api.notify(user_id, i18n.t(person.language, "welcome", name=html.escape(person.name)))

    def _wait_for_claude(self, conn: sqlite3.Connection, msg: store.Message, reason: str) -> None:
        """Claude cannot work at all (logged out, out of usage): the turn goes back to the queue
        without counting as an attempt, and waits with every other turn (``availability``)."""
        store.release(conn, msg.chat_id, keep=msg.id)
        store.requeue(conn, msg.id, counted=False)
        availability.down(conn, self._cfg.data_dir, reason)

    def _retry_or_give_up(self, conn: sqlite3.Connection, person: Person, msg: store.Message, notice: str) -> None:
        store.release(conn, msg.chat_id, keep=msg.id)
        if msg.attempts < self._cfg.max_attempts:
            store.requeue(conn, msg.id)
            return
        store.mark(conn, msg.id, "failed")
        if msg.kind not in _INTERNAL:
            self._api.notify(msg.chat_id, i18n.t(person.language, notice))
            return
        # A plan or a mind's news is never lost: without a turn, it reaches the person as it is.
        key = "plan_fallback" if msg.kind == planner.PLAN else "mind_fallback"
        title = html.escape((msg.payload or {}).get("title", ""))
        text = i18n.t(person.language, key, what=html.escape(msg.text or ""), title=title)
        try:
            sent = self._api.send_message(msg.chat_id, text)
        except TelegramError as exc:
            log.error("plan %s not delivered: %s", msg.payload, exc)
            return
        store.record_outbound(conn, msg.chat_id, sent.get("message_id"), text, person.id, _origin(msg))

    def _react(self, msg: store.Message, emoji: str | None) -> str | None:
        """Put the requested reaction on the person's message; return the emoji used, or None."""
        if not emoji or msg.telegram_id is None:
            return None
        try:
            return self._api.react(msg.chat_id, msg.telegram_id, emoji)
        except TelegramError as exc:
            log.warning("reaction on message %d not set: %s", msg.id, exc)
            return None

    def _deliver(
        self,
        conn: sqlite3.Connection,
        msg: store.Message,
        person: Person,
        text: str,
        spoken: bool,
        options: list[str],
        quoted: int | None,
        progress: Progress | None = None,
    ) -> int | None:
        """Send the reply: as a voice note when asked for and possible, otherwise as formatted
        text, with one button per option on its last part when it asks to choose, answering
        the quoted message if any. Text that answers no quoted message takes the place of the
        progress message. Every message sent is logged with its Telegram id; return the id of
        the first."""
        origin = _origin(msg)
        if spoken and not options and self._speaker is not None and len(text) <= self._cfg.tts_max_chars:
            if progress:
                progress.step(Step("recording"))
            try:
                audio = self._speaker.synthesize(formatting.to_plain(text))
                # The text goes under the voice note, to read again; a text too long for a caption
                # is heard only (spoken replies are one to three sentences).
                written = formatting.to_telegram(text)
                caption = written if len(written) <= CAPTION_LIMIT else ""
                sent = self._api.send_voice(msg.chat_id, audio, reply_to=quoted, caption=caption)
                store.record_outbound(conn, msg.chat_id, sent.get("message_id"), text, person.id, origin)
                if progress:
                    progress.finish()
                return sent.get("message_id")
            except (SpeechError, TelegramError) as exc:
                log.warning("voice reply failed, sending text instead: %s", exc)
        choice = choices.create(conn, msg.chat_id, person.id, options) if options else None
        chunks = formatting.split(formatting.to_telegram(text))
        ids: list[int | None] = []
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            buttons = choices.keyboard(choice, options) if choice and last else None
            sent_id = progress.become(chunk, buttons) if progress and not ids and not quoted else None
            if sent_id is None:
                sent_id = self._api.send_message(
                    msg.chat_id, chunk, buttons=buttons, reply_to=None if ids else quoted
                ).get("message_id")
            logged = choices.logged(chunk, options) if options and last else chunk
            store.record_outbound(conn, msg.chat_id, sent_id, logged, person.id, origin)
            ids.append(sent_id)
        if progress:
            progress.finish()
        if choice:
            choices.attach(conn, choice, ids[-1])
        return ids[0] if ids else None
