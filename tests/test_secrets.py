"""Checks for recognizing keys pasted into a chat and masking them."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe import secrets

SAVED = "[chiave salvata in credenziali]"


def test_keys_of_every_known_shape_are_masked():
    keys = [
        "sk-zylo-v1-" + "0a1b2c3d" * 8,
        "EAA" + "Xy7Kq" * 10,
        "KGAT_" + "0f1e2d3c" * 4,
        "rpa_" + "Zq9Lm2" * 5,
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJtaWEifQ.c2lnbmF0dXJl",
        "0123456789abcdef" * 2,
        "0123456789abcdef" * 4,
        "3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f:" + "ab12" * 8,
    ]
    for key in keys:
        assert secrets.is_secret(key), key
        assert secrets.mask(f"ecco ({key}).", "it") == f"ecco ({SAVED})."


def test_ids_quoted_on_purpose_are_left_alone():
    words = [
        "3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f",
        "9f2c1e7b" * 5,
        "9f2c1e7",
        "AAMCBAADGQEAAg" + "x9Kq" * 12,
        "risk-assessment-for-the-new-office",
    ]
    for word in words:
        assert not secrets.is_secret(word), word
        assert secrets.mask(f"vedi {word}", "it") == f"vedi {word}"


def test_found_lists_each_key_once():
    key = "0123456789abcdef" * 2
    assert secrets.found(f"{key} e {key}", None, "niente") == [key]


def test_the_placeholder_speaks_the_persons_language():
    key = "0123456789abcdef" * 2
    assert secrets.mask(f"key {key}", "en") == "key [key saved in credentials]"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
