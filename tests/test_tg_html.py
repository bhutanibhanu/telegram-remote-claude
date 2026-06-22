"""Unit tests for the CommonMark -> Telegram-HTML converter (``claude_tg.tg_html``).

Pure-function tests — no Telegram, no network. They assert the converter:

* renders the supported inline marks (bold/italic/strike/inline-code) and blocks
  (fenced code with + without a language, headers, links, bullets, blockquotes),
* HTML-escapes ``< > &`` in BOTH prose AND code (so a stray angle bracket from Claude
  can never start a tag or break the message),
* NEVER raises and stays safe on malformed input (unclosed ``**``, a lone ``<``),
* emits ONLY Telegram's supported tag set,
* round-trips a realistic multi-paragraph Claude reply.

The single load-bearing invariant every output must satisfy is in :func:`assert_safe`:
no raw ``<``/``>``/``&`` outside a supported tag, and no unsupported tag.
"""

from __future__ import annotations

import re

import pytest

from claude_tg.tg_html import strip_telegram_html, to_telegram_html

# The ONLY tags Telegram's HTML parse mode accepts.
_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}
_TAG = re.compile(r"<(/?)([a-zA-Z0-9]+)(\s[^>]*)?>")


def assert_safe(out: str) -> None:
    """The converter's hard guarantee: only supported tags, and no stray < > & .

    Strips every well-formed supported tag, then asserts what remains has no bare
    ``<``/``>`` and no bare ``&`` (every entity must be a proper ``&...;``). This is the
    property a Telegram ``BadRequest`` would punish, so it is the real test.
    """
    # Every tag present must be in the allowed set.
    for m in _TAG.finditer(out):
        assert m.group(2).lower() in _ALLOWED_TAGS, f"unsupported tag <{m.group(2)}> in {out!r}"
    # Remove all supported tags, then check the residue for raw markup chars.
    residue = _TAG.sub("", out)
    assert "<" not in residue and ">" not in residue, f"raw angle bracket in {out!r}"
    # Every & in the residue must begin a valid entity.
    for i, ch in enumerate(residue):
        if ch == "&":
            assert re.match(r"&(amp|lt|gt|quot|#\d+);", residue[i:]), f"bare & in {out!r}"


# --- inline marks -----------------------------------------------------------


def test_bold_double_star():
    out = to_telegram_html("a **bold** b")
    assert out == "a <b>bold</b> b"
    assert_safe(out)


def test_bold_double_underscore():
    assert to_telegram_html("a __bold__ b") == "a <b>bold</b> b"


def test_italic_single_star_and_underscore():
    assert to_telegram_html("a *it* b") == "a <i>it</i> b"
    assert to_telegram_html("an _it_ here") == "an <i>it</i> here"


def test_strikethrough():
    out = to_telegram_html("~~gone~~")
    assert out == "<s>gone</s>"
    assert_safe(out)


def test_bold_wins_over_italic_when_double_star():
    # **x** must become <b>x</b>, not <i>*x*</i> (ordering: bold before italic).
    out = to_telegram_html("**strong**")
    assert out == "<b>strong</b>"
    assert "<i>" not in out


def test_bold_inside_sentence_with_italic():
    out = to_telegram_html("This is **very** _important_ today")
    assert out == "This is <b>very</b> <i>important</i> today"
    assert_safe(out)


def test_italic_not_triggered_by_intraword_underscore():
    # snake_case must NOT become italic (flanking rule: _ hugged by word chars).
    out = to_telegram_html("call some_function_name now")
    assert "<i>" not in out
    assert "some_function_name" in out
    assert_safe(out)


def test_arithmetic_stars_not_italicized():
    # "2 * 3 * 4" has space-flanked stars -> NOT emphasis.
    out = to_telegram_html("compute 2 * 3 * 4 please")
    assert "<i>" not in out and "<b>" not in out
    assert_safe(out)


# --- inline code ------------------------------------------------------------


def test_inline_code():
    out = to_telegram_html("run `pytest -q` now")
    assert out == "run <code>pytest -q</code> now"
    assert_safe(out)


def test_inline_code_is_escaped_and_not_reinterpreted():
    # Markdown + angle brackets INSIDE inline code must be literal + escaped.
    out = to_telegram_html("use `a < b && **x**` here")
    assert "<code>a &lt; b &amp;&amp; **x**</code>" in out
    assert "<b>" not in out  # the ** inside code is NOT bold
    assert_safe(out)


# --- fenced code blocks -----------------------------------------------------


def test_fenced_code_no_language():
    out = to_telegram_html("```\nline1\nline2\n```")
    assert out == "<pre>line1\nline2</pre>"
    assert_safe(out)


def test_fenced_code_with_language():
    out = to_telegram_html("```python\nprint('hi')\n```")
    assert out == '<pre><code class="language-python">print(\'hi\')</code></pre>'
    assert_safe(out)


def test_fenced_code_escapes_html_and_markdown_inside():
    src = "```js\nif (a < b && c > d) { x = `**y**` }\n```"
    out = to_telegram_html(src)
    assert "&lt;" in out and "&gt;" in out and "&amp;&amp;" in out
    assert "<b>" not in out  # ** inside the fence stays literal
    assert "<pre><code class=\"language-js\">" in out
    assert_safe(out)


def test_fenced_code_with_prose_around_it():
    src = "Here is code:\n```\nx=1\n```\nAnd **after**."
    out = to_telegram_html(src)
    assert "Here is code:" in out
    assert "<pre>x=1</pre>" in out
    assert "<b>after</b>" in out
    assert_safe(out)


def test_unclosed_fence_stays_safe_as_pre():
    # A fenced block with no closing fence must NOT leak ``` or reinterpret its body.
    out = to_telegram_html("```python\nprint(1)\nmore text")
    assert "```" not in out
    assert "<pre>" in out
    assert_safe(out)


# --- headers / bullets / quotes / links -------------------------------------


@pytest.mark.parametrize("hashes", ["#", "##", "###", "####", "#####", "######"])
def test_headers_all_levels_become_bold(hashes):
    out = to_telegram_html(f"{hashes} Title here")
    assert out == "<b>Title here</b>"
    assert_safe(out)


def test_seven_hashes_is_not_a_header():
    # CommonMark: 7+ # is not an ATX heading. We leave it as escaped text (safe).
    out = to_telegram_html("####### too many")
    assert "<b>" not in out
    assert_safe(out)


def test_bullets_become_dot():
    out = to_telegram_html("- one\n- two\n* three\n+ four")
    assert out == "• one\n• two\n• three\n• four"
    assert_safe(out)


def test_link_basic():
    out = to_telegram_html("see [the docs](https://example.com/x)")
    assert out == 'see <a href="https://example.com/x">the docs</a>'
    assert_safe(out)


def test_link_with_ampersand_in_url_is_escaped():
    out = to_telegram_html("[q](https://e.com/s?a=1&b=2)")
    # The & in the body escape becomes &amp;; the href stays a valid attribute.
    assert '<a href="https://e.com/s?a=1&amp;b=2">q</a>' == out
    assert_safe(out)


def test_blockquote():
    out = to_telegram_html("> quoted line")
    assert out == "<blockquote>quoted line</blockquote>"
    assert_safe(out)


def test_multiline_blockquote_merges():
    out = to_telegram_html("> line one\n> line two")
    assert out == "<blockquote>line one\nline two</blockquote>"
    assert_safe(out)


# --- HTML escaping of plain prose -------------------------------------------


def test_plain_angle_brackets_and_amp_escaped():
    out = to_telegram_html("a < b & c > d")
    assert out == "a &lt; b &amp; c &gt; d"
    assert "<" not in out.replace("&lt;", "") and ">" not in out.replace("&gt;", "")
    assert_safe(out)


def test_html_injection_is_neutralized():
    # A Claude reply containing literal HTML must be escaped, not passed through.
    out = to_telegram_html("inject <script>alert(1)</script> here")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert_safe(out)


# --- malformed input never raises, stays safe -------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "**unclosed bold",
        "a lone < bracket",
        "trailing ` backtick",
        "[link without close](http://x",
        "~~unclosed strike",
        "```\nunterminated fence",
        "* ",  # a bullet marker with nothing after
        "###",  # hashes with no title
        "_",
        "****",
        "> ",
        "",
        "&",
        "<>&",
    ],
)
def test_malformed_never_raises_and_is_safe(bad):
    out = to_telegram_html(bad)  # must not raise
    assert isinstance(out, str)
    assert_safe(out)


def test_unclosed_bold_stays_literal():
    out = to_telegram_html("this is **not closed")
    assert "<b>" not in out
    assert "**not closed" in out
    assert_safe(out)


def test_empty_string():
    assert to_telegram_html("") == ""


# --- mixed / nested + realistic reply ---------------------------------------


def test_mixed_inline_marks_in_one_line():
    out = to_telegram_html("**bold** and *italic* and `code` and ~~strike~~ and [x](http://y)")
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert "<code>code</code>" in out
    assert "<s>strike</s>" in out
    assert '<a href="http://y">x</a>' in out
    assert_safe(out)


def test_bold_containing_inline_code():
    # Code is extracted first, so **`x`** -> <b><code>x</code></b>.
    out = to_telegram_html("**`code`**")
    assert out == "<b><code>code</code></b>"
    assert_safe(out)


def test_realistic_multi_paragraph_claude_reply():
    reply = (
        "## Summary\n"
        "\n"
        "Here is what I found. The **key issue** is in `render.py` — the relay sends "
        "message bodies with *no* `parse_mode`, so Telegram shows raw markdown.\n"
        "\n"
        "### Steps\n"
        "\n"
        "- Add a converter in `tg_html.py`\n"
        "- Wire it into the prose paths\n"
        "- Keep a plain-text fallback\n"
        "\n"
        "Example:\n"
        "\n"
        "```python\n"
        "def to_telegram_html(text: str) -> str:\n"
        "    return text  # a < b & c\n"
        "```\n"
        "\n"
        "See the [Telegram docs](https://core.telegram.org/bots/api#html-style) and note "
        "that `a < b` must be escaped.\n"
        "\n"
        "> This is a final quoted note."
    )
    out = to_telegram_html(reply)
    # Structure rendered.
    assert "<b>Summary</b>" in out
    assert "<b>Steps</b>" in out
    assert "<b>key issue</b>" in out
    assert "<i>no</i>" in out
    assert "<code>render.py</code>" in out
    assert "• Add a converter" in out
    assert '<pre><code class="language-python">' in out
    assert '<a href="https://core.telegram.org/bots/api#html-style">Telegram docs</a>' in out
    assert "<blockquote>This is a final quoted note.</blockquote>" in out
    # The angle brackets inside the code fence are escaped, NOT tags.
    assert "a &lt; b &amp; c" in out
    # The whole thing is a valid Telegram-HTML body.
    assert_safe(out)


# --- strip_telegram_html (the last-resort plain fallback) -------------------


def test_strip_telegram_html_drops_tags_and_unescapes():
    html_str = "a <b>bold</b> and <code>a &lt; b</code>"
    assert strip_telegram_html(html_str) == "a bold and a < b"


def test_strip_telegram_html_empty():
    assert strip_telegram_html("") == ""
