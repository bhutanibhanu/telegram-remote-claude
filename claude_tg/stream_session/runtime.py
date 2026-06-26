"""Runtime dataclasses + module-level pure helpers for the streaming session package.

These carry per-project / per-chat *transient* state (``_ProjectRuntime`` / ``_ChatState`` &
co.) and the small pure functions that operate on engine events — but **none** of them depend
on :class:`StreamingSession`. They depend only on leaf layers (``engine`` / ``render`` /
``permissions`` / ``audit`` / ``claude_runner``) and on :mod:`.types`, so they sit one level
above ``types.py`` in the import graph with no cycle. Relocated verbatim from the original
single-file ``stream_session.py`` (behavior-preserving — see
``docs/features/core-refactor/design.md`` §2).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from ..audit import AuditSink
from ..claude_runner import ClaudeResult, ClaudeRunner
from ..engine import (
    AskEvent,
    Engine,
    ErrorEvent,
    Event,
    PermissionEvent,
    PlanEvent,
    ResultEvent,
    SubstrateDecision,
    TextEvent,
)
from ..engine.adapter_sdk import SdkSubstrate
from ..permissions import PermissionPolicy
from ..render import ChatSendGate, ProjectStatus
from .types import PendingKind


class EngineFactory(Protocol):
    """Builds an :class:`Engine` for a project (injected so tests pass a mock).

    The default production factory wires an :class:`SdkSubstrate` (Substrate A) with
    the engine's decision callback; tests pass a factory returning a scripted fake.
    ``permission_policy`` is the project's single per-session :class:`PermissionPolicy`
    (P2, ADR-003): the SAME object the session mutates via ``/yolo`` and clears on
    ``/reset``, handed in so the engine's gate and the session act on one policy.
    """

    def __call__(
        self, *, cwd: str, backstop_seconds: float, permission_policy: PermissionPolicy
    ) -> Engine: ...


def _default_engine_factory(
    *,
    cwd: str,
    backstop_seconds: float,
    permission_policy: PermissionPolicy,
    allowed_roots: tuple[Path, ...] = (),
    allow_any_path: bool = False,
    send_timeout: float = 120.0,
    model: Optional[str] = None,
    permission_mode: str = "default",
    thinking: bool = False,
    effort: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
    bash_policy_mode: str = "off",
    bash_policy_extra_patterns: tuple[str, ...] = (),
) -> Engine:
    """Production factory: an :class:`Engine` over Substrate A for ``cwd``.

    The substrate's ``decision_callback`` is the engine's own ``on_tool_request`` seam
    (the async answer-hold + the permission gate). No bypass / skip-permissions flag
    is set (SB5): the engine consults the injected ``permission_policy`` and is
    fail-closed by default — risky tools are held for approval unless a grant or
    ``/yolo`` allows them. ``permission_policy`` is the project's shared policy (the one
    the session mutates), so ``/yolo``, allow-session grants, and ``/reset``-clear all
    act on a single object.

    **P6/C2 (SB2):** ``allowed_roots`` + ``allow_any_path`` (the same config the bot uses
    to confine ``/cd``) are handed to the engine along with ``cwd`` so the engine confines
    the paths the SDK's file/search tools ACT on — an out-of-root Read/Write/Glob/… is
    held for approval even when name-only-safe or session-granted (see
    :meth:`~claude_tg.engine.engine.Engine.on_tool_request`). The session binds the live
    config into this factory in ``StreamingSession.__init__`` (see ``_bound_factory``); the
    defaults here keep the path layer a no-op for a bare call.

    **P6/H2/RB2:** ``send_timeout`` is the engine's per-message liveness bound (threaded to
    :class:`~claude_tg.engine.engine.Engine`'s ``send_timeout`` → the substrate's per-message
    ``asyncio.wait_for``). It is SUSPENDED while a decision hold is open and otherwise also
    bounds APPROVED long-running tool execution, so the live bot passes the GENEROUS
    ``config.stream_message_timeout_seconds`` via ``_bound_factory``; the 120 s default here
    only keeps a bare/legacy call's behavior unchanged.

    **T4 (P9):** ``model`` is the per-project model override threaded into the substrate's
    ``ClaudeAgentOptions(model=…)`` at session-creation time (``/fast`` → Haiku, ``/deep`` →
    Opus, ``/auto`` → ``None`` = the SDK/``CLAUDE_MODEL`` default). ``None`` (the default here,
    and what ``/auto`` resolves to) omits ``model`` entirely so behavior is unchanged when no
    override is set. The session resolves the per-project model from the store and passes it
    via ``_bound_factory`` at each ``_ensure_engine`` build, so a ``/fast``/``/deep`` takes
    effect on the NEXT fresh session for that project (model is a session-creation param,
    never hot-swapped mid-session).

    **P12 T-PLAN-1:** ``permission_mode`` is the per-turn SDK permission mode baked into the
    substrate's ``ClaudeAgentOptions(permission_mode=…)`` at session-creation time (mechanism
    (a) — mirrors ``model``). The default ``"default"`` is the unchanged normal turn; the
    session passes ``"plan"`` for exactly the ONE turn armed by ``/plan`` (the one-shot marker
    on ``_ProjectRuntime`` — ``_ensure_engine`` builds a FRESH plan-mode session for that
    turn, then the marker is cleared so the NEXT turn is a normal ``"default"`` session again).
    A plan turn surfaces Claude's ``ExitPlanMode`` plan through the SHIPPED P6 hold/keyboard;
    approving it does NOT auto-allow later tools (ADR-001 C4 — every risky tool still hits the
    permission gate independently, unchanged here). Transient (RB3): the arming never persists.

    **P12 T-THINK:** ``thinking`` is the per-project live-reasoning flag baked into the
    substrate (mirrors ``model`` / ``permission_mode`` — a session-creation knob). When True
    the substrate streams Claude's readable reasoning (``thinking={"type":"adaptive",
    "display":"summarized"}`` + ``include_partial_messages=True``) as ``ThinkingEvent``s →
    the capped ``🧠`` status line. The default ``False`` is byte-for-byte the pre-P12 turn:
    no ``thinking`` option, ``include_partial_messages`` stays off → no extra wire traffic.
    Off by default (cost + flood posture); toggled per project by ``/thinking`` (transient,
    RB3). SB3: the reasoning TEXT is shown; the opaque signature is dropped in ``normalize``.

    **T-EFFORT (STATUSLINE):** ``effort`` is the per-project reasoning-EFFORT override
    (``/effort low…max``) baked into the substrate's ``ClaudeAgentOptions(effort=…)`` at
    session-creation time (mirrors ``model`` — a session-creation knob, distinct from the P12
    ``thinking`` VISIBILITY toggle). ``None`` (the default here, and what a bare ``/effort``
    clears to) omits ``effort`` entirely so behavior is byte-for-byte unchanged when no
    override is set and the SDK's own default effort (``high``) applies. There is NO
    ``CLAUDE_*`` global default for effort: the session resolves the per-project override (else
    ``None``) and passes it via ``_bound_factory`` at each ``_ensure_engine`` build, so an
    ``/effort`` change takes effect on the NEXT fresh session for that project (never hot-swapped
    mid-session).

    **P13 T-AUDIT:** ``audit_sink`` is the optional, BODY-FREE audit sink the engine records
    every gate decision to (a :class:`~claude_tg.audit.ChatBoundSink` over the process
    :class:`~claude_tg.audit.AuditLog`, bound per chat by ``StreamingSession._build_engine``).
    The default ``None`` makes the engine's audit hook a no-op, so a bare factory call (or a
    deploy with audit disabled) is byte-for-byte unchanged. Best-effort (RB1): an audit write
    never breaks a turn.

    **P13 T-BASH:** ``bash_policy_mode`` (``flag``/``deny``/``off``) + ``bash_policy_extra_patterns``
    are the Bash command-policy knobs threaded straight into the engine, where they are
    consulted ADDITIVELY in ``on_tool_request`` for ``Bash`` only (the C2-residual guardrail).
    The default ``"off"`` keeps a bare factory call (and a deploy with the policy off)
    byte-for-byte unchanged; the bot binds ``config.bash_policy_mode`` (default ``flag``) via
    ``_bound_factory``. The policy can only ESCALATE a matched dangerous command (prompt/deny),
    never auto-allow it.
    """
    engine: Engine

    async def decision_callback(
        tool_name: str, tool_input: dict, tool_use_id: Optional[str]
    ) -> SubstrateDecision:
        return await engine.on_tool_request(tool_name, tool_input, tool_use_id)

    substrate = SdkSubstrate(
        cwd=cwd,
        permission_mode=permission_mode,
        decision_callback=decision_callback,
        model=model,
        thinking=thinking,
        effort=effort,
    )
    engine = Engine(
        substrate,
        send_timeout=send_timeout,
        backstop_seconds=backstop_seconds,
        permission_policy=permission_policy,
        cwd=cwd,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
        audit_sink=audit_sink,
        bash_policy_mode=bash_policy_mode,
        bash_policy_extra_patterns=bash_policy_extra_patterns,
    )
    return engine


@dataclass
class _ProjectRuntime:
    """In-memory runtime for ONE project (engine + cwd + policy + its live-turn state).

    Per ADR-004: the durable identity (name, cwd, ``session_id``, timestamps) lives in
    the persisted registry; **this** is the transient runtime — created lazily in memory
    when a project is first used, dropped on a restart (so ``/yolo`` and allow-session
    grants never survive a restart, D3/SB5). ``cwd`` is the project's fixed cwd (D4),
    seeded from the registry record (falling back to ``config.workdir`` only if the
    record's cwd is missing). ``policy`` is a FRESH :class:`PermissionPolicy` per project
    (fail-closed: no grants, yolo off) — the SAME object handed to that project's engine
    and mutated by the session (``/yolo`` via :meth:`set_yolo`, dropped by ``policy.clear()``
    on ``/reset``).

    **P5 / ADR-005 D7 — live-turn state lives HERE (one level down from the chat).** P4
    held the status line + free-text-capture marker on :class:`_ChatState` *because* there
    was one turn per chat. With N concurrent runs each project's turn owns its OWN status
    line (``status_message_id``/``status_text``) and its OWN free-text-capture marker
    (``awaiting_text_*``), so a status edit / pending answer for one project never touches
    another's. A per-project :data:`~claude_tg.render.ProjectStatus` (``status``) feeds the
    ``/projects`` status column (T7).

    **P5 / ADR-005 D1 — the turn lock lives HERE too (T5: concurrency turns ON).** P4 held a
    single turn lock on :class:`_ChatState` (one turn per chat). T5 moves it to the
    per-project runtime (one :class:`asyncio.Lock` per project) so a message to an IDLE
    project starts a run even while OTHER projects run — N projects run concurrently per
    chat (PTB already dispatches handlers concurrently via ``concurrent_updates(True)``). A
    second message to the SAME running project still raises :class:`StreamingBusy` (one run
    per project — unchanged per-project UX). The lock guards ``handle_message``'s turn
    driver; it deliberately does NOT cover the resolve / cancel / free-text paths (they
    unblock a held turn parked inside ``engine.send`` and so must run concurrently with it —
    lock-free, preserved from T2/T4).
    """

    cwd: str
    engine: Optional[Engine] = None
    started: bool = False
    policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    # P5 / ADR-005 D1 (T5): THIS project's turn lock (one per project, moved off
    # _ChatState). Held by handle_message while this project's turn is driven; a second
    # message to the SAME project while it is held raises StreamingBusy, but a message to a
    # DIFFERENT (idle) project takes ITS OWN lock and runs concurrently. Lazily built in
    # __post_init__ so a runtime constructed off the event loop (e.g. a test that builds a
    # bare _ProjectRuntime) still gets a real Lock.
    lock: asyncio.Lock = None  # type: ignore[assignment]
    # P5 / ADR-005 D6 (round-2 cross-model-QA RACE): a per-project IN-FLIGHT marker that is
    # True CONTINUOUSLY from the moment ``handle_message`` accepts a turn for this project
    # (right after the busy-guard passes, BEFORE ``_acquire_slot``) until that turn's
    # end-of-turn ``finally``. It is the single source of truth for the busy-guard so "this
    # project has a turn in flight" stays true at EVERY lifecycle point — submission, queued,
    # the pop→lock slot-transfer window, and running — closing the TOCTOU where ``_is_queued``
    # (already popped) and ``lock.locked()`` (not yet acquired) are BOTH False for a beat and a
    # same-project 2nd message would slip through to create a 2nd ``_QueuedTurn`` (two turns for
    # one project, violating D6). ``is_busy`` and the pre-slot/post-wait guards consult it.
    # Set/cleared ONLY by ``handle_message`` (set after the guard; cleared in its outer
    # ``finally`` on every exit — normal end, mid-stream raise, /cancel of a running turn, a
    # DRAIN-cancel of a queued waiter, resume-failure, the post-wait StreamingBusy re-raise).
    # Transient in-memory like the rest of the runtime (RB3).
    inflight: bool = False
    # P5 / ADR-005 D9 (round-3 cross-model-QA BLOCKERS 1+2): a per-project ABORT signal that
    # makes the in-flight turn a first-class CANCELLABLE entity across its WHOLE lifecycle —
    # queued, the pop→lock slot-transfer window, AND running — the same way ``inflight`` made
    # it busy-guardable. ``handle_cancel`` / ``reset`` / ``forget_project`` SET it (alongside
    # draining a still-queued waiter + cancelling a live engine) so a control command targets
    # the turn no matter WHICH state it is in. The woken queued turn checks it the instant its
    # slot future resolves — BEFORE acquiring the lock / starting the engine / entering
    # ``_drive_turn`` — and if set aborts CLEANLY (releases the slot via the inner finally,
    # clears ``inflight`` via the outer finally, persists NOTHING, never runs). This closes the
    # gap where a turn in the pop→lock window is in NEITHER the run queue (``_drain_queued``
    # already popped it) NOR holding a live engine (``engine.cancel`` finds none) — so a
    # ``/cancel``|``/reset``|``/rm`` in that window used to miss it entirely and it ZOMBIE-RAN.
    # CLEARED at the start of each accepted turn (alongside ``inflight = True``) so a stale
    # abort from a previously-cancelled turn never kills a fresh one. Built lazily in
    # __post_init__ (like ``lock``); transient in-memory (RB3).
    abort: asyncio.Event = None  # type: ignore[assignment]
    # QF3 (B3/RB3): True from the moment ``engine.resume()`` SUCCEEDS until the first
    # turn on that resumed session completes WITHOUT a resume-failure-shaped error. A
    # stale/aged/torn session can resume "successfully" (connect) and then error on the
    # FIRST ``send`` — this flag tells :meth:`_drive_turn` the current turn is that first,
    # unconfirmed use of a resumed session, so it (and ONLY it) applies the
    # ``_is_resume_failure`` heuristic. A FRESH-started session never sets this, so a fresh
    # session erroring is never mistaken for a resume failure. Reset on the in-memory
    # runtime only (never persisted).
    resumed_unverified: bool = False
    # P12 T-PLAN-2 (/plan): a per-project, ONE-SHOT, in-memory marker — True from the moment
    # ``/plan`` arms this project until the NEXT turn for it consumes it. ``_ensure_engine``
    # reads + CLEARS it and builds that one turn's session in ``permission_mode="plan"`` (a
    # FRESH plan-mode session — mechanism (a), mirroring how ``model`` is baked at session
    # creation), so Claude reasons + proposes a plan and surfaces ``ExitPlanMode`` through the
    # SHIPPED P6 hold/keyboard. The turn AFTER is a normal ``"default"`` session again (the
    # marker is one-shot). Transient in-memory like the rest of the runtime (RB3): a process
    # restart drops it — the supervision posture NEVER silently survives a restart, and it is
    # never persisted to the registry. Set by :meth:`arm_plan`; consumed (read + cleared) in
    # :meth:`_ensure_engine`. ADR-001 C4: arming plan mode greenlights NOTHING about tools —
    # an approved plan's later risky tools still hit the permission gate independently.
    plan_next: bool = False
    # STATUSLINE T-SL-WIRE (B3 fix): True WHILE a plan-mode turn is actually running on this
    # project, so the statusline shows ``🔒 plan`` for the live plan turn's duration. The
    # one-shot ``plan_next`` above is CONSUMED (read + cleared) in ``handle_message`` BEFORE
    # ``_drive_turn`` runs, so by the time the plan turn is streaming ``plan_next`` is already
    # False — reading it in :meth:`_statusline_text` would wrongly show ``gate`` DURING the plan
    # turn. So ``_drive_turn`` sets this from the consumed ``plan_turn`` local at turn start and
    # CLEARS it in its finally (turn end) — the line reads THIS for the live mode. Transient
    # in-memory (RB3); a restart drops it (no turn is running across a restart anyway).
    in_plan_turn: bool = False
    # P12 T-PLAN: the SDK ``permission_mode`` the CURRENT live engine (``engine``) was built
    # with — ``"default"`` for an ordinary session, ``"plan"`` for the fresh session built for
    # an armed ``/plan`` turn. ``_ensure_engine`` records it at build time and consults it in
    # the warm fast-path: a warm engine is reused ONLY when its mode matches the turn's
    # requested mode, so (a) a normal turn after a plan turn rebuilds back to ``"default"``
    # (the plan-mode session is one-shot — it never silently lingers onto the next turn), and
    # (b) a plan turn never reuses a ``"default"`` warm engine (mechanism (a) is session-
    # creation — the mode can't be hot-switched). Transient in-memory (RB3); a restart rebuilds
    # the engine from the persisted id in ``"default"`` (the arming never persists).
    engine_permission_mode: str = "default"
    # P12 T-THINK (/thinking): this project's LIVE-REASONING toggle — False by default (cost +
    # flood posture; the SB5-style explicit opt-in). UNLIKE the one-shot ``plan_next`` this is
    # a STICKY per-project flag: it stays on until ``/thinking off`` (every turn while on
    # streams the 🧠 line). ``_ensure_engine`` reads it and, when on, builds the session with
    # ``thinking={"type":"adaptive","display":"summarized"}`` + ``include_partial_messages=True``
    # (mechanism (a) — a session-creation knob, mirroring ``model``); a change takes effect on
    # the NEXT fresh session for the project (a live session keeps streaming as built — we never
    # hot-swap). Transient in-memory (RB3): a restart drops it back to OFF (supervision posture
    # never silently survives a restart) — never persisted. Set by :meth:`set_thinking`.
    thinking: bool = False
    # P12 T-THINK: the ``thinking`` flag the CURRENT live engine was built with. ``_ensure_engine``
    # records it at build time and the warm fast-path reuses the engine ONLY when it matches the
    # turn's requested thinking flag — so toggling ``/thinking`` rebuilds the session on the next
    # turn (thinking is a session-creation knob; it can't be hot-switched), in EITHER direction
    # (off→on streams from the next turn; on→off stops the wire traffic from the next turn).
    # Transient (RB3); a restart rebuilds in the default OFF.
    engine_thinking: bool = False
    # T-EFFORT (STATUSLINE): the reasoning-EFFORT level the CURRENT live engine was built with
    # (the resolved per-project override, else ``None`` = SDK default). ``_ensure_engine``
    # records it at build time and the warm fast-path reuses the engine ONLY when it matches the
    # turn's requested effort — so changing ``/effort`` rebuilds the session on the NEXT turn
    # (effort is a session-creation knob baked into ``ClaudeAgentOptions``; it can't be
    # hot-switched), in either direction. UNLIKE ``engine_thinking`` the override itself is
    # PERSISTED (on the project, like the model override) — only this built-with marker is
    # transient (RB3): a restart resolves the persisted effort fresh and rebuilds. ``None`` (no
    # override) matches ``None`` → a back-to-back no-effort turn reuses the warm engine
    # byte-for-byte (the default-turn path is unchanged).
    engine_effort: Optional[str] = None
    # P11 T2 (attach-fork): True iff this project was ADOPTED from an external session that
    # was LIVE in another process at attach time, so its NEXT resume MUST fork (resume into a
    # fresh id, transcript copied) rather than continue the live id — two writers on one
    # ``(id, cwd)`` silently corrupt the transcript (THE hard safety rule). ``_ensure_engine``
    # reads this and passes ``fork=True`` to ``engine.resume`` for exactly that first resume;
    # it is CLEARED the moment the fork succeeds (the substrate then owns a brand-new id that
    # is ours alone, so every subsequent resume of THIS project is an ordinary continue of the
    # forked id — never re-forking). An IDLE attach leaves this False (continue the same id —
    # nobody else is writing it). Transient in-memory (RB3): a process restart loses it, but
    # the engine is rebuilt from the persisted (forked or continued) id, which is by then ours
    # alone, so a continue is correct after restart. Set by :meth:`attach_session`.
    attach_fork: bool = False
    # P5 / ADR-005 D7: THIS project's status line for in-place coalesced edits (created on
    # the first edit of its turn). Each project's turn has its OWN line so two concurrent
    # turns' status updates never clash (moved off _ChatState).
    status_message_id: Optional[int] = None
    # The text currently shown on THIS project's status line — used to SKIP an edit when
    # the new status is identical (editing a Telegram message to the same text raises
    # "message is not modified", whose fallback used to send a fresh message → spam).
    status_text: Optional[str] = None
    # P5 / ADR-005 D7: THIS project's free-text capture. When set, the NEXT plain message
    # is the answer/feedback for this tool_use_id, in this mode ("ask_other" ->
    # QuestionAnswer; "plan_reject" -> PlanVerdict(approve=False)). Question index is kept
    # for an "Other" answer. The owning project IS this runtime (the marker is per-project,
    # not a chat-global slot), so a free-text reply resolves the project that prompted it.
    awaiting_text_for: Optional[str] = None
    awaiting_text_mode: Optional[str] = None  # "ask_other" | "plan_reject"
    awaiting_text_question_index: Optional[int] = None
    # P5 / ADR-005 D5 (T9): the monotonic arm sequence (from _ChatState.armed_seq) at which
    # THIS project was armed for free-text capture. With several projects awaiting free text
    # the **most-recently-armed** is the default target (the name-echoed prompt said which);
    # the resolver picks the runtime with the HIGHEST armed_at. 0 = never armed. Reset to 0
    # when the marker is cleared (_clear_runtime_text) so a stale value can't win a later
    # routing decision.
    awaiting_text_armed_at: int = 0
    # P5 / ADR-005 D7: this project's run status for the /projects column (T7). Defaults to
    # "idle"; _drive_turn drives it idle->running->awaiting_<kind>->running->idle across a
    # turn (T6 sets "queued" for a queued turn). A project with no runtime reads as "idle".
    status: ProjectStatus = "idle"

    def __post_init__(self) -> None:
        # Build the per-project turn lock lazily (ADR-005 D1 / T5) so a runtime constructed
        # before/outside the running loop still gets a real asyncio.Lock — mirroring the
        # pattern _ChatState used for the (now-removed) chat-level lock.
        if self.lock is None:
            self.lock = asyncio.Lock()
        # The per-project abort signal (ADR-005 D9 / round-3 BLOCKERS 1+2), lazily built for
        # the same reason as ``lock`` (a runtime may be constructed off the running loop).
        if self.abort is None:
            self.abort = asyncio.Event()


def _pending_kind_of(event: Event) -> Optional[PendingKind]:
    """The :data:`PendingKind` an event holds open, or ``None`` if it holds nothing.

    Ask/Plan/Permission are the three interactive holds (D3); every other event
    (text/tool_use/status/error/result) carries no held request. Used by both the
    pending-index registration and the per-project status wiring (ADR-005 D7) so the two
    classify a held event identically.
    """
    if isinstance(event, AskEvent):
        return "ask"
    if isinstance(event, PlanEvent):
        return "plan"
    if isinstance(event, PermissionEvent):
        return "permission"
    return None


@dataclass
class _PendingRef:
    """One entry in the per-chat pending-request index (P5 / ADR-005 D3).

    Maps a ``tool_use_id`` to the project that owns the held request (so a decision-in
    routes to **that** project's engine, not ``_active_engine``), the request ``kind``,
    and the **held event** itself (the :class:`AskEvent`/:class:`PlanEvent`/
    :class:`PermissionEvent` the engine injected) so a tap can reconstruct the native
    answer/verdict. For an :class:`AskEvent` the per-question ``answers`` accumulator
    rides here too (keyed by question index), so a MULTI-question ask resolves only once
    every question is answered — now **per id** (the index entry), never a chat-global
    slot that two concurrent asks would clobber.

    ``project_name`` is the project's STORED (as-created) name (the same key the registry
    and ``runtimes`` use); the engine is looked up by it at resolve time. The whole entry
    is transient (in-memory on :class:`_ChatState`); it is torn down on
    resolve/cancel/turn-end so an id never leaks across turns.
    """

    project_name: str
    kind: PendingKind
    event: Event
    # Per-question answers for a MULTI-question AskUserQuestion (question_index ->
    # chosen option label / "Other" free-text). One AskUserQuestion carries ALL its
    # questions under a single tool_use_id, so the native answers map must cover every
    # question; the relay records each tap here and resolves ONCE all are answered (a
    # partial map is rejected by the tool). Empty for plan/permission. Keyed per id so
    # two concurrent asks accumulate independently.
    ask_answers: dict[int, str] = field(default_factory=dict)


@dataclass
class _QueuedTurn:
    """One parked turn in a chat's FIFO run queue (P5 / ADR-005 D6 + T9 drain).

    A turn that would start AT the concurrency cap parks on ``future`` inside
    :meth:`StreamingSession._acquire_slot` instead of running; a finishing run pops the
    oldest queued turn and transfers it the freed slot by resolving ``future``. ``runtime``
    is the project the parked turn belongs to — recorded so :meth:`StreamingSession.handle_cancel`
    / ``/rm`` can find and **drain** a queued-not-yet-running project's waiter (cancel its
    ``future``) before its turn ever starts: else cancelling/removing a queued project would
    leave a "zombie run" that springs to life when a slot frees (the T6-review hazard). The
    parked turn has no live engine + no pending-index entries yet (it never reached
    ``_drive_turn``), so draining is purely: cancel the future → its ``_acquire_slot`` unwinds
    (dropping the entry + releasing any transferred slot) → the turn task raises
    ``CancelledError`` and ``handle_message``'s ``finally`` releases nothing it didn't hold.
    """

    runtime: "_ProjectRuntime"
    future: "asyncio.Future[None]"


@dataclass
class _ChatState:
    """Per-chat coordinator: the per-project runtimes + the pending-request index.

    The per-**project** runtime (engine/cwd/policy + its turn lock + its live-turn state)
    lives in :attr:`runtimes`, keyed by the stored project name; the *active* project is
    resolved from the store.

    **P5 / ADR-005 D3 — the pending-request index.** P4 held the *single* most-recent
    ask/plan on the chat (one ``pending_ask``/``pending_plan`` slot) and routed every
    decision-in to ``_active_engine``, collapsing everything to the active project. P5
    replaces those slots with a **pending-request index** ``{tool_use_id -> _PendingRef}``
    so a button tap / free-text reply routes by its ``tool_use_id`` to the **owning
    project's** engine regardless of which project is currently active — closing the
    ADR-001 correlation-envelope gap at the relay. The index is populated when a project's
    stream injects an ask/plan/permission (keyed off the project the turn runs on) and
    cleared on resolve/cancel/turn-end. The index is the **cross-project router**, keyed by
    ``tool_use_id -> project``, so it correctly lives on the chat (not a project).

    **P5 / ADR-005 D7 — live-turn state moved OUT, to :class:`_ProjectRuntime`.** P4 also
    held the status line + free-text-capture marker here (one turn per chat). T4 relocated
    those into the per-project runtime (each running project owns its own status line +
    free-text marker).

    **P5 / ADR-005 D1 — the turn lock moved OUT too (T5: concurrency turns ON).** P4 held a
    single chat-level lock here (one turn per chat). T5 moved it onto each
    :class:`_ProjectRuntime` (one lock per project) so different projects run concurrently;
    this shrinks to the pure coordinator it is: the per-project ``runtimes`` and the
    ``pending_index`` router (id -> project — correctly on the chat, not a project).
    """

    # Per-project in-memory runtimes, keyed by the project's STORED (as-created) name.
    # Created lazily by _active_runtime; never persisted (D3 — transient bypass). Each
    # runtime carries THIS project's turn lock (ADR-005 D1) + live-turn state (status line +
    # free-text capture marker + status enum — ADR-005 D7).
    runtimes: dict[str, _ProjectRuntime] = field(default_factory=dict)
    # The pending-request index (ADR-005 D3): tool_use_id -> the owning project + kind +
    # held event (+ the per-id ask accumulator). Replaces P4's single pending_ask/
    # pending_plan slots; every resolve/cancel/free-text routes through it by id. The
    # cross-project router (id -> project) — correctly on the chat, not a project (D7).
    pending_index: dict[str, _PendingRef] = field(default_factory=dict)
    # P5 / ADR-005 D6 (T6): the per-chat FIFO run queue. When a turn would start but the
    # process is AT the concurrency cap (StreamingSession._running >= cap), handle_message
    # appends a queued turn here (its target runtime + a waiter Future) and parks on the
    # future instead of running; a finishing run pops the OLDEST queued turn (FIFO) and
    # hands it the freed slot. Each entry carries its ``runtime`` so /cancel + /rm can DRAIN
    # a queued-not-yet-running project's waiter (T9 — else a zombie run when a slot frees).
    # The QUEUE is per-chat (no cross-chat semantics — the anti-goal); the run COUNTER is
    # process-global (the cap is per-deployment). Transient in-memory, like everything else
    # on _ChatState (RB3 — no in-flight runs survive a restart).
    run_queue: "deque[_QueuedTurn]" = field(default_factory=deque)
    # P5 / ADR-005 D8 (T8): the per-chat send-rate gate. ALL outbound for this chat (every
    # project's status edits + verbatim messages + the proactive notifications) funnels
    # through it so N concurrent projects flushing at once never burst past Telegram's
    # ~1 msg/s/chat ceiling (RB5 under concurrency). Verbatim is prioritized over coalesced
    # status churn (never starved / dropped — D8). Built lazily by the session (it needs the
    # injected clock + the configured interval); transient in-memory like the rest.
    send_gate: "Optional[ChatSendGate]" = None
    # P5 / ADR-005 D4 (T8): throttle for the proactive background pings, so a project
    # bursting does not spam the chat with duplicate 🔔 pings. The key is
    # (project_name, ping_kind) for a TERMINAL/non-actionable ping (done/error) and
    # (project_name, ping_kind, tool_use_id) for an ACTIONABLE hold (permission/ask/plan) —
    # so each DISTINCT held request keeps its own answerable keyboard (cross-model-QA
    # BLOCKER 1) while a re-emit of the SAME id (or a repeated terminal) is coalesced. Maps
    # the key -> the monotonic time the last such ping was SENT; a duplicate within the gate
    # interval is suppressed. Transient in-memory (RB3).
    notify_last: dict[tuple[str, ...], float] = field(default_factory=dict)
    # P5 / ADR-005 D5 (T9): the reply-to map for free-text disambiguation.
    # ``message_id -> tool_use_id`` — populated by the bot when it sends a free-text-
    # eliciting prompt (the ``✏️ <name>: reply…`` follow-up to an "Other"/"Reject" tap),
    # so when the operator REPLIES-TO that prompt the relay routes the answer by its
    # ``tool_use_id`` (the index then maps id -> owning project), overriding the
    # most-recent default. Pruned on resolve / turn-end (so it can't grow unboundedly and a
    # stale entry can't misroute). Transient in-memory (RB3) — the message ids are
    # Telegram's, valid only for the live process.
    reply_to_index: dict[int, str] = field(default_factory=dict)
    # P5 / ADR-005 D5 (T9): a monotonic counter stamped onto a runtime's
    # ``awaiting_text_armed_at`` each time it arms free-text capture, so the resolver can
    # pick the **most-recently-armed** project when several are awaiting free text (newest
    # wins — the name-echoed prompt said which). Bumped by :meth:`_next_armed_seq`; never
    # reset (strictly increasing within the process is all the ordering needs).
    armed_seq: int = 0
    # STATUSLINE T-SL-CORE (design §3.1 / §4 RB3) — the ONE pinned statusline message per chat.
    # ``statusline_message_id`` is the Telegram id of the pinned line (None before the first
    # update / after an orphan-recovery clears it); ``statusline_text`` is the last body shown,
    # for the identical-text skip (no-op edits raise "message is not modified" AND waste a send
    # slot — mirrors the transient status line's ``status_text``). EXACTLY ONE id is ever held
    # (we only edit it; on recovery we re-point it). Transient/in-memory only (RB3): a restart
    # drops the reference (the bot re-creates the line on the first post-restart update) — like
    # ``send_gate``/``status_message_id``, the live pin id is never persisted.
    statusline_message_id: Optional[int] = None
    statusline_text: Optional[str] = None
    # STATUSLINE T-SL-WIRE (pin-retry fix): whether the held ``statusline_message_id`` is
    # actually PINNED. The send and the pin are separate Telegram calls — a send can succeed
    # (id stored) while the pin RAISES (rate-limit, perms, hiccup), leaving the line sent but
    # UNPINNED. Without this flag the identical-text skip would short-circuit every later update
    # and the line would stay unpinned forever. So on a failed pin we leave this False and RETRY
    # the pin on the next update even when the text is unchanged. Transient in-memory (RB3).
    statusline_pinned: bool = False


def _resume_failure_text(event: Event) -> Optional[str]:
    """The error text of ``event`` IF it is an error-shaped turn/result frame, else None.

    Only an :class:`ErrorEvent` or an ``is_error`` :class:`ResultEvent` can carry a
    resume failure — every other event (text/tool_use/ask/plan/permission/status, or a
    CLEAN result) is not an error and returns None so the heuristic is never even
    consulted for it. The text mirrors what the one-shot runner puts in
    ``ClaudeResult.error``: an ``ErrorEvent`` carries its ``message`` (this is where the
    SDK adapter surfaces a torn/aged-transcript ``turn_error`` or a ``driver_error``
    exception string); an ``is_error`` ``ResultEvent`` carries its ``result_text`` /
    ``subtype``.
    """
    if isinstance(event, ErrorEvent) and event.is_error:
        return event.message or ""
    if isinstance(event, ResultEvent) and event.is_error:
        return event.result_text or event.subtype or ""
    return None


def _is_resume_failure_event(event: Event) -> bool:
    """Reuse the one-shot ``_is_resume_failure`` heuristic on a streaming event (QF3/B3).

    The streaming turn surfaces a failed resume as an error/result EVENT (not a returned
    ``ClaudeResult`` like the one-shot path), so we extract that event's error text and
    feed it through the EXACT same heuristic by wrapping it in a ``ClaudeResult`` — no
    forked or re-implemented matching logic. Importing and reusing
    :meth:`ClaudeRunner._is_resume_failure` means a future tightening of the heuristic
    applies to BOTH runners. A non-error event has no error text → never a resume failure.

    Detection signal (judgement call): the heuristic keys on session-gone phrasing —
    "no conversation found", or "session" + ("not found" | "invalid" | "expired") — and
    explicitly excludes "timed out" / "binary not found". So an ORDINARY tool/turn error
    (e.g. "Bash: command not found", a tool stack trace) does NOT match; only a
    resume/session-not-found-shaped message does. This is necessarily a text heuristic
    (the normalized event shape has no dedicated "resume failed" discriminator), shared
    verbatim with the proven one-shot path so the two stay consistent.
    """
    text = _resume_failure_text(event)
    if text is None:
        return False
    return ClaudeRunner._is_resume_failure(ClaudeResult(ok=False, text="", error=text))


class _TurnDedup:
    """Per-turn dedup of the duplicate-render paths (P6/R5). Pure; no I/O.

    Production builds the substrate with ``include_partial_messages=False``, so a normal
    answer turn surfaces the SAME final text on TWO foreground paths and renders it twice:

    * **#1 (every normal answer turn):** Claude's final answer arrives as an assembled
      :class:`~claude_tg.engine.types.TextEvent` (``incremental=False``) → a verbatim
      ``op="new"`` message, AND the terminal :class:`~claude_tg.engine.types.ResultEvent`
      carries the SAME string in ``result_text`` → ANOTHER verbatim ``op="new"``. The
      engine's ``_drain_substrate`` dedups only ask/plan, not this. Fix: when the
      ``result_text`` duplicates assistant prose already emitted this turn, render only the
      compact ``✅ done`` footer instead of re-sending the identical prose (see
      :meth:`result_is_duplicate_prose`; the driver swaps in a footer-only ``ResultEvent``).

    * **#3 (error turns):** a failing tool renders a ``tool_error``
      :class:`~claude_tg.engine.types.ErrorEvent` verbatim, then the terminal
      ``ResultMessage(is_error)`` surfaces a near-identical ``turn_error`` ``ErrorEvent``
      carrying the same message → a SECOND error block. Fix: suppress a terminal
      ``turn_error`` whose message duplicates a ``tool_error`` already rendered this turn
      (see :meth:`suppresses`).

    The comparison is on the **raw source** (the assistant ``TextEvent.text`` /
    ``ErrorEvent.message``), not the rendered HTML, so it is exact-match and intent-clear:
    a result_text or terminal error that DIFFERS from what was already shown is never
    suppressed (the multi-message-turn + distinct-error guards). State is per-turn — one
    instance lives on the stack of a single ``_drive_turn`` call, reset for the next turn.

    Only foreground renders feed this (the driver's background branch pings ``✅``/``🔔``
    and ``continue``s before the render section), so background turns are unaffected.
    """

    def __init__(self) -> None:
        # Raw bodies actually rendered verbatim this turn (newest-last not needed — a set
        # is enough since dedup is exact-match equality, not "immediately-preceding").
        self._assistant_texts: set[str] = set()
        self._tool_error_messages: set[str] = set()

    def record(self, event: Event) -> None:
        """Remember a verbatim body that was just rendered (so a later twin can dedup)."""
        if isinstance(event, TextEvent) and not event.incremental and event.text:
            self._assistant_texts.add(event.text)
        elif (
            isinstance(event, ErrorEvent)
            and event.kind_of_error == "tool_error"
            and event.message
        ):
            self._tool_error_messages.add(event.message)

    def result_is_duplicate_prose(self, event: ResultEvent) -> bool:
        """True iff this result's ``result_text`` repeats assistant prose already shown (#1)."""
        return bool(event.result_text) and event.result_text in self._assistant_texts

    def suppresses(self, event: Event) -> bool:
        """True iff ``event`` is a terminal ``turn_error`` duplicating a shown ``tool_error`` (#3)."""
        return (
            isinstance(event, ErrorEvent)
            and event.kind_of_error == "turn_error"
            and bool(event.message)
            and event.message in self._tool_error_messages
        )


def _footer_only_result(event: ResultEvent) -> ResultEvent:
    """A copy of ``event`` with ``result_text`` dropped → renders the compact ``✅ done``
    footer instead of the (duplicate) prose (#1). The footer still carries ``num_turns`` /
    ``total_cost_usd`` so the done indicator stays informative."""
    return ResultEvent(
        session_id=event.session_id,
        is_error=event.is_error,
        subtype=event.subtype,
        num_turns=event.num_turns,
        total_cost_usd=event.total_cost_usd,
        result_text=None,
    )
