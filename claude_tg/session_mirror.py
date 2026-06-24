"""Live-mirror a session's transcript onto Telegram, read-only (P11 T3 — ``/watch``).

The bot can already DRIVE any discovered session (``/attach``, T2). This module is the
other half of "the entirety of the process becomes remote": **follow** any Claude Code
session's transcript live on the phone WITHOUT driving it — a read-only tail of the
append-only ``~/.claude/projects/<sanitized-cwd>/<id>.jsonl`` that maps each transcript
line to the bot's EXISTING render layer and sends it through the EXISTING per-chat send
gate. The orchestrator you are watching keeps writing; the mirror never writes back.

Three pieces, all dependency-injected so the whole thing is unit-testable with a fake
transcript file + a fake clock (no real SDK, no real ``~/.claude``, no real sleeping):

1. **The tailer** (:class:`TranscriptTailer`). Tails an append-only file by
   ``(byte-offset, mtime)`` and yields each COMPLETE line. The critical correctness rule
   (the spike's append-only-tail fact): a reader can catch a half-written final line, so
   we **consume only up to the last ``\\n``** — a trailing partial is BUFFERED until its
   newline arrives on a later read. **RB1-total:** a missing / rotated / truncated /
   non-JSON line, or a vanished file, NEVER crashes the mirror — it skips the bad line or
   stops cleanly.

2. **The dict→Event normalizer** (:func:`normalize_line`). Maps a transcript line ``type``
   to one of the bot's EXISTING :mod:`claude_tg.engine.types` events so the SAME
   :func:`~claude_tg.render.render_event` that renders the bot's OWN tool activity renders
   the mirror — no parallel renderer. Assistant text → :class:`TextEvent`; ``tool_use`` →
   a :class:`ToolUseEvent` carrying the body-free :func:`~claude_tg.engine.types.safe_input_summary`
   (exactly like the bot's own tool line); ``tool_result`` → a body-free INDICATOR
   (``✓ result (N chars)`` / an error flag) carried as a mirror-authored :class:`TextEvent`
   — **never the result content**; user/human turns → the operator's own prompt text (they
   are entitled to see what they typed); unknown types → skipped (``None``).

3. **⭐ SB3 (the security core).** A raw transcript line carries FULL tool bodies — file
   contents, command output, possibly secrets. :func:`normalize_line` is THE body-free
   scrub: it passes ``tool_use.input`` through :func:`safe_input_summary` (lengths, not
   bodies) and reduces ``tool_result.content`` to a CHARACTER COUNT + an ok/error flag.
   **Raw ``tool_result`` content / a raw large ``tool_use.input`` must NEVER reach the
   returned event** (and so never reach Telegram). This mirrors the P6 render discipline
   exactly — the mirror is just another producer of the same body-free events.

The asyncio ``/watch`` lifecycle (one watch per chat, the bounded queue/coalescing, the
send-gate flood control, shutdown-cancel) lives in :class:`MirrorWatch` and is driven by
:class:`~claude_tg.stream_session.StreamingSession`. This module stays pure + transport-
free below that: the tailer + normalizer have no Telegram, no engine, no SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .engine.types import Event, TextEvent, ToolUseEvent, safe_input_summary
from .render import RenderAction, render_event

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transcript path (matches the SDK / CLI sanitization; symlink-safe within ~/.claude)
# ---------------------------------------------------------------------------
#
# We reuse the discovery module's on-disk convention: a session's transcript lives at
# <home>/projects/<sanitized-cwd>/<id>.jsonl where <sanitized-cwd> replaces every
# non-alphanumeric char with '-'. We re-derive it here (a stable on-disk convention) so the
# mirror's only SDK coupling stays whatever discovery already owns.

#: Same rule the CLI/SDK use to turn a cwd into a project-dir name (mirrors
#: ``sessions_discovery._SANITIZE_RE``): every non-alphanumeric char → ``-``.
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")
#: The SDK truncates an over-long sanitized name; bot/attach cwds are short enough that we
#: never hit it, and a miss here is a clean "no transcript" (the watch reports it), so we
#: keep the simple form (mirrors discovery).
_MAX_SANITIZED_LENGTH = 200


def claude_home() -> Path:
    """The Claude config dir (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``), like the SDK/discovery.

    Pure path construction; never touches the filesystem.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def transcript_path(
    session_id: str, cwd: Optional[str], *, home: Optional[Path] = None
) -> Optional[Path]:
    """Resolve ``session_id``'s transcript path under ``cwd``, confined to ``~/.claude`` (SB2-ish).

    The transcript lives at ``<home>/projects/<sanitized-cwd>/<session-id>.jsonl`` (the CLI
    convention; ``<sanitized-cwd>`` replaces every non-alphanumeric char with ``-``). Returns
    that path, or ``None`` for a missing id/cwd.

    **Symlink containment (the design's read-only SB note).** The mirror is read-only, but we
    still refuse to FOLLOW the transcript out of the projects tree: the path is resolved
    (``..``, symlinks) and must stay under ``<home>/projects`` after resolution. A session
    file that is a symlink pointing outside ``~/.claude/projects`` → ``None`` (we don't tail
    it). The projects ROOT itself is resolved first so a symlinked ``~/.claude`` (a legit
    setup) still matches. Never raises (RB1) — any resolution hiccup degrades to ``None``.
    """
    if not session_id or not cwd:
        return None
    base = home or claude_home()
    projects_root = base / "projects"
    sanitized = _SANITIZE_RE.sub("-", cwd)[:_MAX_SANITIZED_LENGTH]
    path = projects_root / sanitized / f"{session_id}.jsonl"
    try:
        # Resolve the root WITHOUT requiring the file to exist yet (a session that has not
        # written its first line, or a not-yet-created transcript, is still a valid target —
        # the tailer simply sees no bytes until it appears). ``strict=False`` resolves as far
        # as the path exists and leaves the tail literal.
        root_resolved = projects_root.resolve()
        path_resolved = path.resolve()
    except OSError:
        return None
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError:
        # The (resolved) transcript escapes the projects tree — refuse to follow it.
        log.warning("refusing to mirror a transcript outside ~/.claude/projects")
        return None
    return path_resolved


# ---------------------------------------------------------------------------
# 1. The tailer — append-only, offset+mtime, consume only up to the last '\n'
# ---------------------------------------------------------------------------


@dataclass
class TranscriptTailer:
    """Tail an append-only transcript file, yielding each COMPLETE line (RB1-total).

    Construct with the resolved transcript ``path`` (from :func:`transcript_path`). Each
    call to :meth:`poll` reads any bytes appended since the last poll and returns the
    complete lines among them; a trailing partial line (no ``\\n`` yet — a reader catching a
    half-written final line) is BUFFERED on :attr:`_partial` and emitted only once its
    newline arrives on a later poll. This is the spike's append-only-tail discipline: never
    surface a half-written line.

    **Offset + mtime.** We track the byte offset consumed so far and the file's last-seen
    size. On each poll we stat the file: if it has SHRUNK below our offset (truncated /
    rotated / replaced — a new, shorter file at the same path) we RESET to the start and
    re-read (RB1 — a rotation never wedges the tail). Otherwise we open + seek to the offset
    and read the new tail. ``mtime`` is tracked too so a caller could skip a stat-only poll,
    but :meth:`poll` is cheap and self-contained.

    **RB1-total.** A missing/vanished file → :meth:`poll` returns ``[]`` and sets
    :attr:`gone` (the watch stops cleanly). A non-JSON or otherwise odd line is the
    normalizer's problem (it returns ``None``); the tailer only deals in raw text lines and
    never parses, so it cannot crash on content. Any unexpected OSError mid-read degrades to
    "no new lines this poll" (we keep the offset; the next poll retries).

    **Testability.** The ``opener`` seam (default :func:`open`) lets a test inject a fake
    file; the tailer does NO sleeping and holds NO loop — the caller (the watch task) owns
    the poll cadence + the clock. So a test feeds bytes by writing the fake file and calling
    :meth:`poll` deterministically.
    """

    path: Path
    #: Injectable open() so a test can feed bytes without a real file. Signature mirrors the
    #: builtin: ``opener(path, mode, encoding=...) -> file``. Binary is used internally for a
    #: correct byte offset; we decode utf-8 with ``errors="replace"`` so an undecodable byte
    #: can never crash the tail (RB1) — the line still routes (the normalizer JSON-parses it).
    opener: Callable[..., Any] = open
    #: Bytes consumed so far (the seek offset for the next read).
    _offset: int = 0
    #: The last-seen file size (to detect truncation/rotation: size < offset → reset).
    _size: int = 0
    #: The last-seen mtime (epoch seconds), informational.
    _mtime: float = 0.0
    #: A trailing partial line (bytes after the last ``\\n``) buffered until its newline lands.
    _partial: str = ""
    #: Set True once the file has VANISHED after having existed (or never appeared and the
    #: caller chooses to stop). The watch reads this to stop cleanly.
    gone: bool = False

    def poll(self) -> list[str]:
        """Read newly-appended bytes and return the COMPLETE lines among them (RB1-total).

        Returns the list of complete line strings (without the trailing ``\\n``) appended
        since the previous poll, in order. A trailing partial (no newline yet) is buffered,
        not returned. ``[]`` means "nothing complete yet" OR "the file is gone" — check
        :attr:`gone` to distinguish. Never raises.
        """
        try:
            st = self.path.stat()
        except (FileNotFoundError, NotADirectoryError):
            # The transcript vanished (deleted / its dir removed). Stop cleanly — anything we
            # had buffered is dropped (a half-written line we'll never see completed).
            self.gone = True
            return []
        except OSError:
            # A transient stat error (permissions/I.O.) — treat as "no new lines this poll";
            # keep state and retry next poll (RB1: never crash, never falsely declare gone).
            log.debug("transcript stat failed during mirror poll", exc_info=True)
            return []

        size = st.st_size
        self._mtime = getattr(st, "st_mtime", self._mtime)

        # Truncation / rotation: the file is now SHORTER than what we've already consumed, so
        # it was replaced/truncated. Reset to the start and re-read from scratch (RB1) — a
        # buffered partial from the OLD file is meaningless now, so drop it.
        if size < self._offset:
            self._offset = 0
            self._partial = ""

        if size <= self._offset:
            # No new bytes appended (and not a shrink we just reset). Nothing to do.
            self._size = size
            return []

        try:
            with self.opener(self.path, "rb") as fh:
                try:
                    fh.seek(self._offset)
                except (OSError, ValueError):
                    # A non-seekable / odd handle — read from the top defensively.
                    fh.seek(0)
                    self._offset = 0
                    self._partial = ""
                chunk = fh.read()
        except (FileNotFoundError, NotADirectoryError):
            # Raced with a delete between stat and open — stop cleanly.
            self.gone = True
            return []
        except OSError:
            log.debug("transcript read failed during mirror poll", exc_info=True)
            return []

        if not isinstance(chunk, (bytes, bytearray)):
            # An injected opener handed back text — be forgiving and encode for offset math.
            chunk = str(chunk).encode("utf-8", errors="replace")

        # Advance the byte offset by the bytes we actually read; decode for line splitting.
        self._offset += len(chunk)
        self._size = size
        text = self._partial + chunk.decode("utf-8", errors="replace")

        # Consume ONLY up to the last newline (the append-only-tail rule): everything after
        # the final ``\\n`` is an incomplete trailing line — buffer it for the next poll.
        last_nl = text.rfind("\n")
        if last_nl == -1:
            # No complete line yet — the whole thing is a growing partial.
            self._partial = text
            return []
        complete = text[: last_nl + 1]
        self._partial = text[last_nl + 1 :]
        # splitlines() drops the trailing empty from the final ``\\n``; skip blank lines.
        return [line for line in complete.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 2. The dict→Event normalizer  +  ⭐ 3. the SB3 body-free scrub
# ---------------------------------------------------------------------------
#
# A Claude Code transcript line is a JSON object. The shapes we map (only the fields we
# need; everything else is ignored — RB1):
#
#   assistant text:   {"type":"assistant","message":{"content":[{"type":"text","text":...},
#                                                                {"type":"tool_use","name":...,
#                                                                 "input":{...},"id":...}, ...]}}
#   user / tool result:{"type":"user","message":{"content":[{"type":"tool_result",
#                                                             "content":<str|blocks>,
#                                                             "is_error":bool}, ...]}}
#   plain user turn:   {"type":"user","message":{"content":"the operator's prompt"}}
#
# A single transcript line can carry SEVERAL content blocks (text + tool_use). The normalizer
# therefore returns a LIST of events (one per renderable block); unknown blocks/types yield an
# empty list. This keeps the SB3 scrub block-by-block: each block is reduced to a body-free
# event BEFORE it can become a send.

#: Max chars of a tool_result we ever describe (we describe its LENGTH, never its bytes).
#: Body-free: only a count + an ok/error flag is surfaced (SB3).
_RESULT_OK_GLYPH = "✓"
_RESULT_ERR_GLYPH = "⚠️"


def _content_length(content: object) -> int:
    """A character count for a ``tool_result.content`` (str OR a list of block dicts) — SB3.

    The result content is either a plain string or a list of ``{"type":"text","text":...}``
    (and/or image) blocks. We sum the lengths of the text we'd otherwise show — but we NEVER
    return or retain the text itself, only its size. Defensive (RB1): an odd shape degrades to
    ``len(str(content))`` (still a count, never the body re-surfaced as content).
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    total += len(text)
                else:
                    # A non-text block (image, tool_result-of-tool_result, …): count its
                    # serialized size as a body-free proxy — never its content.
                    total += len(str(block.get("content", block)))
            else:
                total += len(str(block))
        return total
    return len(str(content))


def _result_indicator(is_error: bool, length: int) -> str:
    """The body-free one-line indicator for a tool_result (``✓ result (N chars)`` / error).

    Mirror-authored scaffolding — a fixed glyph + the word ``result`` + a CHARACTER COUNT and
    an ok/error flag. Contains NOTHING from the raw result body (SB3). This is what the mirror
    shows in place of the (suppressed) tool output.
    """
    glyph = _RESULT_ERR_GLYPH if is_error else _RESULT_OK_GLYPH
    label = "result error" if is_error else "result"
    return f"{glyph} {label} ({length} chars)"


def _coerce_input(value: object) -> Optional[dict[str, Any]]:
    """A tool_use ``input`` as a dict for :func:`safe_input_summary`, or ``None`` (defensive)."""
    if isinstance(value, dict):
        return value
    return None


def normalize_line(line: object) -> list[Event]:
    """Map ONE transcript line (a JSON dict, or a raw JSON string) to body-free Events (SB3).

    Returns a list of :mod:`claude_tg.engine.types` events — the SAME types the bot's own
    engine emits — so the caller renders them through the EXISTING
    :func:`~claude_tg.render.render_event` (no parallel renderer). An unknown / unparseable /
    bodyless line yields ``[]`` (RB1 — skip it, never crash). A single line can carry several
    content blocks (e.g. assistant text + a tool_use), hence a list.

    Accepts either an already-parsed ``dict`` or a raw ``str`` (the tailer's line) — a string
    is JSON-parsed here, and a parse failure yields ``[]`` (RB1). This is the single seam the
    tests mutation-probe for SB3.

    **⭐ SB3 — the body-free scrub (make-or-break):**

    * ``assistant`` text blocks → :class:`TextEvent` with the assistant's prose (Claude's
      own words, the operator is watching them — rendered as HTML by ``render_event`` like
      any assistant text).
    * ``tool_use`` blocks → :class:`ToolUseEvent` whose ``tool_input_summary`` is
      :func:`safe_input_summary(name, input)` — **lengths, not bodies**: a ``Write`` shows
      ``Write(file_path=…, content=<N chars>)``, never the N characters. The raw ``input``
      dict is consumed HERE and never placed on the event.
    * ``tool_result`` blocks → a body-free :class:`TextEvent` carrying ONLY
      :func:`_result_indicator` (``✓ result (N chars)`` / an error flag). **The raw result
      content (file dump, command stdout, a secret) is reduced to a character count and
      DROPPED** — it never rides the returned event, so it can never reach Telegram.
    * a plain ``user`` turn (string content) → :class:`TextEvent` of the operator's own
      prompt (they typed it; they may see it).
    * anything else (``system`` lines, summaries, unknown block types) → ignored.
    """
    data = line
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return []  # RB1: a non-JSON / truncated line is skipped, never fatal.
    if not isinstance(data, dict):
        return []

    line_type = data.get("type")
    message = data.get("message")

    # ``assistant`` lines carry a content list of text / tool_use blocks.
    if line_type == "assistant":
        return _normalize_assistant(message)

    # ``user`` lines carry EITHER a plain string prompt OR a content list that may include
    # tool_result blocks (the result of a tool the assistant just ran).
    if line_type == "user":
        return _normalize_user(message)

    # ``system`` / summary / unknown line types carry no operator-facing renderable content
    # (or carry only metadata) — ignore (RB1). We deliberately do NOT surface system lines:
    # they can echo tool plumbing, and the design scopes the mirror to text + tool activity.
    return []


def _normalize_assistant(message: object) -> list[Event]:
    """Body-free events for an ``assistant`` line's content blocks (text + tool_use)."""
    blocks = _content_blocks(message)
    out: list[Event] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                out.append(TextEvent(text=text, incremental=False))
        elif btype == "tool_use":
            name = block.get("name")
            tool_name = str(name) if name else "tool"
            # ⭐ SB3: the raw input is reduced to a lengths-not-bodies summary HERE; the raw
            # dict never reaches the event (and so never Telegram).
            summary = safe_input_summary(tool_name, _coerce_input(block.get("input")))
            out.append(
                ToolUseEvent(
                    tool_name=tool_name,
                    tool_input_summary=summary,
                    tool_use_id=_opt_str(block.get("id")),
                )
            )
        # Other assistant block types (thinking, etc.) are ignored (RB1 / scope).
    return out


def _normalize_user(message: object) -> list[Event]:
    """Body-free events for a ``user`` line — a plain prompt OR tool_result block(s)."""
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    # A plain operator prompt is a bare string — the operator typed it, so show it.
    if isinstance(content, str):
        return [TextEvent(text=content, incremental=False)] if content.strip() else []
    if not isinstance(content, list):
        return []
    out: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            # ⭐ SB3: reduce the result to a body-free indicator (count + ok/error flag). The
            # raw content (file contents, command output, a secret) is NEVER placed on the
            # event — only its length is measured, then dropped.
            is_error = bool(block.get("is_error"))
            length = _content_length(block.get("content"))
            out.append(
                TextEvent(text=_result_indicator(is_error, length), incremental=False)
            )
        # A user line can also echo plain text blocks (rare) — surface them as prompt text.
        elif block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                out.append(TextEvent(text=text, incremental=False))
    return out


def _content_blocks(message: object) -> list:
    """The ``message.content`` list (or ``[]``) — defensive against odd shapes (RB1)."""
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return content
    return []


def _opt_str(value: object) -> Optional[str]:
    """``str(value)`` for a non-empty value, else ``None`` (defensive)."""
    if value is None:
        return None
    text = str(value)
    return text if text else None


# ---------------------------------------------------------------------------
# A small pure helper the watch loop uses to bound a fast-writing session
# ---------------------------------------------------------------------------


def iter_normalized(lines: Iterator[str]) -> Iterator[Event]:
    """Flatten a stream of raw transcript lines into body-free events (convenience).

    Pure generator over :func:`normalize_line` — used by the watch loop (and the tests) to
    turn a batch of tailer lines into the render-ready events in order. Each line may expand
    to 0..N events; unknown lines contribute none (RB1).
    """
    for line in lines:
        for event in normalize_line(line):
            yield event


# ---------------------------------------------------------------------------
# Tuning knobs for the watch loop (the asyncio lifecycle lives in stream_session)
# ---------------------------------------------------------------------------

#: Default poll interval (seconds) for the watch tail loop. The spike measured ~0.1–0.3 s
#: flush lag as fine; 0.25 s keeps the mirror feeling live without busy-spinning. Injectable
#: per watch so tests drive the loop with a recorder sleep (no real time).
DEFAULT_POLL_INTERVAL = 0.25

#: Max events surfaced from ONE poll before flood control sheds the rest. A fast-writing
#: session can emit a burst far faster than the ~1 msg/s/chat send gate drains; rather than
#: enqueue an unbounded burst behind the gate (which would lag the mirror minutes behind and
#: balloon memory), a single poll that yields more than this many events SHEDS the tool-line
#: NOISE first (keeping the assistant text + result indicators) and appends a coalesced
#: "(… N events skipped)" marker so the operator knows the tail went lossy under load. The
#: send gate is still the hard rate limiter; this just bounds what we hand it per poll.
DEFAULT_WATCH_QUEUE_MAX = 40


#: One unit of mirror output: the text, its Telegram parse mode, and whether it is the
#: PRIORITY (verbatim) kind in the send gate. Assistant/operator/result text is verbatim
#: (it must reach the operator); a tool-use line is non-verbatim NOISE that yields to it and
#: is shed first under flood control. ``render_event`` decides the body + parse mode; this is
#: the small shape the watch loop sends through the (injected) gate.
@dataclass(frozen=True)
class MirrorMessage:
    text: str
    parse_mode: Optional[str]
    verbatim: bool


def _messages_for(event: Event) -> list[MirrorMessage]:
    """Render ONE body-free event to send-ready :class:`MirrorMessage`\\ s (reuses render_event).

    Routes through the bot's EXISTING :func:`~claude_tg.render.render_event` — the SAME pure
    decision that renders the bot's own tool/text activity — then flattens its
    :class:`~claude_tg.render.RenderAction` into the watch's send units. The mirror does not
    keep an editable status line (unlike a live turn), so a tool/status ``op="edit_status"``
    line is emitted as its OWN low-priority (non-verbatim) message rather than coalesced; an
    ``op="new"`` (assistant/operator/result text) is a verbatim (priority) message. An
    ``op="none"`` yields nothing. SB3 is already enforced upstream (the event is body-free);
    this only formats it.
    """
    action: RenderAction = render_event(event)
    if action.op == "none" or not action.chunks:
        return []
    verbatim = action.op == "new"
    out: list[MirrorMessage] = []
    for chunk in action.chunks:
        if chunk.strip():
            out.append(MirrorMessage(text=chunk, parse_mode=action.parse_mode, verbatim=verbatim))
    return out


def _skipped_marker(n: int) -> MirrorMessage:
    """The coalesced flood-control marker (``… N events skipped`` — body-free scaffolding)."""
    return MirrorMessage(text=f"… {n} events skipped (mirror is behind)", parse_mode=None, verbatim=False)


def render_batch(lines: list[str], *, queue_max: int = DEFAULT_WATCH_QUEUE_MAX) -> list[MirrorMessage]:
    """Normalize + render a batch of raw tailer lines to send-ready messages, with flood control.

    Pure (no I/O): maps each raw line through :func:`normalize_line` → body-free events →
    :func:`_messages_for`, preserving order. **Flood control:** if the batch would emit more
    than ``queue_max`` messages, the tool-line NOISE (non-verbatim) is SHED — the assistant
    text + result indicators (verbatim) are always kept — and a single coalesced
    ``… N events skipped`` marker is appended so the operator knows the tail is lossy. The
    send gate (the watch's transport) is still the hard rate limiter; this bounds the burst
    handed to it per poll so a fast-writing session degrades gracefully instead of flooding.
    Never raises (each line is RB1 via :func:`normalize_line`).
    """
    msgs: list[MirrorMessage] = []
    for line in lines:
        for event in normalize_line(line):
            msgs.extend(_messages_for(event))
    if len(msgs) <= queue_max:
        return msgs
    # Over budget: keep the verbatim (text/result) messages, shed the tool-line noise.
    kept = [m for m in msgs if m.verbatim]
    shed = len(msgs) - len(kept)
    if len(kept) > queue_max:
        # Even the verbatim alone overflow (a huge text burst) — keep the most RECENT
        # queue_max (the tail is what the operator is following live) and count the rest shed.
        shed += len(kept) - queue_max
        kept = kept[-queue_max:]
    kept.append(_skipped_marker(shed))
    return kept


#: A coroutine the watch loop calls to SEND one mirror message through the per-chat send gate.
#: ``StreamingSession`` provides it (bound to the chat's :class:`~claude_tg.render.ChatSendGate`
#: so the mirror can never burst past Telegram's ~1 msg/s/chat ceiling). Returns the sent id.
EmitFn = Callable[..., Awaitable[Optional[int]]]


async def run_mirror(
    tailer: TranscriptTailer,
    *,
    emit: EmitFn,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    queue_max: int = DEFAULT_WATCH_QUEUE_MAX,
    should_stop: Optional[Callable[[], bool]] = None,
    on_gone: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    """The read-only watch loop: poll → normalize → render → emit, until stopped/gone (RB1-total).

    Polls ``tailer`` every ``poll_interval`` seconds; each complete line is mapped to
    body-free events (:func:`normalize_line`), rendered (:func:`render_batch`, which applies
    flood control), and SENT via ``emit`` (which routes through the per-chat send gate so the
    combined rate stays bounded). Stops cleanly when ``should_stop()`` is True (``/unwatch`` /
    a replacement watch / shutdown) or the tailer reports the file ``gone`` (deleted) — in
    which case ``on_gone`` (if given) is awaited so the watch can tell the operator the
    session ended.

    **Read-only + RB1-total.** The loop NEVER writes the transcript. A single bad/non-JSON
    line is skipped by the normalizer; a vanished file stops the loop cleanly; an unexpected
    error while emitting one message is logged and the loop continues (one bad send must not
    kill the whole mirror). The clock/sleep is injected so tests drive the loop with no real
    time. The caller (``StreamingSession``) runs this as a cancellable asyncio task and owns
    the one-watch-per-chat + shutdown-cancel lifecycle; ``CancelledError`` propagates (a
    cancel is a clean stop, never swallowed).
    """
    while True:
        if should_stop is not None and should_stop():
            return
        lines = tailer.poll()
        if tailer.gone:
            if on_gone is not None:
                try:
                    await on_gone()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug("mirror on_gone callback failed (ignored)", exc_info=True)
            return
        if lines:
            for msg in render_batch(lines, queue_max=queue_max):
                try:
                    await emit(text=msg.text, parse_mode=msg.parse_mode, verbatim=msg.verbatim)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One failed send (a Telegram hiccup) must not kill the mirror — log and
                    # keep tailing (RB1). The next poll's lines still flow.
                    log.debug("mirror emit failed for one message (ignored)", exc_info=True)
        await sleep(poll_interval)


__all__ = [
    "transcript_path",
    "claude_home",
    "TranscriptTailer",
    "normalize_line",
    "iter_normalized",
    "render_batch",
    "run_mirror",
    "MirrorMessage",
    "EmitFn",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_WATCH_QUEUE_MAX",
]
