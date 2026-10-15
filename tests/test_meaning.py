"""Checks for the meaning model's download: one fixed revision, every file checked against its SHA-256."""

import hashlib
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import meaning


def test_a_file_that_is_not_the_expected_one_is_refused_and_nothing_is_kept():
    original = urllib.request.urlretrieve, meaning._FILES
    asked = []

    def serve(content: bytes):
        def fetch(url: str, path: Path) -> None:
            asked.append(url)
            Path(path).write_bytes(content)
        return fetch

    try:
        meaning._FILES = {"tokenizer.json": hashlib.sha256(b"the real tokenizer").hexdigest()}
        with tempfile.TemporaryDirectory() as tmp:
            urllib.request.urlretrieve = serve(b"a tampered tokenizer")
            try:
                meaning.download(Path(tmp))
                raise AssertionError("a file with the wrong SHA-256 was kept")
            except ValueError:
                pass
            assert not list(Path(tmp).rglob("tokenizer.json*"))
            urllib.request.urlretrieve = serve(b"the real tokenizer")
            meaning.download(Path(tmp))
            assert (Path(tmp) / meaning.MODEL / "tokenizer.json").read_bytes() == b"the real tokenizer"
            assert all("/resolve/5090578d9565bb06545b4552f76e6bc2c93e4a66/" in url for url in asked)
    finally:
        urllib.request.urlretrieve, meaning._FILES = original


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
