"""Checks for the service's threads: none hides a name Python's own threads use, and each stops and joins."""

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeAPI, worker_setup
from zoe.ingest import Poller
from zoe.minds import Minds
from zoe.observer import Observer
from zoe.planner import Planner
from zoe.say import Outbox
from zoe.worker import Worker

# Private names threading.Thread uses on some Python version. An attribute of ours with one of
# these names breaks a version that has it, as _stop did on 3.12: join() calls self._stop().
THREAD_INTERNALS = {
    "_stop", "_started", "_target", "_name", "_args", "_kwargs", "_daemonic", "_ident", "_native_id",
    "_tstate_lock", "_is_stopped", "_initialized", "_stderr", "_invoke_excepthook", "_handle",
    "_os_thread_handle", "_context", "_bootstrap", "_wait_for_tstate_lock", "_delete", "_reset_internal_locks",
}


def _threads(home: Path) -> tuple[list[threading.Thread], threading.Event]:
    conn, _, worker, *_ = worker_setup(home, on_disk=True)
    conn.close()
    stop, cfg, api = threading.Event(), worker._cfg, FakeAPI()
    return [Worker(cfg, api, stop), Poller(cfg, api, stop), Planner(cfg, stop, interval=0.05),
            Observer(cfg, stop, interval=0.05), Minds(cfg, stop, interval=0.05), Outbox(cfg, api, stop, interval=0.05)], stop


def test_no_thread_of_ours_hides_a_name_python_threads_use():
    with tempfile.TemporaryDirectory() as tmp:
        threads, _ = _threads(Path(tmp))
        for thread in threads:
            ours = set(vars(thread)) - set(vars(threading.Thread()))
            assert not ours & THREAD_INTERNALS, (type(thread).__name__, ours & THREAD_INTERNALS)


def test_every_thread_stops_and_joins():
    with tempfile.TemporaryDirectory() as tmp:
        threads, stop = _threads(Path(tmp))
        quiet = [t for t in threads if not isinstance(t, Poller)]
        for thread in quiet:
            thread.start()
        stop.set()
        for thread in quiet:
            thread.join(timeout=10)
            assert not thread.is_alive(), type(thread).__name__


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
