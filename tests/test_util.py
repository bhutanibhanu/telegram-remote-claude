import pytest

from claude_tg.util import _redact_sid, _redact_sid_in_text, split_message


def test_short_text_single_chunk():
    assert split_message("hello") == ["hello"]


def test_empty_text():
    assert split_message("") == [""]


def test_lossless_and_sized():
    text = "line of text " * 1000
    chunks = split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text
    assert len(chunks) > 1


def test_prefers_newline_boundary():
    text = "a" * 50 + "\n" + "b" * 50
    chunks = split_message(text, limit=60)
    assert chunks[0] == "a" * 50
    assert "".join(chunks) == text


def test_hard_cut_when_no_boundary():
    text = "x" * 250
    chunks = split_message(text, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert "".join(chunks) == text


def test_invalid_limit():
    with pytest.raises(ValueError):
        split_message("x", limit=0)


def test_utf16_aware_chunking():
    text = "😀" * 3000  # astral chars: 2 UTF-16 units each -> 6000 units total
    chunks = split_message(text, limit=4096)
    assert "".join(chunks) == text
    for c in chunks:
        assert len(c.encode("utf-16-le")) // 2 <= 4096  # Telegram's real limit
    assert len(chunks) >= 2


def test_astral_char_wider_than_tiny_limit_makes_progress():
    """A single astral-plane char (2 UTF-16 units) with a pathological limit < 2 must still
    emit the char (forced progress) rather than loop forever on empty chunks."""
    text = "😀😀😀"
    chunks = split_message(text, limit=1)  # each emoji is wider than the limit
    assert "".join(chunks) == text  # lossless
    assert all(c for c in chunks)  # no empty chunks
    assert len(chunks) == 3  # one emoji per chunk


# ---------------------------------------------------------------------------
# _redact_sid (P6/R3 / H1 / SB3): session ids must never appear raw in logs.
# A correlated short hash keeps logs debuggable without exposing the *resumable*
# id (the raw id is a credential — it re-attaches to a live Claude session).
# ---------------------------------------------------------------------------

# A representative Claude session id (the SDK/CLI emit UUID-shaped ids).
_SID = "8f14e45f-ceea-467d-9f0a-1234567890ab"


def test_redact_sid_omits_the_raw_id():
    out = _redact_sid(_SID)
    assert _SID not in out  # the whole id never appears
    # No long contiguous run of the raw id leaks either (defensive against a partial dump).
    assert "8f14e45f" not in out


def test_redact_sid_is_short_stable_and_correlated():
    a = _redact_sid(_SID)
    b = _redact_sid(_SID)
    assert a == b  # stable: the SAME id always maps to the SAME tag (correlatable in logs)
    assert a.startswith("sid:")  # recognizable prefix
    assert len(a) <= 16  # short — a tag, not a payload
    # Different ids → different tags (so two sessions don't collide in the log).
    assert _redact_sid("00000000-0000-0000-0000-000000000000") != a


def test_redact_sid_handles_none_and_empty_without_leaking():
    # No id yet (engine hadn't reported one) → a fixed sentinel, never "None"-as-a-secret.
    assert _redact_sid(None) == "sid:none"
    assert _redact_sid("") == "sid:none"


def test_redact_sid_in_text_scrubs_embedded_uuid_keeps_rest():
    # A raw error body that EMBEDS a session id (e.g. CLI echoing --resume <uuid>): the
    # scrubber replaces the id with its tag and leaves the surrounding text intact (useful).
    body = f"resume failed: no conversation found for --resume {_SID} (exit 1)"
    out = _redact_sid_in_text(body)
    assert _SID not in out  # the embedded resumable id is gone
    assert _redact_sid(_SID) in out  # replaced by its correlatable tag
    assert "no conversation found" in out  # the rest of the body is preserved
    assert "exit 1" in out


def test_redact_sid_in_text_passes_through_non_uuid_and_empty():
    assert _redact_sid_in_text("plain error, no ids here") == "plain error, no ids here"
    assert _redact_sid_in_text(None) == ""
    assert _redact_sid_in_text("") == ""



# ---- T5 (P9): macro expansion ($1..$9 positional, $* = all args) -------------

from claude_tg.util import expand_macro  # noqa: E402


def test_expand_macro_positional():
    assert expand_macro("deploy $1 to $2", ["app", "prod"]) == "deploy app to prod"


def test_expand_macro_star_is_all_args():
    assert expand_macro("run $*", ["a", "b", "c"]) == "run a b c"


def test_expand_macro_missing_positional_is_empty():
    # A $n past the supplied args expands to nothing (documented).
    assert expand_macro("x=$1 y=$2", ["only"]) == "x=only y="


def test_expand_macro_no_placeholders_unchanged():
    assert expand_macro("plain prompt with no vars", ["ignored"]) == "plain prompt with no vars"


def test_expand_macro_non_placeholder_dollars_verbatim():
    # $0, $a, a bare trailing $, and $$ are NOT placeholders — left as-is. ($1..$9 and $*
    # ARE placeholders regardless of surrounding text, so e.g. "$5.00" would treat $5 as a
    # positional — only non-1-9/non-* dollars are verbatim.)
    assert expand_macro("zero=$0 letter=$a end=$ double=$$", []) == "zero=$0 letter=$a end=$ double=$$"


def test_expand_macro_digit_placeholder_substitutes_anywhere():
    # A $5 is positional arg 5 even when glued to other text (documented).
    assert expand_macro("price $5 here", ["a", "b", "c", "d", "e"]) == "price e here"


def test_expand_macro_star_with_no_args_is_empty():
    assert expand_macro("go $*", []) == "go "
