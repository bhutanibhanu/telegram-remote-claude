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

import html
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final, Literal, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .engine.types import (
    AskEvent,
    ErrorEvent,
    Event,
    PermissionEvent,
    PlanEvent,
    ResultEvent,
    StatusEvent,
    TextEvent,
    ToolUseEvent,
)
from .tg_html import strip_telegram_html, to_telegram_html
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

    For Claude-authored **prose** (assembled text / result / plan / ask question text)
    ``chunks`` carries **Telegram HTML** and ``parse_mode == "HTML"``, while
    ``plain_chunks`` carries the parallel **raw** (un-converted) text for the same chunk
    positions. T7's send path tries the HTML chunk first and, on ANY Telegram error,
    resends the parallel raw chunk with ``parse_mode=None`` (so a malformed-entity
    rejection degrades to today's plain-markdown behavior, never a dropped message). For
    bot scaffolding (status lines, the done-footer, error blocks) ``plain_chunks`` is
    empty and the chunk is plain text already.

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
    #: Parallel RAW (un-converted) text for each entry in ``chunks`` — the plain-text
    #: fallback T7 resends if the HTML chunk is rejected by Telegram. Empty when the
    #: chunk is already plain (no HTML conversion happened); then T7 strips tags as a
    #: last resort. Length, when present, MUST equal ``len(chunks)``.
    plain_chunks: tuple[str, ...] = ()

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
#     ask:        "a|<tool_use_id>|<question_index>.<option_index>"
#     other:      "o|<tool_use_id>|<question_index>"   (free-text "Other" affordance)
#     plan:       "p|<tool_use_id>|a"  (approve)  /  "p|<tool_use_id>|r"  (reject)
#     permission: "m|<tool_use_id>|o"  (allow once) / "...|s" (session) / "...|d" (deny)
#
# Kind is a single ASCII char ('a'/'o'/'p'/'m'); payload is a small int (or int.int /
# a single letter) — NEVER the option label (labels can be long / unicode / > 64 B on
# their own). T7 recovers the label from the held AskEvent via the indices (see
# module docstring + answers_from_ask below). The permission kind needs no indices —
# its tool_use_id alone routes the verdict back to the held request — so the payload
# is just a single action char (o/s/d), keeping the data tiny next to the ~49-byte id.
#
# Byte budget (worst case):
#   * ask: "a|" (2) + tool_use_id + "|" (1) + "QQ.OO" (<=5 for question 0..99,
#     option 0..99). With a generous 49-char id (toolu_ + 36-char UUID + slack) that
#     is 2 + 49 + 1 + 5 = 57 <= 64.
#   * permission: "m|" (2) + tool_use_id + "|" (1) + action char (1) = 2 + 49 + 1 + 1
#     = 53 <= 64. (A full "permission|<id>|session" string would be ~68 B and blow the
#     limit — hence the 1-char kind + 1-char action code.)
# encode_callback ASSERTS the bound so an over-long id fails loudly at build time
# rather than Telegram rejecting it at send.

CALLBACK_LIMIT = 64

KIND_ASK = "a"
KIND_OTHER = "o"
KIND_PLAN = "p"
#: Permission-prompt kind (P2, ADR-003 §2). A single char ('m'; 'a'/'o'/'p' are
#: taken) so "m|<~49-byte id>|<action>" stays ~53 B under Telegram's 64-byte limit —
#: a literal "permission|<id>|session" would be ~68 B and fail _check_limit.
KIND_PERMISSION = "m"
#: Switch-active-project kind (T6/P9). A single char ('w'; 'a'/'o'/'p'/'m' are taken) so a
#: "w|<name>|s" tap stays tiny. UNLIKE the four hold kinds (ask/other/plan/permission) it
#: does NOT route to a held ``tool_use_id`` — it carries the TARGET PROJECT NAME in the
#: middle field and a fixed 's' payload. The name is SB4-constrained upstream
#: (``^[A-Za-z0-9_-]{1,32}$`` — session_store._NAME_RE), so it can never contain the ``|``
#: separator (decode would reject a 4-field split anyway) and "w|<<=32-byte name>|s" is
#: <= 36 B, well under the 64-byte limit. The tap is a NAVIGATION action (switch the active
#: project), gated by the bot's SB1 ``_authorized`` recheck like every callback; it touches
#: no pending hold and never resolves a decision (so it cannot collide with the
#: permission/ask/plan/other callback_data — distinct kind char + a name, not an id).
KIND_SWITCH = "w"

PLAN_APPROVE = "a"
PLAN_REJECT = "r"

#: Permission verdict action codes — 1 char each (byte budget; see KIND_PERMISSION).
#: 'o'=allow-once, 's'=allow-session, 'd'=deny. They map to the operator-facing
#: PermissionDecision verdicts (allow_once / allow_session / deny) in T5.
PERMISSION_ONCE = "o"
PERMISSION_SESSION = "s"
PERMISSION_DENY = "d"

#: Action char -> the ``permission_action`` value carried on the decoded Callback.
_PERMISSION_ACTIONS: dict[str, Literal["once", "session", "deny"]] = {
    PERMISSION_ONCE: "once",
    PERMISSION_SESSION: "session",
    PERMISSION_DENY: "deny",
}

_SEP = "|"

#: A stored project name's lexical shape (mirrors ``session_store._NAME_RE``, SB4). Used by
#: :func:`decode_callback` to reject a forged/over-long switch-callback name at the trust
#: boundary BEFORE it reaches the store (defense-in-depth — the store also validates).
_SWITCH_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True)
class Callback:
    """A decoded ``callback_data`` payload (the result of :func:`decode_callback`).

    * ``kind``              — ``"ask"`` | ``"other"`` | ``"plan"`` | ``"permission"``.
    * ``tool_use_id``       — the request id the answer routes back to (correlation).
    * ``question_index``    — index into ``AskEvent.questions`` (ask / other only).
    * ``option_index``      — index into that question's ``options`` (ask only).
    * ``plan_action``       — ``"approve"`` | ``"reject"`` (plan only).
    * ``permission_action`` — ``"once"`` | ``"session"`` | ``"deny"`` (permission only).

    Indices (not labels) are carried so T7 reconstructs the native ``answers`` map
    from the held :class:`AskEvent`; see :func:`answers_from_ask`. The permission
    verdict needs no indices — ``tool_use_id`` alone routes it to the held request
    (T5 turns ``permission_action`` into the engine's ``PermissionDecision``).
    """

    kind: Literal["ask", "other", "plan", "permission", "switch"]
    tool_use_id: str
    question_index: Optional[int] = None
    option_index: Optional[int] = None
    plan_action: Optional[Literal["approve", "reject"]] = None
    permission_action: Optional[Literal["once", "session", "deny"]] = None
    #: The TARGET project name of a ``switch`` tap (T6/P9) — the project to make active.
    #: ``None`` for every other kind (which route by ``tool_use_id``); for a ``switch`` the
    #: ``tool_use_id`` field is unused (set to a sentinel) and this carries the name.
    switch_to: Optional[str] = None


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


#: Fixed payload char for a ``switch`` callback (the kind alone + the name carry the
#: meaning; a constant payload keeps the 3-field scheme uniform). 's' for switch.
SWITCH_PAYLOAD = "s"


def encode_switch_callback(name: str) -> str:
    """Encode a ``[Open <project>]`` switch tap into <=64-byte ``callback_data`` (T6/P9).

    The switch kind does NOT route by ``tool_use_id`` — it carries the TARGET PROJECT NAME
    in the middle field (``w|<name>|s``). ``name`` is the project's STORED (SB4-validated,
    ``^[A-Za-z0-9_-]{1,32}$``) name, so it can never contain the ``|`` separator and the
    whole string is <= 36 B (well under 64). Round-trips with :func:`decode_callback`.
    Raises ``ValueError`` on an empty name or one that (defensively) contains ``|``.
    """
    if not name:
        raise ValueError("project name is required for a switch callback")
    if _SEP in name:
        raise ValueError(f"project name may not contain {_SEP!r}: {name!r}")
    return _check_limit(f"{KIND_SWITCH}{_SEP}{name}{_SEP}{SWITCH_PAYLOAD}")


def encode_callback(
    kind: str,
    tool_use_id: str,
    *,
    question_index: Optional[int] = None,
    option_index: Optional[int] = None,
    plan_action: Optional[str] = None,
    payload: Optional[str] = None,
) -> str:
    """Encode ``(kind, tool_use_id, payload)`` into <=64-byte ``callback_data``.

    Round-trips with :func:`decode_callback`. The payload is an **index** for ask
    (``question_index``[.``option_index``]), ``a``/``r`` for plan, or a 1-char action
    code (``o``/``s``/``d``) for permission — never a label. ``payload`` carries the
    permission action char (the permission kind needs no indices; its ``tool_use_id``
    alone routes the verdict). Raises ``ValueError`` if the result would exceed 64
    bytes (loud at build time; see the byte-budget note above) or on a missing/invalid
    id or payload. The ``switch`` kind has its own builder
    (:func:`encode_switch_callback`) — it carries a project NAME, not a ``tool_use_id``.
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
        data_payload = f"{question_index}.{option_index}"
    elif kind == KIND_OTHER:
        if question_index is None or question_index < 0:
            raise ValueError("other callback requires a non-negative question_index")
        data_payload = str(question_index)
    elif kind == KIND_PLAN:
        if plan_action not in (PLAN_APPROVE, PLAN_REJECT):
            raise ValueError(f"plan_action must be {PLAN_APPROVE!r} or {PLAN_REJECT!r}")
        data_payload = plan_action
    elif kind == KIND_PERMISSION:
        if payload not in _PERMISSION_ACTIONS:
            raise ValueError(
                f"permission callback requires payload in "
                f"{sorted(_PERMISSION_ACTIONS)!r}, got {payload!r}"
            )
        data_payload = payload
    else:
        raise ValueError(f"unknown callback kind: {kind!r}")

    return _check_limit(f"{kind}{_SEP}{tool_use_id}{_SEP}{data_payload}")


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
    if kind == KIND_PERMISSION:
        action = _PERMISSION_ACTIONS.get(payload)
        if action is None:  # unknown action char -> ignorable (defensive, SB1/RB1)
            return None
        return Callback(
            kind="permission", tool_use_id=tool_use_id, permission_action=action
        )
    if kind == KIND_SWITCH:
        # ``w|<name>|s`` — the middle field is the TARGET PROJECT NAME (not a tool id).
        # Defensive: the payload must be the fixed switch char and the name must look like
        # a stored SB4 name (^[A-Za-z0-9_-]{1,32}$) — anything else is a forged/stale tap
        # → ignorable (None). The 3-field split already rejected a ``|`` in the name.
        if payload != SWITCH_PAYLOAD:
            return None
        if not _SWITCH_NAME_RE.match(tool_use_id):
            return None
        # The id field is unused for a switch (the name rides ``switch_to``); keep a sentinel
        # so the dataclass invariant (non-empty ``tool_use_id``) holds.
        return Callback(kind="switch", tool_use_id="-", switch_to=tool_use_id)
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


def ask_question_body(ask: AskEvent, question_index: int) -> str:
    """Message body for ONE question of an ask (its header + text + multi-select hint).

    Multi-question asks are rendered one message per question (paired with
    :func:`ask_question_keyboard`) so each option set sits directly beneath its question —
    a single stacked keyboard for several questions is an unreadable wall of buttons.
    """
    question = ask.questions[question_index]
    header = question.get("header")
    qtext = question.get("question", "")
    total = len(ask.questions)
    if total > 1:
        prefix = f"❓ ({question_index + 1}/{total}) " + (f"{header}: " if header else "")
    else:
        prefix = f"❓ {header}: " if header else "❓ "
    body = f"{prefix}{qtext}"
    if question.get("multiSelect"):
        body += "\n  (you may pick more than one)"
    return body


def ask_question_body_html(ask: AskEvent, question_index: int) -> str:
    """HTML version of :func:`ask_question_body` (the live per-question send path).

    The ``❓ (k/N) Header:`` prefix is bot scaffolding so it is HTML-escaped (not
    Markdown-converted); the **question text** is Claude-authored CommonMark so it is run
    through :func:`to_telegram_html` (so a stray ``<`` / ``&`` from Claude can't break the
    message, and ``**bold**`` etc. render). The plain :func:`ask_question_body` remains
    the parallel raw fallback T7 resends on a Telegram HTML rejection.
    """
    question = ask.questions[question_index]
    header = question.get("header")
    qtext = question.get("question", "")
    total = len(ask.questions)
    if total > 1:
        prefix = "❓ (" + f"{question_index + 1}/{total}" + ") " + (
            f"{_escape_html(str(header))}: " if header else ""
        )
    else:
        prefix = f"❓ {_escape_html(str(header))}: " if header else "❓ "
    body = f"{prefix}{to_telegram_html(str(qtext))}"
    if question.get("multiSelect"):
        body += "\n  (you may pick more than one)"
    return body


def ask_question_keyboard(ask: AskEvent, question_index: int) -> InlineKeyboardMarkup:
    """Inline keyboard for ONE question of an ask — its options (one per row) + an
    "Other" free-text button.

    callback_data carries ``(question_index, option_index)`` so the relay records the
    answer against the right question and resolves the whole ask once every question has
    an answer. ``tool_use_id`` must be present (the engine sets it on the event).
    """
    tool_use_id = ask.tool_use_id
    if not tool_use_id:
        raise ValueError("AskEvent.tool_use_id is required to build an ask keyboard")
    question = ask.questions[question_index]
    options = question.get("options") or []
    rows: list[list[InlineKeyboardButton]] = []
    for o_idx, option in enumerate(options):
        label = str(option.get("label", f"Option {o_idx + 1}"))
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=encode_callback(
                        KIND_ASK, tool_use_id, question_index=question_index, option_index=o_idx
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Other (free text)",
                callback_data=encode_callback(KIND_OTHER, tool_use_id, question_index=question_index),
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


def permission_keyboard(event: PermissionEvent) -> InlineKeyboardMarkup:
    """Build the ``[Allow once] / [Allow for session] / [Deny]`` keyboard for a risky tool.

    Mirrors :func:`ask_keyboard`/:func:`plan_keyboard`: each button's ``callback_data``
    is the compact permission encoding (kind ``m`` + the held ``tool_use_id`` +
    a 1-char action code), so a tap decodes to the verdict T5 routes to the held
    :class:`~claude_tg.engine.pending.PendingRegistry` request (ADR-003 §2):

    * **✅ Allow once**        -> ``once``    (allow this request only; the next use re-asks).
    * **☑️ Allow for session** -> ``session`` (record an engine-side per-tool-NAME grant + allow).
    * **⛔ Deny**              -> ``deny``    (substrate deny carrying the canned message, D5).

    The three buttons stack one-per-row so the (potentially wide) labels stay readable
    on a phone. ``tool_use_id`` is required (the engine sets it when emitting the
    :class:`PermissionEvent`); without it there is nothing to route a verdict to.
    """
    tool_use_id = event.tool_use_id
    if not tool_use_id:
        raise ValueError(
            "PermissionEvent.tool_use_id is required to build a permission keyboard"
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="✅ Allow once",
                    callback_data=encode_callback(
                        KIND_PERMISSION, tool_use_id, payload=PERMISSION_ONCE
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="☑️ Allow for session",
                    callback_data=encode_callback(
                        KIND_PERMISSION, tool_use_id, payload=PERMISSION_SESSION
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⛔ Deny",
                    callback_data=encode_callback(
                        KIND_PERMISSION, tool_use_id, payload=PERMISSION_DENY
                    ),
                )
            ],
        ]
    )


def open_project_keyboard(name: str) -> InlineKeyboardMarkup:
    """Build the ``[Open <project>]`` switch button for a background ping (T6/P9).

    A single inline button whose ``callback_data`` is the compact switch encoding
    (:func:`encode_switch_callback` → ``w|<name>|s``): a tap routes through
    :func:`decode_callback` → the bot's ``on_callback`` (SB1-gated by the ``_authorized``
    recheck there) → switch the chat's active project to ``name`` (reusing ``/switch``'s
    validation). The button *text* shows the (SB4-validated) name for the operator; the
    *callback_data* carries it compactly. Attached to a background project's needs-attention
    + done pings so the operator can jump straight to the project from the ping.

    ``name`` must be the project's STORED name (SB4-validated upstream). The label escapes
    nothing (the button text is plain, not HTML); :func:`encode_switch_callback` enforces the
    no-``|`` / byte-budget invariants.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=f"📂 Open {name}",
                    callback_data=encode_switch_callback(name),
                )
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Smart-reply chips for a free-text prompt (T6/P9) — one-time ReplyKeyboard
# ---------------------------------------------------------------------------
#
# When a turn is awaiting FREE TEXT (the "Other"/reject-feedback capture path) the bot can
# attach a small ReplyKeyboardMarkup of common quick answers so the usual replies are one
# tap (Telegram types the chosen text as the operator's next message — the free-text path
# is unchanged for a typed answer). SCOPE + DISMISS: the keyboard is ONE-TIME
# (``one_time_keyboard=True`` — Telegram hides it after one use) and the bot ALSO sends an
# explicit ``ReplyKeyboardRemove`` once the free text is captured / the prompt resolves
# (:func:`quick_reply_dismiss`), so the chips never linger globally over later turns.
#
# SB3: the chips are FIXED, bot-authored phrases (no event body, no tool input) — they
# reveal nothing about the held request. Pure render objects; no I/O.

#: The fixed quick-reply phrases offered on a free-text prompt (T6/P9). Common operator
#: answers so the usual replies are one tap; kept short + generic (no project/event context,
#: SB3). The operator can always ignore them and type anything.
_QUICK_REPLY_CHIPS: Final[tuple[tuple[str, ...], ...]] = (
    ("proceed", "keep it minimal"),
    ("explain first", "use TypeScript"),
)


def quick_reply_keyboard() -> ReplyKeyboardMarkup:
    """A one-time ``ReplyKeyboardMarkup`` of common quick answers for a free-text prompt.

    Attached to the ``✏️ <name>: reply…`` free-text prompt so the common replies (e.g.
    "proceed", "keep it minimal", "explain first", "use TypeScript") are one tap — tapping a
    chip sends that text as the operator's next message, which the free-text capture path
    resolves exactly as a typed reply (no behavior change for typed input). ``one_time_keyboard``
    hides the keyboard after one use; the bot also explicitly removes it on capture
    (:func:`quick_reply_dismiss`) so it is scoped to the pending prompt and never lingers.
    ``resize_keyboard`` keeps the chips compact; ``selective`` is left off (the chat is the
    single allowlisted operator). Pure render object; no I/O. SB3: fixed phrases only.
    """
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=chip) for chip in row] for row in _QUICK_REPLY_CHIPS],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


def quick_reply_dismiss() -> ReplyKeyboardRemove:
    """The ``ReplyKeyboardRemove`` the bot sends to dismiss the quick-reply chips (T6/P9).

    Sent once the free text is captured / the prompt resolves so the one-time chip keyboard
    (:func:`quick_reply_keyboard`) does not linger over later, unrelated turns (the scope +
    dismiss requirement). Pure render object; no I/O.
    """
    return ReplyKeyboardRemove()


# ---------------------------------------------------------------------------
# /yolo loud indicator (D6) — pure render strings; T5/bot decides WHERE to show them
# ---------------------------------------------------------------------------

#: Loud, unambiguous warning glyph for the bypass posture. A double caution sign so it
#: cannot be mistaken for an ordinary status emoji (D6 — "/yolo" must never be silently on).
_YOLO_GLYPH = "⚠️"


def yolo_banner() -> str:
    """The loud one-shot banner shown when ``/yolo`` is **enabled** (D6).

    A pure string (T5 sends it on the ``/yolo`` toggle); kept here so the wording lives
    with the other render strings. Loud + unambiguous: it spells out that every tool now
    runs with **no approval prompt** until ``/unyolo`` (so allow-all is never silent).
    """
    return (
        f"{_YOLO_GLYPH} YOLO MODE ON {_YOLO_GLYPH}\n"
        "Every tool now runs WITHOUT an approval prompt — no permission gate is "
        "active. Send /unyolo to turn it back off."
    )


def yolo_indicator() -> str:
    """A short persistent ``⚠️`` marker for an auto-allowed / outbound action under ``/yolo``.

    A pure prefix string T5 can prepend to messages while ``/yolo`` is on (e.g. on each
    auto-allowed tool line) so the bypass shows on **every** affected action, not just
    once at enable time (D6). Loud and compact.
    """
    return f"{_YOLO_GLYPH} YOLO"


# ---------------------------------------------------------------------------
# Proactive background-project notifications (P5 / ADR-005 D4) — pure strings
# ---------------------------------------------------------------------------
#
# When a project that is NOT the chat's current foreground needs the operator
# (a held permission/ask/plan) or terminates (result/error), the relay (T5/T7)
# sends a name-prefixed ping so the operator knows WHICH project and can answer
# it. A foreground project renders inline as today (no ping). **T3 only provides
# the strings**; the relay decides *when* to send them (inline-vs-notify by
# foreground) in T5/T7.
#
# SB3 (body-free). These builders carry ONLY the project name + a fixed,
# kind-specific phrase (for needs-attention) or a short, caller-supplied status
# word (for done/error). They take **no tool input** and **never re-derive a
# summary** — there is nothing here from which a Write body / Bash secret could
# leak. The needs-attention strings are entirely fixed phrases; the only
# interpolated values are the (SB4-validated) project name and, for the error
# ping, a short error label the caller already produced body-free (e.g. the
# engine's ``ErrorEvent.kind_of_error`` / a clipped message — never raw input).
#
# Project names are SB4-constrained upstream (``^[A-Za-z0-9_-]{1,32}$`` —
# session_store._NAME_RE), so the name is safe to interpolate with no escaping;
# these helpers do not validate (the relay only ever passes a stored name) and
# do not interpolate anything else unvalidated.

#: Bell glyph for a background project that needs the operator's attention (D4).
_NOTIFY_ATTENTION_GLYPH: Final = "🔔"
#: Done glyph for a background project that finished cleanly (D4).
_NOTIFY_DONE_GLYPH: Final = "✅"
#: Warning glyph for a background project that errored (D4) — matches ``_render_error``.
_NOTIFY_ERROR_GLYPH: Final = "⚠️"

#: Fixed, body-free phrase per pending kind (the needs-attention triggers, D4). Keyed by
#: the SAME :data:`PendingKind` the relay's pending index already holds, so T5/T7 map a
#: held request straight to its ping with no extra branching. The phrases are constant —
#: no event field is interpolated (SB3): a permission/ask/plan ping reveals only that the
#: project needs approval / asks a question / proposes a plan, never *what* it wants.
_NOTIFY_ATTENTION_PHRASE: Final[dict[str, str]] = {
    "permission": "Claude needs approval",
    "ask": "asks a question",
    "plan": "proposes a plan",
}

#: Fallback phrase for an unknown pending kind (RB1 — never crash on a value the relay
#: did not expect; degrade to a generic, still body-free "needs attention" ping).
_NOTIFY_ATTENTION_FALLBACK: Final = "needs attention"


def queued_suffix(queued_waiting: int) -> str:
    """The ``" (N more waiting)"`` queued-counter suffix for a ping (T6/P9), or ``""``.

    ``queued_waiting`` is the count of turns parked behind the concurrency cap (the relay
    pulls it from the per-chat run queue). When ≥1 the ping (and the ``/status`` runs line)
    appends ``" (N more waiting)"`` so the operator knows work is backed up; 0 → ``""`` (no
    dangling tail). Pure string; no I/O — two numbers, never any tool body (SB3).
    """
    if queued_waiting <= 0:
        return ""
    return f" ({queued_waiting} more waiting)"


def notify_attention(name: str, kind: str, *, queued_waiting: int = 0) -> str:
    """Body-free ping for a BACKGROUND project that needs the operator (D4; SB3).

    ``kind`` is the held request's :data:`PendingKind` (``"permission"`` / ``"ask"`` /
    ``"plan"``) — the same value the relay's pending index already carries — and selects
    a **fixed phrase**:

    * ``permission`` → ``🔔 <name> — Claude needs approval``
    * ``ask``        → ``🔔 <name> — asks a question``
    * ``plan``       → ``🔔 <name> — proposes a plan``

    An **unknown** kind degrades to ``🔔 <name> — needs attention`` (RB1) rather than
    raising. ``queued_waiting`` (T6/P9) appends a ``" (N more waiting)"`` counter when ≥1
    project is parked behind the concurrency cap (:func:`queued_suffix`). Pure string; no
    I/O. **SB3:** the phrase is constant per kind — no event field (no question text, plan
    body, or tool input) is ever interpolated, so a ping cannot leak content. ``name`` is an
    SB4-validated project name (safe to interpolate unescaped); the only other interpolated
    value is the bot-derived queue count (a number).
    """
    phrase = _NOTIFY_ATTENTION_PHRASE.get(kind, _NOTIFY_ATTENTION_FALLBACK)
    return f"{_NOTIFY_ATTENTION_GLYPH} {name} — {phrase}{queued_suffix(queued_waiting)}"


def notify_done(name: str, *, queued_waiting: int = 0) -> str:
    """Body-free ping for a BACKGROUND project that finished cleanly (D4; SB3).

    ``✅ <name> — done``. ``queued_waiting`` (T6/P9) appends a ``" (N more waiting)"`` counter
    when ≥1 project is parked behind the cap (:func:`queued_suffix`) — a finishing run frees a
    slot, so the operator sees how many are still backed up. Pure string; no I/O. Carries only
    the project name + a fixed ``done`` word (+ the numeric queue count) — never the result
    text (SB3); the foreground project still renders its full :class:`ResultEvent` inline.
    """
    return f"{_NOTIFY_DONE_GLYPH} {name} — done{queued_suffix(queued_waiting)}"


def notify_error(name: str, short_error: str, *, queued_waiting: int = 0) -> str:
    """Body-free ping for a BACKGROUND project that errored (D4; SB3).

    ``⚠️ <name> — <short_error>``. ``short_error`` is a **short, already-body-free** error
    label the relay supplies — e.g. the engine's :data:`~claude_tg.engine.types.ErrorKind`
    (``tool_error`` / ``turn_error`` / ``driver_error``) — **never** raw tool input or an
    untrimmed dump. This builder neither re-derives nor expands it (SB3); it only prefixes
    the glyph + the (SB4-validated) name. ``queued_waiting`` (T6/P9) appends the
    ``" (N more waiting)"`` counter when ≥1 project is parked behind the cap. A blank
    ``short_error`` degrades to a generic ``error`` so the ping is never an empty tail (RB1).
    Pure string; no I/O.
    """
    label = short_error.strip() or "error"
    return f"{_NOTIFY_ERROR_GLYPH} {name} — {label}{queued_suffix(queued_waiting)}"


# ---------------------------------------------------------------------------
# Free-text prompt (the "Other" / plan-reject follow-up) — name-echoed (D5)
# ---------------------------------------------------------------------------
#
# When the operator taps "Other"/"Reject" on a project's prompt, the bot replies a
# follow-up asking for the free-text answer. Under concurrency several projects can be
# awaiting free text at once, so the prompt is NAME-ECHOED (D5) — the operator can tell
# WHICH project the next plain message will resolve (the most-recently-armed is the
# default; reply-to-message / `/to <name>` override). The free-text prompt is the
# reply-to anchor: the relay maps that prompt's message_id -> tool_use_id, so a reply to
# it routes by id (an explicit disambiguation over the most-recent default).
#
# SB3/SB4: carries ONLY the (SB4-validated) project name + a fixed phrase — no event body
# (the question/plan text is never re-echoed here). Pure string; no I/O.

#: Pencil glyph for a free-text prompt (matches the "✏️ Other (free text)" button).
_FREE_TEXT_GLYPH: Final = "✏️"


def free_text_prompt(name: str) -> str:
    """Name-echoed prompt for a pending "Other" answer / plan-reject feedback (D5).

    ``✏️ <name>: reply with your answer…`` — so with several projects awaiting free text
    the operator knows WHICH project the next plain message (or a reply to THIS prompt)
    resolves (the most-recently-armed project is the default; a reply-to / ``/to <name>``
    overrides it). ``name`` is an SB4-validated project name (safe to interpolate); the
    phrase is fixed (SB3 — no event body). Pure string; no I/O.
    """
    return f"{_FREE_TEXT_GLYPH} {name}: reply with your answer…"


# ---------------------------------------------------------------------------
# Per-project status labels for /projects (P5 / ADR-005 D7) — pure label map
# ---------------------------------------------------------------------------
#
# T4 adds a per-project ``status`` enum on ``_ProjectRuntime`` and T7 renders a
# status column on ``/projects``. T3 provides ONLY the value→label mapping the
# column will consume. The enum VALUES below MUST match what T4/T7 set — they
# are the exact set fixed by design D7 / ADR-005 §D7:
#     idle · running · awaiting_approval · awaiting_answer · awaiting_plan · queued
# (``awaiting_*`` mirrors the three :data:`PendingKind`s the project can hold:
#  permission→awaiting_approval, ask→awaiting_answer, plan→awaiting_plan.)

#: Per-project status enum values (the keys T4 sets on ``_ProjectRuntime.status``; D7).
ProjectStatus = Literal[
    "idle",
    "running",
    "awaiting_approval",
    "awaiting_answer",
    "awaiting_plan",
    "queued",
]

#: Status enum value → human label for the ``/projects`` status column (D7). The labels
#: spell out the ``awaiting_*`` states (design D7 / T7 acceptance: "awaiting approval" /
#: "awaiting answer" / "awaiting plan"). Keep the keys in lock-step with T4's enum.
_STATUS_LABELS: Final[dict[str, str]] = {
    "idle": "idle",
    "running": "running",
    "awaiting_approval": "awaiting approval",
    "awaiting_answer": "awaiting answer",
    "awaiting_plan": "awaiting plan",
    "queued": "queued",
}

#: Fallback label for an unknown/missing status value (RB1 — a project with no runtime, or
#: a value T7 did not expect, never crashes the /projects render; it reads as ``idle``,
#: matching D7's "a project with no runtime defaults to idle").
_STATUS_FALLBACK: Final = "idle"


def project_status_label(status: object) -> str:
    """Map a per-project ``status`` enum value to its ``/projects`` column label (D7).

    Pure mapping, no I/O. ``running`` → ``"running"``, ``awaiting_approval`` →
    ``"awaiting approval"``, etc. Anything **not** a known value (an unexpected enum,
    ``None`` for a project with no runtime, a stray string) falls back to ``"idle"``
    (**RB1** — the status column never crashes on an unknown value, mirroring D7's
    "no runtime → idle" default). T4 sets the enum; T7 calls this to render the column.
    """
    if isinstance(status, str):
        return _STATUS_LABELS.get(status, _STATUS_FALLBACK)
    return _STATUS_FALLBACK


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


def _escape_html(text: str) -> str:
    """HTML-escape ``&`` / ``<`` / ``>`` for bot-scaffolding text in an HTML message.

    Used for the fixed bot-authored bits (e.g. an ask ``Header:`` label) that sit inside
    a ``parse_mode="HTML"`` message: they are NOT Markdown-converted, but a stray ``<`` /
    ``&`` would still break the message, so they are escaped. (Claude-authored prose goes
    through :func:`to_telegram_html` instead, which both escapes AND converts.)
    """
    return html.escape(text, quote=False)


def code_path(path: object) -> str:
    """Wrap a filesystem path in ``<code>…</code>`` for a ``parse_mode="HTML"`` reply (R6).

    Telegram auto-linkifies each ``/segment`` of a bare path in a bot message as a fake
    command-link (a cwd ``/tmp/p5verify/a`` renders as tappable ``/tmp`` ``/p5verify``
    ``/a`` "commands") — ugly and confusing. Wrapping the path in ``<code>`` makes Telegram
    render it as inert monospace instead. The path is HTML-escaped EXACTLY once here
    (``&`` ``<`` ``>``) so a path that contains those characters can't break the HTML
    message or inject a tag, so callers must pass the RAW path (never a pre-escaped one).
    The reply MUST be sent with ``parse_mode="HTML"`` or the literal ``<code>`` tags show.
    Pure string; no I/O.
    """
    return f"<code>{html.escape(str(path), quote=False)}</code>"


def _chunk(text: str, limit: int = TELEGRAM_MAX) -> tuple[str, ...]:
    """Split to Telegram-safe UTF-16 chunks (reuses :func:`split_message`)."""
    return tuple(split_message(text, limit=limit))


#: Starting budget for RAW prose chunks BEFORE HTML conversion. We chunk the raw markdown
#: under Telegram's 4096-UTF-16 limit first (at line boundaries, so a fenced code block is
#: not split mid-fence), THEN convert each chunk to HTML. HTML tags only ADD characters, so
#: a converted chunk can be larger than its raw source; ``_html_chunks`` re-splits (at a
#: smaller raw budget) any chunk that still overflows after conversion, so the final HTML
#: is always under 4096 while tags stay intact (we only ever re-split the RAW, never the
#: emitted HTML). ~3000 is a sensible first cut for ordinary prose.
_HTML_CHUNK_BUDGET = 3000

#: Floor for the raw budget while re-splitting an over-expanding chunk. Below this we stop
#: shrinking and accept the (still individually-sent) chunk — a pathological all-inline-code
#: paragraph would otherwise fragment endlessly. The send-path plain fallback (RB) is the
#: final backstop if such a rare chunk is still rejected.
_HTML_CHUNK_FLOOR = 256


#: A fenced code block in the RAW text (open fence + optional info line, body, close).
#: Mirrors ``tg_html._FENCE_RE`` but used here to pre-split an OVER-budget fence into
#: several complete fences so each survives chunking as its own valid ``<pre>``.
_RAW_FENCE_RE = re.compile(
    r"(?P<fence>```+|~~~+)[ \t]*(?P<lang>[^\n`~]*)\n(?P<body>.*?)\n?(?P=fence)",
    re.DOTALL,
)


def _presplit_big_fences(text: str, budget: int) -> str:
    """Rewrite any fenced block bigger than ``budget`` into several COMPLETE fences.

    A single huge ```` ```code``` ```` block (e.g. a large file dump) would otherwise be
    cut mid-fence by :func:`split_message`, leaving body fragments that convert to plain
    prose (or stray ``<code>``) instead of ``<pre>``. So before chunking we split such a
    block's BODY at line boundaries and re-wrap each piece in its own
    ```` ```lang\n<piece>\n``` ```` — the chunker then sees small *complete* fences, each
    of which converts to a proper, individually-valid ``<pre>``. Blocks already within
    budget are left untouched. Best-effort + pure (never raises).
    """

    def repl(match: re.Match[str]) -> str:
        whole = match.group(0)
        if _utf16(whole) <= budget:
            return whole
        fence = match.group("fence")
        lang = match.group("lang").strip()
        open_line = f"{fence}{lang}" if lang else fence
        # Reserve room for the open/close fence lines around each body piece.
        frame = _utf16(open_line) + 1 + _utf16(fence) + 1
        body_budget = max(_HTML_CHUNK_FLOOR, budget - frame)
        pieces = split_message(match.group("body"), limit=body_budget)
        return "\n".join(f"{open_line}\n{piece}\n{fence}" for piece in pieces)

    try:
        return _RAW_FENCE_RE.sub(repl, text)
    except Exception:
        return text


def _html_chunks(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Chunk RAW ``text``, then HTML-convert each chunk -> (html_chunks, plain_chunks).

    Order matters (per the task): split the *raw* markdown at line boundaries FIRST
    (reusing :func:`split_message`), THEN run each raw chunk through
    :func:`to_telegram_html`. This avoids splitting a fenced code block mid-block
    (``split_message`` breaks on newlines). An over-budget single fence is first
    re-written into several COMPLETE fences (:func:`_presplit_big_fences`) so each piece
    stays a valid ``<pre>``. Because conversion can still EXPAND a chunk past 4096 (e.g.
    many ``<code>`` spans), any converted chunk over the limit is re-split by halving its
    raw budget and re-converting — recursively, down to :data:`_HTML_CHUNK_FLOOR` — so
    every emitted HTML chunk is individually under Telegram's limit while its tags stay
    intact (we re-split the RAW, never the HTML).

    The two returned tuples are positionally parallel — ``plain_chunks[i]`` is the raw
    fallback for ``chunks[i]`` (T7 resends it with ``parse_mode=None`` if the HTML is
    rejected).
    """
    prepared = _presplit_big_fences(text, _HTML_CHUNK_BUDGET)
    html_out: list[str] = []
    plain_out: list[str] = []
    for raw in _chunk(prepared, limit=_HTML_CHUNK_BUDGET):
        _split_chunk(raw, _HTML_CHUNK_BUDGET, html_out, plain_out)
    return tuple(html_out), tuple(plain_out)


def _split_chunk(
    raw: str, budget: int, html_out: list[str], plain_out: list[str]
) -> None:
    """Convert ``raw`` to HTML; if the result overflows, re-split RAW at a smaller budget.

    Appends parallel (html, plain) pairs to the output lists. Recurses by halving
    ``budget`` until the converted chunk fits :data:`~claude_tg.util.TELEGRAM_MAX` or the
    budget hits :data:`_HTML_CHUNK_FLOOR` (then it is emitted as-is — the send-path plain
    fallback is the last resort for a pathological chunk).
    """
    converted = to_telegram_html(raw)
    if _utf16(converted) <= TELEGRAM_MAX or budget <= _HTML_CHUNK_FLOOR:
        html_out.append(converted)
        plain_out.append(raw)
        return
    smaller = max(_HTML_CHUNK_FLOOR, budget // 2)
    pieces = _chunk(raw, limit=smaller)
    if len(pieces) <= 1:
        # split_message could not break it further (one unbroken run) — emit as-is.
        html_out.append(converted)
        plain_out.append(raw)
        return
    for piece in pieces:
        _split_chunk(piece, smaller, html_out, plain_out)


def _utf16(text: str) -> int:
    """UTF-16 code-unit length — what Telegram counts against its 4096 limit."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def tool_use_line(event: ToolUseEvent) -> str:
    """One-liner for a tool call — the SB3-safe summary, wrapped ``<code>`` (HTML; T2/R6).

    Uses the event's already-body-free ``tool_input_summary`` (lengths-not-bodies, built by
    :func:`~claude_tg.engine.types.safe_input_summary`) — NEVER raw input, and this module
    never re-derives it (SB3). The summary is wrapped in ``<code>…</code>`` via
    :func:`code_path` so Telegram renders any path/command (its ``/segment`` runs) as inert
    monospace instead of a row of tappable fake command-links (the R6 fix, extended here to
    the tool-status line). ``code_path`` HTML-escapes the WHOLE summary exactly once, so a
    tool input carrying HTML metacharacters (a misaligned/injected Claude putting ``<b>`` /
    ``&`` / ``</code>`` in a ``file_path`` or ``command``) renders as inert text and can
    never break the message or inject markup. The ``▶️`` glyph is fixed bot scaffolding.
    The action carrying this line MUST be sent with ``parse_mode="HTML"`` (set by
    :func:`render_event`) or the literal ``<code>`` tags would show. **Invariant:** unlike
    the permission prompt (an ``op="new"`` send with a ``plain_chunks`` HTML→plain
    fallback), the status line goes out via ``_edit_status`` which has NO plain fallback —
    so this line MUST always be valid, balanced HTML. ``code_path``'s escape-everything-once
    guarantees that today; anything added here must preserve it (or add a fallback).
    """
    return f"▶️ {code_path(event.tool_input_summary)}"


#: Friendly, STABLE text for the noisy activity phases. Stability matters: consecutive
#: "still working" statuses render identical, so the coalescer's in-place edit is skipped
#: (no duplicate status messages) and the operator sees a calm "thinking…" line instead of
#: raw "connected · thinking_tokens".
_FRIENDLY_PHASE = {
    "init": "💭 Claude is starting…",
    "connected": "💭 Claude is thinking…",
    "thinking": "💭 Claude is thinking…",
    "disconnected": "🔌 Reconnecting…",
}


def status_line(event: StatusEvent) -> str:
    """One-liner for a lifecycle/health status event (no secrets).

    The common activity phases (init/connected/thinking) render as a calm, STABLE
    "Claude is thinking…" line so a burst of them coalesces to a single in-place line
    instead of spamming the chat; phases that carry actionable detail (e.g. ``rate_limit``)
    still surface it. The model name is intentionally dropped — it is noise to the operator
    and its variation would defeat the identical-line dedupe.
    """
    friendly = _FRIENDLY_PHASE.get(event.phase)
    if friendly is not None:
        return friendly
    emoji = _PHASE_EMOJI.get(event.phase, "ℹ️")
    bits = [f"{emoji} {event.phase}"]
    if event.detail:
        bits.append(event.detail)
    return " · ".join(bits)


#: Error kinds whose ``message`` wraps a RAW EXTERNAL body — tool stderr/stdout
#: (``tool_error``, from ``ToolResultBlock.content``) or SDK/CLI result text
#: (``turn_error``, from ``ResultMessage.result``). These can carry file contents or a
#: secret Claude just read, so their raw body is NEVER rendered to the chat (SB3 / H1 /
#: body-free). Everything NOT in this set is treated as a bot-AUTHORED safe message and
#: rendered readably (see :func:`error_is_raw_external`).
_RAW_EXTERNAL_ERROR_KINDS: Final[frozenset[str]] = frozenset({"tool_error", "turn_error"})

#: The fixed, body-free line shown in place of a raw external error body. It names the
#: project nowhere (the foreground render is project-agnostic — the chat thread already
#: scopes it) and points the operator at the local log for the detail. Public so the
#: one-shot reply path (``bot.py``) renders the SAME body-free line as streaming.
BODY_FREE_ERROR_LINE: Final = "the last step failed (details in the local log)"


def error_is_raw_external(event: ErrorEvent) -> bool:
    """Classify an :class:`ErrorEvent` for SB3 body-free rendering (P6/R3 · H1).

    Classification rule — by ``kind_of_error`` (the cleanest, audited discriminator,
    since each kind has a FIXED construction site, see ``adapter_sdk``):

    * ``tool_error``  — built from ``ToolResultBlock.content`` (a tool's raw
      stderr/stdout). RAW EXTERNAL → body-free.
    * ``turn_error``  — built from ``ResultMessage.result`` / ``.subtype`` (the SDK/CLI's
      raw turn-failure text). RAW EXTERNAL → body-free.
    * ``driver_error`` — bot-AUTHORED: ``adapter_sdk`` builds it as a fixed
      ``"send timed out after Ns"`` or a ``"<ExcType>: <exc>"`` diagnostic label (the
      timeout / transport-exception summary the owner explicitly wants to read). Returns
      ``False`` → rendered readably. (An exception's ``str`` is a Python error label, not
      a tool body / file content; if a future driver_error were ever sourced from raw
      external output it should be reclassified here.)

    Returns ``True`` iff the event's ``message`` must be treated as a raw external body
    (render body-free, log the raw detail only locally + scrubbed). **Fail-safe (SB3):**
    only the explicitly bot-authored ``driver_error`` is exempted; every other (incl. an
    UNKNOWN/unexpected) ``kind_of_error`` defaults to body-free — we never leak an
    unclassified body to the chat. ``_RAW_EXTERNAL_ERROR_KINDS`` documents the known
    raw-external kinds; the default-deny below covers anything unforeseen.
    """
    if event.kind_of_error == "driver_error":
        return False
    return True


def _render_error(event: ErrorEvent) -> RenderAction:
    """Render an :class:`ErrorEvent` — body-free for RAW EXTERNAL bodies (SB3 / H1).

    A ``tool_error`` / ``turn_error`` wraps a raw tool/SDK body that can carry file
    content or a secret, so it renders as a SAFE SUMMARY — the error KIND + a fixed
    generic line (:data:`BODY_FREE_ERROR_LINE`) — NOT the raw ``message``. The raw detail
    is written only to the LOCAL debug log (scrubbed) by the driver at the render call
    site; it never rides a Telegram send. A bot-authored ``driver_error`` (timeout /
    transport label) stays readable — it is safe and helpful UX.

    ``ErrorEvent.message`` still carries the raw text (untouched) so R5's ``_TurnDedup``
    can compare raw bodies for de-duplication; only what is RENDERED is body-free.
    """
    if error_is_raw_external(event):
        body = f"⚠️ {event.kind_of_error} — {BODY_FREE_ERROR_LINE}"
    else:
        body = f"⚠️ {event.kind_of_error}: {event.message}"
    return RenderAction(op="new", chunks=_chunk(body), verbatim=True)


def done_footer_suffix(event: ResultEvent) -> str:
    """The ``· N turns · $X.XX`` usage suffix for a done message (T3 / P9), or ``""``.

    Surfaces the SDK-provided usage the engine already carries on a
    :class:`~claude_tg.engine.types.ResultEvent` — ``num_turns`` + ``total_cost_usd`` —
    which the per-turn done render previously dropped whenever there was ``result_text``.
    Each field is included only WHEN the SDK provided it (``None`` → omitted gracefully —
    oneshot / a partial result may carry neither), so:

    * both present → ``" · 3 turns · $0.01"``
    * only turns   → ``" · 3 turns"``
    * neither      → ``""`` (no suffix at all — never a dangling separator).

    The leading ``" · "`` lets a caller append it straight onto a done line / the last
    prose chunk. The cost is rendered to cents (``$X.XX``) per the design; **no secret is
    in this line** (SB3 — it is two numbers the SDK reported, never tool input/output).
    Pure string; no I/O.
    """
    bits: list[str] = []
    if event.num_turns is not None:
        # Pluralize: "1 turn" (singular) vs "N turns" — never the ungrammatical "1 turns".
        unit = "turn" if event.num_turns == 1 else "turns"
        bits.append(f"{event.num_turns} {unit}")
    if event.total_cost_usd is not None:
        bits.append(f"${event.total_cost_usd:.2f}")
    if not bits:
        return ""
    return " · " + " · ".join(bits)


def _render_result(event: ResultEvent) -> RenderAction:
    # Terminal per-turn frame. The result_text (if any) is the final answer — it is
    # Claude-authored CommonMark, so render it as Telegram HTML (with a raw fallback);
    # otherwise a compact, bot-generated status footer stays plain text.
    #
    # T3 (P9): surface the SDK-provided usage (num_turns + total_cost_usd) the done frame
    # used to drop whenever there was result_text. The ``· N turns · $X.XX`` suffix
    # (done_footer_suffix; "" when the SDK gave neither — oneshot may not) is appended to
    # the LAST prose chunk so the answer ends with a compact, secret-free usage line. The
    # suffix is plain bot scaffolding (digits + glyph) so it is HTML-safe to append onto the
    # converted HTML chunk; the parallel plain fallback gets it too (positionally parallel).
    if event.result_text:
        html_chunks, plain = _html_chunks(event.result_text)
        suffix = done_footer_suffix(event)
        if suffix and html_chunks:
            html_chunks = (*html_chunks[:-1], html_chunks[-1] + suffix)
            plain = (*plain[:-1], plain[-1] + suffix)
        return RenderAction(
            op="new",
            chunks=html_chunks,
            plain_chunks=plain,
            parse_mode="HTML",
            verbatim=True,
        )
    body = f"✅ done ({event.subtype})" + done_footer_suffix(event)
    return RenderAction(op="new", chunks=_chunk(body), verbatim=True)


def render_event(event: Event) -> RenderAction:
    """Map ONE normalized event to a :class:`RenderAction` (pure; no I/O).

    The verbatim-vs-one-liner split (design FR4 / render table):

    * ``ask``  -> verbatim message body (the questions, chunked) + the option keyboard.
    * ``plan`` -> verbatim plan text (chunked) + ``[Approve]``/``[Reject+feedback]``.
    * ``permission`` -> verbatim prompt (tool name + body-free summary, chunked) +
      the ``[Allow once]``/``[Allow for session]``/``[Deny]`` keyboard (ADR-003 §2).
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
        # Bot-scaffolding header (kept verbatim) + Claude-authored plan body (HTML). The
        # header stays as-is but is HTML-escaped so it is valid inside the HTML message;
        # the plan text is CommonMark-converted. Chunk the RAW header+plan first, then
        # convert, so the keyboard rides the first chunk and a long plan stays valid HTML.
        header = "📋 Proposed plan — Approve or Reject with feedback:\n\n"
        html_chunks, plain = _html_chunks(header + event.plan)
        return RenderAction(
            op="new",
            chunks=html_chunks,
            plain_chunks=plain,
            parse_mode="HTML",
            reply_markup=plan_keyboard(event),
            verbatim=True,
        )

    if isinstance(event, PermissionEvent):
        # The operator's approve/deny surface. The (body-free) summary is wrapped in <code>
        # so its path/command renders monospace, not /segment fake-links (T2/R6); the prose +
        # tool name are HTML-escaped (the tool name is attacker-influenceable too). It is thus
        # an HTML message; the plain body rides along as the raw fallback T7 resends if
        # Telegram ever rejects the HTML (so the prompt is never dropped — a dropped prompt is
        # a worse bug than plain text). code_path/_escape_html keep the HTML valid for any input.
        return RenderAction(
            op="new",
            chunks=_chunk(_render_permission_body_html(event)),
            plain_chunks=_chunk(_render_permission_body(event)),
            parse_mode="HTML",
            reply_markup=permission_keyboard(event),
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
        # Assembled assistant prose is real content -> its own message, chunked. It is
        # Claude-authored CommonMark, so render as Telegram HTML with a raw fallback.
        if not event.text:
            return RenderAction.none()
        html_chunks, plain = _html_chunks(event.text)
        return RenderAction(
            op="new",
            chunks=html_chunks,
            plain_chunks=plain,
            parse_mode="HTML",
            verbatim=True,
        )

    if isinstance(event, ToolUseEvent):
        # The tool-status line wraps its (body-free) summary in <code> (T2/R6 — stop the
        # /segment auto-linkify), so it is an HTML status edit. The Coalescer carries this
        # parse_mode forward with the line's text (newest-wins), and _edit_status passes it
        # to the send/edit; code_path's html.escape keeps the HTML valid for any input.
        return RenderAction(
            op="edit_status", chunks=(tool_use_line(event),), parse_mode="HTML"
        )

    if isinstance(event, StatusEvent):
        # Lifecycle/health line carries no path → stays PLAIN (parse_mode=None). It shares the
        # coalesced status slot with the HTML tool-use line, but text+parse_mode travel
        # together (newest-wins), so a plain status replacing an HTML tool line correctly
        # carries parse_mode=None — the slot never mixes a stale parse_mode with new text.
        return RenderAction(op="edit_status", chunks=(status_line(event),))

    # Unknown/foreign event — render nothing rather than crash (RB1 spirit).
    return RenderAction.none()


def _render_permission_body(event: PermissionEvent) -> str:
    """Verbatim prompt body for a held risky tool (ADR-003 §2; SB3 body-free).

    Built from ``tool_name`` + the **already-body-free** ``tool_input_summary`` (the
    engine's :func:`~claude_tg.engine.types.safe_input_summary` produced it — lengths,
    not contents). This module does **not** re-summarize or expand it: re-deriving a
    summary here would risk surfacing a raw body (a Write's ``content``, a Bash secret),
    so the SB3-safe string is rendered exactly as given. The keyboard (built separately)
    is the verdict surface.
    """
    return (
        f"🔐 Permission needed — Claude wants to run {event.tool_name}:\n"
        f"{event.tool_input_summary}\n\n"
        "Allow once, allow for this session, or deny?"
    )


def _render_permission_body_html(event: PermissionEvent) -> str:
    """HTML version of :func:`_render_permission_body` (the live send path; T2/R6).

    Same content as the plain body — the fixed prose + the tool name + the
    **already-body-free** ``tool_input_summary`` (lengths-not-bodies; this module does NOT
    re-summarize or expand it, SB3) — but rendered for ``parse_mode="HTML"``:

    * the summary is wrapped in ``<code>…</code>`` (via :func:`code_path`) so its path /
      command renders as inert monospace, not tappable ``/segment`` fake-links (R6, extended
      to the permission prompt);
    * the fixed prose and the (attacker-influenceable) ``tool_name`` are HTML-escaped (via
      :func:`_escape_html`) so a tool name carrying ``<`` / ``&`` can't break the message.

    Every interpolated field is escaped exactly once, so the body is valid HTML for ANY tool
    input — a hostile ``file_path``/``command`` (``</code><b>…`` etc.) renders as inert text,
    never markup, and the prompt always sends (Telegram rejecting invalid HTML would mean the
    operator never sees the approve/deny prompt — a worse failure). The plain
    :func:`_render_permission_body` is the parallel raw fallback T7 resends on an HTML rejection.
    """
    return (
        f"🔐 Permission needed — Claude wants to run {_escape_html(event.tool_name)}:\n"
        f"{code_path(event.tool_input_summary)}\n\n"
        "Allow once, allow for this session, or deny?"
    )


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
    #: parse_mode of the buffered line — travels WITH ``text`` (newest-wins). A tool-use
    #: line is HTML (its summary is <code>-wrapped, T2/R6); a lifecycle status is plain.
    #: Carrying it here is load-bearing: ``_emit_status`` rebuilds the RenderAction, so
    #: without this the HTML tool line would emit with parse_mode=None and its ``<code>``
    #: tags would render literally. Replaced atomically with ``text`` so the slot never
    #: pairs a stale parse_mode with new text.
    parse_mode: ParseMode = None
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
        # Carry the buffered line's parse_mode (HTML for a <code>-wrapped tool-use line, None
        # for a plain lifecycle status) — without it T7 would send the HTML line as plain text
        # and the <code> tags would show literally (T2/R6).
        return RenderAction(
            op="edit_status",
            chunks=(self._pending.text,),
            parse_mode=self._pending.parse_mode,
        )

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

        # op == "edit_status": coalesce. The status line is a REPLACE (newest wins) — its
        # text AND parse_mode are replaced together so a plain status that replaces an HTML
        # tool-use line carries the right (plain) parse_mode, and vice versa.
        self._pending.text = action.text
        self._pending.parse_mode = action.parse_mode
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


# ---------------------------------------------------------------------------
# Per-chat send-rate gate (RB5 under concurrency, P5 / ADR-005 D8)
# ---------------------------------------------------------------------------
#
# The Coalescer (above) throttles ONE project's status line. Under concurrency (P5)
# N projects in one chat each run their own Coalescer, so a status burst in A never
# resets B's throttle — but N projects flushing at once (plus the proactive
# notifications, D4) can still burst PAST Telegram's ~1 msg/s/chat ceiling. The
# ChatSendGate is the per-chat backstop: ALL outbound for a chat (every status edit,
# verbatim message, and notification) funnels through it, and it spaces sends at a
# minimum interval so the COMBINED cross-project rate stays bounded.
#
# Like the Coalescer it is a PURE class over an INJECTED clock: it only *decides* how
# long to wait before a send may proceed (the actual awaiting stays in the session/bot,
# exactly as the Coalescer leaves the real edit-waiting to T7). So it is unit-testable
# with no real sleeps.
#
# **Verbatim is PRIORITY over coalesced status churn (the load-bearing D8 rule).** A
# starved status line is fine (it is noise — the newest wins); a starved ask/plan/error/
# result is NOT (the operator can't answer a prompt they never receive — a deadlock).
# So the gate gives verbatim sends precedence: a verbatim is spaced off the last ACTUAL
# send, and a backlog of FUTURE-dated status reservations does NOT advance that cursor —
# so a verbatim arriving amid K coalesced status edits waits ~1 interval off the last real
# send, NOT K×interval at the back of the backlog. (Spacing verbatim off the gate's running
# tail instead would land it behind all K — the exact starvation D8 forbids.) Status edits
# space off the status tail (so they stay ≥interval apart) and a verbatim pushes that tail
# to its own slot, so the next status falls in BEHIND the verbatim. Nothing is ever dropped
# — the gate ORDERS sends (returns a wait), it never discards a body (RB6/SB3: it does not
# touch message content at all); a verbatim is merely inserted ahead of the pending status
# tail (in that rare case one status may share the verbatim's interval — the accepted cost
# of never starving a prompt). It gates SENDS, never the resolve path (taking it on a
# resolve would deadlock a held turn — the session only ever consults it around outbound
# I/O).


class ChatSendGate:
    """Bound a single chat's COMBINED send/edit rate under concurrency (RB5/D8).

    All outbound for a chat (status edits, verbatim messages, notifications) calls
    :meth:`reserve` to learn how long to wait before sending; the caller does the actual
    awaiting (the gate, like :class:`Coalescer`, decides timing only — no real sleep). A
    minimum ``interval`` between sends keeps the combined cross-project rate under
    Telegram's ~1 msg/s/chat ceiling even when N concurrent projects flush at once.

    The **clock is injected** (``now: Callable[[], float]`` monotonic seconds) so tests
    advance time deterministically.

    Verbatim sends are **priority** (``reserve(verbatim=True)``): they are spaced only off
    the last *actual* send, and a backlog of future-dated status reservations does NOT
    advance that cursor — so a verbatim arriving amid K coalesced status edits waits ~1
    interval off the last real send, never K×interval at the back of the backlog (the D8
    invariant — a starved status line is acceptable noise, a starved prompt is a deadlock).
    Status edits (``verbatim=False``) space off the status tail and yield to verbatim (a
    verbatim pushes the tail to its own slot, so the next status falls in behind it). Either
    way :meth:`reserve` returns a non-negative wait and **never drops** a send — it only
    orders them.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float],
        interval: Optional[float] = None,
    ) -> None:
        chosen = DEFAULT_CHAT_SEND_INTERVAL if interval is None else interval
        if chosen < 0:
            raise ValueError("interval must be non-negative")
        self._now = now
        self._interval = float(chosen)
        # Scheduled time of the last ACTUAL send — a verbatim, or a status edit that fired
        # at the leading edge (scheduled ≤ now, i.e. it went out immediately). A verbatim
        # spaces off THIS (+interval) so it lands ~1 interval off the last real send and
        # JUMPS AHEAD of any status reserved AFTER it (the D8 priority — a prompt must reach
        # the operator; subsequent status churn must not bury it). A future-dated (queued)
        # status does NOT advance this cursor.
        self._last_actual: float = float("-inf")
        # The COMBINED running tail: the latest slot reserved by ANY send (verbatim or
        # status). Every new reservation lands strictly ≥interval after a colliding slot, so
        # NO two sends ever share an interval — this is what bounds the COMBINED per-chat
        # send rate regardless of how verbatim/status interleave (round-3 cross-model-QA
        # BLOCKER 3). The pre-fix gate tracked only a separate status tail, so a verbatim
        # spaced off ``_last_actual`` could land on a slot a status had ALREADY reserved at
        # the same future time → two sends fired in one interval (over-budget under churn).
        self._tail: float = float("-inf")

    def reserve(self, *, verbatim: bool) -> float:
        """Reserve the next send slot; return the wait (seconds, ≥0) before it may go.

        The hard invariant (round-3 cross-model-QA BLOCKER 3): **no two reserved sends ever
        share an interval** — every send, verbatim or status, lands in its OWN ≥interval-
        spaced slot, so the COMBINED per-chat send rate stays bounded under any interleaving.
        The :attr:`_tail` (the latest slot reserved by any kind) enforces it: a new
        reservation that would fall at/within an interval of an already-reserved slot is
        pushed to ``_tail + interval``.

        Within that hard collision-free bound, **verbatim keeps priority** over status:

        * ``verbatim=True`` (a final answer / error / ask / plan / permission / notification)
          targets ``_last_actual + interval`` — ~1 interval off the last ACTUAL send, NOT off
          the (future-dated) status tail — so it JUMPS AHEAD of every status reserved AFTER
          it (the deadlock-prevention case: a prompt is never buried behind subsequent status
          churn). If that target collides with an already-reserved slot (status was reserved
          ahead at that exact time), it is pushed to the next free slot (``_tail + interval``)
          — it cannot leapfrog a send whose fire-time was ALREADY committed to a waiting
          caller (un-scheduling that send is impossible), but it is still ahead of all future
          status. It then advances ``_last_actual`` so following status falls in behind it.
        * ``verbatim=False`` (a status-line edit) spaces off the combined tail (+interval) and
          YIELDS to verbatim; a status that fires at the leading edge (scheduled ≤ now) IS an
          actual send and so also advances ``_last_actual``.

        **Nothing is ever dropped** — the gate only ORDERS sends (returns a wait); it never
        discards a body and never touches message content (RB6/SB3). Pure decision (no I/O,
        no sleep): the caller awaits the returned delay then sends.
        """
        now = self._now()
        if verbatim:
            # Priority: ~1 interval off the last ACTUAL send (jumps ahead of FUTURE status).
            scheduled = max(now, self._last_actual + self._interval)
            # Collision-free: never share a slot with an already-reserved send. If a status
            # was reserved ahead at/within this slot, take the next free slot instead (we
            # cannot un-schedule a send already handed to a waiting caller). Still ahead of
            # any status reserved after this point.
            if scheduled <= self._tail:
                scheduled = self._tail + self._interval
            # A verbatim is always an actual send; the next verbatim + following status space
            # off it.
            self._last_actual = scheduled
        else:
            # Status spaces off the combined tail, so it stays ≥interval from EVERY prior
            # send (verbatim or status) — never colliding, always yielding to a verbatim that
            # advanced the tail ahead of it.
            scheduled = max(now, self._tail + self._interval)
            # A leading-edge status (goes immediately) IS a real send a following verbatim
            # must space off; a FUTURE-dated (queued) status must NOT advance _last_actual —
            # that is precisely what would otherwise push a verbatim to the back of the churn.
            if scheduled <= now:
                self._last_actual = scheduled
        # Advance the combined tail (monotonic) so the NEXT send of either kind is spaced off
        # this one — the collision-free guarantee.
        self._tail = max(self._tail, scheduled)
        wait = scheduled - now
        return wait if wait > 0 else 0.0


#: Default minimum interval between sends through a :class:`ChatSendGate` (seconds).
#: Telegram's practical per-chat send ceiling is ~1 msg/s; 1 s is the conservative
#: per-chat budget for the COMBINED cross-project rate (the per-project status
#: Coalescer already uses a larger ``DEFAULT_MIN_EDIT_INTERVAL`` for its own line).
#: Configurable via ``RENDER_CHAT_SEND_INTERVAL_SECONDS`` (Config, P5/T8).
DEFAULT_CHAT_SEND_INTERVAL = 1.0


__all__ = [
    # action
    "RenderAction",
    "RenderOp",
    "render_event",
    "error_is_raw_external",
    "BODY_FREE_ERROR_LINE",
    # keyboards + codec
    "ask_keyboard",
    "ask_question_body",
    "ask_question_body_html",
    "ask_question_keyboard",
    "plan_keyboard",
    "permission_keyboard",
    "open_project_keyboard",
    "encode_callback",
    "encode_switch_callback",
    "decode_callback",
    "Callback",
    "answers_from_ask",
    "strip_telegram_html",
    "to_telegram_html",
    "CALLBACK_LIMIT",
    "KIND_ASK",
    "KIND_OTHER",
    "KIND_PLAN",
    "KIND_PERMISSION",
    "KIND_SWITCH",
    "SWITCH_PAYLOAD",
    "PERMISSION_ONCE",
    "PERMISSION_SESSION",
    "PERMISSION_DENY",
    # smart-reply chips (T6/P9)
    "quick_reply_keyboard",
    "quick_reply_dismiss",
    # one-liners
    "tool_use_line",
    "status_line",
    # /yolo loud indicator (D6)
    "yolo_banner",
    "yolo_indicator",
    # proactive background-project notifications (D4)
    "notify_attention",
    "notify_done",
    "notify_error",
    "queued_suffix",
    # free-text prompt (name-echoed; D5)
    "free_text_prompt",
    # per-project status labels for /projects (D7)
    "ProjectStatus",
    "project_status_label",
    # coalesce / throttle
    "Coalescer",
    "FlushResult",
    "coalesce_stream",
    "DEFAULT_MIN_EDIT_INTERVAL",
    # per-chat send-rate gate (RB5 under concurrency, D8)
    "ChatSendGate",
    "DEFAULT_CHAT_SEND_INTERVAL",
]
