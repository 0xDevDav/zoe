"""Zoe's menu, a Telegram Mini App: what people see and change about themselves and Zoe.

``webapp.py`` serves the page and checks who asks; this module answers. A view is plain
data the page draws in Telegram's colours: a title, the view its back button leads to,
and blocks of text, lists and buttons. Text is never markup, so nothing a memory, a
brief or a name holds can run in the page. The page asks for its view again every
second and draws it anew only when it changed. A running time travels as the moment
it started (``since``), with ``{elapsed}`` where it shows in the text, and the page
ticks it every second against the server's clock (``now``).

Requests name a view or an action, each with its argument after a colon:

    views    home, lang, plans, plan:<id>, known, learned:<kind>, memory:<id>, minds,
             mind:<id>, people[:<person id open>], request:<code>, link:<code>, improve,
             undo, status
    actions  lang:<code>, unplan:<id>, forget:<id>, observe:<person id>, halt:<id>,
             approve:<code>, link:<code>:<person id>, deny:<code>, add:<person id>:<user id>,
             revoke:<user id>, remove:<person id>, make:<proposal id>, drop:<proposal id>, undo

Everyone sees and deletes their own plans, sees what the observer has learned about
them and makes Zoe forget any of it, and turns their observation on or off. Owners
also follow and stop their parallel minds, manage people and their accounts, decide
what Zoe proposes to improve, see how Zoe is doing and undo her last change to herself. Owners' own accounts are only shown:
they come from ZOE_OWNER_IDS, so no tap in the menu gives or takes an owner's powers.
Owner-only views and actions are checked on every request, not only hidden. People
open the menu from the chat's Menu button, or with /menu, which answers with a
button that opens it.
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
import subprocess
import time
from dataclasses import replace
from datetime import date, datetime

from . import access, claude_runner, evolve, health, i18n, improve, minds, observer, planner, review, signals, store
from .access import Account, Person
from .brain import Brain
from .config import Config
from .improve import Proposal
from .launcher import CORE_ROOT
from .telegram import TelegramAPI
from .usage import Usage

log = logging.getLogger("zoe.menu")

View = dict[str, object]

_OWNER_VIEWS = {"minds", "mind", "people", "request", "link", "improve", "undo", "status"}
_OWNER_ACTIONS = {"halt", "approve", "link", "deny", "add", "revoke", "remove", "make", "drop", "undo"}
_DECIDED = 5
# A limit used this much shows in red.
_NEAR_LIMIT = 90
_MIND_ICON = {"queued": "⏳", "running": "⚙️", "asking": "❓", "done": "✅", "failed": "⚠️", "stopped": "⏹️"}
# Where a running time shows in a text; the page puts the time there and ticks it.
ELAPSED = "{elapsed}"
_LEARNED = 200


def _view(title: str, blocks: list[dict], back: str = "home") -> View:
    """A view; its title drops the emoji its menu entry starts with."""
    if title and not title[0].isalnum():
        title = title.split(" ", 1)[-1]
    return {"title": title, "back": back, "blocks": blocks}


def _since(since: float | None) -> dict:
    """The moment a running time started, for a text that holds ELAPSED."""
    return {"since": since} if since else {}


def _text(text: str, since: float | None = None) -> dict:
    return {"text": text, **_since(since)}


def _items(*items: dict) -> dict:
    return {"items": list(items)}


def _entry(
    label: str, detail: str = "", to: str = "", act: str = "", count: int = 0, side: str = "", confirm: str = "",
    since: float | None = None,
) -> dict:
    """A row of a list: ``to`` opens a view, ``act`` acts, after ``confirm`` when given, and shows
    ``side`` at the right as what it does; a ``count`` above zero shows beside the label, and
    ``since`` ticks the time in the detail."""
    return {"label": label, "detail": detail, "to": to, "act": act, "count": count, "side": side,
            "confirm": confirm, **_since(since)}


def _stat(
    label: str, value: str, detail: str = "", tone: str = "", wide: bool = False, since: float | None = None,
) -> dict:
    """A figure on a card of its own: ``tone`` marks the main one ("accent") or one gone wrong
    ("danger"), a ``wide`` card takes the whole row, and ``since`` ticks the time in the value."""
    return {"label": label, "value": value, "detail": detail, "tone": tone, "wide": wide, **_since(since)}


def _button(label: str, act: str, style: str = "", confirm: str = "") -> dict:
    """A button; ``style`` colours it, "danger" for what removes or blocks, "success" for what
    admits, and ``confirm`` is asked before it acts."""
    return {"button": {"label": label, "act": act, "style": style, "confirm": confirm}}


def _score(value: float, lang: str) -> str:
    text = f"{value:.1f}".removesuffix(".0")
    return text.replace(".", ",") if lang == "it" else text


def _signal_label(kind: str, lang: str) -> str:
    key = f"signal_{kind}"
    label = i18n.t(lang, key)
    return kind if label == key else label


def _version() -> tuple[str, str]:
    """The commit Zoe runs and when it was made."""
    try:
        found = subprocess.run(
            ["git", "-C", str(CORE_ROOT), "log", "-1", "--format=%h|%cd", "--date=format:%d/%m %H:%M"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        found = ""
    commit, _, made = found.partition("|")
    return commit or "?", made


def _short(account: Account) -> str:
    return account.display_name or (f"@{account.username}" if account.username else str(account.telegram_user_id))


class Menu:
    def __init__(self, cfg: Config, api: TelegramAPI, usage: Usage | None = None) -> None:
        self._cfg = cfg
        self._api = api
        # The version this process runs: the code on disk may be newer until the next start.
        self._running = _version()
        self._usage = usage or Usage(cfg)

    def invite(self, chat_id: int, person: Person, view: str = "home") -> None:
        """Answer /menu with a button that opens the menu on ``view``."""
        lang = person.language
        if not self._cfg.menu_url:
            self._api.notify(chat_id, i18n.t(lang, "menu_unavailable"))
            return
        url = self._cfg.menu_url + ("" if view == "home" else f"?view={view}")
        self._api.notify(chat_id, i18n.t(lang, "menu_invite"),
                         [[{"text": i18n.t(lang, "btn_open_menu"), "web_app": {"url": url}}]])

    def answer(self, conn: sqlite3.Connection, person: Person, request: dict) -> View:
        """Answer one request of the page: show a view, or act and show the view that follows."""
        acting = "action" in request
        name, _, arg = str(request.get("action") if acting else request.get("view") or "home").partition(":")
        if person.role != "owner" and name in (_OWNER_ACTIONS if acting else _OWNER_VIEWS):
            return {**self.view(conn, person, "home", ""), "notice": i18n.t(person.language, "owner_only")}
        return self._act(conn, person, name, arg) if acting else self.view(conn, person, name, arg)

    def view(self, conn: sqlite3.Connection, person: Person, name: str, arg: str) -> View:
        """One view in the person's language, with ``at`` naming it for the page's refreshes, and
        what the page needs to tick running times: the server's clock and the word for a day."""
        return {**self._build(conn, person, name, arg), "at": f"{name}:{arg}" if arg else name,
                "brand": self._cfg.bot_name, "now": time.time(), "day": i18n.t(person.language, "day_short")}

    def _build(self, conn: sqlite3.Connection, person: Person, name: str, arg: str) -> View:
        lang = person.language

        def t(key: str, **kwargs: object) -> str:
            return i18n.t(lang, key, **kwargs)

        def gone(title: str, key: str, back: str) -> View:
            return _view(title, [_text(t(key))], back)

        if name == "lang":
            return _view(t("btn_language"), [_text(t("lang_pick")), _items(
                _entry("🇮🇹 Italiano", act="lang:it"), _entry("🇬🇧 English", act="lang:en"))])

        if name == "plans":
            pending = planner.pending(conn, person.id)
            rows = [_entry(f"{'🔁' if p.repeat else '⏰'} {p.instruction}", planner.label(p.due_at, lang),
                           to=f"plan:{p.id}") for p in pending]
            return _view(t("btn_plans"), [_items(*rows)] if rows else [_text(t("plans_none"))])

        if name == "plan":
            plan = planner.get(conn, person.id, int(arg)) if arg.isdigit() else None
            if plan is None:
                return gone(t("btn_plans"), "plan_gone", "plans")
            blocks = [_text(plan.instruction)]
            if plan.repeat:
                blocks.append(_text(t("plan_repeats", rule=plan.repeat)))
            blocks.append(_button(t("btn_unplan"), f"unplan:{plan.id}", "danger", t("confirm_unplan")))
            return _view(planner.label(plan.due_at, lang), blocks, "plans")

        if name == "minds":
            now = time.time()
            working, ended = minds.active(conn, person.id), minds.recent(conn, person.id, 5)
            blocks = [_text(t("minds_intro") if working or ended else t("minds_none"))]
            if working:
                blocks.append(_items(*(self._working(m, lang) for m in working)))
            if ended:
                blocks += [{"heading": t("minds_recent")}, _items(*(
                    _entry(m.title, self._state(m, lang, now), to=f"mind:{m.id}") for m in ended))]
            return _view(t("btn_minds"), blocks)

        if name == "mind":
            mind = minds.get(conn, int(arg), person.id) if arg.isdigit() else None
            if mind is None:
                return gone(t("btn_minds"), "mind_gone", "minds")
            working = mind.state in minds.ACTIVE
            started = (mind.started_at or mind.created_at) if mind.state == "running" else None
            blocks = [_text(self._state(mind, lang, time.time()), started), {"code": mind.workdir}]
            if mind.steps and working:
                blocks += [{"heading": t("mind_steps")}, _text("\n".join(f"• {s}" for s in mind.steps))]
            blocks += [{"heading": t("mind_brief")}, {"quote": mind.brief}]
            if mind.result:
                blocks += [{"heading": t("mind_result")}, {"quote": mind.result}]
            if working:
                blocks.append(_button(t("btn_halt"), f"halt:{mind.id}", "danger", t("confirm_halt")))
            return _view(f"{_MIND_ICON[mind.state]} {mind.title}", blocks, "minds")

        if name == "known":
            watched = observer.enabled(conn, person)
            blocks = [_text(t("known_on" if watched else "known_off"))]
            trends = signals.trends(conn, person.id)
            if trends:
                blocks += [{"heading": t("known_trends")}, {"columns": min(len(trends), 3), "stats": [
                    _stat(_signal_label(tr.kind, lang), f"{_score(tr.average, lang)}/5") for tr in trends]}]
            found = observer.counts(conn, person.id)
            kinds = [_entry(t(f"kind_{kind}"), to=f"learned:{kind}", count=found[kind])
                     for kind in observer.KINDS if kind in found]
            blocks += [{"heading": t("known_learned")}, _items(*kinds)] if kinds else [_text(t("known_empty"))]
            blocks.append(_button(t("btn_observe_stop" if watched else "btn_observe_start"), f"observe:{person.id}"))
            return _view(t("btn_known"), blocks)

        if name == "learned":
            items = Brain(conn, person.id).top(arg, _LEARNED) if arg in observer.KINDS else []
            if not items:
                return gone(t("btn_known"), "memory_gone", "known")
            return _view(t(f"kind_{arg}"), [_items(*(_entry(i.content, to=f"memory:{i.id}") for i in items))], "known")

        if name == "memory":
            memory = Brain(conn, person.id).get(int(arg)) if arg.isdigit() else None
            if memory is None or memory.kind not in observer.KINDS:
                return gone(t("btn_known"), "memory_gone", "known")
            return _view(t(f"kind_{memory.kind}"), [
                _text(memory.content),
                _button(t("btn_forget"), f"forget:{memory.id}", "danger", t("confirm_forget")),
            ], f"learned:{memory.kind}")

        if name == "people":
            # A person whose accounts were all removed is gone from the list; what Zoe knows stays.
            shown = [(m, accounts) for m, accounts in access.people(conn) if accounts or m.role == "owner"]
            opened = int(arg) if arg.isdigit() else next((m.id for m, _ in shown if m.role == "owner"), 0)
            waiting = [_entry(f"⏳ {a.label()}", to=f"request:{a.request_code}") for a in access.pending(conn)]
            return _view(t("btn_people"), [
                {"sections": [self._person(conn, m, accounts, lang, m.id == opened) for m, accounts in shown]},
                {"heading": t("strangers_header")},
                _items(*waiting) if waiting else _text(t("nobody_tried")), _text(t("add_hint")),
            ])

        if name == "request":
            account = access.pending_account(conn, arg)
            if account is None:
                return gone(t("btn_people"), "not_found", "people")
            return _view(t("btn_people"), [
                _text(t("stranger_view", who=account.label())),
                _button(t("btn_approve"), f"approve:{arg}", "success"),
                _items(_entry(t("btn_link"), to=f"link:{arg}")),
                _button(t("btn_deny"), f"deny:{arg}", "danger", t("confirm_deny")),
            ], "people")

        if name == "link":
            account = access.pending_account(conn, arg)
            if account is None:
                return gone(t("btn_people"), "not_found", "people")
            known = [_entry(f"👤 {member.name}", act=f"link:{arg}:{member.id}")
                     for member, _ in access.people(conn) if member.role != "owner"]
            return _view(t("btn_people"), [_text(t("link_pick", who=account.label())), _items(*known)], f"request:{arg}")

        if name == "improve":
            return _view(t("btn_improve"), self._improvements(conn, lang))

        if name == "undo":
            last = evolve.Evolution(self._cfg).last_change()
            if last is None:
                return _view(t("btn_undo"), [_text(t("undo_nothing"))])
            return _view(t("btn_undo"), [_text(t("undo_view", summary=last["message"])),
                                         _button(t("btn_confirm_undo"), "undo", "danger")])

        if name == "status":
            return _view(t("btn_status"), [self._status(conn, lang), *self._limits(lang)])

        return self._home(conn, person)

    def _home(self, conn: sqlite3.Connection, person: Person) -> View:
        lang = person.language

        def t(key: str, **kwargs: object) -> str:
            return i18n.t(lang, key, **kwargs)

        pending = planner.pending(conn, person.id)
        rows = [_entry(t("btn_plans"), to="plans", count=len(pending)),
                _entry(t("btn_known"), to="known"), _entry(t("btn_language"), to="lang")]
        if person.role == "owner":
            working, waiting = minds.active(conn, person.id), access.pending(conn)
            rows = [_entry(t("btn_minds"), to="minds", count=len(working)),
                    _entry(t("btn_people"), to="people", count=len(waiting)),
                    _entry(t("btn_improve"), to="improve", count=len(improve.waiting(conn))),
                    *rows, _entry(t("btn_status"), to="status"), _entry(t("btn_undo"), to="undo")]
        return _view(t("menu_home"), [_items(*rows)], back="")

    @staticmethod
    def _person(conn: sqlite3.Connection, member: Person, accounts: list[Account], lang: str, opened: bool) -> dict:
        """A person's section of the people's list: their accounts and, for a member, adding
        and removing one, observation, and removing the whole person."""

        def t(key: str, **kwargs: object) -> str:
            return i18n.t(lang, key, **kwargs)

        detail = t("people_accounts", role=t(f"role_{member.role}"), count=len(accounts))
        if member.role == "owner":
            blocks = [_items(*(_entry(a.label()) for a in accounts)), _text(t("owner_accounts_hint"))]
            return {"title": member.name, "detail": detail, "open": opened, "blocks": blocks}
        watched = "btn_observed_on" if observer.enabled(conn, member) else "btn_observed_off"
        return {"title": member.name, "detail": detail, "open": opened, "blocks": [
            _items(*(_entry(a.label(), act=f"revoke:{a.telegram_user_id}", side=t("btn_remove"),
                            confirm=t("confirm_remove_account", name=_short(a))) for a in accounts)),
            {"input": {"placeholder": t("account_id"), "label": t("btn_add"), "act": f"add:{member.id}"}},
            _button(t(watched), f"observe:{member.id}"),
            _button(t("btn_remove_person", name=member.name), f"remove:{member.id}", "danger",
                    t("confirm_remove_person", name=member.name)),
        ]}

    def _improvements(self, conn: sqlite3.Connection, lang: str) -> list[dict]:
        """What last night's study and memory review found, the proposals waiting, open one at a
        time, and the last ones decided, each approved one with how its making goes."""

        def t(key: str, **kwargs: object) -> str:
            return i18n.t(lang, key, **kwargs)

        night = [line for line in (store.get_state(conn, improve.SUMMARY), review.last_summary(self._cfg)) if line]
        blocks = [_text(t("improve_intro"))]
        if night:
            blocks += [{"heading": t("improve_night")}, _text("\n".join(night))]
        proposals = improve.waiting(conn)
        blocks += [{"heading": t("improve_waiting")}, {"sections": [
            self._proposal(p, lang, n == 0) for n, p in enumerate(proposals)]} if proposals else _text(t("improve_none"))]
        rows, now = [], time.time()
        for proposal in improve.decided(conn, _DECIDED):
            mind = minds.get(conn, proposal.mind_id) if proposal.mind_id else None
            rows.append(_entry(proposal.title, self._state(mind, lang, now), to=f"mind:{mind.id}") if mind
                        else _entry(proposal.title, t("proposal_dropped")))
        if rows:
            blocks += [{"heading": t("improve_decided")}, _items(*rows)]
        return blocks

    @staticmethod
    def _proposal(proposal: Proposal, lang: str, opened: bool) -> dict:
        """A waiting proposal: what changes for the owner, how and how it was checked, and the
        buttons that make it or set it aside."""

        def t(key: str, **kwargs: object) -> str:
            return i18n.t(lang, key, **kwargs)

        detail = planner.label(proposal.created_at, lang) + (f", {t('improve_careful')}" if proposal.careful else "")
        blocks = [_text(proposal.benefit), {"heading": t("improve_plan")}, {"quote": proposal.plan}]
        if proposal.checked:
            blocks += [{"heading": t("improve_checked")}, {"quote": proposal.checked}]
        blocks += [_button(t("btn_make"), f"make:{proposal.id}", "success", t("confirm_make")),
                   _button(t("btn_drop"), f"drop:{proposal.id}", "danger", t("confirm_drop"))]
        return {"title": proposal.title, "detail": detail, "open": opened, "blocks": blocks}

    @staticmethod
    def _state(mind: minds.Mind, lang: str, now: float) -> str:
        return i18n.t(
            lang, f"mind_{mind.state}",
            elapsed=ELAPSED,
            ago=minds.duration(now - (mind.finished_at or now)),
            question=mind.question,
        )

    @staticmethod
    def _working(mind: minds.Mind, lang: str) -> dict:
        """A mind at work: for how long and what it is doing."""
        doing = mind.steps[-1] if mind.state == "running" and mind.steps else i18n.t(lang, f"doing_{mind.state}")
        return _entry(f"{_MIND_ICON[mind.state]} {mind.title}", f"{ELAPSED}, {doing}", to=f"mind:{mind.id}",
                      since=mind.started_at or mind.created_at)

    def _status(self, conn: sqlite3.Connection, lang: str) -> dict:
        """How the service is doing, card by card: whether Claude can work and with which model,
        since when Zoe runs and which version, the turns and parallel minds at work, and the
        last nightly review."""
        now = time.time()
        down = store.get_state(conn, claude_runner.DOWN)
        reviewed = store.get_state(conn, review.STATE)
        commit, made = self._running

        def t(key: str, **kwargs: object) -> str:
            return i18n.t(lang, key, **kwargs)

        def count(sql: str, *args: object) -> str:
            return str(conn.execute(sql, args).fetchone()[0])

        working = f"SELECT COUNT(*) FROM minds WHERE state IN ({','.join('?' * len(minds.ACTIVE))})"
        return {"columns": 2, "stats": [
            _stat(t("status_claude"), t("status_claude_down" if down else "status_claude_ok"),
                  down or self._cfg.claude_model, "danger" if down else "accent", wide=True),
            _stat(t("status_uptime"), ELAPSED, since=float(store.get_state(conn, health.STARTED) or now)),
            _stat(t("status_version"), commit, made),
            _stat(t("status_turns"), count("SELECT COUNT(DISTINCT chat_id) FROM messages WHERE state = 'processing'"),
                  t("status_queued", count=count("SELECT COUNT(*) FROM messages WHERE state = 'received'"))),
            _stat(t("status_minds"), count(working, *minds.ACTIVE)),
            _stat(t("status_review"), planner.label(float(reviewed), lang) if reviewed else t("status_never"),
                  wide=True),
        ]}

    def _limits(self, lang: str) -> list[dict]:
        """Claude's usage limits on one card, a bar each: how much is used and when it resets;
        nothing until the first reading arrives."""
        limits = self._usage.latest()
        if not limits:
            return []

        def t(key: str, **kwargs: object) -> str:
            return i18n.t(lang, key, **kwargs)

        def name(label: str) -> str:
            week = re.fullmatch(r"Current week \((.+)\)", label)
            if label == "Current session":
                return t("limit_session")
            if week:
                return t("limit_week") if week[1] == "all models" else t("limit_week_model", model=week[1])
            return label

        def resets(ts: float | None) -> str:
            if ts is None:
                return ""
            moment = datetime.fromtimestamp(ts)
            if moment.date() == date.today():
                return t("limit_resets", when=f"{moment:%H:%M}")
            return t("limit_resets", when=f"{t('weekdays').split()[moment.weekday()]} {moment:%H:%M}")

        meters = [{"label": name(limit.label), "value": f"{limit.used:g}%", "fill": min(max(limit.used, 0), 100),
                   "detail": resets(limit.resets_at), "tone": "danger" if limit.used >= _NEAR_LIMIT else ""}
                  for limit in limits]
        return [{"heading": t("status_limits")}, {"meters": meters}]

    def _act(self, conn: sqlite3.Connection, person: Person, name: str, arg: str) -> View:
        """Carry out an action and return the view that follows it, with a short notice."""

        def then(view: str, key: str | None, **kwargs: object) -> View:
            shown, _, shown_arg = view.partition(":")
            notice = i18n.t(person.language, key, **kwargs) if key else None
            return {**self.view(conn, person, shown, shown_arg), "notice": notice}

        if name == "lang":
            if arg in i18n.SUPPORTED:
                access.set_language(conn, person.id, arg)
                person = replace(person, language=arg)
            return then("home", "lang_changed")

        if name == "unplan":
            done = arg.isdigit() and planner.cancel(conn, person.id, int(arg))
            return then("plans", "unplanned" if done else "plan_gone")

        if name == "halt":
            done = arg.isdigit() and minds.stop(conn, person.id, int(arg))
            return then(f"mind:{arg}", "halted" if done else "mind_gone")

        if name == "forget":
            brain = Brain(conn, person.id)
            memory = brain.get(int(arg)) if arg.isdigit() else None
            if memory is None or memory.kind not in observer.KINDS:
                return then("known", "memory_gone")
            brain.forget(memory.id)
            return then(f"learned:{memory.kind}", "forgotten")

        if name == "observe":
            target = access.get_person(conn, int(arg)) if arg.isdigit() else None
            if target is None or (target.id != person.id and person.role != "owner"):
                return then("home", "owner_only")
            on = not observer.enabled(conn, target)
            observer.set_enabled(conn, target.id, on)
            return then("known" if target.id == person.id else f"people:{target.id}",
                        "observe_on" if on else "observe_off")

        if name == "make":
            made = improve.make(conn, person, self._cfg.owner_chat, int(arg), self._cfg.data_dir) if (
                arg.isdigit() and self._cfg.owner_chat) else None
            return then("improve", "made" if made else "proposal_gone")

        if name == "drop":
            return then("improve", "dropped" if arg.isdigit() and improve.drop(conn, int(arg)) else "proposal_gone")

        if name == "undo":
            try:
                outcome = evolve.Evolution(self._cfg).undo()
            except evolve.EvolveError as exc:
                return then("home", "undo_failed", error=str(exc))
            return then("home", "undo_done", summary=outcome.report) if outcome.ok else then("home", "undo_nothing")

        if name in ("approve", "link"):
            code, _, person_id = arg.partition(":")
            if name == "approve":
                result = access.approve(conn, code, None, self._cfg.default_language)
            else:
                result = access.link(conn, code, person_id) if person_id.isdigit() else None
            if result is None:
                return then("people", "not_found")
            member, account = result
            self._welcome(member, account.telegram_user_id)
            return then(f"people:{member.id}", "approved" if name == "approve" else "linked", name=member.name)

        if name == "deny":
            return then("people", "denied" if access.deny(conn, arg) else "not_found")

        if name == "add":
            person_id, _, user_id = arg.partition(":")
            user_id = user_id.strip()
            member = access.add_account(conn, int(user_id), int(person_id)) if (
                person_id.isdigit() and user_id.isdigit()) else None
            if member is None:
                return then(f"people:{person_id}", "account_refused", id=user_id[:20])
            self._welcome(member, int(user_id))
            return then(f"people:{member.id}", "linked", name=member.name)

        if name == "revoke":
            found = access.resolve(conn, int(arg)).person if arg.isdigit() else None
            outcome = access.revoke(conn, int(arg)) if found else None
            return then(f"people:{found.id}" if found else "people",
                        {None: "not_found", False: "cannot_revoke_owner", True: "revoked"}[outcome])

        if name == "remove":
            member, accounts = next(((m, accs) for m, accs in access.people(conn) if str(m.id) == arg), (None, []))
            if member is None or member.role == "owner":
                return then("people", "not_found" if member is None else "cannot_revoke_owner")
            for account in accounts:
                access.revoke(conn, account.telegram_user_id)
            return then("people", "person_removed", name=member.name)

        log.info("menu action %r of person %d unknown", name, person.id)
        return then("home", None)

    def _welcome(self, member: Person, user_id: int) -> None:
        self._api.notify(user_id, i18n.t(member.language, "welcome", name=html.escape(member.name)))
