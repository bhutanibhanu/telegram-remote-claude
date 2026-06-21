"""Shared helpers for the P1 async-latency de-risk spike (throwaway quality).

This spike answers ONE gating question for the streaming engine: can a Claude
session's ``can_use_tool`` callback (and the native interactive-tool answer) be
held OPEN for multiple minutes while a human decides asynchronously — without the
SDK/CLI timing out the control request or wedging the session — and is there a
near-term ceiling that would break the engine's 60-minute backstop design?

It REUSES the proven P0 substrate-A harness (``SDKSessionHarness``), the P0
secret scrubber (``scrub``), and the P0 evidence recorder (``record_criterion``)
from the sibling ``spikes/session-substrate/`` tree — imported via sys.path so we
touch no production file and copy nothing we don't have to.

Everything here is contained: disposable temp fixtures OUTSIDE the repo, host CLI
auth only (NO API key), descendant-scoped CLI-process leak checks (so the running
Telegram bot — a separate process tree — can never be caught), and cleanup of the
temp fixtures plus any ``~/.claude/projects/<sanitized-temp-cwd>`` transcript dirs
the CLI creates for our throwaway cwds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# --- import the proven P0 code from the sibling session-substrate spike -------
# We deliberately reuse rather than re-implement: same scrubber, same evidence
# recorder, same SDK harness that P0 (ADR-001) validated on claude-agent-sdk
# 0.2.105. These are tracked files in this worktree.
_THIS = Path(__file__).resolve()
_SPIKE_DIR = _THIS.parent  # spikes/p1-async-latency
_SS_DIR = _SPIKE_DIR.parent / "session-substrate"
for _p in (str(_SPIKE_DIR), str(_SS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scrub import scrub  # noqa: E402  (P0 SB3 scrubber)
from evidence_recorder import record_criterion  # noqa: E402  (P0 T3 recorder)
from harness_sdk import SDKSessionHarness, assistant_text  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

#: Evidence dir for THIS spike. The P0 recorder defaults base_dir to its OWN
#: location (session-substrate/evidence), so we MUST pass this explicitly on every
#: record_criterion() call — otherwise evidence would land outside this spike,
#: violating the "modify ONLY files under spikes/p1-async-latency/" containment.
EVIDENCE_DIR = _SPIKE_DIR / "evidence"

__all__ = [
    "scrub",
    "record_criterion",
    "EVIDENCE_DIR",
    "SDKSessionHarness",
    "assistant_text",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ResultMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "descendant_claude_pids",
    "clean_project_transcript_dir",
    "safe_input_summary",
    "git_porcelain",
    "WORKTREE",
]

WORKTREE = _SPIKE_DIR.parents[1]  # repo worktree root (…/claude-telegram-bot-streaming-engine)


# --- containment / leak helpers (descendant-scoped; bot tree never in scope) --

def _descendant_pids(root: int | None = None) -> set[int]:
    root = root if root is not None else os.getpid()
    seen: set[int] = set()
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        try:
            out = subprocess.run(
                ["pgrep", "-P", str(parent)],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            out = ""
        for token in out.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid not in seen:
                seen.add(pid)
                frontier.append(pid)
    return seen


def descendant_claude_pids() -> list[int]:
    """Descendant pids of THIS process whose command mentions 'claude'.

    Scoped to descendants of the current process, so it can NEVER catch the
    running Telegram bot's claude subprocesses (a different process tree) — only
    a CLI the SDK spawned under THIS spike is ever in scope. (Mirrors the P0
    leak-check in harness_sdk.py.)
    """
    result: list[int] = []
    for pid in _descendant_pids():
        try:
            cmd = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            cmd = ""
        if "claude" in cmd.lower():
            result.append(pid)
    return sorted(result)


def clean_project_transcript_dir(workdir: str, log) -> None:
    """Remove the ~/.claude/projects/<sanitized-cwd> dir the CLI created, if any.

    Only ever deletes a dir whose sanitized name embeds OUR disposable temp
    workdir basename (which carries the unique mkdtemp suffix), so a real
    project's transcripts can never be touched. (Mirrors P0 c3_ask.py.)
    """
    try:
        projects = Path.home() / ".claude" / "projects"
        if not projects.is_dir():
            return

        def _san(s: str) -> str:
            return s.replace("/", "-").replace(".", "-").replace("_", "-")

        token = _san(Path(workdir).name)
        removed = []
        for child in projects.iterdir():
            if child.is_dir() and token in child.name:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child.name)
        if removed:
            log(f"  cleaned CLI project transcript dir(s): {removed}")
    except Exception:
        pass


def safe_input_summary(tool_name: str, tool_input: Any) -> dict:
    """Summarize tool input WITHOUT dumping raw bodies (lengths, not content)."""
    if not isinstance(tool_input, dict):
        return {"_repr": str(tool_input)[:80]}
    out: dict = {}
    for k, v in tool_input.items():
        if k in ("content", "new_string", "old_string"):
            out[k] = f"<{len(str(v))} chars>"
        elif k in ("file_path", "path", "command", "pattern", "url"):
            out[k] = str(v)[:160]
        else:
            out[k] = str(v)[:60]
    return out


def git_porcelain() -> set[str]:
    """Set of `git status --porcelain` lines for the worktree (delta compare)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(WORKTREE), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return set(out.splitlines()) if out else set()
    except Exception:
        return {"<git status unavailable>"}
