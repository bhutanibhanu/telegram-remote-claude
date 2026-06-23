"""Streaming-mode driver — the collaborator ``bot.py`` delegates to when
``ENGINE_MODE=streaming``.

The one-shot path (``claude_runner.ClaudeRunner``) is untouched; this module is the
*parallel* streaming runner gated behind the S4 flag. It owns everything the live
engine needs that the pure layers (``engine``/``render``) deliberately left to T7,
now **per project, CONCURRENT** (P5 / ADR-005, relaxing P4 / ADR-004's single-active-run):

* **Per-project :class:`~claude_tg.engine.engine.Engine` lifecycle.** One engine per
  project, started lazily on the first turn (or resumed from that project's persisted
  ``(session_id, cwd)`` — the cwd-scoped-resume coupling, ADR-001 / C6). The project
  runtime (``cwd``/``engine``/``started``/``policy`` + its turn lock + live-turn state)
  lives on a :class:`_ProjectRuntime` held in a per-project dict on :class:`_ChatState`;
  the **active** project is resolved from the **store** (the source of truth), and a
  project's ``(session_id, cwd)`` is read/written via the registry CRUD. **P5 / ADR-005
  D1: several projects' engines may be live at once** — switching the active project no
  longer stops another project's in-flight run (the P4 ``_stop_other_started``
  cross-project stop is removed); the engine cap/queue (T6) bounds concurrency.

* **The turn lock (harvested ``ClaudeBusy`` invariant), now PER PROJECT (ADR-005 D1).**
  Each :class:`_ProjectRuntime` owns an :class:`asyncio.Lock` guarding **its** turn
  driver, so a chat runs **one turn per project** but **N projects concurrently** (PTB
  dispatches handlers concurrently via ``concurrent_updates(True)``). A second message to
  the SAME running project raises :class:`StreamingBusy`; a message to a DIFFERENT idle
  project takes its own lock and runs concurrently. **It deliberately does NOT guard
  :meth:`resolve_callback` / :meth:`handle_cancel` / free-text resolve**: those resolve a
  pending decision the *currently running* turn is awaiting, so they MUST run concurrently
  with the held turn (the turn loop is parked inside ``engine.send`` awaiting the operator;
  the callback handler calls ``engine.resolve`` on the same loop to unblock it). Locking
  the resolve would deadlock the very turn it must unblock.

* **The send/edit + coalesce loop.** Drives ``engine.send(prompt)``, runs each event
  through :func:`~claude_tg.render.render_event` via a per-turn
  :class:`~claude_tg.render.Coalescer`, and performs the actual Telegram send / edit
  the render layer deferred — batching incremental/status edits at the min interval,
  flushing verbatim ask/plan/error/result as their own messages, attaching the
  ask/plan inline keyboard. Persists ``session_id`` from the ``result`` event to the
  **active project** (per-project, not a chat-global slot).

* **The free-text "Other" / plan-reject state machine.** A per-chat pending-input
  marker: when the operator taps "Other" on an ask or "Reject + feedback" on a plan,
  the NEXT text message is captured as the free-text answer / reject feedback and
  routed via ``engine.resolve`` instead of opening a new turn.

* **Transient bypass reset on restart (D3/SB5).** The :class:`_ProjectRuntime` (and its
  :class:`~claude_tg.permissions.PermissionPolicy`) is in-memory only — a fresh process
  starts every project with a new policy (``/yolo`` OFF, no allow-session grants).
  Identity (name, cwd, ``session_id``) reloads from the persisted registry at
  :meth:`_ensure_engine` time; the bypass posture never persists.

**SB1 is enforced at the bot** (``filters.Chat(allowed)`` + an explicit
``_authorized`` recheck in the handler) — this module is only reached for an
already-authorized chat. **SB4/SB6:** prompts are passed to the engine verbatim; no
message text is ever interpolated into a shell command or argument, and no bypass /
permission-skip flag is introduced here (the engine's P1 posture — auto-allow ordinary
tools inside the single allowlisted chat — is unchanged; per-tool gating is P2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Protocol

from .claude_runner import ClaudeResult, ClaudeRunner
from .config import Config
from .engine import (
    AskEvent,
    Engine,
    ErrorEvent,
    Event,
    PermissionDecision,
    PermissionEvent,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    ResultEvent,
    SubstrateDecision,
    TextEvent,
)
from .engine.adapter_sdk import SdkSubstrate
from .paths import PathNotAllowed, resolve_within_roots
from .permissions import PermissionPolicy
from .render import (
    Callback,
    ChatSendGate,
    Coalescer,
    ProjectStatus,
    RenderAction,
    answers_from_ask,
    ask_question_body,
    ask_question_body_html,
    ask_question_keyboard,
    decode_callback,
    error_is_raw_external,
    notify_attention,
    notify_done,
    notify_error,
    permission_keyboard,
    plan_keyboard,
    strip_telegram_html,
    yolo_indicator,
)
from .session_store import DEFAULT_PROJECT
from .util import _redact_sid, _redact_sid_in_text

log = logging.getLogger(__name__)

#: A coroutine that sends a NEW message and returns the sent message id (or None).
#: ``reply_markup`` is the inline keyboard for an ask/plan (None otherwise).
SendFn = Callable[..., Awaitable[Optional[int]]]
#: A coroutine that edits an existing message's text in place (best-effort).
EditFn = Callable[..., Awaitable[None]]
#: A coroutine that deletes a message by id (best-effort; used to clear the transient
#: "💭 Claude is thinking…" status line at the end of a turn so it does not linger).
DeleteFn = Callable[..., Awaitable[None]]

#: The three operator verdicts the engine understands (mirrors PermissionDecision.verdict).
PermissionVerdictName = Literal["allow_once", "allow_session", "deny"]

#: Decoded permission tap action -> the engine's PermissionDecision verdict (P2,
#: ADR-003 §2). ``render.decode_callback`` already constrains the action to these three.
_PERMISSION_VERDICTS: dict[str, PermissionVerdictName] = {
    "once": "allow_once",
    "session": "allow_session",
    "deny": "deny",
}
#: Verdict -> the short operator-facing toast for answer_callback_query (no secrets).
_PERMISSION_NOTES: dict[PermissionVerdictName, str] = {
    "allow_once": "Allowed once",
    "allow_session": "Allowed for session",
    "deny": "Denied",
}


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
    """
    engine: Engine

    async def decision_callback(
        tool_name: str, tool_input: dict, tool_use_id: Optional[str]
    ) -> SubstrateDecision:
        return await engine.on_tool_request(tool_name, tool_input, tool_use_id)

    substrate = SdkSubstrate(
        cwd=cwd,
        permission_mode="default",
        decision_callback=decision_callback,
    )
    engine = Engine(
        substrate,
        send_timeout=send_timeout,
        backstop_seconds=backstop_seconds,
        permission_policy=permission_policy,
        cwd=cwd,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
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


#: The kind of interactive request a pending-index entry holds open.
PendingKind = Literal["ask", "plan", "permission"]


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


#: A held request of each kind maps the OWNING project's status to the matching
#: ``awaiting_<kind>`` for the /projects column (ADR-005 D7). These values MUST match the
#: render-layer :data:`~claude_tg.render.ProjectStatus` enum (permission->awaiting_approval,
#: ask->awaiting_answer, plan->awaiting_plan).
_AWAITING_STATUS: dict[PendingKind, ProjectStatus] = {
    "permission": "awaiting_approval",
    "ask": "awaiting_answer",
    "plan": "awaiting_plan",
}


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


class StreamingBusy(Exception):
    """Raised when a chat already has a streaming turn in flight (harvested ClaudeBusy)."""


class StreamingSession:
    """Drives the streaming engine for every chat (the bot delegates here in streaming mode).

    Construct ONE per bot. Methods are called from the Telegram handlers (PTB dispatches
    them concurrently via ``concurrent_updates(True)``). A **per-project** turn lock
    (ADR-005 D1) guards :meth:`handle_message` — one run per project, but different projects
    run concurrently; :meth:`resolve_callback` is intentionally lock-free so it can resolve
    the pending decision a held turn is awaiting.

    **Per project, CONCURRENT (P5 / ADR-005 D1; T5 turns concurrency ON).** The chat's
    active project (and its cwd) is resolved from the store on each turn; the engine is
    built/resumed from that project's ``(session_id, cwd)`` and persists its ``session_id``
    back to that project. P4 kept at most one engine started per chat (D2 single-active-run:
    switching stopped the previously-started engine). T5 **removes** that stop-the-other
    behavior — switching away no longer kills another project's in-flight run, so N engines
    can be live at once (the whole point of background concurrency). The QF5 hardening that
    discards a NON-started / failed engine of the SAME project before building fresh is
    kept (it is per-project, not cross-project).

    **Id-routed decisions (P5 / ADR-005 D3).** Every decision-in (button tap, free-text
    reply, cancel) routes by its ``tool_use_id`` through the per-chat **pending-request
    index** to the **owning project's** engine — **not** ``_active_engine`` (retired from
    the resolve path). The index is keyed off the project a turn runs on, so a tap for
    project A resolves A's request even while B is the active/foreground project; an id
    absent from the index resolves nothing (a benign no-op, RB1). The held event's
    ``session_id`` is checked against the owning engine's current ``session_id`` as
    defense-in-depth (a stale id after a resume never resolves the wrong session).
    **Concurrency is ON (T5+):** N projects' engines may be live at once and a tap routes by
    id to whichever project owns the request, regardless of which project is foreground.
    """

    def __init__(
        self,
        config: Config,
        *,
        session_store=None,
        engine_factory: Optional[EngineFactory] = None,
        clock: Callable[[], float] = time.monotonic,
        min_edit_interval: Optional[float] = None,
        chat_send_interval: Optional[float] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.store = session_store
        # P6/C2 (SB2): bind the live config's path-confinement context into the DEFAULT
        # factory so the production engine confines the SDK's file/search tools to
        # allowed_roots (an out-of-root tool call is held for approval — see
        # Engine.on_tool_request). A bare _default_engine_factory(cwd=...,
        # backstop_seconds=..., permission_policy=...) would default to no path context
        # (the path layer no-ops), so the binding is what turns C2 ON for the real bot.
        # P6/H2/RB2: the SAME binding threads the GENEROUS, configurable per-message liveness
        # bound (config.stream_message_timeout_seconds → Engine.send_timeout) — a bare call
        # keeps the 120 s default, so the binding is what raises it for the real bot (a
        # multi-minute approved tool no longer trips a spurious driver_error). An injected
        # engine_factory (tests) is used verbatim — its 3-kwarg signature is unchanged, so
        # every existing test factory keeps working; tests that want to exercise the path
        # layer (or a small liveness bound) build a real Engine with those kwargs directly.
        if engine_factory is not None:
            self._engine_factory: EngineFactory = engine_factory
        else:

            def _bound_factory(
                *, cwd: str, backstop_seconds: float, permission_policy: PermissionPolicy
            ) -> Engine:
                return _default_engine_factory(
                    cwd=cwd,
                    backstop_seconds=backstop_seconds,
                    permission_policy=permission_policy,
                    allowed_roots=config.allowed_roots,
                    allow_any_path=config.allow_any_path,
                    send_timeout=float(config.stream_message_timeout_seconds),
                )

            self._engine_factory = _bound_factory
        self._clock = clock
        self._min_edit_interval = min_edit_interval
        # P5 / ADR-005 D8 (T8): the per-chat send-rate budget (seconds between any two
        # outbound for one chat) + the awaitable used to honor a gate's computed wait. The
        # gate (render.ChatSendGate) is PURE (decides the wait); the SESSION does the actual
        # awaiting via ``_sleep`` — exactly as the Coalescer leaves the real edit-waiting to
        # the consumer. ``sleep`` is injected so tests can pass a recorder/no-op and stay
        # deterministic with no real time. The interval falls back to config (default ~1 s).
        self._chat_send_interval = (
            chat_send_interval
            if chat_send_interval is not None
            else float(config.render_chat_send_interval_seconds)
        )
        self._sleep = sleep
        self._chats: dict[int, _ChatState] = {}
        # NOTE (P4 / D3): no __init__ harvest of persisted (session_id, cwd). Resume now
        # resolves PER ACTIVE PROJECT from the registry at _ensure_engine time, and the
        # in-memory _ProjectRuntime starts fresh every process (transient bypass reset).
        # P5 / ADR-005 D6 (T6): process-global count of turns currently RUNNING (across all
        # projects + chats). A turn started while ``_running >= config.max_concurrent_runs``
        # is QUEUED (per-chat FIFO on _ChatState.run_queue) instead of run; a finishing run
        # decrements this and hands the freed slot to the next queued waiter. The counter is
        # global (the cap is per-deployment); the queue is per-chat (no cross-chat
        # semantics). MUST decrement exactly once on EVERY turn-exit path (normal end,
        # mid-stream raise, cancel, resume-failure) so a raised turn can never leak a slot
        # and permanently shrink capacity — see handle_message's try/finally + _release_slot.
        self._running = 0

    # -- per-chat state ------------------------------------------------------

    def _chat(self, chat_id: int) -> _ChatState:
        state = self._chats.get(chat_id)
        if state is None:
            state = _ChatState()
            self._chats[chat_id] = state
        return state

    # -- per-chat send-rate gate (RB5 under concurrency, ADR-005 D8) ---------

    def _gate(self, state: _ChatState) -> ChatSendGate:
        """The chat's :class:`~claude_tg.render.ChatSendGate` (built lazily, D8).

        ALL outbound for a chat funnels through one gate so the COMBINED cross-project
        send rate stays bounded (RB5/D8). Built lazily here because the gate needs the
        session's injected clock + the configured per-chat interval; transient in-memory
        on :class:`_ChatState` (reset on restart, RB3).
        """
        if state.send_gate is None:
            state.send_gate = ChatSendGate(
                now=self._clock, interval=self._chat_send_interval
            )
        return state.send_gate

    async def _gated_send(
        self, state: _ChatState, send: SendFn, *, verbatim: bool, **kwargs
    ) -> Optional[int]:
        """Send through the per-chat gate: reserve a slot, await the wait, then send (D8).

        Reserves the next send slot from the chat's :class:`~claude_tg.render.ChatSendGate`
        (``verbatim`` prioritizes a final answer / error / prompt / notification over
        coalesced status churn — D8), awaits the gate's computed wait via the injected
        ``self._sleep`` (the gate decides timing; the session does the awaiting — the
        Coalescer pattern), then performs the real ``send``. Returns the sent message id.
        """
        wait = self._gate(state).reserve(verbatim=verbatim)
        if wait > 0:
            await self._sleep(wait)
        return await send(**kwargs)

    async def _gated_edit(
        self, state: _ChatState, edit: EditFn, **kwargs
    ) -> None:
        """Edit through the per-chat gate (a status-line edit is NON-verbatim — D8).

        A status-line edit is the low-priority kind: it yields to verbatim through the
        gate (so a concurrent project's status churn never starves a prompt). Reserves +
        awaits like :meth:`_gated_send`, then performs the real ``edit``.
        """
        wait = self._gate(state).reserve(verbatim=False)
        if wait > 0:
            await self._sleep(wait)
        await edit(**kwargs)

    # -- foreground (inline-vs-notify decision, ADR-005 D4) ------------------

    def _is_foreground(self, chat_id: int, name: Optional[str]) -> bool:
        """Whether ``name`` is the chat's current foreground (the store's active) project.

        The notification send-decision (D4): an event for the **foreground** project
        renders inline (as P4); a **background** (non-foreground) project's hold/terminal
        becomes a name-prefixed 🔔/✅/⚠️ ping instead. "Foreground" is the store's
        ``active`` (a per-chat marker); reading it is **read-only** (never creates a
        project — RB1). With no store there is a single implicit project, so it is always
        foreground (one-project deployments never notify — no behavior change). Matched
        case-insensitively (mirroring the store's name match) so it agrees with
        ``/projects``/``/switch``.
        """
        if name is None:
            return True
        if self.store is None:
            return True  # single implicit project — always the foreground.
        active = self.store.get_active(chat_id)
        if active is None:
            return True  # nothing active yet → treat the turn's project as foreground.
        return isinstance(active, str) and active.casefold() == name.casefold()

    # -- proactive notifications for a BACKGROUND project (ADR-005 D4) -------

    def _should_notify(
        self, state: _ChatState, name: str, ping_kind: str, *, dedup_id: Optional[str] = None
    ) -> bool:
        """Throttle duplicate pings of one ``ping_kind`` for one project (D4 coalescing).

        A background project must not spam the chat with duplicate 🔔 pings — but the unit
        of "duplicate" differs for actionable vs non-actionable pings:

        * **Actionable holds (``dedup_id`` given — the held request's ``tool_use_id``).**
          A permission / plan / ask ping carries an *answerable keyboard*; each DISTINCT
          held request is a SEPARATE thing the operator must act on, so the throttle is
          keyed by ``(project, kind, tool_use_id)`` — a second DISTINCT-``tool_use_id`` hold
          arriving within the send interval **always** sends its keyboard (it landed in the
          pending index; a tap would resolve it, but only if a keyboard reached the
          operator — the cross-model-QA BLOCKER 1). A re-emit of the **same** id within the
          window IS coalesced (the operator already has that exact keyboard; the first
          ping's button still routes the tap, D3).
        * **Non-actionable / terminal pings (``dedup_id`` omitted).** Repeated status /
          attention pings of the same ``(project, kind)`` (e.g. ``done``/``error``, or a
          burst of the same class) are coalesced within the interval — there is no per-id
          keyboard to lose, so collapsing duplicates is the intended D4 behavior.

        Records the send time on the way through (so the first ping of a key always goes).
        ``ping_kind`` is the notification class — the held :data:`PendingKind`
        (``permission``/``ask``/``plan``) for an attention ping, or ``done``/``error`` for a
        terminal — so e.g. a permission ping never suppresses a later error ping.
        """
        # Actionable holds dedup per id (each distinct request keeps its keyboard); terminal
        # / non-actionable pings dedup per (project, kind) as before.
        key: tuple[str, ...] = (name, ping_kind) if dedup_id is None else (name, ping_kind, dedup_id)
        now = self._clock()
        last = state.notify_last.get(key)
        if last is not None and (now - last) < self._chat_send_interval:
            return False
        state.notify_last[key] = now
        return True

    @staticmethod
    def _keyboard_for(event: Event):
        """The inline keyboard the inline render would attach for a held event, or ``None``.

        A background ping for a hold (permission/ask/plan) must carry the SAME keyboard the
        inline render would (so the tap routes by the D3 index regardless of which project
        is foreground — D4). For a multi-question ask there is one keyboard PER question
        (the inline path sends one message each), so this returns ``None`` for an ask and
        the caller sends per-question (see :meth:`_notify_background_ask`). Non-hold events
        carry no keyboard.
        """
        if isinstance(event, PermissionEvent):
            return permission_keyboard(event)
        if isinstance(event, PlanEvent):
            return plan_keyboard(event)
        return None

    async def _notify_background(
        self,
        state: _ChatState,
        chat_id: int,
        name: str,
        event: Event,
        kind: PendingKind,
        *,
        send: SendFn,
    ) -> None:
        """Send a body-free ``🔔 <name> — …`` attention ping for a BACKGROUND hold (D4/SB3).

        The operator is not watching ``name`` (it is not the foreground), so a held
        permission/ask/plan becomes a name-prefixed ping carrying the SAME keyboard the
        inline render would (the tap routes by the D3 index). **SB3 body-free:**
        :func:`~claude_tg.render.notify_attention` interpolates only the project name + a
        fixed per-kind phrase — never the event body. Throttled per ``(project, kind)`` (D4)
        and rate-gated as **verbatim** (priority — a prompt the operator must answer must
        not be starved by status churn, D8). A multi-question ask sends one keyboard per
        question so every question stays answerable.
        """
        if isinstance(event, AskEvent):
            await self._notify_background_ask(state, chat_id, name, event, send=send)
            return
        # BLOCKER 1: an actionable hold (permission/plan) dedups per tool_use_id, so a second
        # DISTINCT request always sends its keyboard (the throttle only coalesces a re-emit of
        # the SAME id). A hold with no id (defensive — the engine always sets one) falls back
        # to the per-(project, kind) throttle.
        if not self._should_notify(
            state, name, kind, dedup_id=getattr(event, "tool_use_id", None)
        ):
            return
        await self._gated_send(
            state, send, verbatim=True,
            text=notify_attention(name, kind),
            reply_markup=self._keyboard_for(event),
            parse_mode=None,
        )

    async def _notify_background_ask(
        self,
        state: _ChatState,
        chat_id: int,
        name: str,
        ask: AskEvent,
        *,
        send: SendFn,
    ) -> None:
        """Background ask ping: the ``🔔 <name> — asks a question`` line + each question's
        keyboard (so a multi-question ask stays fully answerable while backgrounded, D4).

        The WHOLE ping (the bell line + every question's option keyboard) is gated by a
        SINGLE ``_should_notify`` decision keyed per ``(project, "ask", tool_use_id)``, exactly
        mirroring the permission/plan path in :meth:`_notify_background` (round-2 cross-model-QA
        BLOCKER):

        * A **distinct**-``tool_use_id`` ask within the throttle window always sends its full
          keyboard set — each held request keeps its own answerable keyboards (the round-1
          BLOCKER-1 distinct-id fix, preserved: a distinct id is a separate thing the operator
          must act on).
        * A re-emit of the **same** ``tool_use_id`` inside the window sends **nothing new** —
          neither the bell NOR the question body/keyboards. The operator already has that exact
          set, and the first ping's buttons still route the tap by the D3 index. The round-1 fix
          only suppressed the bell while the per-question keyboard loop re-ran unconditionally,
          which DUPLICATED the question keyboards for a same-id re-emit — this gates them
          together so the same-id-coalesce contract matches permission/plan.

        SB3: only the project name + the fixed "asks a question" phrase are interpolated by
        ``notify_attention`` — the question TEXT rides the keyboard's own (already-safe) body,
        exactly as inline.
        """
        # BLOCKER (round 2): one throttle decision gates the ENTIRE ask ping. A same-id re-emit
        # short-circuits with nothing sent (mirrors the permission/plan path); a distinct id
        # (or a re-emit after the window) sends the bell + every question keyboard.
        if not self._should_notify(
            state, name, "ask", dedup_id=getattr(ask, "tool_use_id", None)
        ):
            return
        await self._gated_send(
            state, send, verbatim=True,
            text=notify_attention(name, "ask"),
            reply_markup=None,
            parse_mode=None,
        )
        for q_idx in range(len(ask.questions)):
            keyboard = ask_question_keyboard(ask, q_idx)
            try:
                await self._gated_send(
                    state, send, verbatim=True,
                    text=ask_question_body_html(ask, q_idx),
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except Exception:
                await self._gated_send(
                    state, send, verbatim=True,
                    text=ask_question_body(ask, q_idx),
                    reply_markup=keyboard,
                    parse_mode=None,
                )

    async def _notify_terminal(
        self,
        state: _ChatState,
        name: str,
        event: Event,
        *,
        send: SendFn,
    ) -> None:
        """Send a body-free terminal ping for a BACKGROUND project (D4/SB3).

        A background project's clean ``ResultEvent`` → ``✅ <name> — done``; an
        ``ErrorEvent`` → ``⚠️ <name> — <ErrorKind>``. **The load-bearing SB3 check
        (T3-review):** the error ping passes the engine's **body-free**
        :data:`~claude_tg.engine.types.ErrorEvent.kind_of_error` (``tool_error`` /
        ``turn_error`` / ``driver_error``) — **NEVER** ``event.message`` (which can carry a
        raw tool body / secret). Rate-gated as verbatim (priority) and throttled per
        ``(project, done|error)``.
        """
        if isinstance(event, ErrorEvent):
            if not self._should_notify(state, name, "error"):
                return
            # SB3 (T3-review SB3 check): the body-free ErrorKind, NEVER event.message.
            await self._gated_send(
                state, send, verbatim=True,
                text=notify_error(name, event.kind_of_error),
                reply_markup=None,
                parse_mode=None,
            )
            return
        if isinstance(event, ResultEvent):
            # An is_error ResultEvent is a failed turn — ping it as an error too (its
            # ErrorKind isn't available on a ResultEvent, so use a generic body-free label;
            # the result_text is NEVER sent — SB3). A clean result → ✅ done.
            if event.is_error:
                if not self._should_notify(state, name, "error"):
                    return
                await self._gated_send(
                    state, send, verbatim=True,
                    text=notify_error(name, "turn_error"),
                    reply_markup=None,
                    parse_mode=None,
                )
                return
            if not self._should_notify(state, name, "done"):
                return
            await self._gated_send(
                state, send, verbatim=True,
                text=notify_done(name),
                reply_markup=None,
                parse_mode=None,
            )

    # -- active-project resolution (the store is the source of truth) --------

    def _active_runtime(
        self, chat_id: int, *, create_default: bool
    ) -> tuple[Optional[str], Optional[_ProjectRuntime]]:
        """Resolve the chat's ACTIVE project + its in-memory :class:`_ProjectRuntime`.

        The **store** owns which project is active and its cwd. Returns ``(name,
        runtime)`` for the active project, lazily creating the runtime in memory (cwd
        from the registry record, falling back to ``config.workdir`` if the record's cwd
        is missing). When the chat has **no active project**:

        * ``create_default=True`` (a turn / ``set_yolo`` — anything that runs the engine):
          auto-create a ``default`` project at ``config.workdir`` and make it active
          (ADR-004 D6 symmetry — preserves the pre-P4 "just send a message and it works"
          UX), then resolve it. If a ``default`` exists but isn't active, switch to it.
        * ``create_default=False`` (a read-only query like :meth:`get_cwd`): no side
          effects — return ``(None, None)``.

        With no store at all (tests that pass ``session_store=None``), fall back to a
        single implicit ``default`` runtime at ``config.workdir`` so the driver still
        works without persistence.
        """
        if self.store is None:
            # No persistence: a single implicit project so the engine still runs.
            if not create_default:
                # Mirror the with-store read-only contract: no runtime unless one exists.
                rt = self._chat(chat_id).runtimes.get(DEFAULT_PROJECT)
                return (DEFAULT_PROJECT, rt) if rt is not None else (None, None)
            return DEFAULT_PROJECT, self._runtime(chat_id, DEFAULT_PROJECT, None)

        active = self.store.get_active(chat_id)
        if active is None:
            if not create_default:
                return None, None
            active = self._ensure_default_active(chat_id)
        record = self.store.get_project(chat_id, active)
        cwd = (record or {}).get("cwd")
        return active, self._runtime(chat_id, active, cwd)

    def _ensure_default_active(self, chat_id: int) -> str:
        """Auto-create (or switch to) a ``default`` project for a chat with no active one.

        ADR-004 D6: a fresh streaming chat with no active project gets a ``default`` at
        ``config.workdir`` (symmetric with the v1→v2 migration) so the operator can just
        send a message. If ``default`` already exists but isn't active, switch to it
        rather than failing on the duplicate. Returns the now-active project name.
        """
        from .session_store import DuplicateProject

        workdir = str(self.config.workdir)
        try:
            self.store.create(chat_id, DEFAULT_PROJECT, workdir, make_active=True)
        except DuplicateProject:
            # A default already exists (e.g. from a prior reset that kept it) but is not
            # active — make it active rather than creating a second.
            self.store.switch(chat_id, DEFAULT_PROJECT)
        return DEFAULT_PROJECT

    def _runtime(
        self, chat_id: int, name: str, cwd: Optional[str]
    ) -> _ProjectRuntime:
        """The in-memory :class:`_ProjectRuntime` for ``name`` (create lazily).

        ``cwd`` is the registry record's cwd; an absent cwd falls back to
        ``config.workdir`` (a project record should always carry a cwd, but a
        hand-edited / partially-written record must not wedge the turn — fail to the
        default workdir). The runtime is created ONCE and reused (so its engine + policy
        persist across turns within the process); a subsequent call ignores ``cwd`` (a
        project's cwd is fixed for the life of its session — D4).
        """
        runtimes = self._chat(chat_id).runtimes
        rt = runtimes.get(name)
        if rt is None:
            rt = _ProjectRuntime(cwd=cwd or str(self.config.workdir))
            runtimes[name] = rt
        return rt

    def get_cwd(self, chat_id: int) -> str:
        """The active project's cwd, or ``config.workdir`` if there is no active project.

        Read-only (RB1): never creates a project or a runtime — a chat that has never run
        a turn simply reports the default workdir.
        """
        _name, rt = self._active_runtime(chat_id, create_default=False)
        if rt is not None:
            return rt.cwd
        # No active project (or no store): fall back to the default workdir without
        # mutating anything.
        if self.store is not None:
            active = self.store.get_active(chat_id)
            if active is not None:
                record = self.store.get_project(chat_id, active)
                cwd = (record or {}).get("cwd")
                if cwd:
                    return cwd
        return str(self.config.workdir)

    def set_yolo(self, chat_id: int, on: bool) -> None:
        """Flip the ``/yolo`` allow-all bit on the ACTIVE project's policy (P2, D6).

        ``/yolo`` -> ``True`` (every tool runs with NO approval prompt this session);
        ``/unyolo`` -> ``False`` (the fail-closed gate is restored). Mutates the SAME
        :class:`PermissionPolicy` object the active project's engine gate consults, so
        the bypass takes effect immediately for in-flight and subsequent turns of that
        project. Auto-creates ``default`` if there is no active project (consistent with
        starting a turn). The bot makes the toggle loud (the enable banner);
        :meth:`_drive_turn` keeps it loud throughout (the persistent ``⚠️`` turn marker).
        Cleared by :meth:`reset` (D7).
        """
        _name, rt = self._active_runtime(chat_id, create_default=True)
        if rt is not None:
            rt.policy.set_yolo(on)

    # -- engine lifecycle ----------------------------------------------------

    async def _ensure_engine(
        self,
        chat_id: int,
        *,
        target: Optional[tuple[str, _ProjectRuntime]] = None,
    ) -> tuple[Engine, bool]:
        """Lazily start (or resume) a project's engine. Idempotent per project.

        Returns ``(engine, resume_failed)`` — ``resume_failed`` is True iff a persisted
        ``session_id`` was present but ``resume`` raised and we fell back to a fresh
        ``start`` THIS call (so the caller can post the RB3 operator notice). It is False
        for a fresh start, a clean resume, and the already-started fast path.

        ``target`` PINS the project to build for (its ``(name, runtime)``). When omitted the
        chat's **active** project is resolved (auto-creating ``default`` if none — a turn
        always has a project). ``handle_message`` passes the project it captured at message
        time so a turn that **queued** behind the cap (D6/T6) — and thus parked BEFORE this
        call, during which the active project may have moved — still builds/resumes ITS OWN
        project, not whatever happens to be active when its slot frees. Then:

        * **SB2 cwd re-validation (the authoritative gate, T7).** Re-validate the stored
          cwd against the permitted roots via :func:`resolve_within_roots` BEFORE building
          or resuming the engine. A project whose cwd was in-roots at ``/new`` can later
          drift out (config narrowed, or a path component became an out-of-root symlink);
          if so this raises :class:`~claude_tg.paths.PathNotAllowed` and the engine is
          **never** built/resumed — :meth:`handle_message` catches it and refuses the turn
          fail-closed (SB6/RB1). ``ALLOW_ANY_PATH=true`` no-ops the check (the resolver
          returns the canonical path), as on ``/new``.
        * **Concurrent runs (P5 / ADR-005 D1; T5).** A DIFFERENT project's started engine
          is left ALONE — N engines may be live at once (T5 removed P4's
          ``_stop_other_started`` cross-project stop so switching away never kills another
          project's in-flight run). The QF5 SAME-project discard below still applies (a
          non-started / failed engine of THIS project is dropped and rebuilt fresh).
        * Build the engine for the active project's cwd + **its** ``policy``, then
          ``resume`` the project's persisted ``session_id`` (cwd-scoped — C6) if one
          exists, else ``start`` fresh. A resume failure falls back to a fresh ``start``
          (the dead id is dropped) so the project is never wedged on a stale session —
          harvested from the runner's resume-failure recovery — and is signalled back to
          the caller (RB3) so the operator learns the previous session could not resume.
        """
        name: Optional[str]
        rt: Optional[_ProjectRuntime]
        if target is not None:
            name, rt = target
        else:
            name, rt = self._active_runtime(chat_id, create_default=True)
        assert name is not None and rt is not None  # create_default guarantees both
        # SB2 (T7): re-validate the stored cwd BEFORE building/resuming the engine. A
        # PathNotAllowed propagates out of _ensure_engine (the engine is NOT started) and
        # is caught by handle_message, which refuses the turn fail-closed.
        resolve_within_roots(
            rt.cwd,
            cwd=rt.cwd,
            allowed_roots=self.config.allowed_roots,
            allow_any=self.config.allow_any_path,
        )
        # P5 / ADR-005 D1 (T5): no cross-project stop here. A different project's started
        # engine is left running so N runs can be concurrent (T5 removed P4's
        # _stop_other_started). Only the SAME project's stale/non-started engine is handled
        # by the QF5 discard below.
        if rt.engine is not None and rt.started:
            return rt.engine, False
        # Past the warm fast-path: rt is either fresh (engine None) OR holds a NON-started
        # engine — a prior start()/resume() that raised AFTER the adapter allocated its
        # client (so the engine is non-None but unusable). Never REUSE such an engine: a
        # start()/resume() on it hits the adapter's "already started" guard → the turn
        # wedges (the same coupling the QF4 resume-raises path recovers from). So if a
        # non-started engine is present, best-effort stop() it (free its partial client)
        # and build a FRESH one — a non-started engine is always discarded + replaced,
        # never reused. This is the SAME-project QF5 hardening, kept under concurrency.
        if rt.engine is not None:
            try:
                await rt.engine.stop()
            except Exception:
                log.debug(
                    "stop of non-started engine raised for chat %s project %s "
                    "(ignored — building fresh)",
                    chat_id,
                    name,
                    exc_info=True,
                )
        engine = self._engine_factory(
            cwd=rt.cwd,
            backstop_seconds=float(self.config.answer_backstop_seconds),
            permission_policy=rt.policy,
        )
        rt.engine = engine
        resume_id = self._resume_id(chat_id, name)
        resume_failed = False
        if resume_id:
            try:
                await engine.resume(resume_id)
            except Exception:
                # QF4 (B3′/RB3): resume() RAISED — e.g. the SDK adapter assigns its
                # client BEFORE connect(), so a connect failure (dead/aged session)
                # leaves the FAILED engine with a partial, non-None client. We CANNOT
                # reuse it: start() on that same instance hits the adapter's
                # "session already started" guard and would re-raise → the turn fails
                # AND the dead id is never cleared → the project is permanently wedged
                # re-resuming the same dead id. So recover onto a FRESH engine instead.
                log.info(
                    "resume failed for chat %s project %s; starting a fresh session",
                    chat_id,
                    name,
                )
                # (a) Best-effort stop the FAILED engine to free its partial SDK client.
                #     A stop failure must not break recovery (the partial client is the
                #     adapter's problem; we proceed regardless).
                try:
                    await engine.stop()
                except Exception:
                    log.debug(
                        "stop of failed-resume engine raised for chat %s project %s "
                        "(ignored — recovering fresh)",
                        chat_id,
                        name,
                        exc_info=True,
                    )
                # (b) Clear the persisted dead id so it is NOT re-resumed on any future
                #     turn (the wedge fix). Done BEFORE the fresh start so even if the
                #     fresh start were to raise, the dead id is already gone. ADR-005 D2:
                #     clear it on the project being BUILT (``name`` — the captured target),
                #     not "active", so a concurrent /switch can't redirect the clear.
                self._persist(chat_id, session_id=None, name=name)
                # (c) Build a FRESH engine instance (its _client is None, so its start()
                #     cannot hit the "already started" guard) and adopt it as the runtime
                #     engine, replacing the failed one.
                engine = self._engine_factory(
                    cwd=rt.cwd,
                    backstop_seconds=float(self.config.answer_backstop_seconds),
                    permission_policy=rt.policy,
                )
                rt.engine = engine
                # (d) Start the FRESH engine — a clean fresh session (the dead id is gone).
                await engine.start()
                # (e) Signal the caller so handle_message posts the T7 "couldn't resume,
                #     started fresh" notice. The session is fresh (start, not resume), so
                #     it is NOT resumed_unverified — a fresh-session error is an ordinary
                #     turn error, never mistaken for a resume failure.
                resume_failed = True
            else:
                # Resume CONNECTED. It is not yet CONFIRMED good — a stale/aged/torn
                # session can connect and then error on the first turn (B3). Mark the
                # runtime so _drive_turn applies the resume-failure heuristic to this
                # first turn only (cleared once a turn completes clean — QF3/RB3).
                rt.resumed_unverified = True
        else:
            await engine.start()
        rt.started = True
        return engine, resume_failed

    def _resume_id(self, chat_id: int, name: str) -> Optional[str]:
        """The active project's persisted ``session_id`` to resume from, if any."""
        if self.store is None:
            return None
        record = self.store.get_project(chat_id, name)
        session_id = (record or {}).get("session_id")
        return session_id if isinstance(session_id, str) and session_id else None

    def request_remove(self, chat_id: int, name: str) -> bool:
        """INFLIGHT-AWARE ``/rm`` admission (ADR-005 D9 / round-3 BLOCKERS 1+2).

        ``cmd_rm`` calls this BEFORE ``store.remove`` so the abort is set while the project's
        store record still exists — closing the persist-race where a turn in the slot-transfer
        window would otherwise start (and try to persist) a project the record-remove had
        already deleted. Returns whether ``/rm`` may proceed:

        * **Refuse (``False``)** iff the project has a turn RUNNING with a live engine — its
          per-project lock is held (it is inside ``_drive_turn``). Tearing that down mid-turn
          would orphan its parked answer-hold (the engine ref would be gone), so the operator
          must ``/cancel`` it first (T9 — the lock-based running refusal, unchanged).
        * **Allow + pre-abort (``True``)** otherwise — idle, QUEUED, or in the pop→lock
          TRANSFER WINDOW. A queued/window turn holds NO lock and has NO started engine, so
          there is nothing to orphan; it is made safe by SETTING this project's abort (so a
          window turn checks it and aborts cleanly before it can start) and DRAINING a still-
          queued waiter (so it unwinds now). The lock-based refusal alone misses the window
          turn (lock not yet held) — the abort is what guarantees it never zombie-runs a
          now-removed project. ``forget_project`` (called after the store-remove) repeats the
          abort+drain idempotently and purges the runtime.

        A project with no in-memory runtime (never run this process) is trivially removable
        (``True``) — there is no in-flight turn to consider.
        """
        state = self._chats.get(chat_id)
        if state is None:
            return True
        key = self._resolve_runtime_key(state.runtimes, name)
        if key is None:
            return True  # no runtime → nothing in flight; the store-remove is safe.
        rt = state.runtimes[key]
        # A live running turn (lock held, inside _drive_turn) → refuse (orphan hazard, T9).
        if rt.lock.locked():
            return False
        # Idle / queued / transfer-window → make it safe to remove: set the abort BEFORE the
        # caller removes the store record (so a window turn can never slip past its checks and
        # persist to a deleted project) and drain any still-queued waiter now.
        rt.abort.set()
        self._drain_queued(state, rt)
        return True

    async def forget_project(self, chat_id: int, name: str) -> None:
        """Drop a project's in-memory runtime (B4 — purge on ``/rm``). No-op if absent.

        ``/rm <name>`` removes a project from the persisted registry, but its transient
        :class:`_ProjectRuntime` (cached engine + cwd + :class:`PermissionPolicy`) lives in
        ``state.runtimes`` keyed by the stored name. ``_runtime`` caches by name and
        deliberately ignores the passed cwd on a hit (a project's cwd is fixed for the life
        of its session — D4), so a stale runtime left here would be reused if the SAME name
        is re-created — running the recreated project in the OLD cwd and inheriting the OLD
        ``/yolo`` + allow-session grants (the SB5 bypass leak / D4 cwd leak). So after the
        store-remove, the runtime must be purged: find it by **case-insensitive** name
        (mirroring the store's case-insensitive match — ``/rm WORK`` must purge the runtime
        stored as ``work``), best-effort ``stop()`` its engine to free any live SDK client
        (try/except — a stop failure must not break the purge), then drop it from
        ``state.runtimes``. A subsequent ``/new <name>`` then
        builds a FRESH runtime from the store's record (new cwd, fail-closed policy) — no
        leak. ``cmd_rm`` already refuses the ACTIVE project **and a currently-RUNNING one**
        (D9), so the purged runtime is never a live turn's.

        **P5 / ADR-005 D9 (T9) — drain a QUEUED turn first.** If the removed project has a
        turn parked in the run queue (queued behind the cap, not yet running), its waiter is
        cancelled (:meth:`_drain_queued`) BEFORE the runtime is dropped — else that turn would
        spring to a "zombie run" of a now-removed project when a slot frees (the T6-review
        hazard). Also drop any pending-index entries the project owns. ``cmd_rm`` refuses a
        RUNNING project (its lock held), so here the runtime is at most queued or idle.
        """
        state = self._chats.get(chat_id)
        if state is None:
            return
        key = self._resolve_runtime_key(state.runtimes, name)
        if key is None:
            return  # no in-memory runtime for that name — clean no-op.
        rt = state.runtimes[key]
        # ADR-005 D9 (round-3 BLOCKERS 1+2): SET this project's abort BEFORE draining/purging.
        # cmd_rm now refuses a project that is in-flight by ANY measure (running OR queued OR in
        # the transfer window — inflight-aware busyness), so by the time forget_project runs the
        # project should be idle; but a turn could be popped into the transfer window in the gap
        # between the bot's busy check and here. Setting the abort guarantees that even such a
        # window turn aborts cleanly (it checks the abort before it can start) and never
        # zombie-runs a NOW-REMOVED project (the dropped store record made its result-persist a
        # late UnknownProject). The draining below still unwinds a still-queued waiter.
        rt.abort.set()
        # D9 (T9): drain a queued-not-yet-running turn for this project (no zombie run) and
        # drop its pending-index entries, before tearing the runtime down.
        self._drain_queued(state, rt)
        self._clear_project_pending(state, key)
        if rt.engine is not None:
            try:
                await rt.engine.stop()
            except Exception:
                log.debug(
                    "stop of engine raised while forgetting chat %s project %s "
                    "(ignored — dropping the runtime regardless)",
                    chat_id,
                    key,
                    exc_info=True,
                )
        del state.runtimes[key]

    @staticmethod
    def _resolve_runtime_key(
        runtimes: dict[str, "_ProjectRuntime"], name: str
    ) -> Optional[str]:
        """The actual ``runtimes`` key whose casefold matches ``name``, or ``None``.

        Mirrors the store's :func:`~claude_tg.session_store._resolve_name`: runtimes are
        keyed by the project's STORED (as-created) name, and the store matches names
        case-insensitively, so a lookup against the in-memory runtimes must too (else a
        casing variant — ``/rm WORK`` for a ``work`` project — would leave the stale runtime
        behind). Defensive against a non-``str`` ``name``.
        """
        if not isinstance(name, str):
            return None
        target = name.casefold()
        for key in runtimes:
            if isinstance(key, str) and key.casefold() == target:
                return key
        return None

    def reset(self, chat_id: int) -> None:
        """Reset the ACTIVE project to a fresh conversation (harvested /reset, D3/D7).

        Clears the active project's persisted ``session_id`` (a fresh conversation — the
        project is KEPT in the registry, not deleted), drops its in-memory engine +
        pending state, and wipes its :class:`PermissionPolicy` (drops every allow-session
        grant and turns ``/yolo`` off — D7) so the next session restarts **fail-closed**.
        A running turn (holding the lock) is not force-killed here; ``/cancel`` aborts a
        live turn. With no active project there is nothing to reset (no side effects).

        **P5 / ADR-005 D7.** Live-turn state now lives on the per-project runtime, so reset
        clears **the active project's** runtime live-turn state (its status line + free-text
        marker + status, set back to ``idle``) — NOT a chat-global slot — and drops the
        active project's pending-index entries. A concurrent project's runtime + held
        requests are untouched (reset is scoped to the active project, D2/D7).

        **NB3 (cross-model QA) — drain a not-yet-started QUEUED turn first.** The bot refuses
        ``/reset`` while the active project's OWN turn is in flight (its lock held), but a
        QUEUED active project (parked behind the cap, lock NOT yet held) is "not busy", so
        ``/reset`` proceeds. If reset just cleared the session and left the queued turn parked,
        that turn would later **zombie-run** when a slot frees (running the project reset was
        meant to clear). So reset first **drains** the active project's queued turn — it never
        started, so there is no orphaned hold (unlike the running case the bot guards against)
        — consistent with the P4 ``/reset``-while-running rationale, then clears the session.
        """
        # Resolve the active project WITHOUT creating one (reset is not a turn): if there
        # is no active project there is no session to clear.
        name, rt = self._active_runtime(chat_id, create_default=False)
        state = self._chats.get(chat_id)
        if rt is not None:
            # ADR-005 D9 (round-3 BLOCKERS 1+2): SET the active project's abort BEFORE clearing
            # its session. The bot refuses /reset while the active project's OWN turn is RUNNING
            # (lock held), but a QUEUED active project (or one in the pop→lock slot-transfer
            # window) is not lock-busy, so /reset proceeds — and draining alone (below) misses a
            # turn already popped from the queue, which would then ZOMBIE-RUN the project reset
            # just cleared. The abort covers that window: the woken turn checks it and aborts
            # cleanly before running. Cleared by the next accepted turn (it can't start until
            # this reset returns since reset runs on the loop).
            rt.abort.set()
            # NB3: drain a QUEUED-not-yet-running turn for the active project FIRST, so reset
            # doesn't leave a parked turn that would zombie-run when a slot frees. The waiter's
            # CancelledError handler removes its queue entry + releases any transferred slot
            # (no leak); the turn never started, so there is no hold to orphan.
            if state is not None:
                self._drain_queued(state, rt)
            rt.engine = None
            rt.started = False
            rt.policy.clear()  # D7: drop grants + yolo so the next session is fail-closed.
            # D7: clear THIS project's live-turn state (status line + free-text marker +
            # status), not a chat-global slot.
            self._clear_runtime_turn_state(rt)
        if state is not None and name is not None:
            # Drop the active project's pending-index entries (+ a free-text marker aimed at
            # one of them); a concurrent project's held requests survive (scoped by name).
            self._clear_project_pending(state, name)
        if name is not None:
            # Clear the persisted session_id for the active project (keep cwd — D4 — and
            # the project record itself). update() writes the active project's fields.
            self._persist(chat_id, session_id=None)

    def _persist(
        self, chat_id: int, *, session_id: Optional[str], name: Optional[str] = None
    ) -> None:
        """Write ``session_id`` to a project (cwd untouched); ``session_id=None`` clears it.

        ``name`` selects which project (P5 / ADR-005 D2, the load-bearing per-project
        persist now that ``/switch`` is free):

        * ``name`` given → write to **that** project via
          :meth:`~JsonSessionStore.set_session_id` (case-insensitive). ``_drive_turn``
          passes the project it **captured at message time** so a turn's result-``session_id``
          (and any QF3 dead-id clear) lands on the project the turn ran **on**, NOT
          "whatever is active now" — because the active project can change mid-turn once
          ``/switch`` no longer waits for the run to finish (the lock-P-drive-Q /
          persist-drift hazard). An :class:`~claude_tg.session_store.UnknownProject` (the
          project was ``/rm``'d mid-turn) is swallowed like any other persist failure (RB1
          — never crash the turn over a write).
        * ``name`` omitted → fall back to the flat :meth:`~JsonSessionStore.update` over the
          chat's **active** project (the pre-P5 contract — used by ``reset`` and the
          ``_ensure_engine`` dead-resume clear, both of which act on the active project).

        ``cwd`` is always left untouched (D4): a project's cwd is fixed for the life of its
        session.
        """
        if self.store is None:
            return
        try:
            if name is not None:
                self.store.set_session_id(chat_id, name, session_id)
            else:
                self.store.update(chat_id, session_id=session_id, cwd=None)
        except Exception:
            log.exception("failed to persist streaming session state for chat %s", chat_id)

    def is_busy(self, chat_id: int, name: Optional[str] = None) -> bool:
        """Whether a turn is in flight (a project's per-project turn lock is held).

        **P5 / ADR-005 D1 (T5).** The turn lock moved off the chat onto each project's
        runtime, so "busy" is now per-project:

        * ``is_busy(chat_id, name)`` → whether **that** project's lock is held (matched
          case-insensitively against the stored runtime key, mirroring the store's name
          match, so ``/projects`` and ``/switch WORK`` agree). A project with no runtime is
          never busy. This is the surface T7's ``/reset`` per-project busy-guard uses.
        * ``is_busy(chat_id)`` (no name) → whether **any** project for the chat is busy
          (back-compat — the chat-level "anything running?" query). ``bot.py``'s current
          ``/switch``/``/new``/``/reset`` guards still call this form; **T7** rewires
          ``/reset`` to the per-project form and drops the guard from ``/switch``/``/new``.

        A chat with no state yet is never busy (RB1).
        """
        state = self._chats.get(chat_id)
        if state is None:
            return False
        if name is None:
            # Any project busy? (the chat-level back-compat query).
            return any(rt.lock.locked() for rt in state.runtimes.values())
        key = self._resolve_runtime_key(state.runtimes, name)
        if key is None:
            return False
        return state.runtimes[key].lock.locked()

    def project_status(self, chat_id: int, name: str) -> ProjectStatus:
        """The per-project run status for ``/projects`` (ADR-005 D7; read by T7's render).

        Read-only (RB1): a project with **no in-memory runtime** (never run a turn this
        process, e.g. just after restart) reads as ``idle`` — the D7 default — without
        creating anything. A live runtime reports its current ``status`` enum
        (``running`` / ``awaiting_approval`` / ``awaiting_answer`` / ``awaiting_plan`` /
        ``queued`` / ``idle``), which :func:`~claude_tg.render.project_status_label` maps
        to the column label. Matched case-insensitively against the stored runtime key
        (mirroring the store's name match) so ``/projects`` and ``/switch WORK`` agree.
        """
        state = self._chats.get(chat_id)
        if state is None:
            return "idle"
        key = self._resolve_runtime_key(state.runtimes, name)
        if key is None:
            return "idle"
        return state.runtimes[key].status

    def register_reply_prompt(
        self, chat_id: int, message_id: Optional[int], tool_use_id: Optional[str]
    ) -> None:
        """Map a sent free-text prompt's ``message_id -> tool_use_id`` (D5 reply-to hatch).

        The bot calls this AFTER it sends the ``✏️ <name>: reply…`` follow-up to an
        "Other"/"Reject" tap, passing the prompt message's id + the armed request's id (both
        from the :class:`CallbackOutcome`). A later reply-to **that** prompt then routes the
        answer by ``tool_use_id`` (overriding the most-recent default — :meth:`handle_message`
        precedence (a)). No-op if either id is missing (a send that returned no id, or a
        non-arming outcome). The entry is pruned on resolve / turn-end / cancel
        (:meth:`_prune_reply_to`) so the map stays bounded (D5) and a reply to a stale prompt
        can't misroute.
        """
        if message_id is None or not tool_use_id:
            return
        self._chat(chat_id).reply_to_index[message_id] = tool_use_id

    # -- the turn driver (LOCK-GUARDED: one turn per PROJECT) ----------------

    async def handle_message(
        self,
        chat_id: int,
        text: str,
        *,
        send: SendFn,
        edit: EditFn,
        delete: Optional[DeleteFn] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> None:
        """Drive ONE operator turn (or capture a free-text answer) for ``chat_id``.

        Free-text capture takes precedence: if any project is awaiting an "Other" answer /
        plan-reject feedback, this text is routed to ``engine.resolve`` (NOT a new turn)
        and the held turn — still inside ``engine.send`` — continues. Otherwise it opens
        a new turn via ``engine.send`` and renders the event stream against the **active
        project's** engine (auto-creating ``default`` on the first turn — ADR-004 D6).

        **Free-text routing under concurrency (P5 / ADR-005 D5; T9).** Several projects can
        be awaiting free text at once, so the target is chosen by this precedence (in one
        small resolver, :meth:`_route_free_text_target`, so the rule is a one-spot edit):
        (a) **reply-to** — if ``reply_to_message_id`` is a reply to a free-text prompt the
        relay sent (the ``message_id -> tool_use_id`` map), route to THAT request's project;
        (b) otherwise the **most-recently-armed** project (the default; the name-echoed
        prompt said which). The explicit ``/to <name> <text>`` escape hatch routes via
        :meth:`resolve_to` at the bot, not here. If NO project is armed → this is a normal
        new turn for the active project (unchanged). The relay **never silently misroutes**:
        a reply-to whose request is gone finds no live armed target and falls through to the
        normal-turn path only when nothing is armed at all — a reply-to that does not match
        a live armed request while OTHERS are armed resolves nothing (it does not silently
        hit the most-recent default).

        **Per-project lock — CONCURRENT runs (P5 / ADR-005 D1; T5).** The turn lock now
        lives on the TARGET project's runtime (the active project at message time), not the
        chat. A second message to the **same** running project raises :class:`StreamingBusy`
        (the bot replies "still working") — one run per project, never two interleaved on
        one project. A message to a **different** (idle) project takes ITS OWN lock and runs
        **concurrently** (N projects at once, PTB dispatches handlers concurrently). The
        lock does NOT cover a free-text resolve targeting an *already running* turn — that
        path must run concurrently with the held turn, so it is handled before locking.

        **Concurrency cap + FIFO queue (P5 / ADR-005 D6; T6).** Before driving, the turn
        acquires a process-global run SLOT (:meth:`_acquire_slot`). While the number of
        running turns is BELOW ``config.max_concurrent_runs`` it runs immediately; AT the
        cap it is **accepted and queued** (per-chat FIFO) — this project reports ``queued``
        to ``/projects``, the operator gets a one-time ``⏳ queued behind N run(s)`` notice,
        and the turn parks until a finishing run hands it the freed slot (SB6 fail-closed —
        queued, never refused, never dropped). A project is never queued behind ITSELF (the
        same-project :class:`StreamingBusy` check above runs first). The slot is released —
        exactly once, on EVERY exit path — by the ``finally`` (the D6 slot-leak hazard: a
        raised turn must never leak a slot and permanently shrink capacity).

        **SB2 fail-closed (T7).** If the active project's stored cwd is no longer within
        the permitted roots, :meth:`_ensure_engine` raises
        :class:`~claude_tg.paths.PathNotAllowed`; the engine is never started, this
        replies a clear refusal via ``send`` and RETURNS cleanly (the lock is released —
        no hang, RB1/SB6). **RB3 resume notice.** If a persisted session could not be
        resumed and a fresh one was started instead, a one-line notice is sent via
        ``send`` BEFORE the turn is driven (the turn still completes — never hangs, RB2).
        """
        state = self._chat(chat_id)

        # Free-text capture for a prior "Other"/reject tap routes to resolve(), not a new
        # turn — and must NOT take the turn lock (the awaiting turn holds it). The capture
        # marker lives on the OWNING project's runtime (ADR-005 D7); under concurrency
        # several projects can be armed, so the target is chosen by the D5 precedence
        # (reply-to > most-recent) in one resolver. ``routed`` is True iff free-text routing
        # CLAIMED this message (it was a free-text reply, even if the target turned out gone
        # — so a stale reply-to never silently falls through to a NEW turn / a misroute).
        armed_name, armed_rt, routed = self._route_free_text_target(
            state, reply_to_message_id
        )
        if routed:
            if armed_rt is not None:
                self._resolve_free_text(state, chat_id, armed_name, armed_rt, text)
            # else: a free-text reply whose target is gone/ambiguous — no-op (never a
            # misroute, never silently a new turn). The marker (if any) was already cleared
            # by _resolve_free_text on a prior attempt; nothing else to do.
            return

        # P5 / ADR-005 D1 (T5): lock the TARGET project — the active project at message
        # time — NOT the chat. Resolving it (create_default=True) auto-creates `default` on
        # the first turn (ADR-004 D6), exactly as a turn always must. A second message to
        # the SAME project while its lock is held raises StreamingBusy; a message to a
        # DIFFERENT idle project takes its own lock and runs concurrently. _ensure_engine /
        # _drive_turn re-resolve + pin the active project (the busy-guards keep it stable for
        # the turn in T5; T7 frees /switch but _drive_turn still pins the turn's project).
        target_name, target_rt = self._active_runtime(chat_id, create_default=True)
        assert target_name is not None and target_rt is not None  # create_default => both
        # Pin the captured (name, runtime) so _ensure_engine + _drive_turn act on THIS
        # project even if the turn QUEUES behind the cap (D6/T6) and the active project moves
        # while it is parked — a queued turn must run ITS OWN project, not whatever is active
        # when its slot frees.
        target = (target_name, target_rt)

        # A project is NEVER queued behind ITSELF (D6): a second message to the SAME project
        # is StreamingBusy, exactly as in T5 — checked BEFORE acquiring a slot so a busy
        # project never consumes a queue entry. "Busy" is the project's IN-FLIGHT marker
        # (``inflight``) — True continuously from the instant a turn is accepted (just below)
        # through queued / the pop→lock transfer window / running, until the end-of-turn
        # finally. The round-2 cross-model-QA RACE: a lock+queue guard (``lock.locked() or
        # _is_queued``) had a TOCTOU — after ``_pop_next_waiter`` pops a queued project's
        # waiter (so ``_is_queued`` is False) but before the woken turn acquires its lock (so
        # ``lock.locked()`` is False), a same-project 2nd message slipped through and appended a
        # SECOND _QueuedTurn (two turns for one project). ``inflight`` has no such gap. (It
        # subsumes the BLOCKER-2 ``_is_queued`` guard — a queued turn is in-flight — and the
        # lock guard; both are kept as belt-and-braces but ``inflight`` alone is sufficient.)
        if target_rt.inflight or target_rt.lock.locked() or self._is_queued(state, target_rt):
            raise StreamingBusy()

        # Accept the turn for THIS project: mark it in-flight BEFORE acquiring a slot, so the
        # busy-guard above rejects any same-project 2nd message at EVERY subsequent point
        # (queued, the slot-transfer window, running). Cleared ONLY in the outer finally below,
        # on every exit path — including a DRAIN-cancel of a queued waiter (which raises
        # CancelledError out of _acquire_slot, BEFORE the slot-release try) — so a cancelled /
        # drained / failed turn never leaves the project wedged as in-flight.
        target_rt.inflight = True
        # ADR-005 D9 (round-3 BLOCKERS 1+2): a FRESH turn starts un-aborted. Clear any stale
        # abort left set by a PREVIOUS turn's /cancel|/reset|/rm so it can't kill this one. Done
        # under inflight=True (after the busy-guard) — no other turn for this project can run
        # concurrently to observe a transient clear.
        target_rt.abort.clear()
        try:
            # P5 / ADR-005 D6 (T6): acquire a run SLOT before driving. Under the cap → run now
            # (the counter is incremented). At the cap → enqueue (per-chat FIFO), set this
            # project's status to "queued", send a one-time "queued behind N run(s)" notice, and
            # park until a finishing run hands this turn the freed slot (SB6: queue, never drop /
            # refuse). After this returns a slot is held and MUST be released exactly once below.
            # A DRAIN-cancel of this project's waiter raises CancelledError here (its own handler
            # in _acquire_slot does the slot bookkeeping); the outer finally still clears inflight.
            await self._acquire_slot(state, target_rt, send=send)
            # SLOT-LEAK SAFETY (the flagged D6 hazard): from here the slot is HELD. The whole
            # remainder — _ensure_engine, the SB2 refusal, the resume notice, AND _drive_turn —
            # runs inside this try so the finally's _release_slot fires on EVERY exit path
            # (normal end, mid-stream raise, cancel, resume-failure return, StreamingBusy below).
            # _release_slot decrements the global counter and pops the next queued waiter exactly
            # once, so a raised turn can never leak a slot (which would permanently shrink
            # capacity) and a slot is never double-released. Mirrors T5's end-of-turn finally.
            try:
                # ADR-005 D9 (round-3 BLOCKERS 1+2): THE SLOT-TRANSFER WINDOW abort check.
                # We have just resumed from _acquire_slot holding a slot. If this turn was
                # QUEUED, it spent the pop→here window in NEITHER the run queue (a transferring
                # _release_slot already popped it — _drain_queued can't see it) NOR holding a
                # live engine (none is started yet — engine.cancel finds nothing). So a
                # /cancel|/reset|/rm landing in that window can't reach this turn via the
                # queue-drain or the engine-cancel paths — it can only SET this project's abort.
                # Honor it HERE, before acquiring the lock / starting the engine / entering
                # _drive_turn: abort CLEANLY — the inner finally releases the slot we hold (no
                # leak), the outer finally clears inflight, and we persist NOTHING and never run.
                # This is the net invariant: a control command in the transfer window → the turn
                # NEVER starts; _running returns to 0; no session is persisted.
                if target_rt.abort.is_set():
                    return
                # While this turn was parked in the queue, another message to the SAME project
                # could have started running it (its lock would now be held). Re-check after the
                # slot is granted so the per-project one-run invariant holds even across a queue
                # wait; the finally still releases the slot this turn acquired.
                if target_rt.lock.locked():
                    raise StreamingBusy()
                async with target_rt.lock:
                    # ADR-005 D9: re-check the abort AFTER taking the lock and BEFORE starting
                    # the engine — a /cancel|/reset|/rm could have set it during the (awaited)
                    # lock acquisition above (the lock-wait sub-window). Bailing here means no
                    # engine is ever started/resumed for an aborted turn (no connected-but-
                    # undriven client, no _drive_turn, no persist). The finally still releases
                    # the slot. Together with the window check above, the abort covers EVERY
                    # pre-run await boundary; once _drive_turn starts streaming, a live engine
                    # exists and the command's engine.cancel() unblocks it instead.
                    if target_rt.abort.is_set():
                        return
                    try:
                        engine, resume_failed = await self._ensure_engine(
                            chat_id, target=target
                        )
                    except PathNotAllowed:
                        # SB2 (T7): the active project's stored cwd drifted out of the permitted
                        # roots (config narrowed, or a path component became an out-of-root
                        # symlink). Refuse the turn fail-closed WITHOUT starting the engine; the
                        # lock releases on return AND the finally releases the slot (no leak).
                        # Operator-facing refusal → verbatim priority through the D8 gate.
                        await self._gated_send(
                            state, send, verbatim=True,
                            text=(
                                f"❌ This project's directory {self.get_cwd(chat_id)} is no "
                                "longer within the permitted roots — use /new <name> <path> to "
                                "create one inside them."
                            ),
                            reply_markup=None,
                            parse_mode=None,
                        )
                        return
                    if resume_failed:
                        # RB3: the persisted session could not be resumed; a fresh one was
                        # started. Tell the operator BEFORE driving the turn (it still completes).
                        await self._gated_send(
                            state, send, verbatim=True,
                            text="⚠️ Couldn't resume this project's previous session; started a fresh one.",
                            reply_markup=None,
                            parse_mode=None,
                        )
                    await self._drive_turn(
                        state, chat_id, engine, text,
                        send=send, edit=edit, delete=delete, target=target,
                    )
            finally:
                # SLOT-LEAK SAFETY: release the slot this turn held — exactly once, on every
                # exit path. _release_slot decrements the global counter and, if a turn is
                # queued (this chat first, then any chat — FIFO), TRANSFERS the freed slot to
                # the oldest waiter (re-incrementing + waking it) so the dequeue fires on every
                # turn-exit too (normal / error / cancel / resume-failure). Pure bookkeeping +
                # a Future.set_result — it never awaits and never raises, so it cannot itself
                # leak or mask the turn's own exception (which propagates after the finally).
                self._release_slot(state)
        finally:
            # RACE fix (D6 / round-2 cross-model QA): clear the in-flight marker on EVERY exit
            # path of this turn — normal end, mid-stream raise, /cancel of a running turn, a
            # DRAIN-cancel of a queued waiter (CancelledError from _acquire_slot, which the
            # inner slot-release try does NOT cover), resume-failure, and the post-wait
            # StreamingBusy re-raise. This OUTER finally wraps _acquire_slot too, so inflight is
            # balanced even when the slot-release try is never entered (the drain-cancel path).
            # Pure attribute write — never awaits, never raises — so it can't mask the turn's
            # own exception. After this, the next same-project message is accepted again.
            target_rt.inflight = False

    # -- the run scheduler: cap + per-chat FIFO queue (ADR-005 D6 / T6) -------

    async def _acquire_slot(
        self, state: _ChatState, target_rt: _ProjectRuntime, *, send: SendFn
    ) -> None:
        """Acquire one process-global run slot — run now if under the cap, else QUEUE.

        The concurrency cap (``config.max_concurrent_runs``, D6) bounds how many turns RUN
        at once across the whole process. When the global :attr:`_running` count is below
        the cap, increment it and return immediately (run now). When AT the cap, the turn
        is **accepted and queued** (never refused / dropped — SB6 fail-closed → queue):

        * mark this project ``queued`` for ``/projects`` (T4 status / T7 render),
        * send a **one-time** ``⏳ queued behind N run(s)`` notice (D6 — N is the number of
          slot-holders ahead, i.e. the cap; richer live position is deferrable),
        * append a waiter :class:`asyncio.Future` to this chat's FIFO :attr:`run_queue` and
          ``await`` it. A finishing run pops the OLDEST waiter and **transfers** it the freed
          slot via :meth:`_release_slot` (which re-increments :attr:`_running` and resolves
          the future) — so on wake the slot is already counted as held and this turn just
          proceeds. FIFO order is preserved (``popleft`` of the oldest).

        Returns once a slot is held; the caller MUST release it exactly once (the
        ``handle_message`` ``finally`` → :meth:`_release_slot`). The counter is global; the
        queue is per-chat (no cross-chat semantics — D6).
        """
        cap = self.config.max_concurrent_runs
        if self._running < cap:
            self._running += 1
            return
        # At the cap → queue this turn (FIFO) and park until a slot is transferred to it.
        # Mark the project queued so /projects shows it (the turn has not started running).
        target_rt.status = "queued"
        # NB2: turns AHEAD of this one = the slot-holders RUNNING (== the cap when full) PLUS
        # any turns already QUEUED ahead of it (counted BEFORE this turn's entry is appended
        # below). Counting only ``_running`` would tell a turn queued behind other queued
        # turns the wrong position (always "behind <cap>"). The queue is per-chat, so only
        # this chat's already-queued turns precede it.
        ahead = self._running + len(state.run_queue)
        waiter: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        # Record the parked turn WITH its target runtime so /cancel + /rm can drain it (T9).
        queued = _QueuedTurn(runtime=target_rt, future=waiter)
        state.run_queue.append(queued)
        # One-time queued notice (D6). Best-effort: a failed notice must not strand the turn
        # in the queue (the wait below is what actually gates it), so swallow a send error.
        # Operator-facing → verbatim priority through the D8 send gate.
        try:
            await self._gated_send(
                state, send, verbatim=True,
                text=f"⏳ Queued behind {ahead} run(s) — it'll start when a slot frees.",
                reply_markup=None,
                parse_mode=None,
            )
        except Exception:
            log.debug("queued-notice send failed (turn still queued)", exc_info=True)
        # Park until a finishing run hands us the slot (it re-incremented _running for us).
        # If the wait is cancelled — shutdown, the awaiting task torn down, OR a /cancel|/rm
        # DRAIN of this queued project (T9: handle_cancel/_drain_queued cancels this future
        # before the turn ever runs, so no zombie run when a slot frees) — we must not leak:
        # either we were still queued (drop our entry — we never held a slot), or a
        # _release_slot had ALREADY transferred us the slot (our future is resolved, the
        # counter holds it for us) — in which case hand that slot straight back on
        # (_release_slot transfers it to the next waiter or decrements). Either way the
        # global count stays correct; the CancelledError then propagates (the turn is gone).
        try:
            await waiter
        except asyncio.CancelledError:
            removed = self._remove_queued(state, waiter)
            if not removed and waiter.done() and not waiter.cancelled():
                # Not in the queue → a transfer resolved our future a tick before the cancel
                # landed; that slot is counted as held for us, so release it (not leak it).
                self._release_slot(state)
            raise

    def _release_slot(self, state: _ChatState) -> None:
        """Release the current turn's run slot — decrement, or TRANSFER to the next waiter.

        Called exactly once per running turn from ``handle_message``'s ``finally`` (every
        exit path — normal end, mid-stream raise, cancel, resume-failure). The slot-leak
        safety contract (the flagged D6 hazard): a turn that consumed a slot ALWAYS reaches
        here, so capacity can never permanently shrink; and it adjusts :attr:`_running` by
        exactly one net step (either ``-1`` to free, or ``0`` because the slot is handed
        straight to a waiter), so the counter never drifts.

        FIFO dequeue: look for the OLDEST queued waiter — this chat's queue first, then any
        other chat's (the cap is global, so a slot freed here may unblock a turn queued in
        another chat; per-chat queues keep order within a chat). If one exists, **transfer**
        the slot to it: keep :attr:`_running` as-is (the slot stays held, now by the waiter)
        and resolve its future (waking the parked :meth:`_acquire_slot`). If none, simply
        decrement (the slot is now free). Pure + non-awaiting + never raises, so it cannot
        itself leak a slot or mask the turn's exception.
        """
        queued = self._pop_next_waiter(state)
        if queued is not None:
            # Transfer: the freed slot stays counted (now held by the woken turn). Do NOT
            # decrement — set the waiter's result so its parked _acquire_slot returns.
            queued.future.set_result(None)
            return
        # No one waiting → the slot is free. Decrement, clamped at 0 (defensive: a double
        # release must never drive the count negative and wrongly grant extra capacity).
        if self._running > 0:
            self._running -= 1

    def _pop_next_waiter(self, state: _ChatState) -> "Optional[_QueuedTurn]":
        """Pop the oldest still-pending queued turn (this chat first, then any), FIFO.

        Skips any already-cancelled/done futures (a queued turn whose task was torn down or
        DRAINED by /cancel|/rm — its CancelledError handler removes it, but a race could
        leave a settled future), so a transferred slot always goes to a LIVE waiter. Returns
        ``None`` when no chat has a pending waiter (the slot is then freed by the caller).
        """
        # This chat's queue first (preserve its FIFO order), then every other chat's.
        queues = [state.run_queue]
        queues.extend(s.run_queue for s in self._chats.values() if s is not state)
        for q in queues:
            while q:
                queued = q.popleft()
                if not queued.future.done():
                    return queued
        return None

    @staticmethod
    def _remove_queued(
        state: _ChatState, waiter: "asyncio.Future[None]"
    ) -> bool:
        """Remove the queue entry whose future is ``waiter``; return whether one was found.

        Used by :meth:`_acquire_slot`'s CancelledError handler (the parked turn was torn
        down / drained) to drop its own entry. ``False`` means it was not queued (a transfer
        already popped it), telling the caller to release the slot it now implicitly holds.
        """
        for i, queued in enumerate(state.run_queue):
            if queued.future is waiter:
                del state.run_queue[i]
                return True
        return False

    @staticmethod
    def _is_queued(state: _ChatState, rt: _ProjectRuntime) -> bool:
        """Whether ``rt`` already has a turn WAITING in the run queue (BLOCKER 2 guard).

        A turn that queued behind the cap parks on a waiter in :meth:`_acquire_slot` and
        holds NO lock until its slot is granted, so :meth:`is_busy` (lock-based) reports it
        idle. The pre-slot busy-guard uses this so a SECOND message to an already-queued
        project is refused (``StreamingBusy``) rather than appending a second
        :class:`_QueuedTurn` — one pending turn per project (D6). A finished/cancelled
        waiter (``future.done()``) does not count: its turn is no longer pending (its
        CancelledError handler removes the entry, but a settled-but-not-yet-popped future
        must not block a fresh turn).
        """
        return any(
            q.runtime is rt and not q.future.done() for q in state.run_queue
        )

    async def _drive_turn(
        self,
        state: _ChatState,
        chat_id: int,
        engine: Engine,
        prompt: str,
        *,
        send: SendFn,
        edit: EditFn,
        delete: Optional[DeleteFn] = None,
        target: Optional[tuple[str, _ProjectRuntime]] = None,
    ) -> None:
        """Iterate ``engine.send`` → render → Telegram send/edit (coalesced).

        D6 "loud throughout": if the active project's policy has ``/yolo`` on, lead the
        turn with a persistent ``⚠️`` marker (its OWN message, before any event renders)
        so an in-progress allow-all session is never silent — the bypass shows on every
        turn, not just at the ``/yolo`` toggle. A plain ``send`` (no coalescer / no
        status-line edit) so it cannot be overwritten by the in-place status edits that
        follow.

        **QF3 (B3/RB3): recover from a resume that connects then errors on first use.**
        If this is the FIRST turn on a freshly-resumed session (the runtime's
        ``resumed_unverified`` flag), every ``error``/``result`` event is checked with the
        ported ``_is_resume_failure`` heuristic. On a resume-failure-shaped event the dead
        ``session_id`` is NOT persisted; instead, AFTER the stream drains, the persisted id
        is cleared, the engine is dropped (so the next turn starts fresh — never re-resumes
        the dead id), and the operator is told to resend. If the turn instead completes
        cleanly, the flag is cleared (the resume is confirmed good). A FRESH session is
        never ``resumed_unverified``, so an unrelated fresh-turn error is never mistaken for
        a resume failure. The check happens INLINE while iterating and recovery happens
        AFTER the loop ends naturally (the substrate stream always terminates — RB2), so we
        never re-drive a turn mid-stream (no double-render / re-entrancy).
        """
        # The project this turn is running on. ``handle_message`` passes the project it
        # captured at message time (``target``) so the result-persist, any QF3 recovery, AND
        # the pending-index registration (ADR-005 D3 — id -> THIS turn's project) act on THIS
        # turn's project — critically for a turn that QUEUED behind the cap (D6/T6) and so
        # parked while the active project may have moved. Falling back to the active project
        # (no target) preserves the prior behavior for any direct caller.
        turn_name: Optional[str]
        turn_rt: Optional[_ProjectRuntime]
        if target is not None:
            turn_name, turn_rt = target
        else:
            turn_name, turn_rt = self._active_runtime(chat_id, create_default=True)
        assert turn_name is not None  # create_default=True always yields a project name
        assert turn_rt is not None  # create_default=True always yields a runtime too
        # This first turn applies the resume-failure heuristic iff the session was resumed
        # (not freshly started) and is not yet confirmed good.
        check_resume = turn_rt.resumed_unverified
        resume_failure_detected = False
        # P6/H2/RB2: latch a transport/liveness ``driver_error`` on this turn. A
        # driver_error means the SDK client is dead/wedged (a 120s liveness timeout that
        # was NOT a held human-approval — that case is suppressed in the adapter now — or a
        # transport failure). On an already-VERIFIED session (not the resume-failure case,
        # which has its OWN rebuild via _recover_failed_resume) the engine must be torn down
        # + rebuilt so the NEXT turn starts a fresh client, instead of every later turn
        # re-timing-out against the same dead client (the wedge-until-restart finding, RB2).
        # Latched here (body-free — only the kind_of_error is read, never the message, SB3)
        # and acted on AFTER the stream drains so we never re-enter the render loop mid-turn.
        driver_error_detected = False

        coalescer = Coalescer(now=self._clock, min_interval=self._min_edit_interval)
        # P6/R5: per-turn duplicate-render dedup (the single foreground policy point for the
        # twin-render paths, alongside the ask/plan dedup the engine does in _drain_substrate).
        # Remembers verbatim bodies emitted THIS turn so the terminal frame doesn't re-send the
        # assistant prose (#1) or re-render a tool_error as a near-identical turn_error (#3).
        # Foreground-only: the background branch pings ✅/🔔 and continues before the render
        # section, so this never touches a backgrounded run.
        dedup = _TurnDedup()
        # P5 / ADR-005 D7: THIS project's status line + status enum (per-project, not a
        # chat-global slot). Status line starts unset (create on first edit_status); the
        # status enum goes idle -> running at turn start, awaiting_<kind> on a hold, back to
        # running on resolve, idle at turn end. Two concurrent turns each drive their OWN
        # runtime's line + status, so they never clash.
        turn_rt.status_message_id = None
        turn_rt.status_text = None
        turn_rt.status = "running"
        # D6 "loud throughout" — but only inline for a FOREGROUND turn (a backgrounded run is
        # silent inline, D4; its yolo posture still shows on each foreground turn + via
        # /projects is not yolo-aware, so this is the loud surface when watched). Verbatim
        # priority through the D8 gate so the marker is never starved by status churn.
        if turn_rt.policy.yolo and self._is_foreground(chat_id, turn_name):
            await self._gated_send(
                state, send, verbatim=True,
                text=yolo_indicator(), reply_markup=None, parse_mode=None,
            )
        # P5 / ADR-005 D1 + T4-review: now runs are CONCURRENT and per-project ``status``
        # feeds /projects, a mid-stream exception in the loop below must NOT leave this
        # project stuck at running/awaiting_* (a stale status would mislead /projects and a
        # lingering "💭 thinking…" line would never clear). So the turn body is wrapped in
        # try/finally: the finally forces this project's status back to ``idle`` and clears
        # its transient status line (best-effort delete) no matter how the loop exits. The
        # per-project lock is released by handle_message's ``async with`` regardless, so a
        # raised turn frees its lock and leaves OTHER concurrent runs untouched (RB1/RB2).
        try:
            async for event in engine.send(prompt):
                # QF3: on the first turn of a resumed session, flag a resume-failure-shaped
                # error/result. Latch on the first hit (the dead id is the same all turn).
                if check_resume and not resume_failure_detected and _is_resume_failure_event(event):
                    resume_failure_detected = True
                # P6/H2/RB2: latch a transport/liveness driver_error (body-free — kind only,
                # never event.message, SB3) so the verified-session engine is rebuilt after
                # the stream drains. Independent of the resume-failure check above: a fresh
                # OR resume-confirmed session can still driver_error mid-life, and that is the
                # wedge this guards. (A resume-failure-shaped driver_error on an UNVERIFIED
                # resumed session is handled by _recover_failed_resume instead — see below.)
                if (
                    not driver_error_detected
                    and isinstance(event, ErrorEvent)
                    and event.kind_of_error == "driver_error"
                ):
                    driver_error_detected = True
                # ADR-005 D3: register an injected ask/plan/permission in the pending index,
                # keyed by tool_use_id -> THIS turn's project, so a later tap / free-text
                # reply routes to THIS project's engine (not _active_engine). Cleared on
                # resolve / cancel / turn-end. Permission is registered too (P4 routed it
                # id-only, but the index must own every held request so the
                # foreground-vs-notify decision (T3) and the cross-project routing cover it).
                self._register_pending(state, turn_name, event)
                # ADR-005 D7: a held request flips THIS project's status to the matching
                # awaiting_<kind> for /projects; it returns to running when the resolve path
                # unblocks the held turn (set in the resolve/cancel methods, which own ref).
                held_kind = _pending_kind_of(event)
                if held_kind is not None:
                    turn_rt.status = _AWAITING_STATUS[held_kind]
                if isinstance(event, ResultEvent):
                    # QF3: do NOT re-persist the dead session_id on a resume-failure result
                    # — it would just re-arm the same broken resume. Recovery below clears it.
                    # (Foreground-INDEPENDENT — the session_id must persist whether the turn
                    # rendered inline or pinged in the background.)
                    if not resume_failure_detected:
                        # ADR-005 D2: persist to THIS turn's CAPTURED project (turn_name), not
                        # the active one — once /switch is free the active project can change
                        # mid-turn, so writing to "active" would clobber a different project's
                        # session_id (the lock-P-drive-Q / persist-drift hazard). turn_name is
                        # the project handle_message pinned at message time.
                        self._persist(
                            chat_id,
                            session_id=event.session_id or engine.session_id,
                            name=turn_name,
                        )
                # ADR-005 D4: the inline-vs-notify send-decision. Re-read foreground PER
                # EVENT — /switch is free (T7), so the foreground can change mid-turn; an
                # event for the foreground project renders inline (as P4), an event for a
                # BACKGROUND project becomes a name-prefixed 🔔/✅/⚠️ ping (the operator is
                # not watching that project). A backgrounded run does NOT spam its verbose
                # status inline — its progress is summarized by the ping + the /projects
                # status column (D4) — so non-hold, non-terminal events are dropped for a
                # background turn (they never reach the coalescer/status line).
                if not self._is_foreground(chat_id, turn_name):
                    if held_kind is not None:
                        await self._notify_background(
                            state, chat_id, turn_name, event, held_kind, send=send
                        )
                    elif isinstance(event, (ResultEvent, ErrorEvent)):
                        await self._notify_terminal(state, turn_name, event, send=send)
                    # else (text/tool_use/status/incremental): a background run is silent —
                    # no inline status spam (D4). Skip the inline render entirely.
                    continue
                # --- foreground: render inline exactly as P4 (through the D8 send gate) ---
                if isinstance(event, AskEvent):
                    # Render each question as its OWN message + option keyboard so a
                    # question's choices sit directly beneath it. A single stacked keyboard
                    # for a multi-question ask is an unreadable wall of buttons (the operator
                    # can't tell which buttons belong to which question). Flush any buffered
                    # status first so the questions appear after it, in order.
                    for action in coalescer.flush().actions:
                        await self._perform(
                            state, turn_rt, action, send=send, edit=edit, delete=delete
                        )
                    for q_idx in range(len(event.questions)):
                        keyboard = ask_question_keyboard(event, q_idx)
                        # The question text is Claude-authored CommonMark -> render as HTML
                        # so **bold** etc. show and a stray < / & can't break the message; on
                        # a Telegram HTML rejection, resend the plain body (raw fallback —
                        # never a dropped question). Verbatim priority in the D8 gate.
                        try:
                            await self._gated_send(
                                state, send, verbatim=True,
                                text=ask_question_body_html(event, q_idx),
                                reply_markup=keyboard,
                                parse_mode="HTML",
                            )
                        except Exception:
                            await self._gated_send(
                                state, send, verbatim=True,
                                text=ask_question_body(event, q_idx),
                                reply_markup=keyboard,
                                parse_mode=None,
                            )
                    continue
                # P6/R5 #3: a terminal turn_error that merely repeats a tool_error already
                # shown this turn is a duplicate error block — drop it (the tool_error already
                # rendered the failure verbatim). Done BEFORE record so we never compare an
                # event against itself.
                if dedup.suppresses(event):
                    continue
                # P6/R5 #1: when the terminal ResultEvent.result_text just repeats assistant
                # prose already emitted this turn, render only the compact ✅ done footer rather
                # than re-sending the identical answer. Swap in a footer-only result (keeps
                # num_turns/cost) — the done indicator still appears, the prose is sent once.
                render_event_ = event
                if isinstance(event, ResultEvent) and dedup.result_is_duplicate_prose(event):
                    render_event_ = _footer_only_result(event)
                # Remember this turn's verbatim bodies (assistant prose + tool_error messages)
                # so a later twin (the result_text / terminal turn_error) can dedup against it.
                dedup.record(event)
                # SB3/H1 (body-free): a RAW EXTERNAL error (tool/SDK stderr) renders as a
                # body-free summary to the chat (see render._render_error); its raw detail
                # goes ONLY to the LOCAL debug log, SCRUBBED through _redact_sid (the body can
                # carry a session id — the bot token is never logged anywhere). This is the
                # single place the raw body is persisted, and only at DEBUG.
                if isinstance(render_event_, ErrorEvent) and error_is_raw_external(render_event_):
                    log.debug(
                        "raw external error (%s) for chat %s project %s [%s]: %s",
                        render_event_.kind_of_error,
                        chat_id,
                        turn_name,
                        _redact_sid(render_event_.session_id),
                        _redact_sid_in_text(render_event_.message),
                    )
                for action in coalescer.offer(render_event_).actions:
                    await self._perform(
                        state, turn_rt, action, send=send, edit=edit, delete=delete
                    )
            # End of turn: flush any trailing coalesced status line, then DELETE the
            # transient status message ("💭 Claude is thinking…") so a stale thinking-line
            # never lingers after the turn's real content. Best-effort (RB1): a failed delete
            # must never kill the turn — the content is already sent. Optional `delete` so
            # existing callers that don't pass one keep working (the status line just stays).
            for action in coalescer.flush().actions:
                await self._perform(
                    state, turn_rt, action, send=send, edit=edit, delete=delete
                )
        finally:
            # T4-review: ALWAYS clear this project's transient status line + set status idle,
            # even if the loop above raised mid-stream — so a concurrent project is never
            # left reading a stale running/awaiting_* status and the "💭 thinking…" line is
            # never orphaned. On the clean path this is the same cleanup that used to follow
            # the loop; on the exception path it is the safety net (then the exception
            # propagates to handle_message, whose ``async with`` releases the per-project
            # lock — the chat stays usable, RB1).
            if delete is not None and turn_rt.status_message_id is not None:
                try:
                    await delete(message_id=turn_rt.status_message_id)
                except Exception:
                    log.debug("status-line delete failed at turn end", exc_info=True)
            turn_rt.status_message_id = None
            turn_rt.status_text = None
            # ADR-005 D7: the turn is over → this project is idle again (no runtime → idle is
            # the /projects default; a running/awaiting project that just ended → idle).
            turn_rt.status = "idle"
            # ADR-005 D3: drop any pending-index entries this turn's project left open (an
            # ask/plan/permission the operator never answered — the engine has stopped
            # awaiting it now the stream drained / the turn died, so a late tap on it is a
            # stale-id no-op). In the finally so a mid-stream raise can't leak a project's
            # index entries either. Scoped to THIS turn's project so a concurrent project's
            # still-open holds survive (T5); an in-flight free-text capture aimed at one of
            # them is cleared with it. Pure + no await, so it can't itself raise here.
            self._clear_project_pending(state, turn_name)

        # QF3 (B3/RB3): finalize the resume verification AFTER the stream has fully drained
        # (so we never re-enter the render loop mid-turn). Either recover from a detected
        # resume failure, or confirm the resume good by clearing the flag.
        recovered = False
        if check_resume:
            if resume_failure_detected:
                await self._recover_failed_resume(chat_id, turn_name, turn_rt, send=send)
                recovered = True  # the engine was already torn down + dropped here.
            elif turn_rt is not None:
                # The first resumed turn completed without a resume failure → confirmed good.
                turn_rt.resumed_unverified = False

        # P6/H2/RB2: a transport/liveness driver_error on a VERIFIED session (a fresh start,
        # or a resume already confirmed good) leaves a dead/wedged SDK client behind — every
        # later turn on it would re-time-out (the wedge-until-restart finding). Tear it down +
        # drop the engine so the NEXT turn rebuilds a fresh client. Skipped when the resume-
        # failure path above already recovered (it dropped the engine + cleared the dead id);
        # acted on AFTER the stream drained (never mid-render). The session_id is NOT cleared
        # here — unlike a resume failure, the persisted (session_id, cwd) is still valid; the
        # rebuilt engine resumes it next turn (RB3). The operator already saw the driver_error
        # rendered, so no extra notice is sent (SB3 — the error body never re-surfaces).
        if driver_error_detected and not recovered:
            await self._rebuild_after_driver_error(chat_id, turn_name, turn_rt)

    async def _recover_failed_resume(
        self,
        chat_id: int,
        name: Optional[str],
        rt: Optional[_ProjectRuntime],
        *,
        send: SendFn,
    ) -> None:
        """Recover when a resumed session errored on its first turn (QF3 / B3 / RB3).

        Fail clean, never hang: clear the active project's persisted ``session_id`` so the
        dead id is NOT retried, drop the runtime's engine/started so the NEXT turn starts
        fresh, and notify the operator to resend (a clean-fail-then-fresh-next-turn rather
        than an in-loop auto-re-send, which would risk double-render / re-entrancy). The
        notice send is best-effort the same as the rest of the turn; if it raises it
        propagates, but the persisted id is ALREADY cleared and the engine dropped first, so
        the project is never left wedged on the dead session.
        """
        log.info(
            "resume connected but first turn failed for chat %s project %s; "
            "clearing the persisted session and recovering fresh",
            chat_id,
            name,
        )
        # 1) Clear the persisted dead id FIRST so even if the notice send fails the stale
        #    session is gone (the next turn will start fresh, not re-resume it). ADR-005 D2:
        #    clear it on the CAPTURED project (``name`` — the project whose turn just failed
        #    to resume), not the active one, since /switch may have moved active mid-turn.
        self._persist(chat_id, session_id=None, name=name)
        # 2) Drop the in-memory engine so the next turn rebuilds + starts fresh. Best-effort
        #    stop() the connected-but-dead engine BEFORE dropping the reference so its SDK
        #    client is closed rather than orphaned (QF3-review non-blocker, same pattern as
        #    the resume-raises path). A stop failure must NOT re-wedge — the dead id is
        #    already cleared above, so even if stop() raises the next turn starts fresh.
        if rt is not None:
            if rt.engine is not None:
                try:
                    await rt.engine.stop()
                except Exception:
                    log.debug(
                        "stop of dead-resumed engine raised for chat %s project %s "
                        "(ignored — id already cleared, recovering fresh)",
                        chat_id,
                        name,
                        exc_info=True,
                    )
            rt.engine = None
            rt.started = False
            rt.resumed_unverified = False
        # 3) Tell the operator (the turn already rendered the underlying error).
        #    Operator-facing → verbatim priority through the D8 send gate.
        await self._gated_send(
            self._chat(chat_id), send, verbatim=True,
            text=(
                "⚠️ Couldn't resume this project's previous session (it may be expired) — "
                "cleared it. Send your message again to start fresh."
            ),
            reply_markup=None,
            parse_mode=None,
        )

    async def _rebuild_after_driver_error(
        self,
        chat_id: int,
        name: Optional[str],
        rt: Optional[_ProjectRuntime],
    ) -> None:
        """Tear down + drop a VERIFIED session's engine after a transport/liveness driver_error
        so the NEXT turn rebuilds a fresh client (P6/H2/RB2 — no wedge-until-restart).

        Mirrors the engine-teardown half of :meth:`_recover_failed_resume`, but for a session
        that was already CONFIRMED good (a fresh start, or a resume verified by a prior clean
        turn) and then driver_errored mid-life — the dead SDK client would otherwise make every
        later turn re-time-out against it. Differences from the resume-failure path:

        * **The persisted ``session_id`` is NOT cleared.** Unlike an expired/torn resume, the
          ``(session_id, cwd)`` is still valid; the rebuilt engine RESUMES it next turn (RB3),
          so the conversation continues rather than starting over. The rebuilt engine is
          ``resumed_unverified`` again iff it resumes a persisted id (set by ``_ensure_engine``).
        * **No operator notice is sent.** The driver_error was already rendered to the operator
          on this turn; re-announcing it would be noise (and the body must not re-surface, SB3).

        Best-effort ``stop()`` (a failing stop must not re-wedge — the reference is dropped
        regardless, so the next turn starts fresh). Per-project + no slot/lock work here: this
        runs INSIDE ``_drive_turn``, after the stream drained, while ``handle_message`` still
        holds this project's lock and its run slot; both are released by ``handle_message``'s
        ``finally`` on return exactly as on any turn exit (no leak, P5 lifecycle preserved).
        Other projects' live engines are untouched (per-project isolation).
        """
        log.info(
            "driver_error on a verified session for chat %s project %s; tearing down the "
            "engine so the next turn rebuilds a fresh client (no wedge)",
            chat_id,
            name,
        )
        if rt is None:
            return
        if rt.engine is not None:
            try:
                await rt.engine.stop()
            except Exception:
                log.debug(
                    "stop of driver_errored engine raised for chat %s project %s "
                    "(ignored — reference dropped, rebuilding fresh next turn)",
                    chat_id,
                    name,
                    exc_info=True,
                )
        # Drop the engine + started flag so _ensure_engine rebuilds on the next turn. The
        # persisted session_id is deliberately LEFT in place (resume it next turn, RB3).
        rt.engine = None
        rt.started = False

    async def _perform(
        self,
        state: _ChatState,
        rt: _ProjectRuntime,
        action: RenderAction,
        *,
        send: SendFn,
        edit: EditFn,
        delete: Optional[DeleteFn] = None,
    ) -> None:
        """Execute ONE :class:`RenderAction` against Telegram (the deferred I/O).

        ``rt`` is the runtime of the project whose turn produced the action — a status
        edit folds into THAT project's status line (ADR-005 D7), so two concurrent turns'
        status lines never clash. All I/O funnels through the chat's send-rate gate
        (``state`` carries it — ADR-005 D8): an ``op="new"`` (verbatim) send is PRIORITY,
        an ``op="edit_status"`` is the low-priority status line that yields to it (so a
        concurrent project's status churn never starves this verbatim message).

        ``delete`` (optional) lets an ``edit_status`` whose in-place edit FAILS clean up the
        orphaned old status line before sending its replacement (P6/R5 #2) — only one status
        line ever lives. Absent (direct callers / tests that pass no ``delete``), the old line
        is simply left as before — no crash.
        """
        if action.op == "none" or not action.chunks:
            return
        if action.op == "edit_status":
            await self._edit_status(state, rt, action, send=send, edit=edit, delete=delete)
            return
        # op == "new": one message per chunk. The keyboard rides the FIRST NON-EMPTY chunk —
        # whitespace-only chunks are skipped, so if the head chunk is whitespace the buttons
        # must still attach to the first real one (else an ask/plan would lose its keyboard).
        first = True
        for i, chunk in enumerate(action.chunks):
            if not chunk.strip():
                continue
            markup = action.reply_markup if first else None
            first = False
            try:
                # Verbatim (final answer / error / ask / plan / permission) is PRIORITY in
                # the per-chat gate (D8) so it is never starved by coalesced status churn.
                await self._gated_send(
                    state, send, verbatim=True,
                    text=chunk, reply_markup=markup, parse_mode=action.parse_mode,
                )
            except Exception:
                # HTML render fallback (CRITICAL): a chunk Telegram rejects as HTML (a bad
                # entity from a converter edge case) must NEVER drop the message. Resend the
                # ORIGINAL raw markdown for this chunk as plain text — worst case equals
                # today's behavior (raw markdown), never a lost message. Only HTML sends can
                # raise this way; a plain send that fails re-raises (nothing left to try).
                if action.parse_mode is None:
                    raise
                plain = self._plain_fallback(action, i, chunk)
                await self._gated_send(
                    state, send, verbatim=True,
                    text=plain, reply_markup=markup, parse_mode=None,
                )

    @staticmethod
    def _plain_fallback(action: RenderAction, i: int, html_chunk: str) -> str:
        """The plain-text fallback for ``action.chunks[i]`` (an HTML chunk Telegram rejected).

        Prefer the parallel RAW chunk the render layer carried (the exact original
        markdown — what the bot showed before HTML rendering); if absent, strip the tags
        from the HTML as a last resort so the operator still sees readable text.
        """
        if action.plain_chunks and i < len(action.plain_chunks):
            return action.plain_chunks[i]
        return strip_telegram_html(html_chunk)

    async def _edit_status(
        self,
        state: _ChatState,
        rt: _ProjectRuntime,
        action: RenderAction,
        *,
        send: SendFn,
        edit: EditFn,
        delete: Optional[DeleteFn] = None,
    ) -> None:
        """Edit THIS project's coalesced status line in place (create on first use).

        The status line id/text live on the per-project :class:`_ProjectRuntime` (ADR-005
        D7), so each running project edits its OWN line — a status burst in one project
        never touches another's. The actual create/edit funnels through the chat's
        send-rate gate as the **non-verbatim** (low-priority) kind (ADR-005 D8), so this
        status churn yields to verbatim and the combined cross-project rate stays bounded.

        **P6/R5 #2 (orphaned status line):** when the in-place edit FAILS (message gone /
        too old) the fallback sends a brand-new status message and re-points
        ``status_message_id`` at it. But turn-end cleanup deletes only the LATEST id, so the
        old line would be ORPHANED — left visible forever. So if a ``delete`` is available we
        best-effort DELETE the stale id BEFORE sending the replacement; only one status line
        ever exists. A failed delete is swallowed (RB1) — the replacement still goes out.
        """
        body = action.text
        if not body.strip():
            return
        if body == rt.status_text:
            # Identical to what's already shown — skip. Editing a Telegram message to the
            # same text raises "message is not modified"; the old fallback then sent a fresh
            # message, which is exactly the status-line spam we must avoid. Skipping BEFORE
            # the gate also means an unchanged status never consumes a send slot.
            return
        if rt.status_message_id is None:
            mid = await self._gated_send(
                state, send, verbatim=False,
                text=body, reply_markup=None, parse_mode=action.parse_mode,
            )
            rt.status_message_id = mid
            rt.status_text = body
            return
        try:
            await self._gated_edit(
                state, edit,
                message_id=rt.status_message_id, text=body, parse_mode=action.parse_mode,
            )
            rt.status_text = body
        except Exception:
            # A genuine edit failure (message gone / too old) must never kill the turn
            # (RB1/RB2); fall back to a fresh status message. Identical-text edits are
            # already skipped above, so this is a real failure, not a no-op edit.
            log.debug("status edit failed for chat; sending a fresh status line", exc_info=True)
            # P6/R5 #2: delete the soon-to-be-orphaned old status line first (best-effort)
            # so the turn-end cleanup's single-id delete doesn't leave it behind. A failed
            # delete is ignored — the replacement must still be sent (RB1).
            if delete is not None:
                stale_id = rt.status_message_id
                try:
                    await delete(message_id=stale_id)
                except Exception:
                    log.debug("orphaned status-line delete failed (ignored)", exc_info=True)
            mid = await self._gated_send(
                state, send, verbatim=False,
                text=body, reply_markup=None, parse_mode=action.parse_mode,
            )
            rt.status_message_id = mid
            rt.status_text = body

    # -- the callback resolve path (LOCK-FREE: SB1 enforced at the bot) ------

    def resolve_callback(self, chat_id: int, data: object) -> "CallbackOutcome":
        """Route a decoded inline-keyboard tap to its OWNING project's pending request.

        **The bot has already enforced SB1** (``filters.Chat(allowed)`` + an explicit
        ``_authorized`` recheck) before calling this; an unauthorized chat never reaches
        here. Defense in depth remains: a ``callback_data`` that ``decode_callback``
        rejects (foreign / stale / malformed → ``None``) is IGNORED — no decision is
        resolved, nothing raises (RB1). This is intentionally **lock-free**: it resolves
        the pending decision a held turn is awaiting (the turn loop is parked inside
        ``engine.send``), so it must run concurrently with the held turn — taking the turn
        lock would deadlock the very turn it must unblock.

        **Id-routed (ADR-005 D3).** The decoded ``tool_use_id`` is looked up in the per-chat
        **pending-request index** → the owning project + held event; the decision resolves
        against **THAT project's** engine — **not** ``_active_engine``. So a tap for project
        A resolves A's request even while B is the active/foreground project. An id absent
        from the index resolves nothing (``handled=False``, a benign no-op — a stale/forged
        button, RB1). As defense-in-depth the held event's ``session_id`` is checked against
        the owning engine's current ``session_id`` before resolving (a stale id colliding
        after a resume never resolves the wrong session).

        Mapping:

        * ask option tap (``a``)   → :class:`QuestionAnswer` (native answers map) →
          ``engine.resolve`` immediately.
        * ask "Other" (``o``)      → set the free-text marker; the NEXT message is the
          free-text answer (no resolve yet).
        * plan approve (``p``/a)   → :class:`PlanVerdict` ``approve=True`` → resolve.
        * plan reject  (``p``/r)   → set the free-text marker; the NEXT message is the
          reject feedback (no resolve yet).

        Returns a :class:`CallbackOutcome` describing what happened so the bot can craft
        the ``answer_callback_query`` toast. A stale/forged callback that maps to no
        pending request resolves nothing and returns ``handled=False``.
        """
        decoded = decode_callback(data)
        if decoded is None:
            return CallbackOutcome(handled=False, note="ignored")
        state = self._chat(chat_id)
        # Route by id: the pending index owns id -> (project, kind, held event). An "Other"
        # tap arms free-text capture (no engine call), but it must still target a KNOWN
        # pending ask, so it too looks the id up first.
        ref = state.pending_index.get(decoded.tool_use_id)
        if ref is None:
            # Unknown / stale / forged id — nothing pending for it (RB1 benign no-op).
            return CallbackOutcome(handled=False, note="no pending request")

        if decoded.kind == "other":
            return self._arm_ask_other(state, ref, decoded)

        engine = self._engine_for_pending(chat_id, ref)
        if engine is None:
            # The owning project has no live engine, or its session no longer matches the
            # held event (stale id after a resume — defense-in-depth) → resolve nothing.
            return CallbackOutcome(handled=False, note="no pending request")

        if decoded.kind == "ask":
            return self._resolve_ask_option(state, engine, ref, decoded)
        if decoded.kind == "plan":
            return self._resolve_plan(state, engine, ref, decoded)
        if decoded.kind == "permission":
            return self._resolve_permission(state, engine, ref, decoded)
        return CallbackOutcome(handled=False, note="ignored")

    def _engine_for_pending(
        self, chat_id: int, ref: _PendingRef
    ) -> Optional[Engine]:
        """The live engine of the project that OWNS ``ref`` — or ``None`` (no-op).

        Routes by the index entry's ``project_name`` (ADR-005 D3) — **not**
        ``_active_engine`` — so a decision resolves against whatever project's turn raised
        the request, regardless of which project is active. Returns ``None`` (the caller
        no-ops) when the owning project has no in-memory runtime / no live engine (a stale
        button after the engine was dropped), **or** when the held event's ``session_id``
        no longer matches the engine's current ``session_id`` (defense-in-depth: a stale id
        that survived a resume must never resolve the wrong session).
        """
        state = self._chats.get(chat_id)
        if state is None:
            return None
        rt = state.runtimes.get(ref.project_name)
        if rt is None or rt.engine is None:
            return None
        # Defense-in-depth (ADR-005 D3): the held event's session must match the engine's
        # current session. The event carries the session id it was injected under; if the
        # engine has since re-attached to a different session (resume), refuse to resolve.
        # A held event with no session id (engine had not reported one yet) is allowed —
        # there is nothing to contradict, and the id alone is a globally-unique routing key.
        held_session = getattr(ref.event, "session_id", None)
        if held_session is not None and rt.engine.session_id is not None:
            if held_session != rt.engine.session_id:
                # SB3/H1: redact both ids — the comparison stays debuggable (two distinct
                # tags ⇒ a genuine mismatch) without logging the raw resumable ids.
                log.debug(
                    "refusing to resolve id for chat %s project %s: held %s != "
                    "engine %s (stale id after resume)",
                    chat_id,
                    ref.project_name,
                    _redact_sid(held_session),
                    _redact_sid(rt.engine.session_id),
                )
                return None
        return rt.engine

    def _resolve_ask_option(
        self, state: _ChatState, engine: Engine, ref: _PendingRef, decoded: Callback
    ) -> "CallbackOutcome":
        if ref.kind != "ask" or not isinstance(ref.event, AskEvent):
            return CallbackOutcome(handled=False, note="no matching question")
        ask = ref.event
        try:
            q_idx = int(decoded.question_index)  # type: ignore[arg-type]
            # answers_from_ask validates the indices and yields {question: label}; keep
            # the label and record it against the question index (accumulate, below).
            one = answers_from_ask(ask, q_idx, int(decoded.option_index))  # type: ignore[arg-type]
        except (IndexError, KeyError, TypeError):
            # Stale/forged indices for a now-different ask — ignore (RB1).
            return CallbackOutcome(handled=False, note="stale option")
        return self._record_ask_answer(state, engine, ref, q_idx, next(iter(one.values()), ""))

    def _record_ask_answer(
        self, state: _ChatState, engine: Engine, ref: _PendingRef, q_idx: int, answer: str
    ) -> "CallbackOutcome":
        """Record ONE question's answer; resolve the whole ask only once EVERY question
        in it has an answer.

        A single ``AskUserQuestion`` carries all its questions under one ``tool_use_id``
        and the native ``answers`` map must cover them all — resolving on the first tap
        (the original bug) sent a partial map the tool rejects, stranding a multi-question
        ask. So we accumulate per-question answers in **this id's** index entry
        (``ref.ask_answers`` — keyed per id so two concurrent asks never clobber each other)
        and call ``engine.resolve`` only when the count reaches ``len(ask.questions)``.
        Re-tapping a question overwrites its answer (count unchanged), so the operator can
        change a choice before the last one. A SINGLE-question ask resolves on the first tap,
        exactly as before — no regression. Shared by the option-tap and "Other" free-text
        paths.
        """
        ask = ref.event
        assert isinstance(ask, AskEvent)  # guarded by the callers (kind == "ask")
        ref.ask_answers[q_idx] = answer
        total = len(ask.questions)
        answered = len(ref.ask_answers)
        if answered < total:
            return CallbackOutcome(
                handled=True,
                note=f"Answered {answered}/{total} — {total - answered} to go",
            )
        # Every question answered → build the full native map and resolve once. Drop the
        # index entry FIRST so a no-op resolve can't strand the chat in "answering" mode.
        answers = {
            str(ask.questions[i].get("question", "")): ans
            for i, ans in ref.ask_answers.items()
        }
        tuid = ask.tool_use_id
        self._drop_pending(state, tuid)
        if tuid is None:
            return CallbackOutcome(handled=False, note="no question id")
        resolved = engine.resolve(tuid, QuestionAnswer(answers=answers))
        if resolved:
            # ADR-005 D7: the held turn resumes → the owning project is running again (the
            # awaiting_answer status reverts). Intermediate taps stayed awaiting_answer.
            self._resume_pending_status(state, ref)
            return CallbackOutcome(handled=True, note=f"All {total} answered ✓")
        return CallbackOutcome(handled=False, note="already answered")

    def _arm_ask_other(
        self, state: _ChatState, ref: _PendingRef, decoded: Callback
    ) -> "CallbackOutcome":
        if ref.kind != "ask" or not isinstance(ref.event, AskEvent):
            return CallbackOutcome(handled=False, note="no matching question")
        # ADR-005 D7: arm free-text capture on the OWNING project's runtime (not a chat
        # slot), so the next plain message resolves THIS project even while another is
        # active. P5 / ADR-005 D5 (T9): SEVERAL projects may be armed at once now — the
        # most-recently-armed wins (the name-echoed prompt said which). We no longer clear a
        # prior armed marker; instead each arm is stamped with a monotonic sequence
        # (``awaiting_text_armed_at``) so the resolver can pick the newest. Re-arming the
        # SAME runtime simply re-stamps it (it becomes the newest again).
        rt = self._runtime_for_pending(state, ref)
        if rt is None:
            return CallbackOutcome(handled=False, note="no pending request")
        rt.awaiting_text_for = decoded.tool_use_id
        rt.awaiting_text_mode = "ask_other"
        rt.awaiting_text_question_index = decoded.question_index
        rt.awaiting_text_armed_at = self._next_armed_seq(state)
        return CallbackOutcome(
            handled=True,
            note="Type your answer",
            expects_text=True,
            project_name=ref.project_name,
            tool_use_id=decoded.tool_use_id,
        )

    def _resolve_plan(
        self, state: _ChatState, engine: Engine, ref: _PendingRef, decoded: Callback
    ) -> "CallbackOutcome":
        if ref.kind != "plan" or not isinstance(ref.event, PlanEvent):
            return CallbackOutcome(handled=False, note="no matching plan")
        if decoded.plan_action == "approve":
            resolved = engine.resolve(decoded.tool_use_id, PlanVerdict(approve=True))
            if resolved:
                self._drop_pending(state, decoded.tool_use_id)
                # ADR-005 D7: the held turn resumes → owning project running again.
                self._resume_pending_status(state, ref)
                return CallbackOutcome(handled=True, note="Plan approved")
            return CallbackOutcome(handled=False, note="already decided")
        # reject → capture feedback as the next message, on the OWNING project's runtime.
        # P5 / ADR-005 D5 (T9): newest-wins — stamp the arm sequence rather than clearing a
        # prior armed marker, so several projects can be awaiting free text and the most-
        # recently-armed is the default target (the name-echoed prompt said which).
        rt = self._runtime_for_pending(state, ref)
        if rt is None:
            return CallbackOutcome(handled=False, note="no pending request")
        rt.awaiting_text_for = decoded.tool_use_id
        rt.awaiting_text_mode = "plan_reject"
        rt.awaiting_text_question_index = None
        rt.awaiting_text_armed_at = self._next_armed_seq(state)
        return CallbackOutcome(
            handled=True,
            note="Type your feedback",
            expects_text=True,
            project_name=ref.project_name,
            tool_use_id=decoded.tool_use_id,
        )

    def _resolve_permission(
        self, state: _ChatState, engine: Engine, ref: _PendingRef, decoded: Callback
    ) -> "CallbackOutcome":
        """Route a permission tap to the held risky-tool request (P2, ADR-003 §2).

        Maps the decoded ``permission_action`` to the engine's three-way
        :class:`~claude_tg.engine.types.PermissionDecision` verdict and resolves the held
        request by ``tool_use_id`` against the OWNING project's engine (ADR-005 D3 — the id
        is looked up in the index, then routed to that project, not ``_active_engine``).
        Lock-free like the ask/plan resolve: it unblocks the held turn parked inside
        ``engine.send``.

        **The allow-session GRANT is recorded by the engine on resolve** (T3
        ``Engine._verdict_for``), NOT here — the session only translates the tap to a
        verdict and routes it. A stale/forged tap that resolves nothing (already decided,
        backstopped) returns ``handled=False`` with a benign note.

        Defense-in-depth (RB1/SB6): the index entry must actually be a **permission** hold.
        A forged ``m|<id>|…`` whose id maps to an ask/plan entry must NOT resolve that
        request with a permission verdict (a type confusion) — refuse it.
        """
        if ref.kind != "permission":
            return CallbackOutcome(handled=False, note="no pending request")
        verdict = _PERMISSION_VERDICTS.get(decoded.permission_action or "")
        if verdict is None:  # unknown action (defensive; decode already validates)
            return CallbackOutcome(handled=False, note="ignored")
        resolved = engine.resolve(decoded.tool_use_id, PermissionDecision(verdict=verdict))
        if resolved:
            self._drop_pending(state, decoded.tool_use_id)
            # ADR-005 D7: the held turn resumes → owning project running again.
            self._resume_pending_status(state, ref)
            return CallbackOutcome(handled=True, note=_PERMISSION_NOTES[verdict])
        # Nothing pending for this id — already decided / backstopped / cancelled.
        return CallbackOutcome(handled=False, note="no pending request")

    def _resolve_free_text(
        self,
        state: _ChatState,
        chat_id: int,
        armed_name: Optional[str],
        armed_rt: _ProjectRuntime,
        text: str,
    ) -> None:
        """Resolve a pending "Other"/reject with the just-typed ``text``; clear the marker.

        Routed from :meth:`handle_message` (free-text capture takes precedence over a new
        turn). An "Other" answer becomes a :class:`QuestionAnswer` keyed by the held
        question text; reject feedback becomes :class:`PlanVerdict` ``approve=False`` with
        the feedback on the deny channel.

        **Per-project marker + id-routed engine (ADR-005 D7/D3).** ``armed_rt`` is the
        runtime whose turn armed free-text capture (its ``awaiting_text_*`` marker, found by
        :meth:`_armed_text_runtime`); the held id is then looked up in the pending index →
        the owning project → THAT project's engine — **not** ``_active_engine``. So a
        free-text reply resolves the project that prompted it even while a different project
        is active. If the id is no longer in the index (turn ended / cancelled), or the
        owning project has no live engine / a mismatched session, this is a harmless no-op
        (the marker is cleared first so the chat is never wedged in capture mode, RB1).
        """
        tool_use_id = armed_rt.awaiting_text_for
        mode = armed_rt.awaiting_text_mode
        q_idx = armed_rt.awaiting_text_question_index
        # Clear the capture marker FIRST (on the armed runtime) so a failure can't wedge the
        # chat (RB1). The index entry itself is dropped by _record_ask_answer (on full
        # resolve) / below.
        self._clear_runtime_text(armed_rt)
        if tool_use_id is None:
            return
        ref = state.pending_index.get(tool_use_id)
        if ref is None:
            return  # the held request is gone (turn ended / cancelled) — no-op.
        engine = self._engine_for_pending(chat_id, ref)
        if engine is None:
            return  # owning project has no live engine / stale session — no-op.
        if mode == "ask_other" and ref.kind == "ask" and isinstance(ref.event, AskEvent):
            ask = ref.event
            if q_idx is not None and 0 <= q_idx < len(ask.questions):
                # Record this question's free-text answer; resolve only once every
                # question in the ask is answered (mirrors the option-tap path so a
                # multi-question ask is not stranded by a single "Other" reply).
                self._record_ask_answer(state, engine, ref, q_idx, text)
            # else: the index is stale for this question — harmless no-op (marker cleared).
        elif mode == "plan_reject" and ref.kind == "plan":
            self._drop_pending(state, tool_use_id)
            engine.resolve(tool_use_id, PlanVerdict(approve=False, feedback=text))
            # ADR-005 D7: the held turn resumes → owning project running again.
            self._resume_pending_status(state, ref)

    def resolve_to(self, chat_id: int, name: str, text: str) -> str:
        """Route ``text`` as the free-text answer/feedback to ``name`` (``/to`` — D5).

        The explicit escape hatch (D5): ``/to <name> <text>`` resolves the named project's
        pending free-text request regardless of which project is the most-recent default or
        what a reply-to points at. Allowlist-gated at the bot like every command (no new
        callback surface). Returns the operator-facing reply string:

        * unknown ``name`` (no runtime) **or** the project is not awaiting free text → a
          clear no-op message (RB1) — **never** silently route to the wrong project (the D5
          "never misroute" bar).
        * armed → resolve via the same :meth:`_resolve_free_text` path the most-recent /
          reply-to routes use (lock-free; it unblocks the held turn) and confirm.

        ``text`` is the answer/feedback verbatim (SB4 — never interpolated into a shell).
        """
        state = self._chats.get(chat_id)
        if state is None:
            return f"❌ No project named {name!r} is awaiting a reply."
        key = self._resolve_runtime_key(state.runtimes, name)
        rt = state.runtimes.get(key) if key is not None else None
        if rt is None or rt.awaiting_text_for is None:
            # Unknown name, or the project has no pending "Other"/reject to answer. Clear,
            # body-free no-op — do NOT fall back to the most-recent default (never misroute).
            return (
                f"❌ {name} is not awaiting a free-text reply "
                "(tap “Other”/“Reject” on its prompt first)."
            )
        self._resolve_free_text(state, chat_id, key, rt, text)
        return f"✅ Sent your reply to {key}."

    # -- cancel --------------------------------------------------------------

    def handle_cancel(self, chat_id: int, name: Optional[str] = None) -> int:
        """Abort a project's in-flight (or queued) turn cleanly (RB4/D9); clear its state.

        **Concurrency-aware target (P5 / ADR-005 D9; T9):**

        * ``name=None`` → the **active** project (``/cancel``).
        * ``name="all"`` → **every** running/queued project in the chat (``/cancel all``).
        * ``name=<project>`` → **that** project (``/cancel <name>``).

        For each targeted project this cancels its RUNNING engine (``engine.cancel()`` —
        every pending interactive request resolved as a clean deny, so a held turn unblocks
        and the session stays usable) AND **drains a QUEUED-not-yet-running turn** (cancels
        its parked waiter so it never springs to a "zombie run" when a slot frees — the
        T6-review hazard), and clears that project's pending-index entries + any free-text
        capture aimed at them (ADR-005 D3). Lock-free for the same reason as
        :meth:`resolve_callback` — a cancelled RUNNING turn holds its lock and ``cancel()``
        unblocks it (taking the lock would deadlock the very turn it must release).

        Returns the number of **cancelled units** across the targeted project(s): pending
        requests aborted by the engine PLUS any drained queued-not-yet-running turn (NB1 — a
        queued-only turn aborts 0 pending requests but the operator DID cancel a turn, so it
        counts, letting ``cmd_cancel`` report it truthfully instead of "nothing in flight")
        PLUS a slot-transfer-window abort (NB round-3 — a turn popped but not yet started is in
        neither bucket, yet the abort cancels it, so it counts as one too). 0 only when nothing
        was running, queued, OR in the transfer window for the target(s).
        """
        state = self._chats.get(chat_id)
        if state is None:
            return 0

        if isinstance(name, str) and name.casefold() == "all":
            # /cancel all → every project with a runtime: cancel its engine + drain its
            # queued waiter. Snapshot the names first (draining mutates the queue / clears
            # pending; cancelling a held turn does not add runtimes synchronously).
            total = 0
            for pname in list(state.runtimes.keys()):
                total += self._cancel_project(state, pname)
            return total

        if name is None:
            # /cancel (no arg) → the ACTIVE project (do not auto-create one — nothing to
            # cancel for a chat that never ran a turn).
            active, _rt = self._active_runtime(chat_id, create_default=False)
            if active is None:
                return 0
            return self._cancel_project(state, active)

        # /cancel <name> → that project (case-insensitive, mirroring the store match).
        key = self._resolve_runtime_key(state.runtimes, name)
        if key is None:
            return 0  # unknown / no-runtime project — nothing to cancel (RB1 no-op).
        return self._cancel_project(state, key)

    def _cancel_project(self, state: _ChatState, project_name: str) -> int:
        """Cancel ONE project's run/queued turn + clear its pending state (D9 helper).

        ``project_name`` is the exact ``runtimes`` key. Drains a queued-not-yet-running
        waiter for it first (no zombie run), then cancels a RUNNING engine, then clears the
        project's pending-index entries (so a late tap on a cancelled request no-ops). A
        concurrent project's still-open holds / queued turn survive (scoped by name, T5).

        Returns the number of **cancelled units**: the pending requests the engine aborted
        PLUS any drained queued-not-yet-running turn (NB1 — a queued-only turn has no live
        engine, so it aborts 0 pending requests, but the operator DID cancel a turn; counting
        it lets ``cmd_cancel`` tell the truth instead of "nothing was in flight") PLUS a
        SLOT-TRANSFER-WINDOW abort (NB round-3 — a turn popped from the queue but not yet
        started is in neither bucket: nothing to drain, no live engine, but the ``abort`` we
        set genuinely cancels it, so it counts as one). 0 only when the project was genuinely
        idle (not running, not queued, not in the transfer window).
        """
        rt = state.runtimes.get(project_name)
        if rt is None:
            return 0
        # (0) ADR-005 D9 (round-3 BLOCKERS 1+2): SET this project's abort signal. This is the
        #     ONE mechanism that covers the slot-transfer window — a queued turn that has been
        #     popped but not yet started holds no lock, no live engine, and no queue entry, so
        #     neither the drain (1) nor the engine-cancel (2) below can reach it; only the abort
        #     does (the woken turn checks it before it can run, and aborts cleanly). Set FIRST so
        #     it is visible no matter which lifecycle state the turn is in (queued / window /
        #     running). A still-queued turn is also drained (1) so it unwinds promptly rather
        #     than waiting for a slot to transfer; a running turn is also cancelled (2). Harmless
        #     for an idle project: the next accepted turn clears it before running.
        rt.abort.set()
        # (1) Drain a QUEUED-not-yet-running turn for this project (T9): cancel its parked
        #     waiter so it never starts when a slot frees. The waiter's CancelledError
        #     handler removes it from the queue + releases any transferred slot (no leak).
        #     A drained queued turn counts toward the cancelled total (NB1).
        drained = self._drain_queued(state, rt)
        # (2) Cancel a RUNNING engine (lock-free — unblocks the held turn).
        aborted = 0
        if rt.engine is not None:
            aborted = rt.engine.cancel()
        # (3) Drop this project's pending-index entries (+ a free-text marker aimed at them)
        #     so a late tap on a cancelled request is a stale-id no-op.
        self._clear_project_pending(state, project_name)
        # (4) NB (round-3 Codex): count a SLOT-TRANSFER-WINDOW abort as one cancelled unit.
        #     A turn that was popped from the queue but has not yet started (the pop→lock window)
        #     is in NEITHER counted bucket — ``_drain_queued`` found nothing (already popped, so
        #     ``drained == 0``) and no engine is live yet (``rt.engine is None``, so ``aborted == 0``)
        #     — yet the ``abort`` we set in (0) genuinely cancels it (the woken turn honors it and
        #     never runs). Without this, ``cmd_cancel`` would wrongly tell the operator "nothing in
        #     flight" for a turn it DID cancel. Predicate = the turn is in flight AND neither other
        #     mechanism reached it: ``rt.inflight and drained == 0 and rt.engine is None``. This
        #     CANNOT double-count — it is mutually exclusive with both other buckets by construction:
        #       * a RUNNING turn has a live engine (set by ``_ensure_engine`` before ``_drive_turn``),
        #         so ``rt.engine is None`` is False here → counted only via ``aborted`` (its pending
        #         requests), never here;
        #       * a still-QUEUED turn is drained by (1), so ``drained >= 1`` → counted only via
        #         ``drained`` (NB1), never here;
        #       * an IDLE project is not in flight (``rt.inflight`` False) → not counted at all.
        #     Mirrors NB1 (the queued-only count): a turn the operator really aborted reports as one.
        window_abort = 1 if (rt.inflight and drained == 0 and rt.engine is None) else 0
        return aborted + drained + window_abort

    @staticmethod
    def _drain_queued(state: _ChatState, rt: _ProjectRuntime) -> int:
        """Cancel every QUEUED (not-yet-running) waiter belonging to ``rt`` (T9 drain).

        A queued turn parks on its waiter inside :meth:`_acquire_slot` before it ever
        reaches ``_drive_turn`` — it has no live engine and no pending-index entries yet, so
        the ONLY thing holding it is the future. Cancelling that future wakes its
        ``_acquire_slot`` into the CancelledError path, which removes the entry from the
        queue and releases any slot already transferred to it — so a cancelled/removed
        queued project can never spring to a "zombie run" when a slot frees (the T6-review
        hazard). We cancel the future and leave the queue mutation to that handler (so the
        slot-accounting stays in one place); a defensive ``status`` reset to ``idle`` covers
        the case where the parked task has not yet been scheduled to run its handler.

        Returns the number of queued waiters drained (NB1: the caller counts these as
        cancelled units so a queued-only ``/cancel`` reports the turn it really aborted).
        Normally 0 or 1 (one pending turn per project — BLOCKER 2), but it drains every
        matching waiter defensively.
        """
        drained = 0
        for queued in list(state.run_queue):
            if queued.runtime is rt and not queued.future.done():
                queued.future.cancel()
                drained += 1
        if drained and rt.status == "queued":
            rt.status = "idle"
        return drained

    # -- shutdown ------------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop every project's engine across every chat (idempotent). For a clean exit."""
        for state in self._chats.values():
            for rt in state.runtimes.values():
                if rt.engine is not None:
                    try:
                        await rt.engine.stop()
                    except Exception:
                        log.exception("error stopping engine during shutdown")

    # -- internals: the pending-request index (ADR-005 D3) -------------------

    @staticmethod
    def _register_pending(state: _ChatState, project_name: str, event: Event) -> None:
        """Register an injected ask/plan/permission in the index, keyed by its id.

        Maps ``tool_use_id -> _PendingRef(project_name, kind, event)`` so a later tap /
        free-text reply / cancel routes to **this** project's engine. Non-interactive
        events (text/tool_use/status/error/result) carry no held request and are ignored.
        A re-register for the same id replaces the entry (last writer wins; a fresh
        accumulator), matching the per-turn ``pending_ask = event`` reset P4 did.
        """
        kind = _pending_kind_of(event)
        if kind is None:
            return
        tuid = getattr(event, "tool_use_id", None)
        if not tuid:
            return  # no id → not routable (defensive; the engine always sets one)
        state.pending_index[tuid] = _PendingRef(
            project_name=project_name, kind=kind, event=event
        )

    @staticmethod
    def _drop_pending(state: _ChatState, tool_use_id: Optional[str]) -> None:
        """Remove one index entry by id (on resolve); also clear a free-text marker on it.

        The free-text marker now lives on the OWNING project's runtime (ADR-005 D7), so if
        that project's runtime is armed for THIS id, its marker is cleared too. Any reply-to
        map entries pointing at this id are pruned (D5) so a reply to a now-resolved prompt
        can't misroute and the map can't grow unboundedly.
        """
        if tool_use_id is None:
            return
        StreamingSession._prune_reply_to(state, tool_use_id)
        ref = state.pending_index.pop(tool_use_id, None)
        if ref is None:
            return
        rt = state.runtimes.get(ref.project_name)
        if rt is not None and rt.awaiting_text_for == tool_use_id:
            StreamingSession._clear_runtime_text(rt)

    @staticmethod
    def _clear_project_pending(state: _ChatState, project_name: str) -> None:
        """Drop every index entry OWNED by ``project_name`` (turn-end / cancel / reset).

        Scoped to one project so a concurrent project's still-open holds survive (T5); the
        owning runtime's free-text marker is cleared iff it pointed at one of the dropped
        ids (the marker is now per-project — ADR-005 D7).
        """
        doomed = [
            tuid
            for tuid, ref in state.pending_index.items()
            if ref.project_name == project_name
        ]
        for tuid in doomed:
            del state.pending_index[tuid]
            # D5: prune any reply-to map entries aimed at this dropped id (so a reply to a
            # now-gone prompt no-ops rather than misroutes, and the map can't grow unbounded).
            StreamingSession._prune_reply_to(state, tuid)
        rt = state.runtimes.get(project_name)
        if rt is not None and rt.awaiting_text_for in doomed:
            StreamingSession._clear_runtime_text(rt)

    @staticmethod
    def _prune_reply_to(state: _ChatState, tool_use_id: str) -> None:
        """Drop every reply-to map entry (message_id -> id) pointing at ``tool_use_id`` (D5).

        Called whenever a held request is resolved / its turn ends / it is cancelled, so the
        ``message_id -> tool_use_id`` map (populated when a free-text prompt is sent) stays
        bounded and a reply to a stale prompt can never resolve the wrong (or a gone)
        request — it simply finds no live armed runtime and no-ops.
        """
        stale = [mid for mid, tuid in state.reply_to_index.items() if tuid == tool_use_id]
        for mid in stale:
            del state.reply_to_index[mid]

    @staticmethod
    def _runtime_for_pending(
        state: _ChatState, ref: _PendingRef
    ) -> Optional[_ProjectRuntime]:
        """The runtime of the project that owns ``ref`` (or ``None``) — ADR-005 D3/D7."""
        return state.runtimes.get(ref.project_name)

    @staticmethod
    def _resume_pending_status(state: _ChatState, ref: _PendingRef) -> None:
        """A held request was resolved → the owning project's turn resumes (status running).

        ADR-005 D7: a successful resolve unblocks the held turn parked inside
        ``engine.send`` (the turn loop continues), so the project goes back from
        ``awaiting_<kind>`` to ``running``; the turn's own end will set it ``idle``.
        Best-effort (RB1): a missing runtime simply no-ops.
        """
        rt = state.runtimes.get(ref.project_name)
        if rt is not None:
            rt.status = "running"

    @staticmethod
    def _armed_text_runtime(
        state: _ChatState,
    ) -> tuple[Optional[str], Optional[_ProjectRuntime]]:
        """The MOST-RECENTLY-ARMED project's (name, runtime), or ``(None, None)`` (D5).

        The free-text marker lives per-project (ADR-005 D7), and under concurrency SEVERAL
        projects can be armed at once (an "Other"/"Reject" tapped on each). The default
        free-text target is the **most-recently-armed** project (D5 — the name-echoed prompt
        said which), so this returns the armed runtime with the HIGHEST
        ``awaiting_text_armed_at`` (newest wins). ``handle_message``'s free-text-vs-new-turn
        decision uses this as the default; a reply-to / ``/to`` overrides it. ``(None, None)``
        when no project is armed (then a plain message is a normal new turn).
        """
        best_name: Optional[str] = None
        best_rt: Optional[_ProjectRuntime] = None
        best_seq = -1
        for name, rt in state.runtimes.items():
            if rt.awaiting_text_for is not None and rt.awaiting_text_armed_at > best_seq:
                best_name, best_rt, best_seq = name, rt, rt.awaiting_text_armed_at
        return best_name, best_rt

    @staticmethod
    def _next_armed_seq(state: _ChatState) -> int:
        """The next monotonic arm sequence for a free-text capture (D5 newest-wins)."""
        state.armed_seq += 1
        return state.armed_seq

    def _route_free_text_target(
        self, state: _ChatState, reply_to_message_id: Optional[int]
    ) -> tuple[Optional[str], Optional[_ProjectRuntime], bool]:
        """Pick the free-text target by the D5 precedence — the ONE routing-rule spot.

        Returns ``(name, runtime, routed)``:

        * ``routed`` — True iff this plain message is a free-text REPLY that free-text
          routing claims (so ``handle_message`` resolves it / no-ops, never opens a new
          turn over it). False means "not a free-text reply" → a normal new turn.
        * ``(name, runtime)`` — the target to resolve against (``runtime`` may be ``None``
          even when ``routed`` is True: a free-text reply whose request is gone — we claim
          it and no-op rather than misroute).

        Precedence (D5):

        1. **reply-to** — if ``reply_to_message_id`` is in the reply-to map (the operator
           replied to a free-text prompt the relay sent), we COMMIT to that id: route to its
           project IFF it is still live-armed for that id, else ``routed=True`` with no
           runtime (no-op — **never** fall through to the most-recent default, which would
           be a misroute). A ``reply_to_message_id`` that is NOT one of our prompts (a reply
           to something else, or no reply) falls through to (2).
        2. **most-recently-armed** — the default target (the name-echoed prompt said which);
           ``routed`` iff some project is armed. No armed project → ``(None, None, False)``
           (a normal new turn).

        ``/to <name>`` is the third escape hatch but routes via :meth:`resolve_to` at the
        bot (an explicit command), not through here.
        """
        # (1) reply-to: only when the replied-to message is one of OUR free-text prompts.
        if reply_to_message_id is not None:
            mapped_id = state.reply_to_index.get(reply_to_message_id)
            if mapped_id is not None:
                name, rt = self._runtime_armed_for_id(state, mapped_id)
                # Claim it either way (it was a reply to our prompt): route if live-armed,
                # else no-op (never misroute to the most-recent default).
                return name, rt, True
        # (2) default: the most-recently-armed project (newest wins).
        name, rt = self._armed_text_runtime(state)
        return name, rt, rt is not None

    def _runtime_armed_for_id(
        self, state: _ChatState, tool_use_id: str
    ) -> tuple[Optional[str], Optional[_ProjectRuntime]]:
        """The (name, runtime) armed for ``tool_use_id`` specifically, or ``(None, None)``.

        Used by the reply-to escape hatch (D5): the operator replied to a free-text prompt
        whose ``message_id`` mapped to ``tool_use_id``; the answer must resolve THAT request,
        not whichever project is the most-recently-armed default. We look the id up in the
        pending index to find the owning project, then return its runtime IFF that runtime
        is currently armed for this exact id (a stale reply-to whose request has since been
        answered / its turn ended finds nothing → the caller no-ops, never misroutes).
        """
        ref = state.pending_index.get(tool_use_id)
        if ref is None:
            return None, None
        rt = state.runtimes.get(ref.project_name)
        if rt is not None and rt.awaiting_text_for == tool_use_id:
            return ref.project_name, rt
        return None, None

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _clear_runtime_text(rt: _ProjectRuntime) -> None:
        """Clear ONE runtime's free-text capture marker (ADR-005 D7).

        Resets the arm sequence to 0 too (D5) so a cleared marker can never win a later
        most-recent routing decision against a freshly-armed project.
        """
        rt.awaiting_text_for = None
        rt.awaiting_text_mode = None
        rt.awaiting_text_question_index = None
        rt.awaiting_text_armed_at = 0

    # NOTE (P5 / ADR-005 D5, T9): the T4 ``_clear_armed_text`` helper (clear the single
    # armed runtime before arming a new one) is gone — under concurrency SEVERAL projects may
    # be armed at once and the **most-recently-armed** wins (``awaiting_text_armed_at`` +
    # :meth:`_armed_text_runtime`), so arming no longer clears a prior marker. A resolved /
    # ended / cancelled request clears its own marker via :meth:`_clear_runtime_text`.

    @staticmethod
    def _clear_runtime_turn_state(rt: _ProjectRuntime) -> None:
        """Clear ONE runtime's live-turn UI/capture state + reset status to idle (D7).

        Used by :meth:`reset` for the active project: drop its status line id/text, its
        free-text marker, and set ``status`` back to ``idle`` (a reset project is idle).
        """
        rt.status_message_id = None
        rt.status_text = None
        rt.status = "idle"
        StreamingSession._clear_runtime_text(rt)


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


@dataclass(frozen=True)
class CallbackOutcome:
    """Result of routing one inline-keyboard tap (so the bot can answer the query).

    * ``handled``      — True iff the tap resolved a decision or armed free-text capture.
    * ``note``         — a short toast string for ``answer_callback_query`` (operator
                         feedback; never carries secrets).
    * ``expects_text`` — True iff the bot should prompt the operator to type the next
                         message (an "Other" answer / reject feedback).
    * ``project_name`` — the OWNING project of an ``expects_text`` arm (D5): the bot
                         name-echoes it in the free-text prompt (``✏️ <name>: reply…``) so
                         the operator can tell which project the next message resolves.
    * ``tool_use_id``  — the armed request's id (D5): the bot maps the free-text **prompt's**
                         ``message_id -> tool_use_id`` so a reply-to that prompt routes by id
                         (the reply-to escape hatch overriding the most-recent default).

    ``project_name`` / ``tool_use_id`` are populated only for an ``expects_text`` outcome
    (the "Other"/"Reject" arm); they are ``None`` for an immediate resolve / a no-op.
    """

    handled: bool
    note: str = ""
    expects_text: bool = False
    project_name: Optional[str] = None
    tool_use_id: Optional[str] = None


__all__ = [
    "StreamingSession",
    "StreamingBusy",
    "CallbackOutcome",
    "EngineFactory",
    "SendFn",
    "EditFn",
    "DeleteFn",
]
