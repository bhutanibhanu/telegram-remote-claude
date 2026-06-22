"""Small helpers."""

from __future__ import annotations

TELEGRAM_MAX = 4096


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
