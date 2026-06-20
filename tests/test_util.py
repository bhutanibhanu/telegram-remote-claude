import pytest

from claude_tg.util import split_message


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
