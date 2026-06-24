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

**P2 tool posture (ADR-003).** Ordinary tools (Write/Bash/Read/…) are now run through
a fail-closed permission gate (replacing P1's interim auto-allow). The engine consults
an injected :class:`~claude_tg.permissions.PermissionPolicy`: a tool the policy reports
as **allowed** (safe read/search, a live allow-session grant, or ``/yolo``) runs free
with no prompt; a **risky, not-granted** tool is **held for approval** — a
:class:`~claude_tg.engine.types.PermissionEvent` (body-free summary, SB3) is injected
onto the turn stream and the request is held via the **same** ``PendingRegistry`` the
ask/plan answer-hold uses, until the operator's
:class:`~claude_tg.engine.types.PermissionDecision` (allow-once / allow-session / deny)
resolves it. The default ``PermissionPolicy`` gates risky tools, so the engine is
**fail-closed by default**; ``/yolo`` is the one loud, per-session bypass (no
``--dangerously-skip-permissions`` on this path, SB5). See :meth:`on_tool_request` /
:meth:`_permission_hold`.

The engine owns the ``(session_id, cwd)`` coupling at the call site (wired in T7); the
substrate does not enforce the cwd-scoped-resume / double-attach rules (ADR-001 / C6),
the engine does.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Sequence

from ..permissions import PermissionPolicy, path_needs_approval
from ..util import _redact_sid
from .pending import DEFAULT_BACKSTOP_SECONDS, PendingRegistry
from .substrate import Substrate
from .types import (
    DENIED_MESSAGE,
    AskEvent,
    Decision,
    Event,
    ImageInput,
    PermissionDecision,
    PermissionEvent,
    PermissionVerdict,
    PlanEvent,
    StatusEvent,
    SubstrateDecision,
    decision_to_substrate,
    safe_input_summary,
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
        permission_policy: PermissionPolicy | None = None,
        cwd: str | None = None,
        allowed_roots: tuple[str | Path, ...] = (),
        allow_any_path: bool = False,
    ) -> None:
        self._substrate = substrate
        self._send_timeout = send_timeout
        self._backstop_seconds = backstop_seconds
        # The per-session permission policy the gate consults (ADR-003). Defaulting to a
        # FRESH PermissionPolicy() makes the engine fail-closed: a fresh policy has no
        # grants and yolo off, so every risky tool gates. The bot (T5) injects the
        # session's shared policy so /yolo + allow-session grants + /reset-clear apply.
        self._policy = permission_policy if permission_policy is not None else PermissionPolicy()
        # P6/C2 (SB2): the SDK-tool path-confinement context. ``cwd`` is the project's
        # fixed working dir (used to resolve a tool's relative path AND as the default
        # target for optional-path tools like Glob/Grep); ``allowed_roots`` are the
        # canonical roots a tool's target must sit inside; ``allow_any_path`` is the
        # explicit ALLOW_ANY_PATH opt-out that disables the path policy. **Defaults
        # (cwd=None, allowed_roots=(), allow_any_path=False) make the path layer a no-op**
        # — an Engine built without them behaves exactly as before (every existing test
        # + the synchronous-provider lifecycle is unchanged): the path check is SKIPPED
        # when there is no cwd to resolve against. The bot's _default_engine_factory wires
        # the real config in (so the live path is confined). See on_tool_request.
        self._cwd = cwd
        self._allowed_roots = allowed_roots
        self._allow_any_path = allow_any_path
        # The answer-hold registry: PendingDecision Futures keyed by tool_use_id, with
        # the per-request backstop timer. notify() pushes the operator-facing event.
        # Shared by the ask/plan answer-hold AND the P2 permission hold (RB4 for free).
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
        :class:`~claude_tg.engine.substrate.DecisionCallback`). Three paths:

        * **AskUserQuestion / ExitPlanMode** → the async answer-hold. Inject the
          corresponding :class:`AskEvent`/:class:`PlanEvent` (with ``tool_use_id``) so
          the operator sees the prompt, register a :class:`PendingDecision`, then
          ``await`` it bounded by the backstop. The resulting
          :class:`~claude_tg.engine.types.Decision` is mapped through the **single**
          load-bearing mapper :func:`~claude_tg.engine.types.decision_to_substrate`
          (native answers-map / plan-reject-rides-deny / allow-carries-updated_input).
          This branch is **unchanged** by P2 — ask/plan are answered, not gated.

        * **ordinary tool the policy ALLOWS** (safe read/search, a live allow-session
          grant, or ``/yolo``) → **allow** with no prompt, echoing the original input as
          the record (the B ``updatedInput`` gotcha).

        * **ordinary RISKY tool, not granted** → **hold for approval**
          (:meth:`_permission_hold`): inject a :class:`PermissionEvent` (body-free
          summary, SB3) and hold the request on the SAME ``PendingRegistry`` until the
          operator's :class:`PermissionDecision` (or the backstop/cancel) resolves it
          (ADR-003 §2/§4; replaces P1's interim auto-allow).
        """
        if tool_name in (ASK_TOOL, PLAN_TOOL) and tool_use_id is not None:
            return await self._answer_hold(tool_name, tool_input, tool_use_id)

        # --- ordinary tool: the permission gate (P2 name-only + P6/C2 path layer) ----
        # Ordering (owner-approved posture, PROMPT-ON-OUT-OF-ROOT):
        #   1. /yolo (D6) bypasses EVERYTHING — the operator took the wheel; an out-of-root
        #      call is allowed under yolo (the explicit allow-all opt-out, checked first).
        #   2. P6/C2 path layer (SB2): a file/search tool whose RESOLVED target is OUTSIDE
        #      allowed_roots must be approved — even an otherwise-auto SAFE tool (Read/Glob/
        #      LS) and even a session-GRANTED risky one (Write/Edit) — so an out-of-root
        #      call ALWAYS re-prompts. This comes BEFORE the name-only safe/grant
        #      short-circuit and is disabled by ALLOW_ANY_PATH=true (the other opt-out) and
        #      when no cwd is wired (the path layer is then a no-op — see __init__).
        #   3. otherwise the P2 name-only verdict: safe→auto, risky→grant-or-prompt.
        if not self._policy.yolo and self._path_out_of_root(tool_name, tool_input):
            # Out-of-root + not yolo → hold for approval regardless of name/grant. A risky
            # tool with no tool_use_id still can't open a resolvable hold (fail closed →
            # deny, below); an out-of-root SAFE tool with no id would be vanishingly rare on
            # the live path but is handled the same fail-closed way.
            log.debug(
                "tool %s target is outside allowed_roots — requiring approval (C2/SB2)",
                tool_name,
            )
        elif not self._policy.needs_approval(tool_name, tool_input):
            log.debug("policy allows tool %s without prompt", tool_name)
            return decision_to_substrate(
                PermissionVerdict(behavior="allow"), tool_input=tool_input
            )

        # Risky + not granted → hold for an operator verdict. A permission hold needs a
        # tool_use_id to route the verdict back (mirrors the ask/plan guard); if a risky
        # tool somehow arrives without one we CANNOT open a resolvable hold, so we fail
        # CLOSED and deny rather than auto-allow (SB6 — never run a risky tool we can't
        # gate). This should not happen on the live path (the SDK supplies an id).
        if tool_use_id is None:
            log.warning(
                "risky tool %s arrived with no tool_use_id; cannot route approval — "
                "failing closed (deny)",
                tool_name,
            )
            return decision_to_substrate(
                PermissionVerdict(behavior="deny", message=DENIED_MESSAGE),
                tool_input=tool_input,
            )

        return await self._permission_hold(tool_name, tool_input, tool_use_id)

    def _path_out_of_root(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Return ``True`` iff this tool's target path is outside ``allowed_roots`` (C2/SB2).

        A thin, side-effect-free wrapper over the pure
        :func:`~claude_tg.permissions.path_needs_approval` that supplies the engine's
        wired path context (``cwd`` / ``allowed_roots`` / ``allow_any_path``). **When no
        ``cwd`` is wired the path layer is a no-op** (returns ``False``) — an Engine built
        without the P6 path context (every pre-C2 construction, incl. the test fakes and
        the synchronous-provider lifecycle) behaves exactly as before. ``Bash`` and the
        no-path tools return ``False`` here by construction (``path_needs_approval`` only
        governs the explicit-path file/search tools — the documented C2 boundary).
        """
        if self._cwd is None:
            return False
        return path_needs_approval(
            tool_name,
            tool_input,
            cwd=self._cwd,
            allowed_roots=self._allowed_roots,
            allow_any_path=self._allow_any_path,
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

    async def _permission_hold(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
    ) -> SubstrateDecision:
        """Hold a risky tool for operator approval and map the verdict (ADR-003 §2/§4).

        Mirrors :meth:`_answer_hold` — same inject + hold + map shape, reusing the SAME
        ``PendingRegistry`` — but for a permission prompt rather than an ask/plan:

        1. Inject a :class:`PermissionEvent` (BODY-FREE summary, SB3) onto the turn
           stream so the operator SEES the ``[Allow once] / [Allow for session] / [Deny]``
           choice, correlated by ``tool_use_id``.
        2. Register + ``await`` the pending decision (bounded by the backstop). It is
           resolved by exactly one of: the operator's :class:`PermissionDecision`
           (:meth:`resolve`), the 60-min backstop (auto-deny + notify), or
           :meth:`cancel` — RB4 comes for free from the shared registry.
        3. Map the resolved decision to a substrate verdict:

           * :class:`PermissionDecision` ``allow_once`` → allow (this request only).
           * :class:`PermissionDecision` ``allow_session`` → record the per-tool-NAME
             grant **here, on resolve** (so the SECOND use of this tool this session
             auto-allows — D4) **then** allow. The grant is recorded in the engine, not
             the pure mapper, because it is a side effect over the policy.
           * :class:`PermissionDecision` ``deny`` → deny carrying :data:`DENIED_MESSAGE`
             (D5).
           * a **backstop / cancel** resolution arrives NOT as a ``PermissionDecision``
             but as a :class:`PermissionVerdict` ``deny`` (the registry's backstop) or a
             :class:`Cancel` (``/cancel`` / turn cancel). Both fall through to
             :func:`decision_to_substrate`, which maps each to a substrate **deny** — so
             a backstopped or cancelled permission request becomes a deny, never hangs
             and never auto-allows (RB4).

        **ADR-001 caveat:** ``allow_session`` grants ONLY ``tool_name`` (per-name, T2);
        a different risky tool is unaffected — nothing here broadens the grant.
        """
        # 1) Surface the prompt with its tool_use_id; the summary is body-free (SB3).
        self._inject(
            PermissionEvent(
                tool_name=tool_name,
                tool_input_summary=safe_input_summary(tool_name, tool_input),
                tool_use_id=tool_use_id,
                session_id=self.session_id,
            )
        )

        # 2) Hold on the SHARED registry until resolved (operator / backstop / cancel).
        decision = await self._pending.await_decision(tool_use_id, tool_name)

        # 3) Map the resolved decision. An operator PermissionDecision is translated to a
        #    PermissionVerdict (recording an allow-session grant as a side effect first);
        #    a backstop PermissionVerdict(deny) or a Cancel falls straight through to the
        #    single mapper, which denies it (RB4 — never auto-allow a backstop/cancel).
        if isinstance(decision, PermissionDecision):
            decision = self._verdict_for(tool_name, decision)
        return decision_to_substrate(decision, tool_input=tool_input)

    def _verdict_for(
        self, tool_name: str, decision: PermissionDecision
    ) -> PermissionVerdict:
        """Translate an operator :class:`PermissionDecision` into a permission verdict.

        Records the per-tool-NAME allow-session grant (D4) as a side effect for
        ``allow_session`` BEFORE returning the allow, so the grant is in place the next
        time this tool is requested this session (the false-pass guard: drop this
        ``grant_session`` and the allow-session-suppresses test re-prompts on the second
        use). ``deny`` carries the canned :data:`DENIED_MESSAGE` (D5).
        """
        if decision.verdict == "allow_session":
            # Record the grant HERE, on resolve, keyed by NAME only (ADR-001 caveat:
            # this greenlights nothing about any other risky tool).
            self._policy.grant_session(tool_name)
            return PermissionVerdict(behavior="allow")
        if decision.verdict == "allow_once":
            return PermissionVerdict(behavior="allow")
        # deny (D5) — the canned message the model adapts to (no free-text reason in P2).
        return PermissionVerdict(behavior="deny", message=DENIED_MESSAGE)

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
        # SB3/H1: log a redacted, correlatable tag — never the raw resumable session id.
        log.debug("engine started; %s", _redact_sid(self.session_id))

    async def resume(self, session_id: str) -> None:
        """Re-attach to an existing session by id (cwd-scoped — engine-owned, C6)."""
        await self._substrate.resume(session_id)
        # SB3/H1: redacted tag only (the raw id is a credential — see _redact_sid).
        log.debug("engine resumed %s", _redact_sid(self.session_id))

    async def send(
        self,
        prompt: str,
        *,
        timeout: Optional[float] = None,
        images: Optional[Sequence[ImageInput]] = None,
    ) -> AsyncIterator[Event]:
        """Send one operator turn; async-yield normalized events out.

        **P10 T1 — optional ``images``.** Defaults to ``None`` (the unchanged text turn);
        when supplied it is threaded straight through to the substrate's ``send`` so the
        turn is multimodal (prompt + pixels). The merge/inject/decision machinery below is
        identical for both — only the substrate's ``query`` argument differs.

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
            self._drain_substrate(prompt, timeout or self._send_timeout, queue, images=images),
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
        self,
        prompt: str,
        timeout: float,
        queue: "asyncio.Queue[Any]",
        *,
        images: Optional[Sequence[ImageInput]] = None,
    ) -> None:
        """Producer: push every substrate event onto ``queue``, then the sentinel.

        **P10 T1:** ``images`` (default ``None`` → text turn) is threaded straight to the
        substrate's ``send`` so a multimodal turn streams the prompt + pixels; everything
        else (the ask/plan dedup below, the sentinel) is unchanged.

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
        # P10 T1: thread ``images`` to the substrate ONLY when present, so the pure TEXT
        # turn calls ``send(prompt, timeout=…)`` with the EXACT pre-P10 signature — every
        # existing substrate (incl. the test fakes whose ``send`` has no ``images`` kwarg)
        # is unchanged. The optional kwarg is added to the Protocol for the image path; a
        # text turn never exercises it, so an old-shape fake keeps working verbatim.
        send_kwargs: dict[str, Any] = {"timeout": timeout}
        if images:
            send_kwargs["images"] = images
        try:
            async for event in self._substrate.send(prompt, **send_kwargs):
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
