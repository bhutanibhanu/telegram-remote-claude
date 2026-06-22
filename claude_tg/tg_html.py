"""CommonMark -> Telegram-HTML converter (render polish).

Claude / ``/grill`` emit **CommonMark** (``**bold**``, ``## headers``,
`` `inline code` ``, ```` ```fenced code``` ````, ``- bullets``, ``[links](url)``).
Telegram's ``sendMessage`` with ``parse_mode="HTML"`` understands only a **small,
fixed** tag set — https://core.telegram.org/bots/api#html-style:

    <b> <i> <u> <s> <code> <pre> <a href="…"> <blockquote>

…and nothing else (any other tag, or a raw unescaped ``<`` / ``>`` / ``&`` outside a
tag, makes Telegram reject the whole message with a ``BadRequest``). This module turns
a Markdown string into a string using **only** those tags, escaping everything else.

**Why the placeholder method.** Code spans must be shown verbatim — a ``**`` *inside*
a code block is literal text, not bold, and a ``<`` inside code must be ``&lt;`` not the
start of a tag. So we:

1. **Extract fenced code blocks** ```` ```…``` ```` first, replacing each with an opaque
   sentinel that survives HTML-escaping and the inline pass. Their content is
   HTML-escaped and wrapped in ``<pre>`` (``<pre><code class="language-x">`` when a
   language is given).
2. **Extract inline code** `` `…` `` -> sentinel; content HTML-escaped, wrapped in
   ``<code>``.
3. **HTML-escape** ``&`` / ``<`` / ``>`` in the remaining text (so any stray Markdown
   angle-bracket from Claude is inert, SB-safe).
4. **Apply inline conversions** on the escaped text — order matters so ``**`` is not
   eaten by ``*``: links, then bold (``**``/``__``), then italic (``*``/``_``), then
   strikethrough (``~~``), then line-leading headers / blockquotes / bullets.
5. **Restore** the code sentinels (now-built ``<pre>`` / ``<code>`` HTML).

The function is **pure** and **never raises** on malformed input: an unclosed ``**``
just stays literal, a lone ``<`` becomes ``&lt;``. Worst case it under-formats; it
never emits an unsupported tag or breaks the message. (The send path additionally
falls back to plain text on a Telegram ``BadRequest`` — defense in depth.)
"""

from __future__ import annotations

import html
import re

__all__ = ["to_telegram_html", "strip_telegram_html"]

# Matches any HTML tag we emit (``<b>``, ``</pre>``, ``<a href="…">``, …) for the
# last-resort plain-text fallback when no parallel raw chunk was carried.
_TAG_RE = re.compile(r"<[^>]+>")

# Sentinels for the extracted code spans. They MUST survive html.escape (so they carry
# no & < >) and must not look like any Markdown the inline pass would touch (no * _ ~ [ `
# # > -). The NUL bytes make an accidental literal collision from real Claude prose
# essentially impossible, and NUL is not valid in a Telegram message anyway.
_BLOCK_SENTINEL = "\x00B{}\x00"
_INLINE_SENTINEL = "\x00I{}\x00"

# A fenced code block: ``` or ~~~ fence, optional info string (language) on the open
# line, body, closing fence. DOTALL so the body spans lines; non-greedy body. The fence
# char class is matched as a group so the close must use the same char.
_FENCE_RE = re.compile(
    r"(?P<fence>```+|~~~+)[ \t]*(?P<lang>[^\n`~]*)\n(?P<body>.*?)(?:\n)?(?P=fence)",
    re.DOTALL,
)
# An UNCLOSED fence (open fence + info line but no closing fence to end of string). We
# still render it as a <pre> block rather than leaking literal ``` and reinterpreting
# its body as Markdown — malformed input stays safe.
_FENCE_OPEN_RE = re.compile(
    r"(?P<fence>```+|~~~+)[ \t]*(?P<lang>[^\n`~]*)\n(?P<body>.*)\Z",
    re.DOTALL,
)
# Inline code: one or more backticks, then the shortest run not containing that many
# backticks, then the same count of backticks (CommonMark's rule). We handle the common
# single/double-backtick cases; an unmatched backtick is left literal.
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?P<code>.+?)(?P=ticks)", re.DOTALL)

# A Markdown link [text](url). text has no unescaped ] ; url has no whitespace or ).
# Applied to ALREADY-escaped text, so `text`/`url` may contain &amp; etc. — fine.
_LINK_RE = re.compile(r"\[(?P<text>[^\]]*?)\]\((?P<url>[^()\s]+)\)")

# Emphasis. ** / __ before * / _ so a double marker is consumed as bold, not two
# italics. Markers must hug non-space content (CommonMark left/right flanking, simplified)
# so ``a * b * c`` and ``2 * 3 * 4`` are NOT turned into italics. Non-greedy inner.
_BOLD_STAR_RE = re.compile(r"\*\*(?P<x>\S(?:.*?\S)?)\*\*", re.DOTALL)
_BOLD_USCORE_RE = re.compile(r"__(?P<x>\S(?:.*?\S)?)__", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?P<x>\S(?:.*?\S)?)\*(?![\w*])", re.DOTALL)
_ITALIC_USCORE_RE = re.compile(r"(?<![\w_])_(?P<x>\S(?:.*?\S)?)_(?![\w_])", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(?P<x>\S(?:.*?\S)?)~~", re.DOTALL)

# Line-leading constructs (applied per-line on the ALREADY-escaped text, so the
# blockquote ``>`` marker appears as the escaped entity ``&gt;`` — match that). Headers
# (``#``) and bullets (``-``/``*``/``+``) are not touched by html.escape, so they match
# their literal marker.
_HEADER_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(?P<text>.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(?P<indent>\s*)[-*+]\s+(?P<rest>.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}&gt;\s?(?P<rest>.*)$")


def _convert_fence(match: re.Match[str]) -> str:
    """Render one fenced block as a Telegram <pre> (with a language class if present)."""
    lang = match.group("lang").strip()
    body = match.group("body")
    escaped = html.escape(body, quote=False)
    if lang:
        # Telegram supports <pre><code class="language-xxx">…</code></pre> for fenced
        # blocks with a language. The class value is attribute-escaped.
        cls = html.escape(lang, quote=True)
        return f'<pre><code class="language-{cls}">{escaped}</code></pre>'
    return f"<pre>{escaped}</pre>"


def _extract_code(text: str) -> tuple[str, list[str]]:
    """Replace fenced + inline code with sentinels; return (text, restored_html_list).

    Fenced blocks are extracted FIRST (so a ``` inside is not mistaken for inline code),
    then inline spans. Each replacement records the already-built, escaped HTML so the
    restore step is a literal swap (the inline pass never sees code content).
    """
    restored: list[str] = []

    def take_block(match: re.Match[str]) -> str:
        restored.append(_convert_fence(match))
        return _BLOCK_SENTINEL.format(len(restored) - 1)

    # Closed fenced blocks first.
    text = _FENCE_RE.sub(take_block, text)
    # A trailing UNCLOSED fence (best-effort: still a <pre>, body not reinterpreted).
    text = _FENCE_OPEN_RE.sub(take_block, text)

    def take_inline(match: re.Match[str]) -> str:
        code = match.group("code")
        # Strip ONE optional surrounding space (CommonMark trims a single padding space
        # so `` ` `` works); keep inner spaces. Best-effort, never raises.
        if len(code) >= 2 and code[0] == " " and code[-1] == " " and code.strip():
            code = code[1:-1]
        restored.append(f"<code>{html.escape(code, quote=False)}</code>")
        return _INLINE_SENTINEL.format(len(restored) - 1)

    text = _INLINE_CODE_RE.sub(take_inline, text)
    return text, restored


def _apply_links(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        label = match.group("text")
        url = match.group("url")
        # The text was already HTML-escaped; the url needs its " and & escaped for the
        # attribute. (& was already turned into &amp; by the earlier body escape; escaping
        # again would double it, so only quote the bare " here.)
        safe_url = url.replace('"', "&quot;")
        return f'<a href="{safe_url}">{label}</a>'

    return _LINK_RE.sub(repl, text)


def _apply_line_leading(text: str) -> str:
    """Per-line: headers -> <b>, blockquotes -> <blockquote>, bullets -> '• '."""
    out_lines: list[str] = []
    quote_buf: list[str] = []

    def flush_quote() -> None:
        if quote_buf:
            out_lines.append("<blockquote>" + "\n".join(quote_buf) + "</blockquote>")
            quote_buf.clear()

    for line in text.split("\n"):
        qm = _QUOTE_RE.match(line)
        if qm is not None:
            quote_buf.append(qm.group("rest"))
            continue
        flush_quote()
        hm = _HEADER_RE.match(line)
        if hm is not None and hm.group("text"):
            out_lines.append(f"<b>{hm.group('text')}</b>")
            continue
        bm = _BULLET_RE.match(line)
        if bm is not None:
            out_lines.append(f"{bm.group('indent')}• {bm.group('rest')}")
            continue
        out_lines.append(line)
    flush_quote()
    return "\n".join(out_lines)


def _apply_inline(text: str) -> str:
    """Emphasis/strike on already-escaped text. Order: bold before italic so ``**`` wins."""
    text = _BOLD_STAR_RE.sub(r"<b>\g<x></b>", text)
    text = _BOLD_USCORE_RE.sub(r"<b>\g<x></b>", text)
    text = _ITALIC_STAR_RE.sub(r"<i>\g<x></i>", text)
    text = _ITALIC_USCORE_RE.sub(r"<i>\g<x></i>", text)
    text = _STRIKE_RE.sub(r"<s>\g<x></s>", text)
    return text


def to_telegram_html(text: str) -> str:
    """Convert a CommonMark ``text`` to a Telegram-``parse_mode="HTML"`` safe string.

    Uses only Telegram's supported tags (``<b> <i> <u> <s> <code> <pre> <a> <blockquote>``)
    and HTML-escapes everything else. **Pure** and **never raises**: malformed Markdown
    (an unclosed ``**``, a lone ``<``) degrades to safe literal text rather than an
    exception or an invalid message. See the module docstring for the placeholder
    method and ordering rationale.
    """
    if not text:
        return ""
    try:
        # 1+2. Pull code out so its contents are never reinterpreted.
        working, restored = _extract_code(text)
        # 3. Escape the remaining prose (sentinels carry no & < > so they survive).
        working = html.escape(working, quote=False)
        # 4. Inline + line-leading conversions on the escaped prose.
        working = _apply_links(working)
        working = _apply_inline(working)
        working = _apply_line_leading(working)
        # 5. Restore the code HTML.
        for i, frag in enumerate(restored):
            working = working.replace(_BLOCK_SENTINEL.format(i), frag)
            working = working.replace(_INLINE_SENTINEL.format(i), frag)
        return working
    except Exception:
        # Absolute backstop (RB1 spirit): never let a converter bug break a turn. A
        # fully-escaped plain string is always a valid HTML message body.
        return html.escape(text, quote=False)


def strip_telegram_html(text: str) -> str:
    """Best-effort plain text from a Telegram-HTML string (last-resort send fallback).

    Used by T7 ONLY when an HTML chunk has no parallel raw chunk to fall back to: it
    drops the tags and unescapes the entities so the operator still sees readable text
    (never a dropped message). Pure; never raises. Note ``plain_chunks`` is the preferred
    fallback (it is the exact original markdown); this is the safety net beneath it.
    """
    if not text:
        return ""
    try:
        return html.unescape(_TAG_RE.sub("", text))
    except Exception:
        return text
