"""Substrate **A** adapter — over ``claude-agent-sdk`` (ADR-001 primary).

Implements the :class:`~claude_tg.engine.substrate.Substrate` Protocol on the
in-process Agent SDK (``ClaudeSDKClient``), mirroring the proven lifecycle in
`spikes/session-substrate/harness_sdk.py` (start / resume / send / stop, bounded
send, session-id capture). The spike module is reference only — its patterns are
re-implemented here; nothing imports the spike into production.

Two design rules this module enforces:

* **Lazy SDK import.** ``claude_agent_sdk`` is imported *inside* the methods that
  need a live client (and inside :func:`normalize` for ``isinstance`` checks), never
  at module top level. Importing :mod:`claude_tg.engine` — and running the mock-based
  unit tests — therefore does NOT require the SDK to be installed or a CLI present.
  (Tests that exercise :func:`normalize` MAY import the SDK to *construct* message
  objects, but they never open a session.)
* **Bounded send / fail-clean (RB2).** Each awaited message is wrapped in
  ``asyncio.wait_for(timeout=…)``; a timeout (or any driver exception) is converted
  into a ``driver_error`` :class:`~claude_tg.engine.types.ErrorEvent` and the stream
  ends — the adapter never hangs.

:func:`normalize` is a **pure** function (raw SDK message -> ``Event | None``) so it
is unit-testable against constructed/fake SDK objects with no live session.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any, AsyncIterator, Optional, Sequence

from .substrate import DecisionCallback
from .types import (
    AskEvent,
    ErrorEvent,
    Event,
    ImageInput,
    PlanEvent,
    ResultEvent,
    StatusEvent,
    SubstrateDecision,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)

log = logging.getLogger(__name__)

# StreamEvent.event["type"] values that are genuine incremental model output
# (everything else — message_start/content_block_start/stop — is framing, not text).
# Mirrors c1_streaming.INCREMENTAL_EVENT_TYPES (the proven C1 contract).
INCREMENTAL_EVENT_TYPES = {"content_block_delta", "message_delta"}

# Tool names that arrive through the permission channel but are really interactive
# prompts (answered via the decision seam), not ordinary tool use.
ASK_TOOL = "AskUserQuestion"
PLAN_TOOL = "ExitPlanMode"


def _user_message_with_images(
    prompt: str, images: Sequence[ImageInput], session_id: str
) -> dict[str, Any]:
    """Build the ONE streamed ``user`` dict carrying ``[text, image…]`` content blocks.

    P10 T1 — the spike-proven multimodal mechanism. ``ClaudeSDKClient.query`` accepts a
    ``str | AsyncIterable[dict]``; for an image turn we feed it an async-iterable that
    yields exactly this dict, which the SDK streams verbatim so the multimodal model sees
    the pixels (confirmed live: Claude read text off a PNG with no Read tool). The shape
    mirrors the Anthropic message API content-block list:

        {"type":"user","message":{"role":"user","content":[
            {"type":"text","text": <caption/prompt>},
            {"type":"image","source":{"type":"base64","media_type":…,"data":…}}, …
        ]},"parent_tool_use_id":None,"session_id": <sid>}

    The ``text`` block is the operator's caption/prompt (always FIRST so the prompt leads
    the image); one ``image`` block per :class:`~claude_tg.engine.types.ImageInput`. Pure
    (no I/O, no SDK import) so it is unit-testable; ``images`` is assumed non-empty (the
    caller only builds this when an image was attached). **SB3:** the base64 ``data`` is
    placed verbatim for the SDK but is NEVER logged here (or anywhere).
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": img.data,
                },
            }
        )
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


def _safe_input_summary(tool_name: str, tool_input: Any) -> str:
    """Render tool input WITHOUT dumping bodies — lengths, not content (SB3).

    Mirrors c2_permission.safe_input_summary: large free-text fields collapse to a
    char count; path/command-like fields are truncated; everything else is short.
    Returns a single compact string suitable for a ``tool_use`` event line.
    """
    if not isinstance(tool_input, dict):
        return f"{tool_name}({str(tool_input)[:80]})"
    parts: list[str] = []
    for k, v in tool_input.items():
        if k in ("content", "new_string", "old_string"):
            parts.append(f"{k}=<{len(str(v))} chars>")
        elif k in ("file_path", "path", "command", "pattern", "url"):
            parts.append(f"{k}={str(v)[:160]}")
        else:
            parts.append(f"{k}={str(v)[:40]}")
    return f"{tool_name}({', '.join(parts)})"


def _session_id_of(msg: Any) -> Optional[str]:
    """Best-effort session id from any SDK message (None if absent).

    Captured from ``SystemMessage.data['session_id']`` first, then any message that
    carries a ``session_id`` attribute (ResultMessage/StreamEvent/AssistantMessage).
    """
    data = getattr(msg, "data", None)
    if isinstance(data, dict) and data.get("session_id"):
        return str(data["session_id"])
    sid = getattr(msg, "session_id", None)
    return str(sid) if sid else None


def _field(obj: Any, key: str) -> Any:
    """Read ``key`` from ``obj`` whether it is a dict or an attribute object (defensive).

    The SDK's ``usage`` / ``get_context_usage()`` shapes are TypedDicts at the type level but
    may surface as plain dicts OR attribute objects at runtime depending on the build; this
    reads either uniformly. Returns ``None`` when absent. Pure; never raises (STATUSLINE ctx-%).
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _coerce_int(value: Any) -> Optional[int]:
    """Coerce a numeric token/percentage value to ``int``, or ``None`` (defensive, RB1)."""
    if isinstance(value, bool):  # bool is an int subclass — never a token count.
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _usage_tokens(usage: Any) -> Optional[int]:
    """Sum the context-relevant token fields of a ``ResultMessage.usage`` (ctx-% fallback §2.1).

    ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens`` — the last turn's
    INPUT side ≈ the current context size (design §2.1; output tokens are NOT part of the
    context the next turn carries). Missing fields read as 0. Returns ``None`` only when the
    whole ``usage`` is absent/odd (so the caller leaves the cached value untouched). Pure;
    never raises.
    """
    if usage is None:
        return None
    total = 0
    seen = False
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        n = _coerce_int(_field(usage, key))
        if n is not None:
            total += n
            seen = True
    return total if seen else None


def _context_window_of(model_usage: Any) -> Optional[int]:
    """The per-model ``contextWindow`` from a ``ResultMessage.model_usage`` (ctx-% fallback).

    ``model_usage`` maps ``model_id -> {…, contextWindow: int, …}`` (design §2.1). One model
    runs per turn, so the FIRST entry carrying a positive ``contextWindow`` is taken (no
    model-id→window table is hard-coded — the SDK reports the model's true window, and a ``1M``
    beta tracks automatically). Returns ``None`` when absent/odd. Pure; never raises.
    """
    if not isinstance(model_usage, dict):
        return None
    for entry in model_usage.values():
        window = _coerce_int(_field(entry, "contextWindow"))
        if window is not None and window > 0:
            return window
    return None


def _percentage_of(resp: Any) -> Optional[int]:
    """``round(resp["percentage"])`` from a live ``ContextUsageResponse``, or ``None`` (§2.1).

    The spike-proven primary ctx source: the SDK's ``percentage`` (0–100, the same figure the
    CLI ``/context`` shows). Reads the value defensively (dict or attribute object), ROUNDS
    (``6.4`` → ``6``, ``6.6`` → ``7`` — design §2.1 says ``round(percentage)``, never truncate),
    and clamps to ``[0, 100]`` (a number outside that range is an unexpected shape → bounded,
    never shown raw). ``None`` when the field is absent/non-numeric (the caller then uses the
    usage fallback). Pure; never raises.
    """
    raw = _field(resp, "percentage")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0, min(100, round(raw)))


# OBSERVABILITY T1: the SDK's rolling-limit status is one of ``allowed`` / ``allowed_warning`` /
# ``rejected`` (``RateLimitInfo.status``). Map it to a STABLE renderer-facing enum so the UI never
# touches the SDK's literal strings: an ``*_warning`` is "approaching" (🟡), a ``rejected`` is
# "limited" (🔴), an ``allowed`` is "ok" (🟢). Anything else (a future/odd word) → None, so the
# caller leaves prior state untouched rather than guess (RB1).
def _normalize_limit_status(raw_status: Any) -> Optional[str]:
    """Map the SDK's rate-limit status to ``ok`` / ``approaching`` / ``limited``, or ``None``."""
    if not isinstance(raw_status, str):
        return None
    s = raw_status.strip().lower()
    if s == "allowed":
        return "ok"
    if s == "allowed_warning":
        return "approaching"
    if s == "rejected":
        return "limited"
    # Forward-compatible fall-backs: an unforeseen ``*_warning``/``*reject*`` variant still maps
    # to the closest meaning rather than being dropped (still bounded — never a fabricated %).
    if "warn" in s:
        return "approaching"
    if "reject" in s or "limit" in s or "exceed" in s:
        return "limited"
    return None


def _pct_from_utilization(util: Any) -> Optional[int]:
    """``round(utilization*100)`` clamped to ``[0, 100]`` from ``RateLimitInfo.utilization``.

    ⭐ SPIKE: ``utilization`` is a FRACTION (0.0–1.0) of the rolling limit consumed (SDK docstring
    + parser confirmed). We scale to a percent and clamp (a number outside [0,1] is an odd shape →
    bounded, never shown raw). ``None`` when the SDK omits it / it is non-numeric (the UI then uses
    the status badge). Pure; never raises.
    """
    if isinstance(util, bool) or not isinstance(util, (int, float)):
        return None
    return max(0, min(100, round(util * 100)))


def normalize(msg: Any) -> Optional[Event]:
    """Map ONE raw SDK message/block-bearing message to a normalized event.

    Pure and side-effect-free (no I/O, no SDK client) so it is unit-testable with
    constructed SDK objects. Returns ``None`` for frames that carry no operator-facing
    event (e.g. pure framing ``StreamEvent``s, echoed ``UserMessage``s without an
    error). The SDK is imported lazily here so importing this module needs no SDK.

    Mapping (per `normalized_interface.md` §1):

    * ``SystemMessage(init)``                  -> ``StatusEvent(phase="init")``
    * ``StreamEvent`` ``text_delta``           -> incremental ``TextEvent``
    * ``StreamEvent`` ``thinking_delta``       -> incremental ``ThinkingEvent`` (P12)
    * ``StreamEvent`` ``signature_delta``      -> ``None`` (opaque signature dropped, SB3)
    * ``AssistantMessage`` text blocks         -> assembled ``TextEvent``
    * ``AssistantMessage`` ``ThinkingBlock``   -> assembled ``ThinkingEvent`` (P12; SB3:
                                                  signature dropped, never surfaced)
    * ``ToolUseBlock`` ``AskUserQuestion``     -> ``AskEvent``
    * ``ToolUseBlock`` ``ExitPlanMode``        -> ``PlanEvent``
    * ``ToolUseBlock`` (other)                 -> ``ToolUseEvent``
    * ``ToolResultBlock(is_error)``            -> ``ErrorEvent(tool_error)``
    * ``ResultMessage``                        -> ``ResultEvent`` (+ ``turn_error``
                                                  ``ErrorEvent`` when ``is_error``)
    * ``RateLimitEvent``                       -> ``StatusEvent(phase="rate_limit")``

    NOTE on multi-block messages: an ``AssistantMessage`` can carry several blocks;
    this returns the **first** operator-facing event so the function stays a clean
    1-message-in/1-event-out unit. The streaming adapter does not rely on that — it
    iterates blocks itself (see :meth:`SdkSubstrate._events_from`). ``normalize`` is
    the audited per-shape mapping; the adapter is the fan-out.
    """
    from claude_agent_sdk import (  # lazy: import only when actually normalizing
        AssistantMessage,
        RateLimitEvent,
        ResultMessage,
        StreamEvent,
        SystemMessage,
    )

    sid = _session_id_of(msg)

    # --- lifecycle / status -------------------------------------------------
    if isinstance(msg, SystemMessage):
        if msg.subtype == "init":
            data = msg.data if isinstance(msg.data, dict) else {}
            return StatusEvent(
                phase="init",
                session_id=sid,
                model=data.get("model"),
                tools=data.get("tools"),
                permission_mode=data.get("permissionMode") or data.get("permission_mode"),
            )
        # Other system subtypes are non-content control; surface as a generic status.
        return StatusEvent(phase="connected", session_id=sid, detail=msg.subtype)

    if isinstance(msg, RateLimitEvent):
        return StatusEvent(phase="rate_limit", session_id=sid, detail=str(msg.rate_limit_info))

    # --- incremental text deltas -------------------------------------------
    if isinstance(msg, StreamEvent):
        event = msg.event or {}
        etype = event.get("type")
        if etype in INCREMENTAL_EVENT_TYPES:
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            # Text deltas carry the answer prose -> incremental TextEvent (status line).
            if dtype in ("text_delta", "text"):
                text = delta.get("text") or ""
                if text:
                    return TextEvent(text=text, incremental=True, session_id=sid)
            # P12 T-THINK: thinking deltas carry Claude's REASONING -> incremental
            # ThinkingEvent (the 🧠 status line). The reasoning text rides ``delta["thinking"]``
            # (exactly parallel to ``text_delta``'s ``delta["text"]`` — spike-verified). SB3:
            # the opaque ``signature`` arrives as a SEPARATE ``signature_delta`` (handled by the
            # explicit drop below) and is NEVER carried on a ThinkingEvent.
            elif dtype == "thinking_delta":
                text = delta.get("thinking") or ""
                if text:
                    return ThinkingEvent(text=text, incremental=True, session_id=sid)
            # SB3: ``signature_delta`` is the opaque crypto signature — drop it (return None
            # below), never surface it. (Listed explicitly so the intent is unmistakable; a
            # bare fall-through would do the same, but this documents the SB3 contract.)
            elif dtype == "signature_delta":
                return None
            # Defensive (RB1/SB3): a future ``redacted_thinking`` stream delta (the API
            # encrypts some reasoning) carries NO readable text — emit an OPAQUE marker
            # (``redacted=True``, empty text) so the renderer shows the fixed hidden line,
            # NEVER raw. The installed SDK never produces this (no block class, no parser
            # case), so this is a forward-compatible fail-safe, not a live path.
            elif dtype == "redacted_thinking":
                return ThinkingEvent(text="", incremental=True, redacted=True, session_id=sid)
        return None  # framing / other non-content delta -> no operator-facing event

    # --- assistant content blocks ------------------------------------------
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            ev = _normalize_block(block, sid)
            if ev is not None:
                return ev
        return None

    # --- terminal per-turn frame -------------------------------------------
    if isinstance(msg, ResultMessage):
        if msg.is_error:
            return ErrorEvent(
                kind_of_error="turn_error",
                message=(msg.result or msg.subtype or "turn failed"),
                is_error=True,
                session_id=sid,
            )
        return ResultEvent(
            session_id=sid,
            is_error=bool(msg.is_error),
            subtype=msg.subtype,
            num_turns=getattr(msg, "num_turns", None),
            total_cost_usd=getattr(msg, "total_cost_usd", None),
            result_text=getattr(msg, "result", None),
        )

    # Echoed UserMessage tool_results etc. carry no operator-facing event here.
    return None


def _normalize_block(block: Any, sid: Optional[str]) -> Optional[Event]:
    """Map one content block to an event (helper for :func:`normalize`).

    Imports the SDK block types lazily here too (so this module needs no SDK at
    import) AND so ``isinstance`` narrows the ``Any`` block to the concrete block
    type for the type checker.
    """
    from claude_agent_sdk import (  # lazy
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
    )

    if isinstance(block, TextBlock):
        text = block.text or ""
        if not text:
            return None
        return TextEvent(text=text, incremental=False, session_id=sid)

    # P12 T-THINK: an assembled ThinkingBlock (Claude's reasoning) -> a non-incremental
    # ThinkingEvent. SB3: the SDK block has ``thinking`` + an opaque ``signature``; we carry
    # ONLY ``block.thinking`` and DROP ``signature`` (it is never read here, so it cannot
    # leak). An empty ``thinking`` (e.g. a signature-only block when ``display="omitted"``)
    # yields no event — there is nothing readable to show, and the signature is dropped.
    if isinstance(block, ThinkingBlock):
        text = getattr(block, "thinking", "") or ""
        if not text:
            return None
        return ThinkingEvent(text=text, incremental=False, session_id=sid)

    if isinstance(block, ToolUseBlock):
        tool_input = block.input if isinstance(block.input, dict) else {}
        if block.name == ASK_TOOL:
            questions = tool_input.get("questions")
            return AskEvent(
                questions=list(questions) if isinstance(questions, list) else [],
                tool_use_id=block.id,
                session_id=sid,
            )
        if block.name == PLAN_TOOL:
            return PlanEvent(
                plan=str(tool_input.get("plan", "")),
                tool_use_id=block.id,
                session_id=sid,
            )
        return ToolUseEvent(
            tool_name=block.name,
            tool_input_summary=_safe_input_summary(block.name, tool_input),
            tool_use_id=block.id,
            session_id=sid,
        )

    if isinstance(block, ToolResultBlock) and block.is_error:
        content = block.content
        message = content if isinstance(content, str) else str(content)
        return ErrorEvent(
            kind_of_error="tool_error",
            message=message,
            is_error=True,
            tool_use_id=block.tool_use_id,
            session_id=sid,
        )

    return None


class SdkSubstrate:
    """Substrate A: a persistent multi-turn Claude session over the Agent SDK.

    Conforms to :class:`~claude_tg.engine.substrate.Substrate`. One instance owns one
    session (one CLI subprocess the SDK spawns) between ``start``/``resume`` and
    ``stop``; not thread-safe (single asyncio task).

    The ``decision_callback`` is the engine's permission/decision seam. When the SDK
    raises ``can_use_tool`` (ordinary tools AND the interactive AskUserQuestion /
    ExitPlanMode, which arrive through the same channel), this adapter calls the
    callback, gets a neutral :class:`~claude_tg.engine.types.SubstrateDecision`, and
    renders it to ``PermissionResultAllow``/``PermissionResultDeny`` for the SDK
    (allow always carries ``updated_input`` as a record — mirrors the proven path).
    """

    def __init__(
        self,
        *,
        cwd: Optional[str | os.PathLike[str]] = None,
        permission_mode: str = "default",
        decision_callback: Optional[DecisionCallback] = None,
        include_partial_messages: bool = False,
        allowed_tools: Optional[list[str]] = None,
        disallowed_tools: Optional[list[str]] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        effort: Optional[str] = None,
    ) -> None:
        self._cwd = str(cwd) if cwd is not None else None
        self._permission_mode = permission_mode
        self._decision_callback = decision_callback
        self._include_partial = include_partial_messages
        # P12 T-THINK: when True, this session surfaces Claude's readable reasoning. The
        # SDK default (Opus 4.7+) is ``display="omitted"`` (signature only — NO text), so we
        # must pass ``display="summarized"`` to get text; AND thinking only streams live when
        # ``include_partial_messages`` is on. So a thinking-ON session forces partials on
        # below regardless of the ``include_partial_messages`` arg. A thinking-OFF session is
        # byte-for-byte unchanged: no ``thinking`` option, partials stay as passed (default
        # False) → no StreamEvent traffic, exactly as before P12. Threaded per project by the
        # factory (mirrors ``model`` / ``permission_mode``), so it is a session-creation knob.
        self._thinking = bool(thinking)
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools
        # T4 (P9): the per-project model override threaded into ClaudeAgentOptions(model=…)
        # at session-creation time (start/resume). None → omit `model` entirely so the SDK
        # uses its own default (or CLAUDE_MODEL via the CLI env), exactly as before T4. The
        # model is a session-creation param: it is baked into the options when the client is
        # built, so it applies to THIS session for its whole life — a change takes effect on
        # the NEXT fresh session, never mid-session (the session is rebuilt with new options).
        self._model = str(model).strip() if isinstance(model, str) and str(model).strip() else None
        # T-EFFORT (STATUSLINE): the per-project reasoning-EFFORT override threaded into
        # ClaudeAgentOptions(effort=…) at session-creation time (start/resume). None → omit
        # `effort` entirely so the SDK applies its own default (`high`), exactly as before this
        # knob. Like `model` it is a session-creation param: baked into the options when the
        # client is built, so it applies to THIS session for its whole life — a change takes
        # effect on the NEXT fresh session (the warm engine is rebuilt with new options), never
        # mid-session. There is NO CLAUDE_* global default for effort (the session resolves the
        # per-project override → None and lets the SDK default stand). Distinct from `thinking`
        # (P12), which is a VISIBILITY toggle (display="summarized" + partials); effort is the
        # DEPTH dial (low→max) and adds no wire traffic.
        self._effort = str(effort).strip() if isinstance(effort, str) and str(effort).strip() else None

        self._client: Any = None  # ClaudeSDKClient | None (lazily typed)
        self.session_id: Optional[str] = None
        # P6/H2/RB2: how many decision holds are OPEN right now (the SDK's can_use_tool
        # is outstanding, awaiting the engine's answer-hold for the operator's verdict).
        # Incremented by ``can_use_tool`` before it awaits the engine callback and
        # decremented in its finally. The receive loop SUSPENDS the per-message liveness
        # timeout while this is > 0: a permission/ask/plan hold is bounded by the engine's
        # ~60-min answer-backstop (the SDK awaits can_use_tool with no fail_after — see
        # engine/pending.py), NOT by the 120s liveness bound, which exists only to catch a
        # genuinely-SILENT Claude. The counter (not a bool) tolerates the unlikely nested/
        # concurrent hold without a premature un-suspend. Single asyncio task per the
        # substrate contract, so no lock is needed.
        self._hold_depth = 0
        # STATUSLINE T-SL-CORE: the honest ctx-% usage fallback (design §2.1/§5 T5). The live
        # ``get_context_usage()`` is the primary source (``context_percentage()`` below); when
        # it is unavailable/raises, the bot can still compute an honest % from the LAST turn's
        # usage — the last ``ResultMessage`` carries ``usage`` (input + cache_read +
        # cache_creation tokens ≈ the current context size) and ``model_usage[…].contextWindow``
        # (the model's window). We stash those two numbers as each ResultMessage drains
        # (``_capture_usage``), so a None from the live call has a derived figure to fall back to.
        # Both default None → no fallback before the first turn completes (→ ``ctx —``, never a
        # fabricated 0%). In-memory only (RB3); reset on stop. Single asyncio task → no lock.
        self._last_usage_tokens: Optional[int] = None
        self._last_context_window: Optional[int] = None
        # STATUSLINE: the ACTUAL model id the SDK reports for this session — captured from the
        # ``init`` system event (at session start, so the statusline shows the real model from
        # the first turn — closes the "🤖 default" gap when no CLAUDE_MODEL/override is set) and
        # refreshed from each AssistantMessage / terminal ResultMessage.model_usage. Lets the
        # statusline show the model that is genuinely running (incl. after /fast·/deep routing)
        # instead of the literal word "default". In-memory only (RB3); reset on stop.
        self._last_model: Optional[str] = None
        # OBSERVABILITY T1: the rolling session-limit signal, for the statusline 🪙 field + the
        # one-time warning. Captured from each ``RateLimitEvent`` the SDK emits when the rolling
        # rate-limit state changes (``_capture_limit``). SPIKE: the SDK DOES expose a precise % —
        # ``RateLimitInfo.utilization`` is a fraction (0.0–1.0) of the rolling limit consumed — so
        # we record BOTH a stable normalized status (``ok`` / ``approaching`` / ``limited``,
        # mapped from the SDK's ``allowed`` / ``allowed_warning`` / ``rejected``) AND the precise
        # percent (round(utilization*100)) when present. ``_last_limit_pct`` stays None when the
        # SDK omits ``utilization`` (the UI then falls back to the status badge). In-memory only
        # (RB3); reset on stop. Single asyncio task per the substrate contract → no lock needed.
        self._last_limit_status: Optional[str] = None
        self._last_limit_pct: Optional[int] = None

    # -- options -------------------------------------------------------------

    def _build_options(self, resume: Optional[str] = None, *, fork: bool = False) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions  # lazy

        kwargs: dict[str, Any] = {
            "permission_mode": self._permission_mode,
            # P12 T-THINK: a thinking-ON session REQUIRES partials (thinking only streams as
            # ``thinking_delta`` StreamEvents, which need ``include_partial_messages``). So OR
            # the per-session thinking flag in. A thinking-OFF session keeps the configured
            # value (default False) → no StreamEvent traffic, byte-for-byte unchanged.
            "include_partial_messages": self._include_partial or self._thinking,
        }
        if self._thinking:
            # P12 T-THINK: ask the model for READABLE reasoning. ``adaptive`` lets the model
            # choose depth; ``display="summarized"`` is the gotcha — without it Opus 4.7+
            # returns a signature-only ThinkingBlock (no text). Set ONLY when thinking is on,
            # so a normal turn's options are unchanged (no ``thinking`` key at all). SB3: this
            # surfaces the reasoning TEXT; the opaque signature is dropped in ``normalize``.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        if self._cwd is not None:
            kwargs["cwd"] = self._cwd
        if self._model is not None:
            # T4 (P9): per-project model override (Haiku/Opus via /fast·/deep, or a custom
            # CLAUDE_MODEL). Omitted when None so the SDK keeps its own default. Set on every
            # session-creation path (start AND resume) so a resumed session honors the
            # project's current model from the next session onward.
            kwargs["model"] = self._model
        if self._effort is not None:
            # T-EFFORT (STATUSLINE): per-project reasoning-EFFORT override (/effort low…max).
            # Set ONLY when an override is present — a default (no-effort) turn NEVER sets the
            # kwarg, so its options stay byte-for-byte the pre-knob baseline and the SDK's own
            # default effort applies. Set on every session-creation path (start AND resume), so
            # a resumed session honors the project's current effort from the next session onward.
            kwargs["effort"] = self._effort
        if self._decision_callback is not None:
            kwargs["can_use_tool"] = self._make_can_use_tool()
        if self._allowed_tools is not None:
            kwargs["allowed_tools"] = self._allowed_tools
        if self._disallowed_tools is not None:
            kwargs["disallowed_tools"] = self._disallowed_tools
        if resume:
            kwargs["resume"] = resume
            if fork:
                # P11 T2: fork the resumed session — the SDK resumes into a NEW session id
                # with the transcript copied, NEVER writing to the resumed (``resume``) id.
                # This is the load-bearing safety primitive: when the target session is LIVE
                # in another process, attaching with ``fork_session=True`` means two writers
                # never share one ``(id, cwd)`` transcript (which silently fork-corrupts the
                # conversation tree). Set ONLY alongside ``resume`` (a fork with no base is a
                # fresh ``start``); ``fork=False`` (the default + idle attach + every pre-P11
                # resume) omits it entirely so behavior is unchanged. The spike proved the SDK
                # honors ``fork_session`` on resume (the new id arrives in the init frame and
                # is captured by ``_capture_session_id`` exactly as a normal resume's id).
                kwargs["fork_session"] = True
        return ClaudeAgentOptions(**kwargs)

    def _make_can_use_tool(self) -> Any:
        """Build the SDK ``can_use_tool`` callback that bridges to the engine seam.

        Renders the engine's neutral :class:`SubstrateDecision` to the SDK's
        ``PermissionResultAllow``/``PermissionResultDeny``. On **allow** it always
        passes ``updated_input`` as a record (the contract guarantees a dict),
        mirroring the proven C2/C3 path.
        """
        callback = self._decision_callback
        assert callback is not None

        async def can_use_tool(tool_name: str, tool_input: dict, context: Any) -> Any:
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny  # lazy

            tool_use_id = getattr(context, "tool_use_id", None)
            # P6/H2/RB2: a decision hold is now OPEN for this turn — the operator may take
            # up to the answer-backstop to decide. Mark it so the receive loop suspends the
            # 120s liveness timeout for the duration (the human-wait must NOT be counted as
            # Claude going silent). Restored in the finally the instant the verdict returns,
            # so a Claude that then goes silent is bounded by the 120s again (the bound is
            # suspended, not removed). The engine's own backstop bounds the hold itself.
            self._hold_depth += 1
            try:
                decision: SubstrateDecision = await callback(
                    tool_name, tool_input, tool_use_id
                )
            finally:
                self._hold_depth -= 1
            if decision.allow:
                return PermissionResultAllow(updated_input=dict(decision.updated_input or {}))
            return PermissionResultDeny(message=decision.message or "")

        return can_use_tool

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient  # lazy

        if self._client is not None:
            raise RuntimeError("session already started; call stop() first")
        self._client = ClaudeSDKClient(options=self._build_options())
        await self._client.connect()

    async def resume(self, session_id: str, *, fork: bool = False) -> None:
        from claude_agent_sdk import ClaudeSDKClient  # lazy

        if not session_id:
            raise ValueError("resume() requires a non-empty session_id")
        if self._client is not None:
            raise RuntimeError("session already started; call stop() first")
        self._client = ClaudeSDKClient(
            options=self._build_options(resume=session_id, fork=fork)
        )
        await self._client.connect()
        # P11 T2: a CONTINUE resume keeps the same id, so we can seed it immediately. A FORK
        # resumes into a BRAND-NEW id (the SDK copies the transcript under a fresh id, leaving
        # the base id — which may be live elsewhere — untouched), so we must NOT seed the base
        # id here: the real forked id arrives in the init/result frame and is captured by
        # ``_capture_session_id`` on the first send, exactly like a fresh ``start``. Seeding the
        # base id on a fork would persist + route against an id we never actually write to.
        if not fork:
            self.session_id = session_id

    async def send(
        self,
        prompt: str,
        *,
        timeout: float = 120.0,
        images: Optional[Sequence[ImageInput]] = None,
    ) -> AsyncIterator[Event]:
        """Send one turn; async-yield normalized events. Bounded → fail-clean (RB2).

        The whole receive loop runs inside a try/except; a per-message
        ``asyncio.wait_for`` timeout OR any driver exception is converted into a
        single ``driver_error`` event and the stream ends. The session is never left
        hanging waiting on the SDK.

        **P10 T1 — multimodal (optional ``images``).** ``images`` defaults to ``None``,
        in which case this is the unchanged text turn: ``self._client.query(prompt)`` (a
        plain ``str``). When one or more :class:`~claude_tg.engine.types.ImageInput` are
        passed, the prompt + pixels are streamed as ONE ``user`` message whose ``content``
        is ``[text, image…]`` — built by :func:`_user_message_with_images` and fed to the
        SDK as a single-item async-generator (``query`` accepts ``str | AsyncIterable[dict]``;
        the SDK streams the dict verbatim so the multimodal model sees the image). The
        receive loop, the liveness bound, and the decision seam are all IDENTICAL to the
        text path — only the ``query`` argument differs. **SB3:** the base64 image data is
        never logged on this path.

        **P6/H2/RB2 — the liveness timeout bounds CLAUDE's responsiveness, not the
        operator's approval time.** The ``timeout`` catches a genuinely-silent Claude
        (the SDK stopped delivering messages). But while a permission/ask/plan hold is
        OPEN (``can_use_tool`` is parked awaiting the operator's verdict — tracked by
        ``_hold_depth``), the SDK legitimately delivers no further messages: the next
        ``__anext__`` would block for the whole human-wait. Counting that against the
        120s would fire a spurious ``driver_error`` (and, on a verified session, wedge
        the project — see :meth:`StreamingSession._drive_turn`). So the per-message
        bound is APPLIED only when no hold is open; while one is, the await is unbounded
        and the engine's ~60-min answer-backstop is what bounds the human-wait. The
        invariant: a turn must NOT ``driver_error`` solely because the operator took
        >120s to approve — but a silent Claude with NO hold open still times out cleanly.
        """
        if self._client is None:
            raise RuntimeError("session not started; call start()/resume() first")

        try:
            if images:
                # P10 T1: stream the [text, image…] content-block user dict as a single-item
                # async-iterable. ``session_id`` rides the dict (the SDK's query default is
                # "default"); use the captured id when we have one, else "default" so a fresh
                # first turn still streams cleanly (the SDK assigns the real id on init).
                user_dict = _user_message_with_images(
                    prompt, images, self.session_id or "default"
                )

                async def _one_user_message() -> AsyncIterator[dict[str, Any]]:
                    yield user_dict

                await self._client.query(_one_user_message())
            else:
                await self._client.query(prompt)
            iterator = self._client.receive_response().__aiter__()
            while True:
                try:
                    msg = await self._next_message(iterator, timeout)
                except StopAsyncIteration:
                    break
                self._capture_session_id(msg)
                self._capture_usage(msg)
                self._capture_model(msg)
                self._capture_limit(msg)
                for ev in self._events_from(msg):
                    yield ev
        except asyncio.TimeoutError:
            yield ErrorEvent(
                kind_of_error="driver_error",
                message=f"send timed out after {timeout:.0f}s",
                is_error=True,
                session_id=self.session_id,
            )
        except Exception as exc:  # noqa: BLE001 - fail clean, surface as an event
            yield ErrorEvent(
                kind_of_error="driver_error",
                message=f"{type(exc).__name__}: {exc}",
                is_error=True,
                session_id=self.session_id,
            )

    async def _next_message(self, iterator: Any, timeout: float) -> Any:
        """Await the next SDK message, bounding it by ``timeout`` ONLY when no hold is open.

        P6/H2/RB2. The per-message liveness bound exists to catch a SILENT Claude. A
        decision hold (``can_use_tool`` parked on the operator) is NOT silence — it is the
        SDK correctly waiting on us, and it can legitimately last up to the engine's
        ~60-min answer-backstop. So:

        * **No hold open** → a single ``asyncio.wait_for(__anext__, timeout)``: a genuinely
          silent Claude trips it and the caller converts the ``TimeoutError`` to a clean
          ``driver_error`` (the bound is preserved, not removed).
        * **A hold is open OR opens during the wait** → each ``timeout`` tick that elapses
          while ``_hold_depth > 0`` is SWALLOWED and the await re-armed, so the human-wait
          is never charged against the bound. Re-checking the depth every tick (rather than
          committing to one unbounded await) means the bound is RESTORED the instant the
          hold closes: if Claude is still silent after the verdict, the very next tick has
          ``_hold_depth == 0`` and times out cleanly.

        **Boundary-race guard (independent-review fix 1).** ``asyncio.wait_for`` can take its
        ``TimeoutError`` branch in the SAME event-loop tick that ``pending`` resolves — both
        the timeout handle and the awaitable's done-callback are scheduled, and ``wait_for``
        picks the timeout. ``pending`` then holds a real, ready message (most often the
        terminal ``ResultMessage``, which lands right as a freshly-closed hold un-suspends the
        bound). So before cancelling + raising, re-check ``pending``: if it is already done
        (and not cancelled) we RETURN its result rather than dropping it. Dropping it would
        cancel a ready ``ResultMessage`` and surface a COMPLETED turn as a spurious
        ``driver_error`` + a needless engine rebuild (see :meth:`StreamingSession._drive_turn`);
        it also produces the cosmetic "StopAsyncIteration ... in shielded future" asyncio log
        noise for the normal end-of-stream case. The guard makes the timeout branch only fire
        for a genuinely-not-yet-resolved await.

        A single ``ClaudeSDKClient.receive_response()`` drives ``can_use_tool`` from inside
        the same ``__anext__`` it is producing, on one asyncio task (the substrate
        contract), so the depth read is race-free and ``__anext__`` is polled (re-awaited)
        only across timeout boundaries — never duplicated within one.
        """
        step = iterator.__anext__()
        # Wrap once in a Task so the SAME pending awaitable survives across re-armed
        # wait_for windows (re-calling __anext__ would drop an already-arrived message).
        pending = asyncio.ensure_future(step)
        try:
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(pending), timeout=timeout)
                except asyncio.TimeoutError:
                    # Boundary race (fix 1): the timeout branch can win the tick in which
                    # ``pending`` already resolved. Don't drop a ready message — return it.
                    if pending.done() and not pending.cancelled():
                        return pending.result()
                    # A hold open during this window means the operator is still deciding —
                    # don't count it as silence; re-arm. No hold → genuine silence → raise.
                    if self._hold_depth > 0:
                        continue
                    pending.cancel()
                    raise
        except BaseException:
            # On any exit other than a clean return (timeout-raise, cancel, StopAsyncIteration
            # propagating from the awaitable), make sure the wrapped task isn't orphaned.
            if not pending.done():
                pending.cancel()
            raise

    def _events_from(self, msg: Any) -> list[Event]:
        """Fan one SDK message out to all its operator-facing events.

        ``normalize`` returns the first event for a multi-block message (so it stays
        a clean unit); here — on the live path — we want EVERY block, so an
        ``AssistantMessage`` carrying text + a tool_use surfaces both. Non-assistant
        messages defer to ``normalize`` (0 or 1 event).
        """
        from claude_agent_sdk import AssistantMessage  # lazy

        sid = self.session_id or _session_id_of(msg)
        if isinstance(msg, AssistantMessage):
            events: list[Event] = []
            for block in msg.content:
                ev = _normalize_block(block, sid)
                if ev is not None:
                    events.append(ev)
            return events
        ev = normalize(msg)
        return [ev] if ev is not None else []

    def _capture_session_id(self, msg: Any) -> None:
        sid = _session_id_of(msg)
        if sid and not self.session_id:
            self.session_id = sid

    def _capture_usage(self, msg: Any) -> None:
        """Stash the last ``ResultMessage``'s token usage + context window (ctx-% fallback).

        STATUSLINE T-SL-CORE (design §2.1/§5 T5). Only a terminal ``ResultMessage`` carries
        ``usage`` + ``model_usage``; for one we record the honest fallback inputs so a None
        from the live ``get_context_usage()`` can still produce a % (:meth:`context_percentage`):

        * ``tokens`` = ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``
          from ``msg.usage`` — the LAST turn's input ≈ the current context size (design §2.1).
        * ``window`` = the per-model ``contextWindow`` from ``msg.model_usage[<model>]`` (the SDK
          reports the model's raw window; no model-id→window table is hard-coded — a ``1M`` beta
          tracks automatically). We take the FIRST model entry's window (one model per turn).

        **Best-effort + fully defensive (RB1):** any missing key / odd shape / exception leaves
        the stored values UNCHANGED (we never overwrite a good figure with a broken one, and we
        never raise on the hot receive path). ``usage`` may be a dict or an attribute object, so
        both are probed. A non-positive/absent window is ignored (a 0 window would divide-by-zero
        downstream). Pure-ish: only mutates the two cached ints; no I/O.
        """
        from claude_agent_sdk import ResultMessage  # lazy

        if not isinstance(msg, ResultMessage):
            return
        try:
            usage = getattr(msg, "usage", None)
            tokens = _usage_tokens(usage)
            window = _context_window_of(getattr(msg, "model_usage", None))
            if tokens is not None:
                self._last_usage_tokens = tokens
            if window is not None and window > 0:
                self._last_context_window = window
        except Exception:  # pragma: no cover - defensive; never break the receive loop (RB1)
            log.debug("usage capture failed (ignored)", exc_info=True)

    def _capture_model(self, msg: Any) -> None:
        """Stash the ACTUAL model id the SDK reports for this session (statusline source).

        The bot does not always know the model up front: when no per-project override and no
        ``CLAUDE_MODEL`` is set, the SDK picks its own default — so the statusline would show
        ``🤖 default``. The SDK, however, names the model it actually used, so we capture it
        (most-recent wins) from whichever message carries it:

        * ``SystemMessage(init).data["model"]`` — emitted at session start, BEFORE any output,
          so the real model is known from the very first turn (closes the ``default`` gap).
        * ``AssistantMessage.model`` — the per-message model (defensive / mid-turn refresh).
        * ``ResultMessage.model_usage`` — the terminal turn's model (first/only key); the most
          authoritative, and tracks ``/fast``·``/deep`` routing changes.

        **Best-effort + fully defensive (RB1):** any missing key / odd shape / exception leaves
        the stored value UNCHANGED — never overwrite a known model with a blank, never raise on
        the hot receive path. In-memory only (RB3); dropped on :meth:`stop`.
        """
        from claude_agent_sdk import (  # lazy
            AssistantMessage,
            ResultMessage,
            SystemMessage,
        )

        try:
            found: Optional[str] = None
            if isinstance(msg, SystemMessage) and getattr(msg, "subtype", None) == "init":
                data = msg.data if isinstance(msg.data, dict) else {}
                found = data.get("model")
            elif isinstance(msg, AssistantMessage):
                found = getattr(msg, "model", None)
            elif isinstance(msg, ResultMessage):
                model_usage = getattr(msg, "model_usage", None)
                if isinstance(model_usage, dict):
                    found = next(iter(model_usage), None)
            if isinstance(found, str) and found.strip():
                self._last_model = found.strip()
        except Exception:  # pragma: no cover - defensive; never break the receive loop (RB1)
            log.debug("model capture failed (ignored)", exc_info=True)

    def last_model(self) -> Optional[str]:
        """The actual model id the SDK reported for this session, or ``None`` (statusline).

        Captured by :meth:`_capture_model` from the ``init``/assistant/result messages. Pure
        in-memory read (no I/O, never raises) — the statusline uses it as the model fallback so
        it shows the genuinely-running model instead of the literal ``default`` when no override
        / ``CLAUDE_MODEL`` is configured. ``None`` before the first message of the first turn.
        """
        return self._last_model

    def _capture_limit(self, msg: Any) -> None:
        """Stash the rolling session-limit signal (statusline 🪙 field + the one-time warning).

        OBSERVABILITY T1. Only a ``RateLimitEvent`` carries the rolling rate-limit state; the SDK
        emits one whenever that state changes. From its ``RateLimitInfo`` we record:

        * a **stable normalized status** — the SDK's ``status`` is one of ``allowed`` /
          ``allowed_warning`` / ``rejected``; we map it to ``ok`` / ``approaching`` / ``limited``
          so the UI never re-derives the SDK's literal strings (and a future SDK status word can
          be slotted in here, not scattered across the renderer).
        * a **precise percent** — ⭐ SPIKE ANSWER: a precise % of the rolling limit IS exposed,
          via ``RateLimitInfo.utilization`` (a fraction 0.0–1.0; the docstring + parser confirm
          ``info.get("utilization")``). We record ``round(utilization*100)`` when present; if the
          SDK omits it (``None`` / odd type) ``_last_limit_pct`` is left None and the UI falls
          back to the status badge. SB3: only status / percent / reset are touched — NEVER any
          request content (we read ``status``/``utilization`` only, not ``raw``'s body).

        **Best-effort + fully defensive (RB1):** any missing field / odd shape / exception leaves
        the stored values UNCHANGED — never break the hot receive loop. In-memory only (RB3);
        dropped on :meth:`stop`.
        """
        from claude_agent_sdk import RateLimitEvent  # lazy

        if not isinstance(msg, RateLimitEvent):
            return
        try:
            info = getattr(msg, "rate_limit_info", None)
            raw_status = getattr(info, "status", None)
            status = _normalize_limit_status(raw_status)
            if status is None:
                return  # an unrecognized status leaves prior state intact (RB1)
            util = getattr(info, "utilization", None)
            pct = _pct_from_utilization(util)
            self._last_limit_status = status
            self._last_limit_pct = pct
        except Exception:  # pragma: no cover - defensive; never break the receive loop (RB1)
            log.debug("limit capture failed (ignored)", exc_info=True)

    def limit_status(self) -> Optional[tuple[str, Optional[int]]]:
        """The rolling session-limit signal, or ``None`` if none seen yet (statusline + warning).

        Returns ``(status, pct_or_None)`` where ``status`` is the normalized ``ok`` /
        ``approaching`` / ``limited`` (mapped by :meth:`_capture_limit` from the SDK's status) and
        the second element is the precise percent of the rolling limit (``round(utilization*100)``)
        when the SDK exposed one, else ``None`` (the UI then shows the 🟢/🟡/🔴 badge). ``None``
        when no ``RateLimitEvent`` has arrived yet (never a fabricated value). Pure in-memory read
        (no I/O, never raises) — an observer off the turn's critical path (RB1).
        """
        if self._last_limit_status is None:
            return None
        return (self._last_limit_status, self._last_limit_pct)

    async def context_percentage(self) -> Optional[int]:
        """Best-effort % of the context window currently used — the honest ctx figure (§2.1).

        **Primary path:** call the LIVE client's ``get_context_usage()`` and return
        ``round(resp["percentage"])`` — the same number the CLI ``/context`` shows (spike-proven,
        design §2.1). **Fallback:** if there is no live client, the method is absent, or it
        raises, derive ``round(100 * tokens / window)`` from the LAST ``ResultMessage``'s usage
        captured by :meth:`_capture_usage` (an honest ratio, not a fabricated number). If neither
        is available (no client AND no completed turn yet) → ``None`` (the caller shows ``ctx —``,
        NEVER a fake 0%).

        ⭐ **ASYNC — the installed SDK's ``ClaudeSDKClient.get_context_usage()`` is a COROUTINE**
        (verified: ``inspect.iscoroutinefunction`` is True), so it MUST be awaited or the headline
        percentage is never read (it would return an un-awaited coroutine that the dict-extractor
        rejects, silently degrading to the usage fallback). We await it when it returns an
        awaitable, and still accept a plain dict (defensive — a future/sync build keeps working).

        Fully best-effort (RB1): this is an observer off the turn's critical path — it NEVER
        raises (any error / no client → usage fallback → ``None``).
        """
        client = self._client
        if client is not None:
            try:
                getter = getattr(client, "get_context_usage", None)
                if getter is not None:
                    resp = getter()
                    if inspect.isawaitable(resp):
                        resp = await resp  # ⭐ the SDK call is a coroutine — AWAIT it (B1 fix).
                    pct = _percentage_of(resp)
                    if pct is not None:
                        return pct
            except Exception:
                # The live call is best-effort; fall through to the usage-derived fallback.
                log.debug("get_context_usage() failed; using usage fallback", exc_info=True)
        # Fallback: the honest ratio from the last completed turn's usage (§2.1).
        tokens = self._last_usage_tokens
        window = self._last_context_window
        if tokens is not None and window is not None and window > 0:
            try:
                return round(100 * tokens / window)
            except Exception:  # pragma: no cover - arithmetic guard (RB1)
                return None
        return None

    async def stop(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None
            # STATUSLINE T-SL-CORE: drop the ctx-% fallback cache with the session — it
            # described THAT session's context; a fresh session starts with no figure (→ ctx —
            # until its first turn completes), never a stale carryover. RB3 (in-memory only).
            self._last_usage_tokens = None
            self._last_context_window = None
            # Drop the captured model id with the session — a fresh session re-captures its own
            # model from its first ``init`` event (never a stale carryover). RB3 (in-memory).
            self._last_model = None
            # OBSERVABILITY T1: drop the rolling-limit signal with the session — it described THAT
            # session's limit state; a fresh session starts with no signal (→ no 🪙 field / re-armed
            # warning) until its own first RateLimitEvent (never a stale carryover). RB3 (in-memory).
            self._last_limit_status = None
            self._last_limit_pct = None


__all__ = [
    "SdkSubstrate",
    "normalize",
    "INCREMENTAL_EVENT_TYPES",
    "_user_message_with_images",
]
