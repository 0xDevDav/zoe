"""Checks for the privacy guard's reading of files: text is searched, binary files are judged by path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import privacy_check

# Made up at run time, so this file holds no address the guard would stop.
ADDRESS = "alex" + "@" + "personal-mail.net"


def _scanner() -> privacy_check.Scanner:
    scanner = privacy_check.Scanner.__new__(privacy_check.Scanner)
    scanner.terms, scanner.findings = [], []
    return scanner


def test_text_is_searched_and_binary_bytes_are_not_read_as_text():
    scanner = _scanner()
    scanner.content("notes.md", f"write to {ADDRESS}".encode())
    assert scanner.findings == [f"notes.md:1: email address {ADDRESS}"]
    scanner.findings.clear()
    scanner.content("shot.png", bytes([0x89]) + b"PNG" + bytes([0, 0]) + ADDRESS.encode() + bytes([0]))
    assert scanner.findings == []
    scanner.path("data/zoe.db", "data/zoe.db")
    assert scanner.findings == ["data/zoe.db: file that must never be published"]


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
