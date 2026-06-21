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
    ToolUseEvent,
    AskEvent,
    PlanEvent,
    ErrorEvent,
    ResultEvent,
    StatusEvent,
]


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


#: Discriminated union of every decision the bot can route in.
Decision = Union[
    PermissionVerdict,
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


__all__ = [
    # events
    "TextEvent",
    "ToolUseEvent",
    "AskEvent",
    "PlanEvent",
    "ErrorEvent",
    "ResultEvent",
    "StatusEvent",
    "Event",
    "ErrorKind",
    "StatusPhase",
    # decisions
    "PermissionVerdict",
    "QuestionAnswer",
    "PlanVerdict",
    "FreeTextReply",
    "Cancel",
    "Decision",
    "PermissionScope",
    # mapping
    "SubstrateDecision",
    "decision_to_substrate",
]
