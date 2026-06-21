"""Render layer (T6) — normalized engine events -> Telegram output *descriptions*.

This module is **pure logic**: it decides *what* the bot should do with each
:mod:`claude_tg.engine.types` event and returns a :class:`RenderAction` describing
it. It performs **no** Telegram I/O and opens no network — T7 (``bot.py``) executes
the actual ``send_message`` / ``edit_message_text`` / ``answer_callback_query`` calls
and does the real rate-limit waiting. Keeping the decision pure makes the whole
verbatim-vs-one-liner split, the inline-keyboard construction, and the
coalesce/throttle behavior unit-testable with an injected clock and no SDK/network.

Three pieces:

1. **Event -> RenderAction** (:func:`render_event`). Per the design render table
   (`docs/interactive-remote-design.md`) and FR4 (`design.md`):

   * **Verbatim** — ``ask`` / ``plan`` / ``error`` / ``result`` render *in full*,
     chunked to Telegram's UTF-16 limit via :func:`claude_tg.util.split_message`,
     each as its **own** new message (``op="new"``). These are the meaningful output
     the operator must see whole.
   * **One-liner / status** — ``tool_use`` ("▶️ Bash(...)"), ``status``
     ("ℹ️ ..."), and **incremental** ``text`` deltas are *noise*; they fold into a
     single **edit-in-place status line** (``op="edit_status"``) so a burst becomes a
     few edits, never a flood (RB5).
   * **Assembled** (non-incremental) ``text`` is real content -> ``op="new"``
     (chunked).

2. **Inline keyboards + callback codec.**

   * ``ask`` -> one button per option (per question) + an **"Other" (free-text)**
     affordance (:func:`ask_keyboard`).
   * ``plan`` -> ``[Approve]`` + ``[Reject + feedback]`` (:func:`plan_keyboard`).
   * :func:`encode_callback` / :func:`decode_callback` carry ``(kind, tool_use_id,
     payload)`` in **<=64 bytes** (Telegram's hard ``callback_data`` limit) and are
     round-trippable. The ask payload is the **option index** (an int) — never the
     label, which can be long/unicode — and ``(question index, option index)`` are
     both encoded so T7 can rebuild the native ``answers`` map (question text -> label)
     from the held :class:`~claude_tg.engine.types.AskEvent`. See
     :func:`encode_callback` for the byte-budget proof.

3. **Coalesce / throttle (RB5)** — :class:`Coalescer`. Given a stream of events and
   an **injected clock**, it batches incremental text + status into edit-in-place
   updates at a bounded rate (a configurable minimum edit interval) so N rapid deltas
   collapse into a bounded number of edits; verbatim events flush immediately as their
   own messages. The clock is a ``Callable[[], float]`` (monotonic seconds) so tests
   drive it deterministically with **no real sleeps**; T7 owns the actual waiting.

**SB3 (no secret/raw-body leakage).** ``tool_use`` renders the event's
``tool_input_summary`` (already lengths-not-bodies, built by the adapter); this module
never re-derives a summary from raw input and **never logs message content**. There is
no logging in this module at all — rendering is content, and content with secrets must
not be logged (the bot's logger, T7, applies the SB3 scrubber to anything it logs).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .engine.types import (
    AskEvent,
    ErrorEvent,
    Event,
    PlanEvent,
    ResultEvent,
    StatusEvent,
    TextEvent,
    ToolUseEvent,
)
from .util import TELEGRAM_MAX, split_message

# ---------------------------------------------------------------------------
# RenderAction — what T7 should do with an event (T7 does it; this only decides)
# ---------------------------------------------------------------------------

#: What the bot should do with a rendered event.
#:   * ``"new"``         — send a NEW message (one per chunk). Verbatim content +
#:                         assembled assistant text.
#:   * ``"edit_status"`` — edit the chat's single coalesced status line in place
#:                         (create it on first use). Noise: tool_use / status /
#:                         incremental text deltas. Respects RB5 throttling.
#:   * ``"none"``        — nothing operator-facing (e.g. an empty text delta).
RenderOp = Literal["new", "edit_status", "none"]

#: parse_mode hint passed through to T7. We default to ``None`` (plain text) so
#: verbatim plans / questions / errors / tool output are shown EXACTLY as produced
#: and a stray ``*`` or ``_`` can never raise a Telegram "can't parse entities"
#: error or get silently dropped. T7 may override per its own policy.
ParseMode = Optional[str]


@dataclass(frozen=True)
class RenderAction:
    """A pure description of the Telegram effect for one event (T7 executes it).

    ``chunks`` is the message body already split to Telegram-safe UTF-16 lengths via
    :func:`split_message`; ``op`` says whether to send each chunk as a new message,
    fold it into the edit-in-place status line, or do nothing. ``reply_markup`` is the
    inline keyboard for ``ask``/``plan`` (``None`` otherwise). ``parse_mode`` is a hint.

    This object never touches Telegram — it is the contract between the (pure) render
    layer and T7's transport code.
    """

    op: RenderOp
    chunks: tuple[str, ...] = ()
    reply_markup: Optional[InlineKeyboardMarkup] = None
    parse_mode: ParseMode = None
    #: True for the verbatim kinds (ask/plan/error/result) — T7 flushes these
    #: immediately as their own message(s), bypassing the status-line coalescer.
    verbatim: bool = False

    @property
    def text(self) -> str:
        """The assembled body (chunks rejoined) — convenience for callers/tests.

        Lossless against :func:`split_message` (``"".join(chunks)``); a status-line
        edit uses the single coalesced string, but the property still reflects what
        was rendered.
        """
        return "".join(self.chunks)

    @staticmethod
    def none() -> "RenderAction":
        return RenderAction(op="none")


# ---------------------------------------------------------------------------
# callback_data codec — (kind, tool_use_id, payload), round-trippable, <=64 bytes
# ---------------------------------------------------------------------------
#
# Telegram limits ``callback_data`` to 1..64 BYTES. We must fit (kind, tool_use_id,
# payload) inside that. tool_use_id is a UUID-shaped string (~36 chars: 32 hex +
# 4 dashes) plus the SDK sometimes prefixes ``toolu_``; framing must stay tiny.
#
# Scheme (``|``-delimited ASCII):
#
#     ask:    "a|<tool_use_id>|<question_index>.<option_index>"
#     other:  "o|<tool_use_id>|<question_index>"       (free-text "Other" affordance)
#     plan:   "p|<tool_use_id>|a"  (approve)  /  "p|<tool_use_id>|r"  (reject+feedback)
#
# Kind is a single ASCII char ('a'/'o'/'p'); payload is a small int (or int.int / a
# single letter) — NEVER the option label (labels can be long / unicode / > 64 B on
# their own). T7 recovers the label from the held AskEvent via the indices (see
# module docstring + answers_from_ask below).
#
# Byte budget (worst case): "a|" (2) + tool_use_id + "|" (1) + "QQ.OO" (<=5 for
# question 0..99, option 0..99). With a generous 49-char id (toolu_ + 36-char UUID +
# slack) that is 2 + 49 + 1 + 5 = 57 <= 64. encode_callback ASSERTS the bound so an
# over-long id fails loudly at build time rather than Telegram rejecting it at send.

CALLBACK_LIMIT = 64

KIND_ASK = "a"
KIND_OTHER = "o"
KIND_PLAN = "p"

PLAN_APPROVE = "a"
PLAN_REJECT = "r"

_SEP = "|"


@dataclass(frozen=True)
class Callback:
    """A decoded ``callback_data`` payload (the result of :func:`decode_callback`).

    * ``kind``           — ``"ask"`` | ``"other"`` | ``"plan"``.
    * ``tool_use_id``    — the request id the answer routes back to (correlation).
    * ``question_index`` — index into ``AskEvent.questions`` (ask / other only).
    * ``option_index``   — index into that question's ``options`` (ask only).
    * ``plan_action``    — ``"approve"`` | ``"reject"`` (plan only).

    Indices (not labels) are carried so T7 reconstructs the native ``answers`` map
    from the held :class:`AskEvent`; see :func:`answers_from_ask`.
    """

    kind: Literal["ask", "other", "plan"]
    tool_use_id: str
    question_index: Optional[int] = None
    option_index: Optional[int] = None
    plan_action: Optional[Literal["approve", "reject"]] = None


def _check_limit(data: str) -> str:
    """Assert ``data`` fits Telegram's callback_data byte budget (1..64)."""
    n = len(data.encode("utf-8"))
    if n == 0:
        raise ValueError("callback_data must be non-empty")
    if n > CALLBACK_LIMIT:
        raise ValueError(
            f"callback_data is {n} bytes (> {CALLBACK_LIMIT}); tool_use_id "
            f"{data.split(_SEP)[1]!r} too long for the compact scheme"
        )
    return data


def encode_callback(
    kind: str,
    tool_use_id: str,
    *,
    question_index: Optional[int] = None,
    option_index: Optional[int] = None,
    plan_action: Optional[str] = None,
) -> str:
    """Encode ``(kind, tool_use_id, payload)`` into <=64-byte ``callback_data``.

    Round-trips with :func:`decode_callback`. The payload is an **index** for ask
    (``question_index``[.``option_index``]) or ``a``/``r`` for plan — never a label.
    Raises ``ValueError`` if the result would exceed 64 bytes (loud at build time;
    see the byte-budget note above) or on a missing/invalid id or payload.
    """
    if not tool_use_id:
        raise ValueError("tool_use_id is required for callback_data")
    if _SEP in tool_use_id:
        raise ValueError(f"tool_use_id may not contain {_SEP!r}: {tool_use_id!r}")

    if kind == KIND_ASK:
        if question_index is None or option_index is None:
            raise ValueError("ask callback requires question_index and option_index")
        if question_index < 0 or option_index < 0:
            raise ValueError("indices must be non-negative")
        payload = f"{question_index}.{option_index}"
    elif kind == KIND_OTHER:
        if question_index is None or question_index < 0:
            raise ValueError("other callback requires a non-negative question_index")
        payload = str(question_index)
    elif kind == KIND_PLAN:
        if plan_action not in (PLAN_APPROVE, PLAN_REJECT):
            raise ValueError(f"plan_action must be {PLAN_APPROVE!r} or {PLAN_REJECT!r}")
        payload = plan_action
    else:
        raise ValueError(f"unknown callback kind: {kind!r}")

    return _check_limit(f"{kind}{_SEP}{tool_use_id}{_SEP}{payload}")


def decode_callback(data: object) -> Optional[Callback]:
    """Decode ``callback_data`` -> :class:`Callback`, or ``None`` if malformed/foreign.

    Defensive by design (this is the trust boundary that feeds SB1 at T7): ANY input
    that is not our exact 3-field scheme — wrong type, wrong field count, unknown
    kind, non-integer indices, over-long, empty id — returns ``None`` rather than
    raising, so a tampered or stale button can be safely ignored. T7 still applies the
    SB1 allowlist check on the *chat* before acting on a decoded callback.
    """
    if not isinstance(data, str):
        return None
    n = len(data.encode("utf-8"))
    if n == 0 or n > CALLBACK_LIMIT:
        return None
    parts = data.split(_SEP)
    if len(parts) != 3:
        return None
    kind, tool_use_id, payload = parts
    if not tool_use_id or not payload:
        return None

    if kind == KIND_ASK:
        qo = payload.split(".")
        if len(qo) != 2:
            return None
        q, o = qo
        if not (q.isdigit() and o.isdigit()):
            return None
        return Callback(
            kind="ask",
            tool_use_id=tool_use_id,
            question_index=int(q),
            option_index=int(o),
        )
    if kind == KIND_OTHER:
        if not payload.isdigit():
            return None
        return Callback(
            kind="other", tool_use_id=tool_use_id, question_index=int(payload)
        )
    if kind == KIND_PLAN:
        if payload == PLAN_APPROVE:
            return Callback(kind="plan", tool_use_id=tool_use_id, plan_action="approve")
        if payload == PLAN_REJECT:
            return Callback(kind="plan", tool_use_id=tool_use_id, plan_action="reject")
        return None
    return None


# ---------------------------------------------------------------------------
# Reconstruct the native answers-map from a held AskEvent + a decoded callback
# ---------------------------------------------------------------------------


def answers_from_ask(ask: AskEvent, question_index: int, option_index: int) -> dict[str, str]:
    """Rebuild the native ``answers`` map ({question text: chosen label}) for one tap.

    T7 holds the :class:`AskEvent` (the engine emitted it and parked the pending
    request keyed by ``tool_use_id``). When a button tap decodes to ``(question_index,
    option_index)``, T7 calls this to turn the *indices* back into the
    question-text -> label entry the engine needs (which it then wraps in a
    :class:`~claude_tg.engine.types.QuestionAnswer` and routes via the engine's
    decision seam → ``decision_to_substrate`` → the allow-channel ``answers`` map).

    Indexing — not label round-tripping — keeps ``callback_data`` tiny AND robust to
    long/unicode labels. Raises ``IndexError``/``KeyError`` on an out-of-range index
    (a stale/forged button); T7 treats that as an ignorable bad callback.
    """
    question = ask.questions[question_index]
    text = question["question"]
    label = question["options"][option_index]["label"]
    return {str(text): str(label)}


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------

#: How option buttons wrap. Telegram renders wide buttons poorly; one option per row
#: keeps long labels readable. T7 may re-flow; this is a sensible default.
_OPTIONS_PER_ROW = 1


def ask_keyboard(ask: AskEvent) -> InlineKeyboardMarkup:
    """Build the inline keyboard for an ``AskUserQuestion``.

    One button per option for every question (callback carries the question+option
    indices), plus a trailing **"Other" (free-text)** button per question so the
    operator can answer outside the offered options (T7 prompts for a free-text reply
    and routes it as the answer). Button *text* is the (possibly long) label for the
    operator to read; the *callback_data* is the compact index encoding.

    A multi-question ask stacks each question's option rows; T7 may add a per-question
    header line in the message body. ``tool_use_id`` must be present (the engine sets
    it when emitting the event); without it there is nothing to route an answer to.
    """
    tool_use_id = ask.tool_use_id
    if not tool_use_id:
        raise ValueError("AskEvent.tool_use_id is required to build an ask keyboard")

    rows: list[list[InlineKeyboardButton]] = []
    for q_idx, question in enumerate(ask.questions):
        options = question.get("options") or []
        row: list[InlineKeyboardButton] = []
        for o_idx, option in enumerate(options):
            label = str(option.get("label", f"Option {o_idx + 1}"))
            row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=encode_callback(
                        KIND_ASK,
                        tool_use_id,
                        question_index=q_idx,
                        option_index=o_idx,
                    ),
                )
            )
            if len(row) >= _OPTIONS_PER_ROW:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        # "Other" (free-text) affordance for this question.
        rows.append(
            [
                InlineKeyboardButton(
                    text="✏️ Other (free text)",
                    callback_data=encode_callback(
                        KIND_OTHER, tool_use_id, question_index=q_idx
                    ),
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def plan_keyboard(plan: PlanEvent) -> InlineKeyboardMarkup:
    """Build the ``[Approve]`` / ``[Reject + feedback]`` keyboard for an ``ExitPlanMode``.

    Approve -> a bare allow (T7 routes a :class:`~claude_tg.engine.types.PlanVerdict`
    ``approve=True``); Reject -> T7 prompts for feedback text and routes
    ``PlanVerdict(approve=False, feedback=...)`` (feedback rides the deny message —
    there is no native plan-feedback field; see ADR-001 / the normalized contract).
    """
    tool_use_id = plan.tool_use_id
    if not tool_use_id:
        raise ValueError("PlanEvent.tool_use_id is required to build a plan keyboard")
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="✅ Approve",
                    callback_data=encode_callback(
                        KIND_PLAN, tool_use_id, plan_action=PLAN_APPROVE
                    ),
                ),
                InlineKeyboardButton(
                    text="✋ Reject + feedback",
                    callback_data=encode_callback(
                        KIND_PLAN, tool_use_id, plan_action=PLAN_REJECT
                    ),
                ),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Event -> RenderAction (the verbatim-vs-one-liner split)
# ---------------------------------------------------------------------------

# One-liner status-line emoji for the noise kinds. Cosmetic only; never carries data.
_PHASE_EMOJI = {
    "init": "🟢",
    "connected": "🔌",
    "disconnected": "🔌",
    "rate_limit": "⏳",
}


def _chunk(text: str, limit: int = TELEGRAM_MAX) -> tuple[str, ...]:
    """Split to Telegram-safe UTF-16 chunks (reuses :func:`split_message`)."""
    return tuple(split_message(text, limit=limit))


def tool_use_line(event: ToolUseEvent) -> str:
    """One-liner for a tool call — uses the SB3-safe summary, never raw input."""
    return f"▶️ {event.tool_input_summary}"


def status_line(event: StatusEvent) -> str:
    """One-liner for a lifecycle/health status event (no secrets — phase + detail)."""
    emoji = _PHASE_EMOJI.get(event.phase, "ℹ️")
    bits = [f"{emoji} {event.phase}"]
    if event.model:
        bits.append(event.model)
    if event.detail:
        bits.append(event.detail)
    return " · ".join(bits)


def _render_error(event: ErrorEvent) -> RenderAction:
    body = f"⚠️ {event.kind_of_error}: {event.message}"
    return RenderAction(op="new", chunks=_chunk(body), verbatim=True)


def _render_result(event: ResultEvent) -> RenderAction:
    # Terminal per-turn frame. The result_text (if any) is the final answer — show it
    # verbatim; otherwise a compact status footer. Errors come via ErrorEvent.
    if event.result_text:
        body = event.result_text
    else:
        bits = [f"✅ done ({event.subtype})"]
        if event.num_turns is not None:
            bits.append(f"{event.num_turns} turns")
        if event.total_cost_usd is not None:
            bits.append(f"${event.total_cost_usd:.4f}")
        body = " · ".join(bits)
    return RenderAction(op="new", chunks=_chunk(body), verbatim=True)


def render_event(event: Event) -> RenderAction:
    """Map ONE normalized event to a :class:`RenderAction` (pure; no I/O).

    The verbatim-vs-one-liner split (design FR4 / render table):

    * ``ask``  -> verbatim message body (the questions, chunked) + the option keyboard.
    * ``plan`` -> verbatim plan text (chunked) + ``[Approve]``/``[Reject+feedback]``.
    * ``error``-> verbatim error block.
    * ``result`` -> verbatim final answer (or a compact done-footer).
    * ``text`` (assembled) -> verbatim assistant message (chunked, ``op="new"``).
    * ``text`` (incremental) / ``tool_use`` / ``status`` -> a one-liner folded into
      the edit-in-place status line (``op="edit_status"``) — the Coalescer batches
      these (RB5). An empty incremental delta -> ``op="none"``.

    Returns the action; the :class:`Coalescer` decides *when* edit_status actions are
    flushed, and T7 performs the send/edit.
    """
    if isinstance(event, AskEvent):
        body = _render_ask_body(event)
        return RenderAction(
            op="new",
            chunks=_chunk(body),
            reply_markup=ask_keyboard(event),
            verbatim=True,
        )

    if isinstance(event, PlanEvent):
        header = "📋 Proposed plan — Approve or Reject with feedback:\n\n"
        return RenderAction(
            op="new",
            chunks=_chunk(header + event.plan),
            reply_markup=plan_keyboard(event),
            verbatim=True,
        )

    if isinstance(event, ErrorEvent):
        return _render_error(event)

    if isinstance(event, ResultEvent):
        return _render_result(event)

    if isinstance(event, TextEvent):
        if event.incremental:
            # Token-delta noise -> coalesced status line (or nothing if empty).
            if not event.text:
                return RenderAction.none()
            return RenderAction(op="edit_status", chunks=(event.text,))
        # Assembled assistant prose is real content -> its own message, chunked.
        if not event.text:
            return RenderAction.none()
        return RenderAction(op="new", chunks=_chunk(event.text), verbatim=True)

    if isinstance(event, ToolUseEvent):
        return RenderAction(op="edit_status", chunks=(tool_use_line(event),))

    if isinstance(event, StatusEvent):
        return RenderAction(op="edit_status", chunks=(status_line(event),))

    # Unknown/foreign event — render nothing rather than crash (RB1 spirit).
    return RenderAction.none()


def _render_ask_body(ask: AskEvent) -> str:
    """Verbatim message body for an ask — the question text(s) + a hint.

    The buttons carry the options, so the body shows each question (and its header,
    if present) verbatim; the keyboard (built separately) is the answer surface.
    """
    lines: list[str] = []
    for i, question in enumerate(ask.questions):
        header = question.get("header")
        qtext = question.get("question", "")
        prefix = f"❓ {header}: " if header else "❓ "
        if len(ask.questions) > 1:
            prefix = f"❓ ({i + 1}/{len(ask.questions)}) " + (
                f"{header}: " if header else ""
            )
        lines.append(f"{prefix}{qtext}")
        if question.get("multiSelect"):
            lines.append("  (you may pick more than one)")
    lines.append("\nTap an option below, or “Other” to type a free-text answer.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coalescer / throttle (RB5)
# ---------------------------------------------------------------------------


@dataclass
class _PendingStatus:
    """Buffered status-line state between flushes."""

    text: str = ""
    dirty: bool = False  # there is unflushed status content
    last_flush: float = field(default=float("-inf"))  # monotonic seconds


@dataclass(frozen=True)
class FlushResult:
    """What :meth:`Coalescer.offer` / :meth:`Coalescer.flush_due` decided right now.

    * ``actions``    — RenderActions T7 should perform immediately, in order. Verbatim
                       events appear here as ``op="new"``; a due status edit appears as
                       a single ``op="edit_status"`` carrying the coalesced line.
    * ``next_due_at``— monotonic time the next *pending* status edit becomes due, or
                       ``None`` if nothing is buffered. T7 can schedule a wakeup at this
                       time (it owns the real waiting); tests just advance the clock.
    """

    actions: tuple[RenderAction, ...] = ()
    next_due_at: Optional[float] = None


class Coalescer:
    """Batch noisy edit-in-place updates into a bounded edit rate (RB5).

    Feed every event through :meth:`offer`. The Coalescer:

    * **flushes verbatim events immediately** (ask/plan/error/result/assembled-text):
      they return as ``op="new"`` actions in the result, unbatched.
    * **coalesces** ``edit_status`` actions (incremental text + tool_use + status):
      it accumulates the latest status line and emits **at most one** ``edit_status``
      action per ``min_interval`` seconds. A burst of N deltas inside one interval
      therefore yields a bounded number of edits (typically 1 immediately + 1 at the
      next interval boundary), never N.

    The **clock is injected** (``now: Callable[[], float]`` returning monotonic
    seconds) so tests advance time deterministically with no real sleeps. The
    Coalescer makes *decisions only* — it returns the actions; T7 performs the
    sends/edits and the actual inter-edit waiting (sleeping until ``next_due_at``).

    Coalescing policy (leading + trailing edge):

    * The **first** status update after an idle period flushes immediately (leading
      edge) so the operator sees activity start without lag.
    * Subsequent updates within ``min_interval`` are buffered; the newest wins (a
      status line is a *replace*, not an append). They flush when the interval elapses
      (driven by a later :meth:`offer` or an explicit :meth:`flush_due`) — the
      trailing edge — or are force-flushed by a verbatim event / :meth:`flush`.

    A verbatim event also **force-flushes** any pending status first, so ordering is
    preserved (the operator never sees a final answer before the status that preceded
    it). ``min_interval`` defaults to ``DEFAULT_MIN_EDIT_INTERVAL`` (configurable;
    Telegram's practical edit ceiling is ~1 msg/s/chat — a 2 s default leaves margin).
    """

    def __init__(
        self,
        *,
        now: Callable[[], float],
        min_interval: Optional[float] = None,
    ) -> None:
        interval = DEFAULT_MIN_EDIT_INTERVAL if min_interval is None else min_interval
        if interval < 0:
            raise ValueError("min_interval must be non-negative")
        self._now = now
        self._min_interval = float(interval)
        self._pending = _PendingStatus()

    # -- internal -----------------------------------------------------------

    def _due(self, t: float) -> bool:
        return (t - self._pending.last_flush) >= self._min_interval

    def _emit_status(self, t: float) -> RenderAction:
        self._pending.last_flush = t
        self._pending.dirty = False
        return RenderAction(op="edit_status", chunks=(self._pending.text,))

    def _next_due_at(self) -> Optional[float]:
        if not self._pending.dirty:
            return None
        return self._pending.last_flush + self._min_interval

    # -- public API ---------------------------------------------------------

    def offer(self, event: Event) -> FlushResult:
        """Offer one event; return the actions to perform right now.

        Verbatim events force-flush any buffered status (to preserve order) then emit
        their own ``op="new"`` action. ``edit_status`` events update the buffered
        status line and emit an edit only if the throttle interval has elapsed.
        ``op="none"`` events buffer nothing and emit nothing.
        """
        action = render_event(event)
        t = self._now()

        if action.op == "none":
            return FlushResult(actions=(), next_due_at=self._next_due_at())

        if action.verbatim or action.op == "new":
            # Force-flush pending status first so ordering holds, then the verbatim msg.
            actions: list[RenderAction] = []
            if self._pending.dirty:
                actions.append(self._emit_status(t))
            actions.append(action)
            return FlushResult(actions=tuple(actions), next_due_at=self._next_due_at())

        # op == "edit_status": coalesce. The status line is a REPLACE (newest wins).
        self._pending.text = action.text
        self._pending.dirty = True
        if self._due(t):
            return FlushResult(
                actions=(self._emit_status(t),), next_due_at=self._next_due_at()
            )
        # Buffered; not yet due. T7 should wake at next_due_at to flush the trailing edge.
        return FlushResult(actions=(), next_due_at=self._next_due_at())

    def flush_due(self) -> FlushResult:
        """Emit the buffered status edit IFF the throttle interval has now elapsed.

        T7 calls this on a timer (woken at the previous ``next_due_at``) to release the
        trailing-edge edit when no new event arrived to drive it. No-op if nothing is
        buffered or the interval has not elapsed.
        """
        if not self._pending.dirty:
            return FlushResult(actions=(), next_due_at=None)
        t = self._now()
        if self._due(t):
            return FlushResult(
                actions=(self._emit_status(t),), next_due_at=self._next_due_at()
            )
        return FlushResult(actions=(), next_due_at=self._next_due_at())

    def flush(self) -> FlushResult:
        """Force-flush any buffered status NOW, ignoring the interval (end-of-turn).

        T7 calls this when a turn ends (after the ``result``) so the final status line
        is not left unshown. Ignores the throttle deliberately — there is nothing
        more coming to coalesce with.
        """
        if not self._pending.dirty:
            return FlushResult(actions=(), next_due_at=None)
        return FlushResult(actions=(self._emit_status(self._now()),), next_due_at=None)


#: Default minimum interval between coalesced status edits (seconds). Telegram's
#: practical per-chat edit/send ceiling is ~1 msg/s; 2 s leaves comfortable margin
#: under bursts (RB5). Configurable via the Coalescer constructor (T7 may surface a
#: ``RENDER_EDIT_INTERVAL_SECONDS`` setting; not added to Config in T6 — YAGNI until
#: T7 wires it).
DEFAULT_MIN_EDIT_INTERVAL = 2.0


def coalesce_stream(
    events: Iterable[Event],
    *,
    now: Callable[[], float],
    min_interval: float = DEFAULT_MIN_EDIT_INTERVAL,
) -> list[RenderAction]:
    """Run a whole event iterable through a :class:`Coalescer` (test/offline helper).

    Returns the flat ordered list of RenderActions a consumer would perform, including
    a final force-:meth:`Coalescer.flush`. Convenience for tests and any non-live
    batch rendering; the live path uses :class:`Coalescer` incrementally so it can
    interleave with the real clock and Telegram waits (T7).
    """
    coalescer = Coalescer(now=now, min_interval=min_interval)
    out: list[RenderAction] = []
    for event in events:
        out.extend(coalescer.offer(event).actions)
    out.extend(coalescer.flush().actions)
    return out


__all__ = [
    # action
    "RenderAction",
    "RenderOp",
    "render_event",
    # keyboards + codec
    "ask_keyboard",
    "plan_keyboard",
    "encode_callback",
    "decode_callback",
    "Callback",
    "answers_from_ask",
    "CALLBACK_LIMIT",
    # one-liners
    "tool_use_line",
    "status_line",
    # coalesce / throttle
    "Coalescer",
    "FlushResult",
    "coalesce_stream",
    "DEFAULT_MIN_EDIT_INTERVAL",
]
