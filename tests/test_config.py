"""Checks for the configuration: a change to the .env file reaches the next start of a process
whose environment comes from one that read the file before the change."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe.config import Config

NAMES = ("ZOE_SHOW_STRENGTH", "ZOE_MINDS", "ZOE_OWNER_NAME", "ZOE_BOT_NAME")


def test_the_file_as_it_is_now_wins_over_an_environment_inherited_from_an_earlier_read():
    before = {name: os.environ.get(name) for name in NAMES}
    try:
        for name in NAMES:
            os.environ.pop(name, None)
        os.environ["ZOE_OWNER_NAME"] = "Alex"
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("ZOE_SHOW_STRENGTH=1\nZOE_MINDS=4\nZOE_BOT_NAME=Mia\n", encoding="utf-8")
            # The launcher reads the file at its start and hands the service its environment.
            assert Config.from_env(dotenv).show_strength
            dotenv.write_text("# shown under replies\nZOE_SHOW_STRENGTH=0\nZOE_MINDS='4'\nZOE_BOT_NAME=\n",
                              encoding="utf-8")
            cfg = Config.from_env(dotenv)
        assert not cfg.show_strength
        assert cfg.minds == 4 and cfg.bot_name == "Zoe"
        assert cfg.owner_name == "Alex"
    finally:
        for name, value in before.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
