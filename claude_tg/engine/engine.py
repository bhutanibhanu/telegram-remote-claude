"""The ``Engine`` — drives a :class:`~claude_tg.engine.substrate.Substrate`.

This is the production object behind the normalized interface. Its job:

* **lifecycle passthrough** — ``start`` / ``resume`` / ``send`` / ``stop`` over the
  injected substrate, carrying the session id;
* **events out** — expose the substrate's normalized event stream, *merged* with any
  operator-facing events the engine injects (the ``ask``/``plan`` it surfaces from the
  decision callback — see below);
* **decisions in (the SEAM)** — wire the substrate's permission/decision callback to
  the **async answer-hold** (ADR-002): a ``PendingDecision`` Future per interactive
  request, awaited inside the callback, resolved by the operator (:meth:`resolve`), a
  60-min backstop timer, or :meth:`cancel`.

T4 shipped a synchronous default provider so the lifecycle was testable; **T5**
replaces the provider body with the answer-hold **without changing the seam's shape**
(``on_tool_request(tool_name, tool_input, tool_use_id) -> SubstrateDecision`` is
unchanged — see :meth:`on_tool_request`).

**Why the engine injects the ask/plan.** On Substrate A the interactive
``AskUserQuestion`` / ``ExitPlanMode`` arrive through the ``can_use_tool`` permission
channel (this callback), which is a *different* path from the events-out stream that
``send()`` yields. To guarantee the operator SEES the prompt it must answer — with its
``tool_use_id`` so the answer can be routed back — the engine injects an
:class:`~claude_tg.engine.types.AskEvent` / :class:`~claude_tg.engine.types.PlanEvent`
into the outgoing stream the moment it registers the hold. The injection and the
substrate's own events are merged through one :class:`asyncio.Queue` so neither is
dropped and the await never deadlocks the stream.

**Interim P1 tool posture (design S3).** Ordinary tools (Write/Bash/Read/…) are
**auto-allowed** here — the request is allowed unchanged, echoing the original input as
the record. Per-tool permission GATING (deny-by-default, allow/deny buttons) is **P2**;
this introduces **no bypass flag** — it is the same single-allowlisted-chat,
default-permission posture P0 ran, just expressed at the engine seam. P2 replaces this
branch with real gating (see the marker in :meth:`on_tool_request`).

The engine owns the ``(session_id, cwd)`` coupling at the call site (wired in T7); the
substrate does not enforce the cwd-scoped-resume / double-attach rules (ADR-001 / C6),
the engine does.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

from .pending import DEFAULT_BACKSTOP_SECONDS, PendingRegistry
from .substrate import Substrate
from .types import (
    AskEvent,
    Decision,
    Event,
    PermissionVerdict,
    PlanEvent,
    StatusEvent,
    SubstrateDecision,
    decision_to_substrate,
)

log = logging.getLogger(__name__)

# Tool names that arrive through the permission channel but are really interactive
# prompts answered by the operator (held open via the answer-hold), not ordinary
# tool use. Mirrors adapter_sdk.ASK_TOOL / PLAN_TOOL (kept local to avoid importing
# the adapter — the engine is substrate-neutral).
ASK_TOOL = "AskUserQuestion"
PLAN_TOOL = "ExitPlanMode"

#: Sentinel pushed onto the merge queue when the substrate stream for a turn is
#: exhausted, so the consumer in :meth:`send` knows to stop once it is drained.
_STREAM_DONE = object()


class Engine:
    """Drives a single :class:`Substrate` session behind the normalized interface."""

    def __init__(
        self,
        substrate: Substrate,
        *,
        send_timeout: float = 120.0,
        backstop_seconds: float = DEFAULT_BACKSTOP_SECONDS,
    ) -> None:
        self._substrate = substrate
        self._send_timeout = send_timeout
        self._backstop_seconds = backstop_seconds
        # The answer-hold registry: PendingDecision Futures keyed by tool_use_id, with
        # the per-request backstop timer. notify() pushes the operator-facing event.
        self._pending = PendingRegistry(
            backstop_seconds=backstop_seconds,
            notify=self._on_backstop,
        )
        # The merge queue for the CURRENT turn (None when no turn is in flight). The
        # decision callback and the backstop notify push injected events onto it; the
        # substrate stream is drained onto it by send()'s producer task.
        self._out_queue: Optional[asyncio.Queue[Any]] = None

    # -- session id ----------------------------------------------------------

    @property
    def session_id(self) -> Optional[str]:
        """The current Claude session id (None before the substrate reports one)."""
        return self._substrate.session_id

    # -- the decision seam (the async answer-hold) ---------------------------

    async def on_tool_request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: Optional[str],
    ) -> SubstrateDecision:
        """Resolve one substrate tool/interactive request to a substrate decision.

        This is the callback the engine hands the substrate (see
        :class:`~claude_tg.engine.substrate.DecisionCallback`). Two paths:

        * **AskUserQuestion / ExitPlanMode** → the async answer-hold. Inject the
          corresponding :class:`AskEvent`/:class:`PlanEvent` (with ``tool_use_id``) so
          the operator sees the prompt, register a :class:`PendingDecision`, then
          ``await`` it bounded by the backstop. The resulting
          :class:`~claude_tg.engine.types.Decision` is mapped through the **single**
          load-bearing mapper :func:`~claude_tg.engine.types.decision_to_substrate`
          (native answers-map / plan-reject-rides-deny / allow-carries-updated_input).

        * **ordinary tools** (Write/Bash/Read/…) → **auto-allow**, echoing the original
          input as the record (the B ``updatedInput`` gotcha). P1 interim posture
          (design S3): NO per-tool gating, NO bypass flag — P2 replaces this branch.
        """
        if tool_name in (ASK_TOOL, PLAN_TOOL) and tool_use_id is not None:
            return await self._answer_hold(tool_name, tool_input, tool_use_id)

        # --- ordinary tool: auto-allow (P1 interim posture; gating is P2) -------
        # NOTE(P2): this is THE site where per-tool permission gating (deny-by-default,
        # allow/deny/allow-session, the risk classifier) replaces auto-allow. P1
        # introduces NO bypass — a bare allow echoes the original input as the record
        # (the B updatedInput gotcha; decision_to_substrate guarantees a dict), exactly
        # the substrate-default posture P0 ran inside the single allowlisted chat
        # (design S3 / SB6). No bypass flag is introduced.
        log.debug("auto-allow ordinary tool %s (P1 interim; P2 gates)", tool_name)
        return decision_to_substrate(
            PermissionVerdict(behavior="allow"), tool_input=tool_input
        )

    async def _answer_hold(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
    ) -> SubstrateDecision:
        """Surface an ask/plan to the operator and hold the request open (ADR-002)."""
        # 1) Make the operator SEE the prompt with its tool_use_id (so resolve() can
        #    route the answer). Injected into the SAME outgoing stream send() yields.
        self._inject(_interactive_event(tool_name, tool_input, tool_use_id, self.session_id))

        # 2) Register the PendingDecision + backstop and AWAIT the operator's answer.
        #    resolve()/cancel()/backstop are the only things that complete this.
        decision = await self._pending.await_decision(tool_use_id, tool_name)

        # 3) Map the decision through the single load-bearing mapper (every [FLAG]).
        return decision_to_substrate(decision, tool_input=tool_input)

    # -- engine API for the bot (T7 will call these) -------------------------

    def resolve(self, tool_use_id: str, decision: Decision) -> bool:
        """Route an operator decision to its pending request (the answer-hold).

        The bot's SB1-allowlist-checked callback handler (T7) calls this when a button
        tap / "Other" reply / plan verdict arrives; the awaiting callback in
        :meth:`on_tool_request` unblocks and returns the mapped substrate decision. An
        **unknown / already-resolved id is a no-op** (returns ``False``, never raises) —
        a stray or late tap cannot crash the engine (RB1). Returns ``True`` iff a
        pending request was resolved.
        """
        return self._pending.resolve(tool_use_id, decision)

    def cancel(self, tool_use_id: Optional[str] = None) -> int:
        """Cancel a pending request, or the whole in-flight turn, cleanly (RB4).

        With a ``tool_use_id`` it aborts that one pending request; with ``None`` it
        aborts every pending request in the turn. Each is resolved as a **clean abort
        (deny)** so the awaiting callback returns and the session is not wedged (the SDK
        gets a prompt deny rather than a hung callback). The full disconnect/teardown is
        :meth:`stop`; this is the in-turn ``/cancel``. Returns the number of pending
        requests aborted. Unknown id / nothing pending is a no-op (returns 0).
        """
        return self._pending.cancel(tool_use_id)

    # -- lifecycle passthrough ----------------------------------------------

    async def start(self) -> None:
        """Establish a fresh session (host CLI auth; no API key)."""
        await self._substrate.start()
        log.debug("engine started; session_id=%s", self.session_id)

    async def resume(self, session_id: str) -> None:
        """Re-attach to an existing session by id (cwd-scoped — engine-owned, C6)."""
        await self._substrate.resume(session_id)
        log.debug("engine resumed session_id=%s", self.session_id)

    async def send(self, prompt: str, *, timeout: Optional[float] = None) -> AsyncIterator[Event]:
        """Send one operator turn; async-yield normalized events out.

        Merges TWO sources onto one stream so the operator sees everything in order and
        nothing deadlocks:

        * the substrate's bounded, fail-clean event stream (RB2 lives in the adapter —
          a timeout/driver error surfaces as a ``driver_error`` event, not an
          exception), drained by a producer task onto an :class:`asyncio.Queue`;
        * engine-**injected** events — the ``ask``/``plan`` the decision callback
          surfaces, and any backstop notify — pushed onto the same queue.

        The substrate stream stays blocked inside its ``receive_response()`` while the
        decision callback holds a request open; meanwhile the already-queued ``ask``
        flows out to the operator, whose :meth:`resolve` unblocks the callback and lets
        the turn continue. The consumer ends once the substrate producer signals done
        AND the queue is drained.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._out_queue = queue
        producer = asyncio.create_task(
            self._drain_substrate(prompt, timeout or self._send_timeout, queue),
            name="substrate-drain",
        )
        try:
            done = False
            while not done:
                item = await queue.get()
                if item is _STREAM_DONE:
                    done = True
                    # Drain anything injected up to the sentinel before stopping.
                    while not queue.empty():
                        leftover = queue.get_nowait()
                        if leftover is not _STREAM_DONE:
                            yield leftover
                    break
                yield item
            # Surface a producer failure (should not happen — RB2 keeps it inside the
            # stream — but never swallow one silently).
            await producer
        finally:
            if not producer.done():
                producer.cancel()
            self._out_queue = None

    async def _drain_substrate(
        self, prompt: str, timeout: float, queue: "asyncio.Queue[Any]"
    ) -> None:
        """Producer: push every substrate event onto ``queue``, then the sentinel.

        The substrate's ``send`` is already bounded + fail-clean (RB2): a timeout or
        driver error is yielded as a ``driver_error`` event, not raised, so this loop
        always terminates with the sentinel and never hangs.

        **Dedup the interactive ask/plan (the single point — engine policy).** The
        interactive ``AskUserQuestion`` / ``ExitPlanMode`` reach the operator on TWO
        paths: (1) the adapter maps the assistant-message ``ToolUseBlock`` to an
        ``AskEvent`` / ``PlanEvent`` onto this substrate stream — but this arrives
        *before* the SDK fires ``can_use_tool``, so **no pending decision exists yet**
        and a decision made against it would be lost (``resolve() -> False``); and (2)
        the engine injects the authoritative ``AskEvent`` / ``PlanEvent`` from the
        permission channel in :meth:`_answer_hold`, *synced with* registering the
        pending (``PendingRegistry.await_decision`` registers synchronously before its
        first await, so the injected copy is always resolvable). These two tools ALWAYS
        traverse ``can_use_tool`` (P0 C3/C4 + the T9 live run), so every substrate-stream
        ask/plan is paired with an engine-injected one — we drop the substrate copy here.
        Net: exactly ONE ask/plan per request reaches the operator, and it is always
        resolvable (no duplicate keyboard, no pre-registration race).

        (Assumption: production does NOT pre-approve these via ``allowed_tools`` OR a
        ``permissions.allow`` rule in the user/project ``~/.claude`` settings the SDK reads
        — either would suppress ``can_use_tool`` for that tool, so the injected copy would
        not come and this drop would remove a prompt with no replacement. The engine never
        sets ``allowed_tools`` for ask/plan (see ``adapter_sdk._make_can_use_tool``); a
        hand-added settings allow-rule for ``AskUserQuestion``/``ExitPlanMode`` is the only
        way to break this invariant.)
        """
        try:
            async for event in self._substrate.send(prompt, timeout=timeout):
                if isinstance(event, (AskEvent, PlanEvent)):
                    # Drop: the engine injects the authoritative, pending-synced copy.
                    log.debug(
                        "dropping substrate-stream %s (id=%s); engine injects the "
                        "authoritative copy via the permission channel",
                        type(event).__name__,
                        getattr(event, "tool_use_id", None),
                    )
                    continue
                await queue.put(event)
        finally:
            await queue.put(_STREAM_DONE)

    # -- event injection -----------------------------------------------------

    def _inject(self, event: Event) -> None:
        """Push an engine-generated event onto the current turn's outgoing stream.

        Used to surface the ``ask``/``plan`` (so the operator can answer) and the
        backstop notify. If no turn is in flight (no queue) the event is dropped with a
        debug log rather than raising — the answer-hold mechanism still functions; the
        operator simply would not have a live stream to render it on (shouldn't happen
        on the live path, where the callback only fires mid-``send``).
        """
        queue = self._out_queue
        if queue is None:
            log.debug("no active stream to inject %s onto (dropped)", type(event).__name__)
            return
        queue.put_nowait(event)

    async def _on_backstop(self, tool_use_id: str, reason: str) -> None:
        """Notify hook the backstop timer fires: emit an operator-facing status event.

        The pending request is auto-resolved to DENY by the registry; here we tell the
        operator it happened (a ``status`` event carrying the reason) and leave the
        session usable (RB4-shape, proven in T1.5).
        """
        self._inject(
            StatusEvent(
                phase="connected",
                session_id=self.session_id,
                detail=f"{reason} [tool_use_id={tool_use_id}]",
            )
        )

    async def stop(self) -> None:
        """Tear down the session. Idempotent.

        Aborts any still-pending decisions cleanly first (so a held callback is not left
        awaiting when the substrate goes away — RB2/RB4), then stops the substrate.
        """
        self._pending.cancel()  # clean-abort every pending hold (no-op if none)
        await self._substrate.stop()
        log.debug("engine stopped")


# ---------------------------------------------------------------------------
# Small helper (module-level + pure so it is trivially testable)
# ---------------------------------------------------------------------------


def _interactive_event(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    session_id: Optional[str],
) -> Event:
    """Build the operator-facing event for an ask/plan held request.

    Mirrors the adapter's ``normalize`` mapping for the same tool blocks so the injected
    event is shape-identical to one that would come off the events-out stream — only the
    source differs (the permission channel vs an assistant block).
    """
    if tool_name == ASK_TOOL:
        questions = tool_input.get("questions")
        return AskEvent(
            questions=list(questions) if isinstance(questions, list) else [],
            tool_use_id=tool_use_id,
            session_id=session_id,
        )
    # PLAN_TOOL
    return PlanEvent(
        plan=str(tool_input.get("plan", "")),
        tool_use_id=tool_use_id,
        session_id=session_id,
    )


__all__ = ["Engine"]
