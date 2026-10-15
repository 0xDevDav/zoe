"""Refuse commits and pushes that would publish personal data.

Every file a commit adds or a push publishes is checked for:

- paths that must never be versioned: databases, vault keys, environment files, audio;
- secrets recognizable by their format: bot tokens, API keys, private keys, email addresses;
- personal terms listed one per line in ``ZOE_HOME/private-terms`` (``~/.zoe`` by
  default) or ``.git/info/private-terms``. Both live outside the published tree,
  so the list itself is never published.

Binary files (images, fonts) are checked by their path only: read as text, their bytes
would turn up addresses and terms that are not there.

On pushes and full scans, commit messages and author identities are checked too.

    python tools/privacy_check.py --staged       # what the next commit adds
    python tools/privacy_check.py --range A..B   # what a push publishes
    python tools/privacy_check.py --all          # the whole history
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_PATHS = (
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3", "*.key", "*.pem",
    ".env", "*/.env", "*.ogg", "*.oga", "*.opus", "*.mp3", "*.wav", "*.m4a",
    "data/*", "*/data/*",
)

SECRET_PATTERNS = (
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("API key", re.compile(r"\bsk[_-](?:ant-)?[A-Za-z0-9_-]{24,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL_DOMAINS = ("users.noreply.github.com", "example.com", "example.org")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _private_terms() -> list[re.Pattern]:
    home = Path(os.environ.get("ZOE_HOME", "").strip() or "~/.zoe").expanduser()
    git_dir = Path(_git("rev-parse", "--git-dir").decode().strip())
    terms: list[str] = []
    for source in (home / "private-terms", git_dir / "info" / "private-terms"):
        if source.exists():
            for line in source.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line)
    return [re.compile(rf"(?<!\w){re.escape(t)}(?!\w)", re.IGNORECASE) for t in dict.fromkeys(terms)]


class Scanner:
    def __init__(self) -> None:
        self.terms = _private_terms()
        self.findings: list[str] = []

    def path(self, where: str, path: str) -> None:
        name = path.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in FORBIDDEN_PATHS):
            self.findings.append(f"{where}: file that must never be published")

    def content(self, where: str, data: bytes) -> None:
        if b"\0" not in data:
            self.text(where, data.decode("utf-8", errors="ignore"))

    def text(self, where: str, content: str) -> None:
        for number, line in enumerate(content.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    self.findings.append(f"{where}:{number}: {label}")
            for match in EMAIL.finditer(line):
                if not match.group().lower().endswith(ALLOWED_EMAIL_DOMAINS):
                    self.findings.append(f"{where}:{number}: email address {match.group()}")
            for pattern in self.terms:
                match = pattern.search(line)
                if match:
                    self.findings.append(f"{where}:{number}: personal term '{match.group()}'")


def scan_staged(scanner: Scanner) -> None:
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0")
    for name in filter(None, names):
        scanner.path(name, name)
        scanner.content(name, _git("show", f":{name}"))
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        scanner.text(f"identity ({var})", _git("var", var).decode(errors="ignore"))


def scan_history(scanner: Scanner, revisions: list[str]) -> None:
    for commit in _git("rev-list", *revisions).decode().split():
        meta = _git("log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B", commit).decode(errors="ignore")
        scanner.text(f"commit {commit[:8]}", meta)

    seen: set[str] = set()
    for line in _git("rev-list", "--objects", *revisions).decode(errors="ignore").splitlines():
        sha, _, path = line.partition(" ")
        if not path or sha in seen:
            continue
        seen.add(sha)
        if _git("cat-file", "-t", sha).decode().strip() != "blob":
            continue
        scanner.path(path, path)
        scanner.content(path, _git("cat-file", "-p", sha))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Block personal data from being committed or pushed.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--range", metavar="REVISIONS")
    mode.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    scanner = Scanner()
    if args.staged:
        scan_staged(scanner)
    else:
        scan_history(scanner, ["--all"] if args.all else [args.range])

    if not scanner.terms:
        print("privacy check: no private-terms list found, personal terms are not being checked", file=sys.stderr)
    if scanner.findings:
        print("privacy check failed:", file=sys.stderr)
        for finding in dict.fromkeys(scanner.findings):
            print(f"  {finding}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
