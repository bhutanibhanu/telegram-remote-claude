"""Streaming-mode driver — the collaborator ``bot.py`` delegates to when
``ENGINE_MODE=streaming``.

The one-shot path (``claude_runner.ClaudeRunner``) is untouched; this module is the
*parallel* streaming runner gated behind the S4 flag. It owns everything the live
engine needs that the pure layers (``engine``/``render``) deliberately left to T7,
now **per active project** (P4 / ADR-004):

* **Per-project :class:`~claude_tg.engine.engine.Engine` lifecycle.** One engine per
  *active project* (D2 single-active-run: at most one started engine per chat), started
  lazily on the first turn (or resumed from that project's persisted ``(session_id,
  cwd)`` — the cwd-scoped-resume coupling, ADR-001 / C6). The project runtime
  (``cwd``/``engine``/``started``/``policy``) lives on a :class:`_ProjectRuntime` held
  in a per-project dict on :class:`_ChatState`; the **active** project is resolved from
  the **store** (the source of truth), and a project's ``(session_id, cwd)`` is
  read/written via the registry CRUD.

* **The turn lock (harvested ``ClaudeBusy`` invariant).** A per-chat
  :class:`asyncio.Lock` guards the **turn driver** (``handle_message`` /
  ``handle_cancel``-as-turn) so a chat runs one turn at a time — exactly the
  single-active-run invariant ``ClaudeRunner`` enforces (one active turn per chat —
  hence the live-turn state stays on the chat, not the project). **It deliberately does
  NOT guard :meth:`resolve_callback`**: a button tap / "Other" reply resolves a pending
  decision that the *currently running* turn is awaiting, so it MUST run concurrently
  with the held turn (the turn loop is parked inside ``engine.send`` awaiting the
  operator; the callback handler calls ``engine.resolve`` on the same loop to unblock
  it). Locking the resolve would deadlock the very turn it must unblock.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Optional, Protocol

from .claude_runner import ClaudeResult, ClaudeRunner
from .config import Config
from .engine import (
    AskEvent,
    Engine,
    ErrorEvent,
    Event,
    PermissionDecision,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    ResultEvent,
    SubstrateDecision,
)
from .engine.adapter_sdk import SdkSubstrate
from .paths import PathNotAllowed, resolve_within_roots
from .permissions import PermissionPolicy
from .render import (
    Callback,
    Coalescer,
    RenderAction,
    answers_from_ask,
    ask_question_body,
    ask_question_body_html,
    ask_question_keyboard,
    decode_callback,
    strip_telegram_html,
    yolo_indicator,
)
from .session_store import DEFAULT_PROJECT

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
    *, cwd: str, backstop_seconds: float, permission_policy: PermissionPolicy
) -> Engine:
    """Production factory: an :class:`Engine` over Substrate A for ``cwd``.

    The substrate's ``decision_callback`` is the engine's own ``on_tool_request`` seam
    (the async answer-hold + the P2 permission gate). No bypass / skip-permissions flag
    is set (SB5): the engine consults the injected ``permission_policy`` and is
    fail-closed by default — risky tools are held for approval unless a grant or
    ``/yolo`` allows them. ``permission_policy`` is the project's shared policy (the one
    the session mutates), so ``/yolo``, allow-session grants, and ``/reset``-clear all
    act on a single object.
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
        backstop_seconds=backstop_seconds,
        permission_policy=permission_policy,
    )
    return engine


@dataclass
class _ProjectRuntime:
    """In-memory runtime for ONE project (engine + cwd + its permission policy).

    Per ADR-004: the durable identity (name, cwd, ``session_id``, timestamps) lives in
    the persisted registry; **this** is the transient runtime — created lazily in memory
    when a project is first used, dropped on a restart (so ``/yolo`` and allow-session
    grants never survive a restart, D3/SB5). ``cwd`` is the project's fixed cwd (D4),
    seeded from the registry record (falling back to ``config.workdir`` only if the
    record's cwd is missing). ``policy`` is a FRESH :class:`PermissionPolicy` per project
    (fail-closed: no grants, yolo off) — the SAME object handed to that project's engine
    and mutated by the session (``/yolo`` via :meth:`set_yolo`, dropped by ``policy.clear()``
    on ``/reset``).
    """

    cwd: str
    engine: Optional[Engine] = None
    started: bool = False
    policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    # QF3 (B3/RB3): True from the moment ``engine.resume()`` SUCCEEDS until the first
    # turn on that resumed session completes WITHOUT a resume-failure-shaped error. A
    # stale/aged/torn session can resume "successfully" (connect) and then error on the
    # FIRST ``send`` — this flag tells :meth:`_drive_turn` the current turn is that first,
    # unconfirmed use of a resumed session, so it (and ONLY it) applies the
    # ``_is_resume_failure`` heuristic. A FRESH-started session never sets this, so a fresh
    # session erroring is never mistaken for a resume failure. Reset on the in-memory
    # runtime only (never persisted).
    resumed_unverified: bool = False


@dataclass
class _ChatState:
    """Per-chat LIVE-TURN state (turn lock + ask/plan + free-text marker) + project runtimes.

    There is exactly ONE active turn per chat (the turn lock enforces it), so the
    live-turn fields (status line, pending ask/plan, free-text capture) belong to the
    **chat**. The per-**project** runtime (engine/cwd/policy) lives in :attr:`runtimes`,
    keyed by the stored project name; the *active* project is resolved from the store.
    """

    lock: asyncio.Lock = None  # type: ignore[assignment]
    # Per-project in-memory runtimes, keyed by the project's STORED (as-created) name.
    # Created lazily by _active_runtime; never persisted (D3 — transient bypass).
    runtimes: dict[str, _ProjectRuntime] = field(default_factory=dict)
    # The status-line message id for in-place coalesced edits (created on first edit).
    status_message_id: Optional[int] = None
    # The text currently shown on that status line — used to SKIP an edit when the new
    # status is identical. Editing a Telegram message to the same text raises "message is
    # not modified", whose fallback used to send a fresh message → status-line spam.
    status_text: Optional[str] = None
    # The most recent ask/plan awaiting an answer (so a tap reconstructs the answer).
    pending_ask: Optional[AskEvent] = None
    pending_plan: Optional[PlanEvent] = None
    # Accumulated answers for a MULTI-question AskUserQuestion (question_index ->
    # chosen option label / "Other" free-text). One AskUserQuestion carries ALL its
    # questions under a single tool_use_id, so the native answers map must cover every
    # question; the relay holds the ask open, recording each tap, and resolves ONCE all
    # are answered (a partial map is rejected by the tool). Reset when a new ask arrives
    # and on clear.
    ask_answers: dict[int, str] = field(default_factory=dict)
    # Free-text capture: when set, the NEXT text message is the answer/feedback for
    # this tool_use_id, in this mode ("ask_other" -> QuestionAnswer; "plan_reject" ->
    # PlanVerdict(approve=False)). Question index is kept for an "Other" answer.
    awaiting_text_for: Optional[str] = None
    awaiting_text_mode: Optional[str] = None  # "ask_other" | "plan_reject"
    awaiting_text_question_index: Optional[int] = None

    def __post_init__(self) -> None:
        if self.lock is None:
            self.lock = asyncio.Lock()


class StreamingBusy(Exception):
    """Raised when a chat already has a streaming turn in flight (harvested ClaudeBusy)."""


class StreamingSession:
    """Drives the streaming engine for every chat (the bot delegates here in streaming mode).

    Construct ONE per bot. Methods are called from the Telegram handlers (single asyncio
    loop). The turn lock guards :meth:`handle_message` (one turn per chat at a time —
    the harvested ``ClaudeBusy`` invariant); :meth:`resolve_callback` is intentionally
    lock-free so it can resolve the pending decision the held turn is awaiting.

    **Per active project (P4).** The chat's active project (and its cwd) is resolved
    from the store on each turn; the engine is built/resumed from that project's
    ``(session_id, cwd)`` and persists its ``session_id`` back to that project. At most
    one engine is started per chat at a time (D2): switching the active project stops the
    previously-started one before starting/resuming the new active one.
    """

    def __init__(
        self,
        config: Config,
        *,
        session_store=None,
        engine_factory: Optional[EngineFactory] = None,
        clock: Callable[[], float] = time.monotonic,
        min_edit_interval: Optional[float] = None,
    ) -> None:
        self.config = config
        self.store = session_store
        self._engine_factory = engine_factory or _default_engine_factory
        self._clock = clock
        self._min_edit_interval = min_edit_interval
        self._chats: dict[int, _ChatState] = {}
        # NOTE (P4 / D3): no __init__ harvest of persisted (session_id, cwd). Resume now
        # resolves PER ACTIVE PROJECT from the registry at _ensure_engine time, and the
        # in-memory _ProjectRuntime starts fresh every process (transient bypass reset).

    # -- per-chat state ------------------------------------------------------

    def _chat(self, chat_id: int) -> _ChatState:
        state = self._chats.get(chat_id)
        if state is None:
            state = _ChatState()
            self._chats[chat_id] = state
        return state

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

    async def _ensure_engine(self, chat_id: int) -> tuple[Engine, bool]:
        """Lazily start (or resume) the ACTIVE project's engine. Idempotent per project.

        Returns ``(engine, resume_failed)`` — ``resume_failed`` is True iff a persisted
        ``session_id`` was present but ``resume`` raised and we fell back to a fresh
        ``start`` THIS call (so the caller can post the RB3 operator notice). It is False
        for a fresh start, a clean resume, and the already-started fast path.

        Resolves the chat's active project (auto-creating ``default`` if none — a turn
        always has a project), then:

        * **SB2 cwd re-validation (the authoritative gate, T7).** Re-validate the stored
          cwd against the permitted roots via :func:`resolve_within_roots` BEFORE building
          or resuming the engine. A project whose cwd was in-roots at ``/new`` can later
          drift out (config narrowed, or a path component became an out-of-root symlink);
          if so this raises :class:`~claude_tg.paths.PathNotAllowed` and the engine is
          **never** built/resumed — :meth:`handle_message` catches it and refuses the turn
          fail-closed (SB6/RB1). ``ALLOW_ANY_PATH=true`` no-ops the check (the resolver
          returns the canonical path), as on ``/new``.
        * **Single-active-run (D2).** If a DIFFERENT project's engine is currently
          started for this chat (the operator switched), ``stop()`` it first — at most
          one engine is live per chat.
        * Build the engine for the active project's cwd + **its** ``policy``, then
          ``resume`` the project's persisted ``session_id`` (cwd-scoped — C6) if one
          exists, else ``start`` fresh. A resume failure falls back to a fresh ``start``
          (the dead id is dropped) so the project is never wedged on a stale session —
          harvested from the runner's resume-failure recovery — and is signalled back to
          the caller (RB3) so the operator learns the previous session could not resume.
        """
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
        await self._stop_other_started(chat_id, keep=name)
        if rt.engine is not None and rt.started:
            return rt.engine, False
        engine = rt.engine or self._engine_factory(
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
                log.info(
                    "resume failed for chat %s project %s; starting a fresh session",
                    chat_id,
                    name,
                )
                await engine.start()
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

    async def _stop_other_started(self, chat_id: int, *, keep: str) -> None:
        """Stop any STARTED engine for a project other than ``keep`` (D2 single-active-run).

        The operator can only drive one active project at a time; when they switch, the
        previously-started engine must be stopped so we never hold two live engines for
        one chat. Best-effort: a stop failure is logged, the runtime is marked stopped,
        and we proceed (a wedged old engine must not block the new active turn).
        """
        for other_name, other_rt in self._chat(chat_id).runtimes.items():
            if other_name == keep:
                continue
            if other_rt.started and other_rt.engine is not None:
                try:
                    await other_rt.engine.stop()
                except Exception:
                    log.exception(
                        "error stopping engine for chat %s project %s on switch",
                        chat_id,
                        other_name,
                    )
                other_rt.started = False
                other_rt.engine = None

    def reset(self, chat_id: int) -> None:
        """Reset the ACTIVE project to a fresh conversation (harvested /reset, D3/D7).

        Clears the active project's persisted ``session_id`` (a fresh conversation — the
        project is KEPT in the registry, not deleted), drops its in-memory engine +
        pending state, and wipes its :class:`PermissionPolicy` (drops every allow-session
        grant and turns ``/yolo`` off — D7) so the next session restarts **fail-closed**.
        A running turn (holding the lock) is not force-killed here; ``/cancel`` aborts a
        live turn. With no active project there is nothing to reset (no side effects).
        """
        # Live-turn state is per-chat → always cleared.
        state = self._chats.get(chat_id)
        if state is not None:
            state.status_message_id = None
            state.status_text = None
            self._clear_pending(state)
        # Resolve the active project WITHOUT creating one (reset is not a turn): if there
        # is no active project there is no session to clear.
        name, rt = self._active_runtime(chat_id, create_default=False)
        if rt is not None:
            rt.engine = None
            rt.started = False
            rt.policy.clear()  # D7: drop grants + yolo so the next session is fail-closed.
        if name is not None:
            # Clear the persisted session_id for the active project (keep cwd — D4 — and
            # the project record itself). update() writes the active project's fields.
            self._persist(chat_id, session_id=None)

    def _persist(self, chat_id: int, *, session_id: Optional[str]) -> None:
        """Write ``session_id`` to the chat's ACTIVE project (flat update, cwd untouched).

        The flat :meth:`~JsonSessionStore.update` writes the active project's fields;
        ``cwd=None`` leaves the project's fixed cwd untouched (D4). ``session_id=None``
        clears it (a reset / fresh conversation).
        """
        if self.store is None:
            return
        try:
            self.store.update(chat_id, session_id=session_id, cwd=None)
        except Exception:
            log.exception("failed to persist streaming session state for chat %s", chat_id)

    def is_busy(self, chat_id: int) -> bool:
        """Whether a turn is in flight for ``chat_id`` (the turn lock is held).

        The D2 busy-guard surface: ``/switch`` and ``/new`` (T5/T6) refuse while a turn
        is running so the active project can't change mid-turn. A chat with no state yet
        is never busy.
        """
        state = self._chats.get(chat_id)
        return state is not None and state.lock.locked()

    # -- the turn driver (LOCK-GUARDED: one turn per chat) -------------------

    async def handle_message(
        self,
        chat_id: int,
        text: str,
        *,
        send: SendFn,
        edit: EditFn,
        delete: Optional[DeleteFn] = None,
    ) -> None:
        """Drive ONE operator turn (or capture a free-text answer) for ``chat_id``.

        Free-text capture takes precedence: if the chat is awaiting an "Other" answer /
        plan-reject feedback, this text is routed to ``engine.resolve`` (NOT a new turn)
        and the held turn — still inside ``engine.send`` — continues. Otherwise it opens
        a new turn via ``engine.send`` and renders the event stream against the **active
        project's** engine (auto-creating ``default`` on the first turn — ADR-004 D6).

        Guarded by the per-chat turn lock (the harvested single-active-turn invariant):
        a second concurrent message raises :class:`StreamingBusy` (the bot replies
        "still working"), never two interleaved turns. The lock does NOT cover a
        free-text resolve targeting an *already running* turn — that path must run
        concurrently with the held turn, so it is handled before acquiring the lock.

        **SB2 fail-closed (T7).** If the active project's stored cwd is no longer within
        the permitted roots, :meth:`_ensure_engine` raises
        :class:`~claude_tg.paths.PathNotAllowed`; the engine is never started, this
        replies a clear refusal via ``send`` and RETURNS cleanly (the lock is released —
        no hang, RB1/SB6). **RB3 resume notice.** If a persisted session could not be
        resumed and a fresh one was started instead, a one-line notice is sent via
        ``send`` BEFORE the turn is driven (the turn still completes — never hangs, RB2).
        """
        state = self._chat(chat_id)

        # Free-text capture for a prior "Other"/reject tap routes to resolve(), not a
        # new turn — and must NOT take the turn lock (the awaiting turn holds it). It
        # resolves against the engine of whatever project is active (the same one the
        # held turn is running on).
        if state.awaiting_text_for is not None:
            self._resolve_free_text(state, chat_id, text)
            return

        if state.lock.locked():
            raise StreamingBusy()

        async with state.lock:
            try:
                engine, resume_failed = await self._ensure_engine(chat_id)
            except PathNotAllowed:
                # SB2 (T7): the active project's stored cwd drifted out of the permitted
                # roots (config narrowed, or a path component became an out-of-root
                # symlink). Refuse the turn fail-closed WITHOUT starting the engine; the
                # lock releases on return (no hang).
                await send(
                    text=(
                        f"❌ This project's directory {self.get_cwd(chat_id)} is no longer "
                        "within the permitted roots — use /new <name> <path> to create one "
                        "inside them."
                    ),
                    reply_markup=None,
                    parse_mode=None,
                )
                return
            if resume_failed:
                # RB3: the persisted session could not be resumed; a fresh one was started.
                # Tell the operator BEFORE driving the turn (the turn still completes).
                await send(
                    text="⚠️ Couldn't resume this project's previous session; started a fresh one.",
                    reply_markup=None,
                    parse_mode=None,
                )
            await self._drive_turn(
                state, chat_id, engine, text, send=send, edit=edit, delete=delete
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
        # Capture the project this turn is running on AT TURN START (defense-in-depth, T7
        # review): the busy-guard keeps the active project stable for the turn, but pinning
        # the name/runtime here means the result-persist and any QF3 recovery act on THIS
        # turn's project, not "whatever is active when the turn ends".
        turn_name, turn_rt = self._active_runtime(chat_id, create_default=True)
        # This first turn applies the resume-failure heuristic iff the session was resumed
        # (not freshly started) and is not yet confirmed good.
        check_resume = turn_rt is not None and turn_rt.resumed_unverified
        resume_failure_detected = False

        coalescer = Coalescer(now=self._clock, min_interval=self._min_edit_interval)
        # Status line for THIS turn starts unset; create on first edit_status.
        state.status_message_id = None
        state.status_text = None
        if self._active_policy(chat_id).yolo:
            await send(text=yolo_indicator(), reply_markup=None, parse_mode=None)
        async for event in engine.send(prompt):
            # QF3: on the first turn of a resumed session, flag a resume-failure-shaped
            # error/result. Latch on the first hit (the dead id is the same all turn).
            if check_resume and not resume_failure_detected and _is_resume_failure_event(event):
                resume_failure_detected = True
            # Remember an ask/plan so a tap can reconstruct the native answer.
            if isinstance(event, AskEvent):
                state.pending_ask = event
                state.ask_answers = {}  # fresh accumulator for this ask's questions
                # Render each question as its OWN message + option keyboard so a question's
                # choices sit directly beneath it. A single stacked keyboard for a
                # multi-question ask is an unreadable wall of buttons (the operator can't
                # tell which buttons belong to which question). Flush any buffered status
                # first so the questions appear after it, in order.
                for action in coalescer.flush().actions:
                    await self._perform(state, action, send=send, edit=edit)
                for q_idx in range(len(event.questions)):
                    keyboard = ask_question_keyboard(event, q_idx)
                    # The question text is Claude-authored CommonMark -> render as HTML so
                    # **bold** etc. show and a stray < / & can't break the message; on a
                    # Telegram HTML rejection, resend the plain body (raw fallback — never
                    # a dropped question).
                    try:
                        await send(
                            text=ask_question_body_html(event, q_idx),
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                    except Exception:
                        await send(
                            text=ask_question_body(event, q_idx),
                            reply_markup=keyboard,
                            parse_mode=None,
                        )
                continue
            if isinstance(event, PlanEvent):
                state.pending_plan = event
            elif isinstance(event, ResultEvent):
                # QF3: do NOT re-persist the dead session_id on a resume-failure result —
                # it would just re-arm the same broken resume. The recovery below clears it.
                if not resume_failure_detected:
                    self._persist(chat_id, session_id=event.session_id or engine.session_id)
            for action in coalescer.offer(event).actions:
                await self._perform(state, action, send=send, edit=edit)
        # End of turn: flush any trailing coalesced status line, then DELETE the transient
        # status message ("💭 Claude is thinking…") so a stale thinking-line never lingers
        # after the turn's real content. Best-effort (RB1): a failed delete must never kill
        # the turn — the content is already sent. Optional `delete` so existing callers that
        # don't pass one keep working (the status line just stays, as before).
        for action in coalescer.flush().actions:
            await self._perform(state, action, send=send, edit=edit)
        if delete is not None and state.status_message_id is not None:
            try:
                await delete(message_id=state.status_message_id)
            except Exception:
                log.debug("status-line delete failed at turn end", exc_info=True)
            state.status_message_id = None
            state.status_text = None

        # QF3 (B3/RB3): finalize the resume verification AFTER the stream has fully drained
        # (so we never re-enter the render loop mid-turn). Either recover from a detected
        # resume failure, or confirm the resume good by clearing the flag.
        if check_resume:
            if resume_failure_detected:
                await self._recover_failed_resume(chat_id, turn_name, turn_rt, send=send)
            elif turn_rt is not None:
                # The first resumed turn completed without a resume failure → confirmed good.
                turn_rt.resumed_unverified = False

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
        #    session is gone (the next turn will start fresh, not re-resume it).
        self._persist(chat_id, session_id=None)
        # 2) Drop the in-memory engine so the next turn rebuilds + starts fresh.
        if rt is not None:
            rt.engine = None
            rt.started = False
            rt.resumed_unverified = False
        # 3) Tell the operator (the turn already rendered the underlying error).
        await send(
            text=(
                "⚠️ Couldn't resume this project's previous session (it may be expired) — "
                "cleared it. Send your message again to start fresh."
            ),
            reply_markup=None,
            parse_mode=None,
        )

    def _active_policy(self, chat_id: int) -> PermissionPolicy:
        """The active project's :class:`PermissionPolicy` (auto-create ``default`` if needed).

        Used by :meth:`_drive_turn` for the loud-yolo marker; a turn always has an active
        project (``_ensure_engine`` created one), so this resolves the same runtime.
        """
        _name, rt = self._active_runtime(chat_id, create_default=True)
        return rt.policy if rt is not None else PermissionPolicy()

    async def _perform(
        self,
        state: _ChatState,
        action: RenderAction,
        *,
        send: SendFn,
        edit: EditFn,
    ) -> None:
        """Execute ONE :class:`RenderAction` against Telegram (the deferred I/O)."""
        if action.op == "none" or not action.chunks:
            return
        if action.op == "edit_status":
            await self._edit_status(state, action, send=send, edit=edit)
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
                await send(text=chunk, reply_markup=markup, parse_mode=action.parse_mode)
            except Exception:
                # HTML render fallback (CRITICAL): a chunk Telegram rejects as HTML (a bad
                # entity from a converter edge case) must NEVER drop the message. Resend the
                # ORIGINAL raw markdown for this chunk as plain text — worst case equals
                # today's behavior (raw markdown), never a lost message. Only HTML sends can
                # raise this way; a plain send that fails re-raises (nothing left to try).
                if action.parse_mode is None:
                    raise
                plain = self._plain_fallback(action, i, chunk)
                await send(text=plain, reply_markup=markup, parse_mode=None)

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
        action: RenderAction,
        *,
        send: SendFn,
        edit: EditFn,
    ) -> None:
        """Edit the chat's single coalesced status line in place (create on first use)."""
        body = action.text
        if not body.strip():
            return
        if body == state.status_text:
            # Identical to what's already shown — skip. Editing a Telegram message to the
            # same text raises "message is not modified"; the old fallback then sent a fresh
            # message, which is exactly the status-line spam we must avoid.
            return
        if state.status_message_id is None:
            mid = await send(text=body, reply_markup=None, parse_mode=action.parse_mode)
            state.status_message_id = mid
            state.status_text = body
            return
        try:
            await edit(message_id=state.status_message_id, text=body, parse_mode=action.parse_mode)
            state.status_text = body
        except Exception:
            # A genuine edit failure (message gone / too old) must never kill the turn
            # (RB1/RB2); fall back to a fresh status message. Identical-text edits are
            # already skipped above, so this is a real failure, not a no-op edit.
            log.debug("status edit failed for chat; sending a fresh status line", exc_info=True)
            mid = await send(text=body, reply_markup=None, parse_mode=action.parse_mode)
            state.status_message_id = mid
            state.status_text = body

    # -- the callback resolve path (LOCK-FREE: SB1 enforced at the bot) ------

    def resolve_callback(self, chat_id: int, data: object) -> "CallbackOutcome":
        """Route a decoded inline-keyboard tap to the chat's pending request.

        **The bot has already enforced SB1** (``filters.Chat(allowed)`` + an explicit
        ``_authorized`` recheck) before calling this; an unauthorized chat never reaches
        here. Defense in depth remains: a ``callback_data`` that ``decode_callback``
        rejects (foreign / stale / malformed → ``None``) is IGNORED — no decision is
        resolved, nothing raises (RB1). This is intentionally **lock-free**: it resolves
        the pending decision the currently-running turn is awaiting (the turn loop is
        parked inside ``engine.send``), so it must run concurrently with the held turn.

        The decision resolves against the **active project's** engine — the same engine
        the held turn is running on (one active run per chat, D2).

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
        engine = self._active_engine(chat_id)
        if engine is None:
            # No live engine for the active project → nothing to resolve (stale button).
            return CallbackOutcome(handled=False, note="no active session")

        if decoded.kind == "ask":
            return self._resolve_ask_option(state, engine, decoded)
        if decoded.kind == "other":
            return self._arm_ask_other(state, decoded)
        if decoded.kind == "plan":
            return self._resolve_plan(state, engine, decoded)
        if decoded.kind == "permission":
            return self._resolve_permission(engine, decoded)
        return CallbackOutcome(handled=False, note="ignored")

    def _active_engine(self, chat_id: int) -> Optional[Engine]:
        """The active project's engine, or ``None`` (read-only — no project creation).

        A callback / cancel only makes sense against a live turn, which runs on the
        active project's engine. Resolves WITHOUT creating a default (a tap with no
        active project / no started engine is a stale button → no-op).
        """
        _name, rt = self._active_runtime(chat_id, create_default=False)
        return rt.engine if rt is not None else None

    def _resolve_ask_option(
        self, state: _ChatState, engine: Engine, decoded: Callback
    ) -> "CallbackOutcome":
        ask = state.pending_ask
        if ask is None or ask.tool_use_id != decoded.tool_use_id:
            return CallbackOutcome(handled=False, note="no matching question")
        try:
            q_idx = int(decoded.question_index)  # type: ignore[arg-type]
            # answers_from_ask validates the indices and yields {question: label}; keep
            # the label and record it against the question index (accumulate, below).
            one = answers_from_ask(ask, q_idx, int(decoded.option_index))  # type: ignore[arg-type]
        except (IndexError, KeyError, TypeError):
            # Stale/forged indices for a now-different ask — ignore (RB1).
            return CallbackOutcome(handled=False, note="stale option")
        return self._record_ask_answer(state, engine, ask, q_idx, next(iter(one.values()), ""))

    def _record_ask_answer(
        self, state: _ChatState, engine: Engine, ask: AskEvent, q_idx: int, answer: str
    ) -> "CallbackOutcome":
        """Record ONE question's answer; resolve the whole ask only once EVERY question
        in it has an answer.

        A single ``AskUserQuestion`` carries all its questions under one ``tool_use_id``
        and the native ``answers`` map must cover them all — resolving on the first tap
        (the original bug) sent a partial map the tool rejects, stranding a multi-question
        ask. So we accumulate per-question answers in ``state.ask_answers`` and call
        ``engine.resolve`` only when the count reaches ``len(ask.questions)``. Re-tapping
        a question overwrites its answer (count unchanged), so the operator can change a
        choice before the last one. A SINGLE-question ask resolves on the first tap,
        exactly as before — no regression. Shared by the option-tap and "Other" free-text
        paths.
        """
        state.ask_answers[q_idx] = answer
        total = len(ask.questions)
        answered = len(state.ask_answers)
        if answered < total:
            return CallbackOutcome(
                handled=True,
                note=f"Answered {answered}/{total} — {total - answered} to go",
            )
        # Every question answered → build the full native map and resolve once. Clear the
        # held state first so a no-op resolve can't strand the chat in "answering" mode.
        answers = {
            str(ask.questions[i].get("question", "")): ans
            for i, ans in state.ask_answers.items()
        }
        tuid = ask.tool_use_id
        state.pending_ask = None
        state.ask_answers = {}
        if tuid is None:
            return CallbackOutcome(handled=False, note="no question id")
        resolved = engine.resolve(tuid, QuestionAnswer(answers=answers))
        if resolved:
            return CallbackOutcome(handled=True, note=f"All {total} answered ✓")
        return CallbackOutcome(handled=False, note="already answered")

    def _arm_ask_other(self, state: _ChatState, decoded: Callback) -> "CallbackOutcome":
        ask = state.pending_ask
        if ask is None or ask.tool_use_id != decoded.tool_use_id:
            return CallbackOutcome(handled=False, note="no matching question")
        state.awaiting_text_for = decoded.tool_use_id
        state.awaiting_text_mode = "ask_other"
        state.awaiting_text_question_index = decoded.question_index
        return CallbackOutcome(handled=True, note="Type your answer", expects_text=True)

    def _resolve_plan(
        self, state: _ChatState, engine: Engine, decoded: Callback
    ) -> "CallbackOutcome":
        plan = state.pending_plan
        if plan is None or plan.tool_use_id != decoded.tool_use_id:
            return CallbackOutcome(handled=False, note="no matching plan")
        if decoded.plan_action == "approve":
            resolved = engine.resolve(decoded.tool_use_id, PlanVerdict(approve=True))
            if resolved:
                state.pending_plan = None
                return CallbackOutcome(handled=True, note="Plan approved")
            return CallbackOutcome(handled=False, note="already decided")
        # reject → capture feedback as the next message.
        state.awaiting_text_for = decoded.tool_use_id
        state.awaiting_text_mode = "plan_reject"
        state.awaiting_text_question_index = None
        return CallbackOutcome(handled=True, note="Type your feedback", expects_text=True)

    def _resolve_permission(
        self, engine: Engine, decoded: Callback
    ) -> "CallbackOutcome":
        """Route a permission tap to the held risky-tool request (P2, ADR-003 §2).

        Maps the decoded ``permission_action`` to the engine's three-way
        :class:`~claude_tg.engine.types.PermissionDecision` verdict and resolves the held
        request by ``tool_use_id`` (no held-event lookup needed — the verdict needs no
        indices, unlike ask/plan; the id alone routes it). Lock-free like the ask/plan
        resolve: it unblocks the held turn parked inside ``engine.send``.

        **The allow-session GRANT is recorded by the engine on resolve** (T3
        ``Engine._verdict_for``), NOT here — the session only translates the tap to a
        verdict and routes it. A stale/forged tap that resolves nothing (no live engine,
        already decided, backstopped) returns ``handled=False`` with a benign note.
        """
        verdict = _PERMISSION_VERDICTS.get(decoded.permission_action or "")
        if verdict is None:  # unknown action (defensive; decode already validates)
            return CallbackOutcome(handled=False, note="ignored")
        resolved = engine.resolve(decoded.tool_use_id, PermissionDecision(verdict=verdict))
        if resolved:
            return CallbackOutcome(handled=True, note=_PERMISSION_NOTES[verdict])
        # Nothing pending for this id — already decided / backstopped / cancelled.
        return CallbackOutcome(handled=False, note="no pending request")

    def _resolve_free_text(self, state: _ChatState, chat_id: int, text: str) -> None:
        """Resolve a pending "Other"/reject with the just-typed ``text``; clear the marker.

        Routed from :meth:`handle_message` (free-text capture takes precedence over a new
        turn). An "Other" answer becomes a :class:`QuestionAnswer` keyed by the held
        question text; reject feedback becomes :class:`PlanVerdict` ``approve=False`` with
        the feedback on the deny channel. Resolves against the active project's engine (the
        one the held turn is running on). If the engine has nothing pending for the id
        (already resolved / cancelled), this is a harmless no-op.
        """
        engine = self._active_engine(chat_id)
        tool_use_id = state.awaiting_text_for
        mode = state.awaiting_text_mode
        q_idx = state.awaiting_text_question_index
        # Clear FIRST so a failure can't wedge the chat in capture mode (RB1).
        self._clear_pending_text(state)
        if engine is None or tool_use_id is None:
            return
        if mode == "ask_other":
            ask = state.pending_ask
            if ask is not None and q_idx is not None and 0 <= q_idx < len(ask.questions):
                # Record this question's free-text answer; resolve only once every
                # question in the ask is answered (mirrors the option-tap path so a
                # multi-question ask is not stranded by a single "Other" reply).
                self._record_ask_answer(state, engine, ask, q_idx, text)
            # else: the ask is gone / index stale — harmless no-op (marker already cleared).
        elif mode == "plan_reject":
            engine.resolve(tool_use_id, PlanVerdict(approve=False, feedback=text))
            state.pending_plan = None

    # -- cancel --------------------------------------------------------------

    def handle_cancel(self, chat_id: int) -> int:
        """Abort the chat's in-flight turn cleanly (RB4); clear any free-text capture.

        Delegates to ``engine.cancel()`` on the ACTIVE project's engine (cancels every
        pending interactive request as a clean deny, so a held turn unblocks and the
        session stays usable). Lock-free for the same reason as :meth:`resolve_callback`
        — the turn being cancelled holds the lock. Returns the number of pending requests
        aborted (0 if there is no active engine / it is idle).
        """
        state = self._chats.get(chat_id)
        if state is None:
            return 0
        engine = self._active_engine(chat_id)
        if engine is None:
            return 0
        self._clear_pending(state)
        return engine.cancel()

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

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _clear_pending_text(state: _ChatState) -> None:
        state.awaiting_text_for = None
        state.awaiting_text_mode = None
        state.awaiting_text_question_index = None

    def _clear_pending(self, state: _ChatState) -> None:
        self._clear_pending_text(state)
        state.pending_ask = None
        state.pending_plan = None
        state.ask_answers = {}


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


@dataclass(frozen=True)
class CallbackOutcome:
    """Result of routing one inline-keyboard tap (so the bot can answer the query).

    * ``handled``      — True iff the tap resolved a decision or armed free-text capture.
    * ``note``         — a short toast string for ``answer_callback_query`` (operator
                         feedback; never carries secrets).
    * ``expects_text`` — True iff the bot should prompt the operator to type the next
                         message (an "Other" answer / reject feedback).
    """

    handled: bool
    note: str = ""
    expects_text: bool = False


__all__ = [
    "StreamingSession",
    "StreamingBusy",
    "CallbackOutcome",
    "EngineFactory",
    "SendFn",
    "EditFn",
    "DeleteFn",
]
