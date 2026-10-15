"""Zoe changes her own code and skills through a gated pipeline.

Nothing is edited in place. A change is made in its own git worktree, must pass
the checks (compilation, tests, privacy guard) and an independent review against
the principles, and is then applied as one squashed commit. Every applied change
can be undone. A core change takes effect through a graceful restart, and a
restart that fails is rolled back by the launcher.

Targets:

- ``core``: this repository, the service code and the built-in skills;
- ``skills``: the private plugin with the skills made for this installation.

Command line::

    python -m zoe.evolve start core better-glossary
    python -m zoe.evolve check core better-glossary
    python -m zoe.evolve review core better-glossary
    python -m zoe.evolve apply core better-glossary -m "Glossary favors recent terms"
    python -m zoe.evolve discard core better-glossary
    python -m zoe.evolve undo
    python -m zoe.evolve status
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import notices, plugins
from .claude_runner import ClaudeRunner, Profile
from .config import Config

TARGETS = ("core", "skills")
PRINCIPLES = "plugin/skills/self-improvement/PRINCIPLES.md"
PROTECTED = frozenset({
    "zoe/launcher.py", "zoe/evolve.py", "zoe/access.py", "zoe/vault.py",
    "tools/privacy_check.py", ".githooks/pre-commit", ".githooks/pre-push",
    "tests/test_access.py", "tests/test_vault.py", "tests/test_evolve.py", "tests/test_launcher.py",
    PRINCIPLES,
})
_DIFF_LIMIT = 80_000

# Compiles every Python file under the given folders without writing any file, so
# checks leave the worktree exactly as the change made it.
_COMPILE = (
    "import pathlib, sys\n"
    "for root in sys.argv[1:]:\n"
    "    for path in pathlib.Path(root).rglob('*.py'):\n"
    "        compile(path.read_text(encoding='utf-8'), str(path), 'exec')\n"
)


class EvolveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Outcome:
    ok: bool
    report: str


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise EvolveError(f"git {args[0]}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _review_prompt(target: str, principles: str, diff: str) -> str:
    return (
        f"You review a change that Zoe, a personal assistant, wants to make to her own {target}. "
        "Approve only if it follows the principles below. Reject a change that patches a symptom instead "
        "of fixing the cause, adds a special case, duplicates existing code, leaves dead code, changes "
        "behavior without a test, weakens a protected part or a test, adds comments about history or "
        "earlier versions, or puts personal data in the core.\n\n"
        f"PRINCIPLES\n{principles}\n\nDIFF\n{diff}\n\n"
        "Give your reasons in at most ten lines, then end with a line that is exactly "
        "VERDICT: APPROVE or VERDICT: REJECT."
    )


class Evolution:
    """The change pipeline of one installation."""

    def __init__(
        self,
        cfg: Config,
        core_root: Path = plugins.CORE_ROOT,
        reviewer: Callable[[str], str] | None = None,
    ) -> None:
        self._cfg = cfg
        self._core = core_root
        self._work = cfg.data_dir / "evolve"
        self._state_file = self._work / "state.json"
        self._history_file = self._work / "history.jsonl"
        self._reviewer = reviewer or self._claude_review

    # --- pipeline ------------------------------------------------------------

    def start(self, target: str, name: str) -> Path:
        """Open a worktree for a change and return the folder to edit."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", name):
            raise EvolveError("the name uses lowercase letters, digits and dashes")
        repo, tree = self._repo(target), self._tree(target, name)
        if not tree.exists():
            self._work.mkdir(parents=True, exist_ok=True)
            _git(repo, "worktree", "add", "-q", "-b", f"evolve/{name}", str(tree))
        return tree

    def check(self, target: str, name: str) -> Outcome:
        """Commit the work in progress and run every check against it."""
        repo, tree = self._repo(target), self._existing_tree(target, name)
        try:
            _git(tree, "add", "-A")
            if _git(tree, "status", "--porcelain"):
                _git(tree, "commit", "-q", "-m", "Work in progress")
        except EvolveError as exc:
            return Outcome(False, f"the work could not be committed: {exc}")

        base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        changed = _git(tree, "diff", "--name-only", f"{base}...HEAD").split()
        if not changed:
            return Outcome(False, "there are no changes yet")

        ok, lines = True, []
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        for label, command in self._checks(target, tree, base):
            run = subprocess.run(
                command, cwd=tree, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            if run.returncode == 0:
                lines.append(f"ok      {label}")
            else:
                ok = False
                lines.append(f"FAILED  {label}\n{(run.stdout + run.stderr).strip()[-2000:]}")
        protected = sorted(set(changed) & PROTECTED) if target == "core" else []
        if protected:
            lines.append("protected files changed: " + ", ".join(protected))

        self._save(target, name, checked=_git(tree, "rev-parse", "HEAD") if ok else None, protected=protected)
        return Outcome(ok, "\n".join(lines))

    def review(self, target: str, name: str) -> Outcome:
        """Have the checked change judged against the principles in force."""
        repo, tree = self._repo(target), self._existing_tree(target, name)
        head = _git(tree, "rev-parse", "HEAD")
        if self._state(target, name).get("checked") != head:
            return Outcome(False, "run check first: the change differs from the one last checked")
        base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        diff = _git(tree, "diff", f"{base}...HEAD")
        if len(diff) > _DIFF_LIMIT:
            diff = diff[:_DIFF_LIMIT] + "\n[diff truncated]"
        principles = _git(self._core, "show", f"HEAD:{PRINCIPLES}")
        verdict = self._reviewer(_review_prompt(target, principles, diff)).strip()
        approved = bool(verdict) and verdict.splitlines()[-1].strip().upper() == "VERDICT: APPROVE"
        self._save(target, name, reviewed=head if approved else None)
        return Outcome(approved, verdict)

    def apply(self, target: str, name: str, message: str, owner_approved: bool = False) -> Outcome:
        """Merge a checked and approved change as one commit."""
        repo, tree = self._repo(target), self._existing_tree(target, name)
        if _git(tree, "status", "--porcelain"):
            raise EvolveError("the change was edited after its check: run check and review again")
        state, head = self._state(target, name), _git(tree, "rev-parse", "HEAD")
        if state.get("checked") != head or state.get("reviewed") != head:
            raise EvolveError("the change must pass check and review in its current form")
        if state.get("protected") and not owner_approved:
            raise EvolveError("protected files changed: apply them only on the owner's explicit request")
        if not message.strip():
            raise EvolveError("a message saying what changes and why is required")
        if _git(repo, "status", "--porcelain"):
            raise EvolveError("the live copy has uncommitted changes")

        if target == "core":
            _git(repo, "config", "core.hooksPath", ".githooks")
        _git(repo, "merge", "--squash", f"evolve/{name}")
        try:
            _git(repo, "commit", "-q", "-m", message.strip())
        except EvolveError:
            _git(repo, "reset", "-q", "--hard", "HEAD")
            raise
        applied = _git(repo, "rev-parse", "HEAD")
        self._close(target, name)
        self._log({"target": target, "sha": applied, "message": message.strip(), "at": time.time()})
        if target == "core":
            notices.post(self._cfg.data_dir, "self_updated", summary=message.strip())
            self._request_restart()
        return Outcome(True, f"applied as {applied[:8]}" + ("; restarting when idle" if target == "core" else ""))

    def discard(self, target: str, name: str) -> Outcome:
        self._close(target, name)
        return Outcome(True, f"discarded {target}/{name}")

    def last_change(self) -> dict | None:
        """Return the most recent applied change that is still in place, if any."""
        entries = self._history()
        undone = {e["undo"] for e in entries if "undo" in e}
        return next((e for e in reversed(entries) if "sha" in e and e["sha"] not in undone), None)

    def undo(self) -> Outcome:
        """Revert the most recent applied change that is still in place."""
        last = self.last_change()
        if last is None:
            return Outcome(False, "")
        repo = self._repo(last["target"])
        try:
            _git(repo, "revert", "--no-edit", last["sha"])
        except EvolveError:
            subprocess.run(["git", "revert", "--abort"], cwd=repo, capture_output=True)
            raise
        self._log({"undo": last["sha"], "at": time.time()})
        if last["target"] == "core":
            self._request_restart()
        return Outcome(True, last["message"])

    def status(self) -> str:
        open_changes = sorted(p.name for p in self._work.glob("*-*") if p.is_dir()) if self._work.exists() else []
        applied = [e for e in self._history() if "sha" in e][-5:]
        lines = ["open: " + (", ".join(open_changes) or "none")]
        lines += [f"applied {e['target']} {e['sha'][:8]}: {e['message']}" for e in applied]
        return "\n".join(lines)

    # --- internals -----------------------------------------------------------

    def _repo(self, target: str) -> Path:
        if target == "core":
            return self._core
        if target == "skills":
            return plugins.ensure_private(self._cfg.data_dir)
        raise EvolveError(f"unknown target {target!r}: use core or skills")

    def _tree(self, target: str, name: str) -> Path:
        return self._work / f"{target}-{name}"

    def _existing_tree(self, target: str, name: str) -> Path:
        tree = self._tree(target, name)
        if not tree.exists():
            raise EvolveError(f"no open change {target}/{name}: start it first")
        return tree

    def _checks(self, target: str, tree: Path, base: str) -> list[tuple[str, list[str]]]:
        python = sys.executable
        if target == "core":
            checks = [("compile", [python, "-c", _COMPILE, "zoe", "tests", "tools"])]
            checks += [(f"tests/{t.name}", [python, str(t.relative_to(tree))]) for t in sorted(tree.glob("tests/test_*.py"))]
            checks.append(("privacy", [python, "tools/privacy_check.py", "--range", f"{base}..HEAD"]))
            return checks
        checks = [("compile", [python, "-c", _COMPILE, "."])]
        checks += [(str(t.relative_to(tree)), [python, str(t.relative_to(tree))]) for t in sorted(tree.rglob("test_*.py"))]
        return checks

    def _close(self, target: str, name: str) -> None:
        repo, tree = self._repo(target), self._tree(target, name)
        if tree.exists():
            _git(repo, "worktree", "remove", "--force", str(tree))
        subprocess.run(["git", "branch", "-D", f"evolve/{name}"], cwd=repo, capture_output=True)
        state = self._all_states()
        state.pop(f"{target}/{name}", None)
        self._write_states(state)

    def _claude_review(self, prompt: str) -> str:
        runner = ClaudeRunner(self._cfg.claude_bin, self._cfg.claude_model, 900, 300)
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.run(
                prompt, Profile(("--tools", "", "--strict-mcp-config", "--setting-sources", "project"), Path(tmp))
            )
        if not result.ok:
            raise EvolveError("the review did not complete")
        return result.text

    def _request_restart(self) -> None:
        (self._cfg.data_dir / "restart.request").touch()

    def _all_states(self) -> dict:
        if not self._state_file.exists():
            return {}
        return json.loads(self._state_file.read_text(encoding="utf-8"))

    def _write_states(self, states: dict) -> None:
        self._work.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(states, indent=2), encoding="utf-8")

    def _state(self, target: str, name: str) -> dict:
        return self._all_states().get(f"{target}/{name}", {})

    def _save(self, target: str, name: str, **values: object) -> None:
        states = self._all_states()
        states.setdefault(f"{target}/{name}", {}).update(values)
        self._write_states(states)

    def _history(self) -> list[dict]:
        if not self._history_file.exists():
            return []
        return [json.loads(line) for line in self._history_file.read_text(encoding="utf-8").splitlines() if line]

    def _log(self, entry: dict) -> None:
        self._work.mkdir(parents=True, exist_ok=True)
        with self._history_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zoe-evolve", description="Change Zoe's code and skills safely.")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "check", "review", "discard"):
        p = sub.add_parser(command)
        p.add_argument("target", choices=TARGETS)
        p.add_argument("name")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("target", choices=TARGETS)
    p_apply.add_argument("name")
    p_apply.add_argument("-m", "--message", required=True)
    p_apply.add_argument("--owner-approved", action="store_true")
    sub.add_parser("undo")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    evolution = Evolution(Config.from_env())
    try:
        if args.command == "start":
            print(evolution.start(args.target, args.name))
            return 0
        if args.command == "status":
            print(evolution.status())
            return 0
        if args.command == "apply":
            outcome = evolution.apply(args.target, args.name, args.message, args.owner_approved)
        elif args.command == "undo":
            outcome = evolution.undo()
            outcome = outcome if outcome.ok else Outcome(False, "nothing to undo")
        else:
            outcome = getattr(evolution, args.command)(args.target, args.name)
    except EvolveError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(outcome.report)
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
