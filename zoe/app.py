"""Wire the parts together and run the service until stopped or asked to restart."""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import access, bot_setup, claude_runner, health, i18n, instance, meaning, notices, plugins, say, store
from .brain import Brain
from .config import Config
from .db import apply_schema, connect
from .improve import Improver
from .ingest import Poller
from .launcher import RESTART
from .minds import Minds
from .observer import Observer
from .planner import Cron, Planner
from .say import Outbox
from .speech import Speaker, Transcriber
from .telegram import TelegramAPI
from .vault import Vault, open_vault
from .webapp import MenuServer
from .worker import Worker

log = logging.getLogger("zoe")

# Seconds between two heartbeats written for the launcher.
_BEAT_EVERY = 5.0


# Seconds a stop waits for the sessions it set aside to end and be noted.
_SETTLE = 60.0


def at_work(db_path: Path) -> bool:
    """Whether a turn is waiting or running, a session followed after its answer is replying or
    running a command, or a parallel mind is working. A requested restart waits for them, so it
    never cuts work short. A followed session that only waits for its background tasks is not
    waited for: the stop ends it and plans its resumption. The service's own loop asks, so the
    sessions it follows are counted in memory."""
    if claude_runner.working():
        return True
    conn = connect(db_path)
    try:
        return bool(conn.execute(
            "SELECT EXISTS (SELECT 1 FROM messages WHERE state IN ('received', 'processing')) "
            "OR EXISTS (SELECT 1 FROM minds WHERE state = 'running')"
        ).fetchone()[0])
    finally:
        conn.close()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _speaker(cfg: Config, vault: Vault) -> Speaker | None:
    key = vault.get("elevenlabs", "api_key")
    if not key or not cfg.elevenlabs_voice_id:
        log.info("voice replies off: needs elevenlabs/api_key in the vault and ZOE_ELEVENLABS_VOICE_ID")
        return None
    return Speaker(key, cfg.elevenlabs_voice_id, cfg.elevenlabs_model, pronunciation=cfg.pronunciation)


def _deliver_notices(cfg: Config, api: TelegramAPI, conn: sqlite3.Connection) -> None:
    pending = notices.drain(cfg.data_dir)
    for owner_id, language in say.notice_recipients(conn, cfg):
        for notice in pending:
            api.notify(owner_id, i18n.t(language, notice["key"], **notice["params"]))


def _prepare(cfg: Config, api: TelegramAPI) -> None:
    conn = connect(cfg.db_path)
    apply_schema(conn)
    owner = access.bootstrap_owner(conn, cfg.owner_ids, cfg.owner_name, cfg.default_language)
    if owner is None and not access.owner_recipients(conn):
        log.warning("no owner configured: set ZOE_OWNER_IDS, otherwise nobody can approve access requests")
    store.set_state(conn, health.STARTED, str(time.time()))
    reclaimed = store.reset_stale_processing(conn)
    if reclaimed:
        log.info("requeued %d message(s) left processing by a previous run", reclaimed)
    bot_setup.configure(api, cfg, conn)
    _deliver_notices(cfg, api, conn)
    conn.close()


def main() -> None:
    cfg = Config.from_env()
    _setup_logging(cfg.log_level)
    # Kept open until the service exits: it is what stops a second Zoe from reading the same bot.
    launched = bool(os.environ.get(instance.HELD))
    lock = None if launched else instance.hold(cfg.data_dir)
    if not launched and lock is None:
        raise SystemExit(f"another Zoe is already running with {cfg.data_dir}")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    instance.protect(cfg.data_dir)
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    plugins.ensure_private(cfg.data_dir)
    Brain.meaning = meaning.load(cfg.data_dir / "models")

    try:
        Cron(cfg.review_at)
    except ValueError:
        raise SystemExit(f"ZOE_REVIEW_AT is not a five-field cron rule: {cfg.review_at!r}") from None
    vault = open_vault(cfg)
    token = cfg.bot_token or vault.get("telegram", "bot_token")
    if not token:
        raise SystemExit("no Telegram bot token: set ZOE_BOT_TOKEN or store telegram/bot_token in the vault")
    speaker = _speaker(cfg, vault)
    vault.close()

    api = TelegramAPI(token)
    _prepare(cfg, api)
    transcriber = Transcriber(cfg.whisper_url) if cfg.whisper_url else None
    stop = threading.Event()
    restart_request = cfg.data_dir / "restart.request"

    def _shutdown(*_: object) -> None:
        log.info("shutting down")
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    poller = Poller(cfg, api, stop)
    threads: list[threading.Thread] = [
        poller, Planner(cfg, stop), Observer(cfg, stop), Minds(cfg, stop), Outbox(cfg, api, stop),
        Improver(cfg, stop),
    ]
    if cfg.menu_url:
        try:
            threads.append(MenuServer(cfg, api, token, stop))
        except OSError as exc:
            log.error("the menu is not served: port %d: %s", cfg.menu_port, exc)
    threads += [
        Worker(cfg, api, stop, transcriber=transcriber, speaker=speaker, name=f"worker-{i + 1}")
        for i in range(cfg.workers)
    ]
    for thread in threads:
        thread.start()
    log.info("zoe is running with %d worker(s)", cfg.workers)

    restarting = broken = False
    started, beaten = time.time(), 0.0
    while not stop.wait(1.0):
        now = time.time()
        if now - beaten >= _BEAT_EVERY:
            beaten = now
            alive = all(thread.is_alive() for thread in threads)
            health.write(cfg.data_dir, healthy=alive and poller.turning(now), ready=poller.polled > started)
        if restart_request.exists() and not at_work(cfg.db_path):
            log.info("restart requested and nothing at work: restarting")
            restarting = True
            stop.set()
        dead = [thread.name for thread in threads if not thread.is_alive()]
        if dead:
            # A part that died would leave Zoe running but deaf or mute: exit, and the launcher starts her again.
            log.error("stopped working: %s; exiting so that the launcher restarts the service", ", ".join(dead))
            broken = True
            stop.set()

    for thread in threads:
        thread.join(timeout=cfg.claude_timeout + 60 if restarting else 10)
    if not claude_runner.settle(_SETTLE):
        log.warning("%d background wait(s) not noted before stopping", claude_runner.following())
    if restarting:
        restart_request.unlink(missing_ok=True)
        sys.exit(RESTART)
    if broken:
        sys.exit(1)


if __name__ == "__main__":
    main()
