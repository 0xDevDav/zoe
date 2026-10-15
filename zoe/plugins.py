"""The skill plugins loaded into owner turns.

- The built-in plugin ships with Zoe in this repository and holds generic skills.
- The private plugin lives in ZOE_HOME and holds the skills made for this
  installation. It is its own git repository, so every change to it can be
  reviewed and undone, and it is never published.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1]
BUILTIN = CORE_ROOT / "plugin"


def private(home: Path) -> Path:
    return home / "plugin"


def ensure_private(home: Path) -> Path:
    """Create the private plugin and its repository the first time."""
    root = private(home)
    manifest = root / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        manifest.parent.mkdir(parents=True, exist_ok=True)
        (root / "skills").mkdir(parents=True, exist_ok=True)
        (root / "skills" / ".gitkeep").touch()
        manifest.write_text(
            json.dumps({"name": "zoe-private", "version": "0.1.0", "description": "Skills of this installation"},
                       indent=2) + "\n",
            encoding="utf-8",
        )
    if not (root / ".git").exists():
        for args in (
            ("init", "-q"),
            ("config", "user.name", "Zoe"),
            ("config", "user.email", "zoe@localhost"),
            ("add", "-A"),
            ("commit", "-q", "-m", "Private skills of this installation"),
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def plugin_args(home: Path) -> tuple[str, ...]:
    """Return the Claude arguments that load every plugin present."""
    roots = (BUILTIN, private(home))
    return tuple(
        arg for root in roots if (root / ".claude-plugin" / "plugin.json").exists()
        for arg in ("--plugin-dir", str(root))
    )
