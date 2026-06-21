#!/usr/bin/env python3
"""Dependency-light secret scanner for CI (cross-cutting requirement SB3).

Scans git-tracked text files for credential-shaped strings (Anthropic / OpenAI
``sk-`` keys, Telegram bot tokens, AWS access keys, ``Bearer`` tokens, private
key headers, and generic ``api_key=...`` assignments) and fails the build if any
*real-looking* secret is committed.

Design goals:
  * No third-party deps and no network — runs anywhere ``python3`` runs, never
    contacts Claude/Anthropic or any service, never needs an API key.
  * Detection rules are intentionally aligned with the P0 scrubber
    (``spikes/session-substrate/scrub.py``) so "what we redact" and "what we
    block from being committed" stay in sync.
  * Low false positives on this repo: obvious placeholders (``.env.example``
    sample values like ``123456789:AA-replace-with-your-bot-token``) are
    allow-listed via marker words so the CLEAN repo passes, while a genuinely
    random token of the same shape still trips the scan.

Usage:
    python scripts/secret_scan.py            # scan git-tracked files
    python scripts/secret_scan.py PATH ...   # scan explicit files/dirs

Exit code 0 = clean, 1 = at least one finding, 2 = usage/IO error.

NOTE (tighten later): this is a pragmatic baseline. It favors a few well-known
high-signal shapes over exhaustive coverage. A future hardening pass could swap
in / add gitleaks (no secrets required) for broader rule coverage; the contract
(fail on a real token, pass on the clean repo, no network/API key) stays.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

# --- detection rules ---------------------------------------------------------
# Each rule is (name, compiled-regex). Kept deliberately close to the shapes the
# P0 scrubber redacts. Telegram/sk- shapes are the load-bearing ones for this
# project (the bot token is the crown-jewel secret per SB3).
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")),
    ("openai-style-key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    # Telegram bot token: <digits>:<35+ token chars>. The threshold is 35 (real
    # tokens are ~35) rather than the scrubber's looser 30, so the 30-char
    # ".env.example" placeholder does not even reach this rule.
    ("telegram-bot-token", re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{35,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    ("private-key-header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    # Generic credential assignment with a high-entropy-ish value (>=12 chars,
    # not pure words). Keeps the key name out of the match via a group.
    (
        "credential-assignment",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|secret|token|password|passwd|pwd)"
            r"\s*[=:]\s*['\"]?([A-Za-z0-9_./+=-]{16,})['\"]?",
            re.IGNORECASE,
        ),
    ),
]

# Lines containing any of these (case-insensitive) marker words are treated as
# documentation / placeholders, not real secrets. This is what lets the clean
# repo — which legitimately ships sample tokens in .env.example and dummy values
# like bot_token="t" in tests — pass while a real leaked credential fails.
_PLACEHOLDER_MARKERS = (
    "replace",
    "your-",
    "your_",
    "yourtoken",
    "example",
    "placeholder",
    "changeme",
    "change-me",
    "dummy",
    "fake",
    "sample",
    "xxxxx",
    "<token>",
    "<your",
    "redacted",
)

# Paths (relative, prefix match) that are sample/templated by design.
_ALLOWLIST_PREFIXES = (
    ".env.example",
    "scripts/secret_scan.py",  # this file documents the patterns it scans for
    "spikes/session-substrate/scrub.py",  # the scrubber documents the same shapes
    "spikes/session-substrate/test_scrub.py",
)

# Only scan plausibly-textual files; skip obvious binaries/artifacts by suffix.
_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".whl", ".pyc", ".so", ".dylib", ".bin", ".lock",
}

_MAX_BYTES = 2_000_000  # don't read very large files into memory


def _git_tracked_files(root: Path) -> list[Path]:
    """Return git-tracked files (deterministic; ignores venvs/caches/untracked)."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Not a git repo (or git missing): fall back to walking the tree.
        return [p for p in root.rglob("*") if p.is_file()]
    return [root / name for name in out.split("\0") if name]


def _iter_files(paths: Iterable[Path]) -> Iterator[Path]:
    for p in paths:
        if p.is_dir():
            yield from (f for f in p.rglob("*") if f.is_file())
        elif p.is_file():
            yield p


def _is_allowlisted(rel: str) -> bool:
    return any(rel == pre or rel.startswith(pre) for pre in _ALLOWLIST_PREFIXES)


def _looks_like_placeholder(line: str) -> bool:
    low = line.lower()
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def scan_file(path: Path, root: Path) -> list[tuple[int, str, str]]:
    """Return a list of (line_no, rule_name, line_text) findings for ``path``."""
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    if _is_allowlisted(rel) or path.suffix.lower() in _SKIP_SUFFIXES:
        return []
    try:
        if path.stat().st_size > _MAX_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []  # unreadable / binary -> skip, don't crash CI

    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _looks_like_placeholder(line):
            continue
        for name, pattern in _RULES:
            if pattern.search(line):
                findings.append((lineno, name, line.strip()[:120]))
                break  # one finding per line is enough to fail
    return findings


def main(argv: list[str]) -> int:
    root = Path.cwd()
    if argv:
        targets = list(_iter_files(Path(a) for a in argv))
    else:
        targets = _git_tracked_files(root)

    total = 0
    for path in targets:
        for lineno, rule, snippet in scan_file(path, root):
            rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            print(f"{rel}:{lineno}: [{rule}] {snippet}")
            total += 1

    if total:
        print(f"\nsecret-scan: FAILED — {total} potential secret(s) found.", file=sys.stderr)
        return 1
    print(f"secret-scan: OK — scanned {len(targets)} file(s), no secrets found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
