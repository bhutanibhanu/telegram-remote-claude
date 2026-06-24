"""Small helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Optional

TELEGRAM_MAX = 4096

#: Length of the short hex tag appended after ``sid:`` (6 hex chars ≈ 24 bits — wide
#: enough that two live sessions almost never collide in a log, short enough to stay a
#: tag, not a payload).
_SID_TAG_LEN = 6


def _redact_sid(sid: Optional[str]) -> str:
    """Redact a Claude session id for LOGGING (P6/R3 · H1 · SB3).

    The raw ``claude_session_id`` is a **credential**: ``--resume <id>`` re-attaches to a
    live Claude session, so it must never appear verbatim in a log line (or anywhere an
    operator log could be shipped/pasted). This maps an id to a short, STABLE, one-way tag
    — ``sid:ab12cd`` (the first :data:`_SID_TAG_LEN` hex chars of its SHA-256) — so the
    same session is still *correlatable* across log lines (debuggable) while the resumable
    id stays out of the logs. The mapping is non-reversible (a truncated hash) and carries
    no part of the raw id.

    A missing id (``None`` / ``""`` — e.g. the engine has not reported a session yet) maps
    to the fixed sentinel ``sid:none`` so a log line never has to interpolate a raw value
    or leak a stray ``None``-shaped placeholder.
    """
    if not sid:
        return "sid:none"
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()
    return f"sid:{digest[:_SID_TAG_LEN]}"


#: A UUID-shaped token — Claude's session-id format (the SDK/CLI emit
#: ``8f14e45f-ceea-467d-9f0a-1234567890ab``). Used to find any session id embedded inside
#: a free-text error body so the scrubber can redact it before the body hits the local log.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _redact_sid_in_text(text: Optional[str]) -> str:
    """Scrub any embedded session id out of a free-text body before LOGGING it (SB3 · H1).

    The raw external error body written to the local debug log (``stream_session``) can
    EMBED a session id — e.g. a CLI error echoing ``--resume <uuid>``. :func:`_redact_sid`
    redacts a *whole* id; this finds every UUID-shaped token *inside* the text and replaces
    each with its :func:`_redact_sid` tag, so a resumable id never lands in the log even
    when wrapped in other text. Anything that is not UUID-shaped is left intact (the body
    is still useful for debugging). This is the minimum scrubber H1 requires; the bot token
    is never written to any log path, so it needs no scrub here.
    """
    if not text:
        return ""
    return _UUID_RE.sub(lambda m: _redact_sid(m.group(0)), text)


#: T5 (P9) macro placeholders: ``$1``..``$9`` positional, ``$*`` = all args. Matched
#: longest-first so ``$*`` wins over a would-be ``$`` and a multi-digit ``$12`` is read as
#: ``$1`` then literal ``2`` (single-digit positionals only — documented). A ``$`` not
#: followed by a digit or ``*`` is left verbatim.
_MACRO_PLACEHOLDER_RE = re.compile(r"\$([1-9]|\*)")


def expand_macro(body: str, args: list[str]) -> str:
    """Expand a macro ``body`` with positional ``$1``..``$9`` and ``$*`` (all args). Pure.

    Substitution rule (T5 / P9):

    * ``$1`` … ``$9`` → the 1-indexed positional arg, or the **empty string** when no such
      arg was given (a leftover ``$n`` past the supplied args expands to nothing — chosen
      over leaving it literal so a template never fires a stray ``$3`` at the model).
    * ``$*`` → all args joined by single spaces (the whole argument tail).
    * Any other ``$`` (``$0``, ``$a``, a trailing ``$``, ``$$``) is left **verbatim** — only
      ``$1``..``$9`` and ``$*`` are placeholders.

    Side-effect free + injection-neutral: the result is fired as an ordinary turn (the same
    path a plain message takes), so there is no shell/HTML context here — args are
    substituted as-is. ``args`` is the operator's whitespace-split argument list.
    """
    def _sub(m: "re.Match[str]") -> str:
        token = m.group(1)
        if token == "*":
            return " ".join(args)
        index = int(token) - 1  # $1 -> args[0]
        return args[index] if 0 <= index < len(args) else ""

    return _MACRO_PLACEHOLDER_RE.sub(_sub, body)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — what Telegram actually counts against 4096.

    Astral-plane characters (most emoji) are 2 units; everything else is 1.
    """
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def split_message(text: str, limit: int = TELEGRAM_MAX) -> list[str]:
    """Split ``text`` into Telegram-safe chunks (each <= ``limit`` UTF-16 units).

    Lossless: ``"".join(split_message(t)) == t``. Prefers to break on a newline,
    then a space, falling back to a hard cut for unbroken runs. Measures length in
    UTF-16 code units (Telegram's actual limit) so emoji-heavy text never overflows.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if text is None:
        text = ""
    if _utf16_len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while _utf16_len(remaining) > limit:
        # Largest char-prefix whose UTF-16 width fits in `limit`.
        width = 0
        idx = 0
        for ch in remaining:
            w = 2 if ord(ch) > 0xFFFF else 1
            if width + w > limit:
                break
            width += w
            idx += 1
        if idx == 0:
            # A single char wider than `limit` (an astral-plane emoji with a pathological
            # tiny limit): emit it anyway so we ALWAYS make progress — never an empty chunk
            # or an infinite loop.
            idx = 1
        window = remaining[:idx]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = idx
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks
