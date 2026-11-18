"""Runtime configuration, loaded from an optional .env file and the environment.

A setting the file names takes the file's value as it is when the process starts,
over the one in the environment: a process inherits its environment from one that
may have read an older version of the file, as the service does from its launcher,
so only the file can say what a setting is now. The environment sets the rest,
``ZOE_HOME`` first.

Everything personal lives under ``ZOE_HOME`` (``~/.zoe`` by default), outside the
source tree: the configuration file, the database with the neuron graph, the
vault and its key, each person's working directory, and the owner's workspace.

Secrets do not belong in the configuration: tokens and keys live in the vault.
The bot token may also come from the environment so a fresh install can start
before the vault is filled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import i18n


def read_dotenv(path: Path) -> dict[str, str]:
    """Return the settings the file names, none when it does not exist."""
    settings: dict[str, str] = {}
    if not path.exists():
        return settings
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip().strip('"').strip("'")
    return settings


def load_dotenv(path: Path) -> None:
    os.environ.update(read_dotenv(path))


def home() -> Path:
    """Return the directory holding all personal state. Set ZOE_HOME in the real environment."""
    return Path(os.environ.get("ZOE_HOME", "").strip() or "~/.zoe").expanduser()


def env_file() -> Path:
    """Return the configuration file: ZOE_ENV_FILE, else ZOE_HOME/.env, else ./.env."""
    explicit = os.environ.get("ZOE_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    in_home = home() / ".env"
    return in_home if in_home.exists() else Path(".env")


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_ids: frozenset[int]
    data_dir: Path
    db_path: Path
    vault_path: Path
    vault_key_path: Path
    workdir: Path
    owner_name: str = "Owner"
    # The first of ZOE_OWNER_IDS, which keeps their order: the owner's main chat, where Zoe's own notices go.
    owner_chat: int | None = None
    bot_name: str = "Zoe"
    bot_photo: Path | None = None
    workers: int = 3
    default_language: str = i18n.DEFAULT
    claude_bin: str = "claude"
    # The model every turn, parallel mind and study runs on, at the effort strength.py gives it.
    claude_model: str = "claude-opus-5-5"
    # Whether the person sees the model and effort of each run: under every reply, beside each parallel mind.
    show_strength: bool = False
    claude_timeout: float = 1800.0
    claude_inactivity_timeout: float = 240.0
    claude_background_timeout: float = 14400.0
    max_attempts: int = 2
    # The model of the short runs beside the chat: the observer, recall checks, notices read with their chat.
    observer_model: str = "claude-sonnet-5-5"
    observer_idle: float = 600.0
    # Tools a finished owner turn or parallel mind must have run to be reviewed for lessons (0: no reviews),
    # and the most lesson reviews in a day (lessons.py).
    lesson_tools: int = 10
    lessons_per_day: int = 12
    minds: int = 2
    mind_timeout: float = 14400.0
    mind_inactivity_timeout: float = 1800.0
    mind_min_free_mb: int = 1024
    # Commands that publish work, beside ``git commit``, each as its words with ``*`` for any one word:
    # a mind that launched one and tells nothing for some minutes is asked for its news (minds.py).
    publish_commands: tuple[str, ...] = ()
    temp_file_days: float = 7.0
    whisper_url: str = "http://127.0.0.1:8790"
    glossary_chars: int = 700
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_v4"
    tts_max_chars: int = 1500
    pronunciation: Path | None = None
    review_at: str = "0 3 * * *"
    improve_at: str = "30 3 * * *"
    menu_url: str = ""
    menu_port: int = 8791
    poll_timeout: int = 50
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, dotenv: Path | None = None) -> "Config":
        load_dotenv(dotenv or env_file())
        env = os.environ.get

        listed = [int(x) for x in env("ZOE_OWNER_IDS", "").replace(",", " ").split() if x]
        data_dir = home()

        return cls(
            bot_token=env("ZOE_BOT_TOKEN", "").strip(),
            owner_ids=frozenset(listed),
            owner_chat=listed[0] if listed else None,
            data_dir=data_dir,
            db_path=data_dir / "zoe.db",
            vault_path=data_dir / "vault.db",
            vault_key_path=data_dir / "vault.key",
            workdir=_path("ZOE_WORKDIR", data_dir / "workspace"),
            owner_name=env("ZOE_OWNER_NAME", "Owner").strip() or "Owner",
            bot_name=env("ZOE_BOT_NAME", "Zoe").strip() or "Zoe",
            bot_photo=_path("ZOE_BOT_PHOTO", data_dir / "avatar.png"),
            workers=max(1, int(env("ZOE_WORKERS", "3"))),
            default_language=i18n.normalize(env("ZOE_DEFAULT_LANG"), i18n.DEFAULT),
            claude_bin=env("ZOE_CLAUDE_BIN", "claude"),
            claude_model=env("ZOE_CLAUDE_MODEL", "claude-opus-5-5"),
            show_strength=env("ZOE_SHOW_STRENGTH", "").strip().lower() in ("1", "true", "yes", "on"),
            claude_timeout=float(env("ZOE_CLAUDE_TIMEOUT", "1800")),
            claude_inactivity_timeout=float(env("ZOE_CLAUDE_INACTIVITY_TIMEOUT", "240")),
            claude_background_timeout=float(env("ZOE_CLAUDE_BACKGROUND_TIMEOUT", "14400")),
            max_attempts=int(env("ZOE_MAX_ATTEMPTS", "2")),
            observer_model=env("ZOE_OBSERVER_MODEL", "claude-sonnet-5-5"),
            observer_idle=float(env("ZOE_OBSERVER_IDLE", "600")),
            lesson_tools=int(env("ZOE_LESSON_TOOLS", "10")),
            lessons_per_day=int(env("ZOE_LESSONS_PER_DAY", "12")),
            minds=max(1, int(env("ZOE_MINDS", "2"))),
            mind_timeout=float(env("ZOE_MIND_TIMEOUT", "14400")),
            mind_inactivity_timeout=float(env("ZOE_MIND_INACTIVITY_TIMEOUT", "1800")),
            mind_min_free_mb=int(env("ZOE_MIND_MIN_FREE_MB", "1024")),
            publish_commands=tuple(
                " ".join(command.split()) for command in env("ZOE_PUBLISH_COMMANDS", "").split(",") if command.strip()),
            temp_file_days=float(env("ZOE_TEMP_FILE_DAYS", "7")),
            whisper_url=env("ZOE_WHISPER_URL", "http://127.0.0.1:8790").strip(),
            glossary_chars=int(env("ZOE_GLOSSARY_CHARS", "700")),
            elevenlabs_voice_id=env("ZOE_ELEVENLABS_VOICE_ID", "").strip(),
            elevenlabs_model=env("ZOE_ELEVENLABS_MODEL", "eleven_v4").strip(),
            tts_max_chars=int(env("ZOE_TTS_MAX_CHARS", "1500")),
            pronunciation=_path("ZOE_PRONUNCIATION", data_dir / "pronunciation.txt"),
            review_at=env("ZOE_REVIEW_AT", "0 3 * * *").strip(),
            improve_at=env("ZOE_IMPROVE_AT", "30 3 * * *").strip(),
            menu_url=env("ZOE_MENU_URL", "").strip(),
            menu_port=int(env("ZOE_MENU_PORT", "8791")),
            poll_timeout=int(env("ZOE_POLL_TIMEOUT", "50")),
            log_level=env("ZOE_LOG_LEVEL", "INFO").upper(),
        )
