"""Secret scrubber for the session-substrate spike.

This module is the single chokepoint that EVERY transcript write must route
through before any evidence artifact is persisted (see T3's evidence recorder,
which imports and calls ``scrub()``). It exists to satisfy cross-cutting
requirement SB3 (secret hygiene): the bot token is never logged and sensitive
tool output is kept out of logs / committed evidence.

``scrub(text, extra_secrets=None)`` redacts known secret shapes (API keys,
bearer tokens, ``Authorization:`` headers, Telegram bot tokens, and
``key=value`` style credential assignments) plus any caller-supplied literal
secret strings, replacing each with a single fixed marker (``REDACTION``).

Design properties:
  * Single redaction marker, exported as ``REDACTION``.
  * Idempotent: ``scrub(scrub(x)) == scrub(x)`` — the marker itself never
    matches any secret pattern, so re-scrubbing is a no-op.
  * Conservative: ordinary prose, code, file paths, and plain numbers are left
    intact; only credential-shaped substrings are touched.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional, Union

# Single fixed redaction marker. Chosen so it can never itself match any of the
# secret patterns below (no `sk-`/`Bearer`/`key=` shape, contains a space and
# brackets), which is what makes scrub() idempotent.
REDACTION = "[REDACTED]"


# Ordered list of (compiled regex, replacement) rules. Each replacement keeps
# any non-secret prefix (e.g. the header name or the `key=` part) via a capture
# group, and substitutes the secret value with REDACTION.
#
# Order matters: more specific shapes (Anthropic keys, telegram tokens) run
# before the generic `key=value` rule so the whole secret is consumed in one go.
_Replacement = Union[str, Callable[[re.Match[str]], str]]
_RULES: list[tuple[re.Pattern[str], _Replacement]] = [
    # Anthropic-style keys: sk-ant-... (and any sk-... family key). Matches a
    # long run of key-ish characters after the prefix. Listed first so the
    # `sk-ant-` form is fully consumed.
    (
        re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
        REDACTION,
    ),
    # Generic OpenAI/Anthropic-style sk- keys (sk-proj-..., sk-...).
    (
        re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
        REDACTION,
    ),
    # Telegram bot-token shape: <digits>:<35+ token chars> (SB3). Anchored on
    # word boundaries so ordinary "12:34" timestamps are not touched.
    (
        re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
        REDACTION,
    ),
    # Authorization: Bearer <token>  /  standalone "Bearer <token>".
    # Keep the literal word "Bearer"; redact only the token value.
    (
        re.compile(r"(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
        r"\1 " + REDACTION,
    ),
    # Generic "Authorization: <value>" header (any scheme/value). Runs after the
    # Bearer rule so a Bearer header is handled by the more specific rule above;
    # this catches Basic / token / opaque values. Keeps the header name. If the
    # value is already redacted (e.g. "Bearer [REDACTED]" from the rule above,
    # or a previously-scrubbed line), it is left as-is — that preserves the
    # scheme word and keeps scrub() idempotent.
    (
        re.compile(r"(Authorization:)\s*(\S.*)", re.IGNORECASE),
        lambda m: m.group(0) if REDACTION in m.group(2) else m.group(1) + " " + REDACTION,
    ),
    # key=value / key: value credential assignments. The key name is preserved;
    # the value (quoted or bare, up to whitespace) is redacted. Covers
    # api_key, apikey, token, secret, password/passwd/pwd, access_token,
    # ANTHROPIC_API_KEY, etc.
    (
        re.compile(
            r"\b([A-Za-z0-9_.-]*"
            r"(?:api[_-]?key|access[_-]?token|secret|token|password|passwd|pwd)"
            r"[A-Za-z0-9_.-]*)"
            r"(\s*[=:]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|\S+)",
            re.IGNORECASE,
        ),
        r"\1\2" + REDACTION,
    ),
]


def scrub(text: str, extra_secrets: Optional[Iterable[str]] = None) -> str:
    """Redact secrets from ``text``, returning a scrubbed copy.

    Args:
        text: The text to scrub. Any non-``str`` value is coerced via ``str()``.
        extra_secrets: Optional iterable of literal secret strings (e.g. the
            host's actual bot token or auth value) to redact by exact match.
            Empty / whitespace-only entries are skipped so they never cause the
            whole text to be redacted.

    Returns:
        ``text`` with every detected secret replaced by ``REDACTION``.
        Idempotent: scrubbing already-scrubbed text returns it unchanged.
    """
    if text is None:
        return text  # type: ignore[return-value]
    if not isinstance(text, str):
        text = str(text)

    result = text

    # 1) Literal caller-supplied secrets first, so an exact-known token is gone
    #    even if it doesn't match any generic shape. Longest-first avoids a
    #    short secret partially clobbering a longer overlapping one.
    if extra_secrets:
        literals = sorted(
            {s for s in extra_secrets if s and s.strip()},
            key=len,
            reverse=True,
        )
        for secret in literals:
            result = result.replace(secret, REDACTION)

    # 2) Pattern-based redaction.
    for pattern, replacement in _RULES:
        result = pattern.sub(replacement, result)

    return result


__all__ = ["scrub", "REDACTION"]
