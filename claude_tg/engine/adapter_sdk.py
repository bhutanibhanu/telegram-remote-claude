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
import os
from typing import Any, AsyncIterator, Optional

from .substrate import DecisionCallback
from .types import (
    AskEvent,
    ErrorEvent,
    Event,
    PlanEvent,
    ResultEvent,
    StatusEvent,
    SubstrateDecision,
    TextEvent,
    ToolUseEvent,
)

# StreamEvent.event["type"] values that are genuine incremental model output
# (everything else — message_start/content_block_start/stop — is framing, not text).
# Mirrors c1_streaming.INCREMENTAL_EVENT_TYPES (the proven C1 contract).
INCREMENTAL_EVENT_TYPES = {"content_block_delta", "message_delta"}

# Tool names that arrive through the permission channel but are really interactive
# prompts (answered via the decision seam), not ordinary tool use.
ASK_TOOL = "AskUserQuestion"
PLAN_TOOL = "ExitPlanMode"


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


def normalize(msg: Any) -> Optional[Event]:
    """Map ONE raw SDK message/block-bearing message to a normalized event.

    Pure and side-effect-free (no I/O, no SDK client) so it is unit-testable with
    constructed SDK objects. Returns ``None`` for frames that carry no operator-facing
    event (e.g. pure framing ``StreamEvent``s, echoed ``UserMessage``s without an
    error). The SDK is imported lazily here so importing this module needs no SDK.

    Mapping (per `normalized_interface.md` §1):

    * ``SystemMessage(init)``                  -> ``StatusEvent(phase="init")``
    * ``StreamEvent`` (content/message delta)  -> incremental ``TextEvent``
    * ``AssistantMessage`` text blocks         -> assembled ``TextEvent``
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
            # Only text deltas carry renderable prose; thinking/signature deltas don't.
            if delta.get("type") in ("text_delta", "text"):
                text = delta.get("text") or ""
                if text:
                    return TextEvent(text=text, incremental=True, session_id=sid)
        return None  # framing / non-text delta -> no operator-facing event

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
    from claude_agent_sdk import TextBlock, ToolResultBlock, ToolUseBlock  # lazy

    if isinstance(block, TextBlock):
        text = block.text or ""
        if not text:
            return None
        return TextEvent(text=text, incremental=False, session_id=sid)

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
    ) -> None:
        self._cwd = str(cwd) if cwd is not None else None
        self._permission_mode = permission_mode
        self._decision_callback = decision_callback
        self._include_partial = include_partial_messages
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools

        self._client: Any = None  # ClaudeSDKClient | None (lazily typed)
        self.session_id: Optional[str] = None

    # -- options -------------------------------------------------------------

    def _build_options(self, resume: Optional[str] = None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions  # lazy

        kwargs: dict[str, Any] = {
            "permission_mode": self._permission_mode,
            "include_partial_messages": self._include_partial,
        }
        if self._cwd is not None:
            kwargs["cwd"] = self._cwd
        if self._decision_callback is not None:
            kwargs["can_use_tool"] = self._make_can_use_tool()
        if self._allowed_tools is not None:
            kwargs["allowed_tools"] = self._allowed_tools
        if self._disallowed_tools is not None:
            kwargs["disallowed_tools"] = self._disallowed_tools
        if resume:
            kwargs["resume"] = resume
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
            decision: SubstrateDecision = await callback(tool_name, tool_input, tool_use_id)
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

    async def resume(self, session_id: str) -> None:
        from claude_agent_sdk import ClaudeSDKClient  # lazy

        if not session_id:
            raise ValueError("resume() requires a non-empty session_id")
        if self._client is not None:
            raise RuntimeError("session already started; call stop() first")
        self._client = ClaudeSDKClient(options=self._build_options(resume=session_id))
        await self._client.connect()
        self.session_id = session_id

    async def send(self, prompt: str, *, timeout: float = 120.0) -> AsyncIterator[Event]:
        """Send one turn; async-yield normalized events. Bounded → fail-clean (RB2).

        The whole receive loop runs inside a try/except; a per-message
        ``asyncio.wait_for`` timeout OR any driver exception is converted into a
        single ``driver_error`` event and the stream ends. The session is never left
        hanging waiting on the SDK.
        """
        if self._client is None:
            raise RuntimeError("session not started; call start()/resume() first")

        try:
            await self._client.query(prompt)
            iterator = self._client.receive_response().__aiter__()
            while True:
                try:
                    msg = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    break
                self._capture_session_id(msg)
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

    async def stop(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None


__all__ = ["SdkSubstrate", "normalize", "INCREMENTAL_EVENT_TYPES"]
