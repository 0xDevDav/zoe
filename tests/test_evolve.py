"""Checks for the change pipeline: nothing reaches the live code without passing every gate."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zoe import notices
from zoe.config import Config
from zoe.evolve import EvolveError, Evolution


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _approve(_prompt: str) -> str:
    return "Small and tested.\nVERDICT: APPROVE"


def _reject(_prompt: str) -> str:
    return "This is a special case.\nVERDICT: REJECT"


def _setup(tmp: Path, reviewer=_approve) -> tuple[Path, Path, Evolution]:
    repo, home = tmp / "core", tmp / "home"
    files = {
        "zoe/__init__.py": "",
        "zoe/calc.py": "def add(a, b):\n    return a + b\n",
        "zoe/access.py": "ROLE = 'member'\n",
        "tests/test_calc.py": (
            "import sys, pathlib\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
            "from zoe.calc import add\n"
            "assert add(2, 2) == 4\n"
        ),
        "plugin/skills/self-improvement/PRINCIPLES.md": "Fix causes, not symptoms.\n",
    }
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
    (repo / "tools").mkdir()
    shutil.copy(ROOT / "tools" / "privacy_check.py", repo / "tools" / "privacy_check.py")
    _git(tmp, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Start")
    os.environ["ZOE_HOME"] = str(home)
    cfg = Config(
        bot_token="", owner_ids=frozenset(), data_dir=home, db_path=home / "zoe.db",
        vault_path=home / "v.db", vault_key_path=home / "v.key", workdir=home / "workspace",
    )
    return repo, home, Evolution(cfg, core_root=repo, reviewer=reviewer)


def test_a_change_passes_check_and_review_then_lands_as_one_commit():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, home, evolution = _setup(Path(tmp))
        tree = evolution.start("core", "clearer-add")
        (tree / "zoe" / "calc.py").write_text("def add(a, b):\n    return sum((a, b))\n", encoding="utf-8")

        assert evolution.check("core", "clearer-add").ok
        assert evolution.review("core", "clearer-add").ok
        assert evolution.apply("core", "clearer-add", "Somma con sum").ok

        assert "sum((a, b))" in (repo / "zoe" / "calc.py").read_text(encoding="utf-8")
        assert _git(repo, "log", "-1", "--format=%s") == "Somma con sum"
        assert _git(repo, "rev-list", "--count", "HEAD") == "2"
        assert not tree.exists()
        assert (home / "restart.request").exists()
        assert notices.drain(home)[0]["key"] == "self_updated"


def test_undo_reverts_the_last_change():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, _, evolution = _setup(Path(tmp))
        tree = evolution.start("core", "clearer-add")
        (tree / "zoe" / "calc.py").write_text("def add(a, b):\n    return sum((a, b))\n", encoding="utf-8")
        evolution.check("core", "clearer-add")
        evolution.review("core", "clearer-add")
        evolution.apply("core", "clearer-add", "Somma con sum")

        assert evolution.undo().ok
        assert (repo / "zoe" / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
        assert not evolution.undo().ok


def test_failing_tests_block_the_change():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _, _, evolution = _setup(Path(tmp))
        tree = evolution.start("core", "broken")
        (tree / "zoe" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        assert not evolution.check("core", "broken").ok
        assert not evolution.review("core", "broken").ok
        try:
            evolution.apply("core", "broken", "x")
            raise AssertionError("a failing change must not apply")
        except EvolveError:
            pass


def test_a_rejected_review_blocks_the_change():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _, _, evolution = _setup(Path(tmp), reviewer=_reject)
        tree = evolution.start("core", "special-case")
        (tree / "zoe" / "calc.py").write_text("def add(a, b):\n    return 4 if a == 2 else a + b\n", encoding="utf-8")
        assert evolution.check("core", "special-case").ok
        assert not evolution.review("core", "special-case").ok
        try:
            evolution.apply("core", "special-case", "x")
            raise AssertionError("a rejected change must not apply")
        except EvolveError:
            pass


def test_edits_after_the_review_need_a_new_check():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _, _, evolution = _setup(Path(tmp))
        tree = evolution.start("core", "late-edit")
        (tree / "zoe" / "calc.py").write_text("def add(a, b):\n    return sum((a, b))\n", encoding="utf-8")
        evolution.check("core", "late-edit")
        evolution.review("core", "late-edit")
        (tree / "zoe" / "calc.py").write_text("def add(a, b):\n    return b + a\n", encoding="utf-8")
        try:
            evolution.apply("core", "late-edit", "x")
            raise AssertionError("an edit after review must not apply")
        except EvolveError:
            pass


def test_protected_files_need_the_owner():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, _, evolution = _setup(Path(tmp))
        tree = evolution.start("core", "open-access")
        (tree / "zoe" / "access.py").write_text("ROLE = 'owner'\n", encoding="utf-8")
        report = evolution.check("core", "open-access")
        assert report.ok and "protected files changed: zoe/access.py" in report.report
        evolution.review("core", "open-access")
        try:
            evolution.apply("core", "open-access", "x")
            raise AssertionError("a protected change must need the owner")
        except EvolveError:
            pass
        assert evolution.apply("core", "open-access", "Ruolo cambiato su richiesta", owner_approved=True).ok
        assert "owner" in (repo / "zoe" / "access.py").read_text(encoding="utf-8")


def test_private_skills_apply_without_restart():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _, home, evolution = _setup(Path(tmp))
        tree = evolution.start("skills", "weather")
        (tree / "skills" / "weather").mkdir(parents=True)
        (tree / "skills" / "weather" / "SKILL.md").write_text("---\nname: weather\n---\nForecasts.\n", encoding="utf-8")
        assert evolution.check("skills", "weather").ok
        assert evolution.review("skills", "weather").ok
        assert evolution.apply("skills", "weather", "Skill meteo").ok
        assert (home / "plugin" / "skills" / "weather" / "SKILL.md").exists()
        assert not (home / "restart.request").exists()


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
