"""Normalized engine types — the "events out / decisions in" contract.

Implements `spikes/session-substrate/normalized_interface.md` §1 (events out) and
§2 (decisions in) as plain dataclasses, decoupled from any substrate. Production
code never imports the spike; these types are the real, substrate-neutral contract
the engine speaks (the spike only *drafted* it).

Two families live here:

* **Events out** (engine -> bot): seven kinds, each carrying ``kind`` (a stable
  discriminator) and ``session_id`` (``None`` until the substrate reports it). The
  session id rides every event from day one so P4/P5 can extend, not rewrite, the
  correlation envelope (design "Future" note).
* **Decisions in** (bot -> engine): five kinds, plus ONE load-bearing helper,
  :func:`decision_to_substrate`, that maps a decision onto the substrate's
  per-request allow/deny primitive. Keeping that mapping in a single place is
  deliberate: it is where every empirical **[FLAG]** from the contract is honored
  (native ``answers`` map keyed by question text; plan reject rides the deny
  message; allow always carries ``updated_input``).

The substrate-facing result of that mapping is :class:`SubstrateDecision` — an
adapter-neutral ``{allow, updated_input, message}`` triple. The SDK adapter turns
it into ``PermissionResultAllow``/``PermissionResultDeny``; a future B adapter would
turn it into a ``control_response``. Neither shape leaks into this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

# ---------------------------------------------------------------------------
# Events out (engine -> bot) — normalized_interface.md §1
# ---------------------------------------------------------------------------
#
# Every event carries:
#   * ``kind``       — a stable string discriminator (matches the §1 names), so a
#                      consumer can branch without isinstance chains and so the
#                      wire shape is stable across refactors.
#   * ``session_id`` — the Claude session id, or None before it is known. Captured
#                      from SystemMessage(init)/ResultMessage by the adapter.


@dataclass(frozen=True)
class TextEvent:
    """Assistant prose / model reply chunk (§1 ``text``)."""

    text: str
    incremental: bool = False  # True for a token-delta, False for an assembled block
    turn_id: str | None = None
    session_id: str | None = None
    kind: Literal["text"] = "text"


@dataclass(frozen=True)
class ThinkingEvent:
    """Claude's extended-thinking / reasoning text (P12 T-THINK; SB3).

    The model's reasoning surfaced for **live supervision** — rendered as a capped,
    collapsed ``🧠`` status line that updates in place and is cleared at turn end (never a
    permanent message; see :func:`~claude_tg.render.render_event`). Parallel to
    :class:`TextEvent`: ``incremental=True`` for a ``thinking_delta`` stream chunk,
    ``incremental=False`` for an assembled ``ThinkingBlock`` (in an ``AssistantMessage``).

    **SB3 — two hard rules the adapter enforces and this type makes structurally true:**

    * **The opaque ``signature`` is NEVER carried here.** The SDK ``ThinkingBlock`` has a
      ``signature: str`` (an opaque crypto signature) and the stream emits a separate
      ``signature_delta``; both are DROPPED by :func:`~claude_tg.engine.adapter_sdk.normalize`
      and never reach this event. There is deliberately no ``signature`` field — a
      ``ThinkingEvent`` can only carry reasoning ``text``, so a signature cannot leak through
      it even by mistake.
    * **Redacted thinking renders OPAQUE, never raw.** ``redacted=True`` marks a thinking
      block the API encrypted (the SDK has no ``RedactedThinkingBlock`` class and its parser
      drops ``redacted_thinking`` silently, so this is a defensive fail-safe). A redacted
      event carries NO reasoning text (``text=""``); the renderer shows a fixed
      ``🧠 (reasoning hidden)`` line and never the (absent) body.
    """

    text: str
    incremental: bool = False  # True for a thinking_delta, False for an assembled block
    redacted: bool = False  # True → opaque "reasoning hidden" line; NEVER raw (SB3)
    session_id: str | None = None
    kind: Literal["thinking"] = "thinking"


@dataclass(frozen=True)
class ToolUseEvent:
    """The model is about to use a tool (§1 ``tool_use``).

    Carries a *summary* of the input (lengths/keys, not bodies) — never the raw
    tool input, per the §1 "log lengths, not bodies" safe-rendering note (SB3).
    """

    tool_name: str
    tool_input_summary: str
    tool_use_id: str | None = None
    session_id: str | None = None
    kind: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True)
class AskEvent:
    """An ``AskUserQuestion`` surfaced for a multiple-choice answer (§1 ``ask``).

    ``questions`` mirrors the substrate schema: a list of
    ``{question, header, options: [{label, description}], multiSelect}`` dicts.
    The answer is delivered via the native ``answers`` map keyed by question text
    (see :class:`QuestionAnswer` / :func:`decision_to_substrate`).
    """

    questions: list[dict[str, Any]]
    tool_use_id: str | None = None
    session_id: str | None = None
    kind: Literal["ask"] = "ask"


@dataclass(frozen=True)
class PlanEvent:
    """A proposed ``ExitPlanMode`` plan for approve/reject (§1 ``plan``)."""

    plan: str
    tool_use_id: str | None = None
    session_id: str | None = None
    kind: Literal["plan"] = "plan"


@dataclass(frozen=True)
class PermissionEvent:
    """A risky tool held for operator approval (P2, ADR-003 §2/§4).

    The engine injects this onto the outgoing stream when a tool the policy reports
    as needing approval is requested — mirroring how :class:`AskEvent`/:class:`PlanEvent`
    surface the interactive prompts — so the operator SEES the
    ``[Allow once] / [Allow for session] / [Deny]`` choice it must make, correlated by
    ``tool_use_id``. The verdict comes back as a :class:`PermissionDecision` routed to
    the held :class:`~claude_tg.engine.pending.PendingRegistry` request (T4 renders the
    prompt; T5 routes the tap).

    ``tool_input_summary`` is **body-free** (lengths / short values, NOT raw contents,
    SB3) — built by :func:`safe_input_summary`. The raw tool input never rides this
    event, so a Write's file contents or a Bash secret are never surfaced or logged.

    **P13 T-BASH — ``bash_flag`` / ``bash_flag_label`` (the Bash-policy escalation).** When
    the Bash command policy matches a dangerous command in ``flag`` mode, the engine sets
    ``bash_flag=True`` (and ``bash_flag_label`` to the matched pattern's short, body-free
    label, e.g. ``"git force-push (can overwrite remote history)"``). The renderer then shows
    a ``⚠️`` warning + the label and **omits the ``[Allow for session]`` button** — so the
    only way through is a deliberate one-time ``[Allow once]`` (a flagged command can never be
    session-granted, and is re-prompted even under a prior grant / ``/yolo``). Both default to
    the unflagged values (``False`` / ``None``) so every existing :class:`PermissionEvent` and
    its render are byte-for-byte unchanged. The label is body-free (a fixed pattern label, NOT
    the command body — the command text the operator sees is the existing 160-char
    ``tool_input_summary``), so SB3 still holds.
    """

    tool_name: str
    tool_input_summary: str
    tool_use_id: str
    session_id: str | None = None
    kind: Literal["permission"] = "permission"
    # P13 T-BASH: set True when the Bash policy flagged this command (flag mode) — the render
    # shows ⚠️ + the matched pattern and DROPS [Allow for session] (deliberate one-time only).
    bash_flag: bool = False
    # P13 T-BASH: the matched pattern's short, body-free label (None when not flagged). NEVER
    # the raw command — that stays in the existing body-free tool_input_summary (SB3).
    bash_flag_label: str | None = None


# Error origin: a tool failed, the turn failed, or the substrate/driver failed.
ErrorKind = Literal["tool_error", "turn_error", "driver_error"]


@dataclass(frozen=True)
class ErrorEvent:
    """A tool/turn/driver failure — render clean, never hang (§1 ``error`` / RB2)."""

    kind_of_error: ErrorKind
    message: str
    is_error: bool = True
    tool_use_id: str | None = None
    session_id: str | None = None
    kind: Literal["error"] = "error"


@dataclass(frozen=True)
class ResultEvent:
    """Terminal per-turn frame carrying the session id to persist (§1 ``result``)."""

    session_id: str | None
    is_error: bool
    subtype: str
    num_turns: int | None = None
    total_cost_usd: float | None = None
    result_text: str | None = None
    kind: Literal["result"] = "result"


# Non-content lifecycle / health phases.
StatusPhase = Literal["init", "connected", "disconnected", "rate_limit"]


@dataclass(frozen=True)
class StatusEvent:
    """Non-content lifecycle/health signal (§1 ``status``)."""

    phase: StatusPhase
    session_id: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    permission_mode: str | None = None
    detail: str | None = None  # e.g. rate-limit detail
    kind: Literal["status"] = "status"


#: Discriminated union of every event the engine emits.
Event = Union[
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
    AskEvent,
    PlanEvent,
    PermissionEvent,
    ErrorEvent,
    ResultEvent,
    StatusEvent,
]


# Fields whose values are free-text bodies — collapse to a length, never dump (SB3).
_BODY_FIELDS = frozenset({"content", "new_string", "old_string"})
# Fields that are path/command/url-like — short, useful, truncated (never a body).
_IDENT_FIELDS = frozenset({"file_path", "path", "command", "pattern", "url"})


def safe_input_summary(tool_name: str, tool_input: dict[str, Any] | None) -> str:
    """Render a tool's input WITHOUT dumping bodies — lengths, not content (SB3).

    A **pure**, SDK-free mirror of ``adapter_sdk._safe_input_summary``'s lengths-not-
    bodies style, kept here so the engine can build a :class:`PermissionEvent` summary
    without importing the adapter (the engine is substrate-neutral). Large free-text
    fields (``content`` / ``new_string`` / ``old_string``) collapse to a ``<N chars>``
    count; path/command/url-like fields are truncated; everything else is short. The
    raw body of a sensitive field is therefore NEVER present in the returned string —
    a Write's ``content`` shows ``content=<500 chars>``, not the 500 characters.
    """
    if not isinstance(tool_input, dict):
        return f"{tool_name}({str(tool_input)[:80]})"
    parts: list[str] = []
    for k, v in tool_input.items():
        if k in _BODY_FIELDS:
            parts.append(f"{k}=<{len(str(v))} chars>")
        elif k in _IDENT_FIELDS:
            parts.append(f"{k}={str(v)[:160]}")
        else:
            parts.append(f"{k}={str(v)[:40]}")
    return f"{tool_name}({', '.join(parts)})"


# ---------------------------------------------------------------------------
# Decisions in (bot -> engine) — normalized_interface.md §2
# ---------------------------------------------------------------------------

# Allow/deny scope. "once" = this request only; "session" = remember the verdict
# for matching subsequent requests. Per §2 [FLAG], the substrate primitive is ALWAYS
# the per-request verdict; "session" is engine-side state (T5), so the substrate
# mapping below treats once/session identically — scope is carried for the engine,
# not the wire.
PermissionScope = Literal["once", "session"]


@dataclass(frozen=True)
class PermissionVerdict:
    """A per-tool permission decision (§2 ``permission verdict``).

    ``allow`` MAY carry ``updated_input`` (modified-input channel); ``deny`` MAY
    carry a ``message`` (reason). ``scope`` is engine-side (see above).
    """

    behavior: Literal["allow", "deny"]
    updated_input: dict[str, Any] | None = None
    message: str | None = None
    scope: PermissionScope = "once"


@dataclass(frozen=True)
class QuestionAnswer:
    """An ``AskUserQuestion`` answer — the native ``answers`` map (§2 ⭐).

    ``answers`` is keyed by **verbatim question text** -> selected option label
    (multi-select = comma-separated labels in one string). This is the proven
    native path: it rides the ALLOW channel as ``updated_input["answers"]``.
    """

    answers: dict[str, str]


@dataclass(frozen=True)
class PlanVerdict:
    """An ``ExitPlanMode`` verdict (§2 ``plan verdict``).

    ``approve`` -> allow. Reject (``approve=False``) -> deny; the optional
    ``feedback`` rides the **deny message** channel — there is NO native
    plan-feedback field ([FLAG], schema-confirmed).
    """

    approve: bool
    feedback: str | None = None


@dataclass(frozen=True)
class FreeTextReply:
    """An ordinary mid-session operator message (§2 ``free-text reply``).

    Not an answer to a tool prompt — the same seam as ``send`` (§3); carried here
    so the decisions-in surface is complete and the engine can route it.
    """

    text: str


@dataclass(frozen=True)
class Cancel:
    """Abort a waiting/in-flight run cleanly (§2 ``cancel`` / RB4)."""


# The canned denial relayed to the model when the operator denies a tool call
# (ADR-003 §2, D5 — a fixed message the model adapts to; NO free-text reason in P2).
DENIED_MESSAGE = "Operator denied this tool call."


@dataclass(frozen=True)
class PermissionDecision:
    """The operator's verdict on a held risky-tool prompt (P2, ADR-003 §2, D5).

    Carried in from the bot (T5) and routed to the held
    :class:`~claude_tg.engine.pending.PendingRegistry` request the engine opened in
    ``Engine._permission_hold``. The engine maps the three verdicts:

    * ``allow_once`` -> allow this request only (the next use of the tool re-asks).
    * ``allow_session`` -> record an engine-side per-tool-NAME grant **then** allow, so
      subsequent uses of that tool name this session auto-allow with no prompt (D4).
    * ``deny`` -> a substrate deny carrying :data:`DENIED_MESSAGE` (D5).

    This is distinct from :class:`PermissionVerdict` (the lower-level allow/deny the
    substrate mapper speaks): a ``PermissionDecision`` is the *operator-facing* tap
    (it knows about allow-once vs allow-session, which is engine-side state, ADR-001);
    the engine translates it into a :class:`PermissionVerdict` after recording any grant.
    """

    verdict: Literal["allow_once", "allow_session", "deny"]


#: Discriminated union of every decision the bot can route in.
Decision = Union[
    PermissionVerdict,
    PermissionDecision,
    QuestionAnswer,
    PlanVerdict,
    FreeTextReply,
    Cancel,
]


# ---------------------------------------------------------------------------
# The load-bearing mapping: decision -> substrate per-request verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubstrateDecision:
    """Adapter-neutral result of answering one permission/interactive request.

    The substrate primitive on BOTH A and B is a per-request allow/deny:
      * allow -> ``{behavior:"allow", updated_input/updatedInput:<record>}``
      * deny  -> ``{behavior:"deny",  message:<reason>}``

    This triple is the neutral form; each adapter renders it to its own type
    (SDK: ``PermissionResultAllow``/``PermissionResultDeny``; B: ``control_response``).

    [FLAG] ``updated_input`` is ALWAYS a dict on allow — never ``None`` — because an
    allow on substrate B must carry ``updatedInput`` as a record or the tool fails
    with a ZodError. Callers building an allow MUST pass the (possibly unchanged)
    original tool input as the base; the helper below guarantees a dict.
    """

    allow: bool
    updated_input: dict[str, Any] | None = None
    message: str | None = None

    @classmethod
    def make_allow(
        cls, updated_input: dict[str, Any] | None = None
    ) -> "SubstrateDecision":
        # Guarantee a record on allow (the B updatedInput gotcha; mirrors the SDK).
        return cls(allow=True, updated_input=dict(updated_input or {}), message=None)

    @classmethod
    def make_deny(cls, message: str | None = None) -> "SubstrateDecision":
        return cls(allow=False, updated_input=None, message=message or "")


def decision_to_substrate(
    decision: Decision,
    *,
    tool_input: dict[str, Any] | None = None,
) -> SubstrateDecision:
    """Map a decision-in onto the substrate's per-request allow/deny primitive.

    This is THE single place the contract's [FLAG] behaviors are encoded — keep all
    decision->verdict logic here so there is one audited mapping:

    * :class:`PermissionVerdict` ``allow`` -> allow carrying ``updated_input`` (its
      own, else the original ``tool_input``); ``deny`` -> deny carrying ``message``.
    * :class:`QuestionAnswer` -> **allow** whose ``updated_input`` is
      ``{**tool_input, "answers": {question_text: label}}`` — the native ``answers``
      map keyed by question text (the proven C3 path).
    * :class:`PlanVerdict` ``approve`` -> allow; reject -> **deny** whose ``message``
      is the feedback (no native plan-feedback field — feedback rides deny).
    * :class:`Cancel` -> deny (clean abort of the pending request; the full
      disconnect/cancel lifecycle is the engine's ``stop()``).
    * :class:`FreeTextReply` is NOT a permission/interactive verdict — it is a new
      turn (``send``), so it has no substrate-decision form; calling this with one
      is a programming error.

    ``tool_input`` is the request's original tool input; it is the base that an
    allow's ``updated_input`` is built on so the B ``updatedInput``-record gotcha is
    always satisfied (every allow returns a dict, never ``None``).
    """
    base = dict(tool_input or {})

    if isinstance(decision, PermissionVerdict):
        if decision.behavior == "allow":
            # Modified-input channel: explicit updated_input wins, else echo original.
            merged = dict(decision.updated_input) if decision.updated_input is not None else base
            return SubstrateDecision.make_allow(merged)
        return SubstrateDecision.make_deny(decision.message)

    if isinstance(decision, QuestionAnswer):
        # Native answers-map on the ALLOW channel, keyed by verbatim question text.
        merged = {**base, "answers": dict(decision.answers)}
        return SubstrateDecision.make_allow(merged)

    if isinstance(decision, PlanVerdict):
        if decision.approve:
            # Approve = bare allow (echo original input to satisfy the record gotcha).
            return SubstrateDecision.make_allow(base)
        # Reject: feedback rides the deny message (no native plan-feedback field).
        return SubstrateDecision.make_deny(decision.feedback)

    if isinstance(decision, Cancel):
        # Cancel a pending request = deny it; turn-level cancel is engine.stop().
        return SubstrateDecision.make_deny("cancelled")

    if isinstance(decision, FreeTextReply):
        raise TypeError(
            "FreeTextReply is a new turn (send), not a permission verdict; "
            "route it via Engine.send(), not decision_to_substrate()."
        )

    raise TypeError(f"unsupported decision type: {type(decision).__name__}")


# ---------------------------------------------------------------------------
# Multimodal input (P10 T1) — an operator-supplied image threaded INTO a turn
# ---------------------------------------------------------------------------


#: The Anthropic image ``media_type`` values the API/SDK accept. Derived from the
#: inbound file's mime/extension by the bot (jpeg/png/webp/gif); anything else is
#: rejected at the handler before it ever reaches the engine, so the substrate only
#: ever sees one of these.
ImageMediaType = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]


@dataclass(frozen=True)
class ImageInput:
    """One operator-supplied image to thread INTO a turn (P10 T1, multimodal).

    A normalized, substrate-neutral carrier for a screenshot/photo the operator sent
    via Telegram: the **already-base64-encoded** pixel ``data`` plus its ``media_type``
    (one of :data:`ImageMediaType`, derived from the inbound mime/extension). It is an
    *input* to ``send`` (decisions-in/turn direction), the mirror of the events-out
    types — not an event. The SDK adapter renders a list of these into the ``image``
    content-blocks of the streamed ``user`` message (the proven spike mechanism).

    **SB3:** ``data`` is the raw base64 of operator-supplied pixels. It is acceptable to
    forward to Claude (operator-provided), but it MUST NEVER be logged — log a size
    summary ("received an image (<N> KB)") only. ``repr`` would dump the base64, so a
    custom one elides it (defense-in-depth against an accidental ``log.debug(image)``).
    """

    data: str  # base64-encoded image bytes (NEVER logged — SB3)
    media_type: ImageMediaType

    def __repr__(self) -> str:  # SB3: never let repr leak the base64 into a log line.
        return f"ImageInput(media_type={self.media_type!r}, data=<{len(self.data)} b64 chars>)"


__all__ = [
    # events
    "TextEvent",
    "ThinkingEvent",
    "ToolUseEvent",
    "AskEvent",
    "PlanEvent",
    "PermissionEvent",
    "ErrorEvent",
    "ResultEvent",
    "StatusEvent",
    "Event",
    "ErrorKind",
    "StatusPhase",
    "safe_input_summary",
    # decisions
    "PermissionVerdict",
    "PermissionDecision",
    "QuestionAnswer",
    "PlanVerdict",
    "FreeTextReply",
    "Cancel",
    "Decision",
    "PermissionScope",
    "DENIED_MESSAGE",
    # mapping
    "SubstrateDecision",
    "decision_to_substrate",
    # multimodal input (P10 T1)
    "ImageInput",
    "ImageMediaType",
]
