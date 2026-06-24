"""Guards for HELP_TEXT Markdown safety (P9 fix).

`/help` is sent with `parse_mode="Markdown"`. A stray/odd `*` (e.g. a literal `$*` in
the macro help) leaves the bold markers UNBALANCED and Telegram REJECTS the whole send.
These tests pin that HELP_TEXT stays a valid Markdown payload so the send can't break.
"""

from __future__ import annotations

import re

from claude_tg.bot import HELP_TEXT


def _strip_code_spans(text: str) -> str:
    """Remove `` `...` `` code spans — a `*` inside a code span is literal, not a marker."""
    return re.sub(r"`[^`]*`", "", text)


def test_help_text_bold_markers_balanced() -> None:
    """Bold `*` markers (outside code spans) must be balanced — an odd count rejects the send.

    Regression: the `/run` help carried a literal `$*` which, as a bare `*`, made the total
    count odd and broke `/help`. It's now inside a code span (`$*`), so it doesn't count.
    """
    stars = _strip_code_spans(HELP_TEXT).count("*")
    assert stars % 2 == 0, (
        f"HELP_TEXT has an ODD number of bold `*` markers ({stars}) outside code spans — "
        "Telegram Markdown will reject the /help send. Wrap any literal `*` (e.g. $*) in "
        "backticks or remove it."
    )


def test_help_text_macro_star_placeholder_is_code_spanned() -> None:
    """The macro `$*` placeholder must live inside a code span so its `*` is inert."""
    assert "`$*`" in HELP_TEXT, "the $* macro placeholder must be wrapped in backticks"
    # And no BARE `$*` (a `$*` not immediately inside backticks) sneaks back in.
    assert not re.search(r"(?<!`)\$\*(?!`)", HELP_TEXT), "found a bare $* (would break Markdown)"
