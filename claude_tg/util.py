"""Small helpers."""

from __future__ import annotations

TELEGRAM_MAX = 4096


def split_message(text: str, limit: int = TELEGRAM_MAX) -> list[str]:
    """Split ``text`` into Telegram-safe chunks (<= ``limit`` chars each).

    Lossless: ``"".join(split_message(t)) == t``. Prefers to break on a newline,
    then a space, falling back to a hard cut for unbroken runs.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if text is None:
        text = ""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks
