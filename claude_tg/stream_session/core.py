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
import html
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Optional

from telegram import InlineKeyboardMarkup, LinkPreviewOptions

from ..audit import (
    KIND_POLICY_EVENT,
    KIND_SESSION_EVENT,
    AuditEvent,
    AuditLog,
    AuditSink,
    ChatBoundSink,
)
from ..config import Config
from ..engine import (
    AskEvent,
    Engine,
    ErrorEvent,
    Event,
    ImageInput,
    PermissionEvent,
    PlanEvent,
    ResultEvent,
)
from ..paths import PathNotAllowed, resolve_within_roots
from ..permissions import PermissionPolicy
from ..render import (
    ChatSendGate,
    Coalescer,
    ProjectStatus,
    RenderAction,
    ask_question_body,
    ask_question_body_html,
    ask_question_keyboard,
    code_path,
    error_is_raw_external,
    notify_attention,
    notify_done,
    notify_error,
    open_project_keyboard,
    permission_keyboard,
    plan_keyboard,
    strip_telegram_html,
    yolo_indicator,
)
from ..scheduler import Schedule
from ..session_mirror import (
    TranscriptTailer,
    run_mirror,
    transcript_path,
)
from ..session_store import (
    _EFFORT_LEVELS,
    DEFAULT_PROJECT,
    DuplicateProject,
    InvalidProjectName,
)
from ..sessions_discovery import DiscoveredSession, SessionDiscovery, discover_sessions
from ..util import _redact_sid, _redact_sid_in_text
from .callbacks import CallbacksMixin
from .concurrency import ConcurrencyMixin
from .knobs import PROJECT_KNOBS, SESSION_KEY_KNOBS
from .runtime import (
    EngineFactory,
    _ChatState,
    _default_engine_factory,
    _footer_only_result,
    _is_resume_failure_event,
    _pending_kind_of,
    _ProjectRuntime,
    _TurnDedup,
)
from .statusline import StatuslineMixin
from .types import (
    _AWAITING_STATUS,
    AttachOutcome,
    DeleteFn,
    EditFn,
    PendingKind,
    PinFn,
    SendFn,
    StreamingBusy,
    UnpinFn,
    WatchOutcome,
    _basename_of,
    _sanitize_attach_name,
)

log = logging.getLogger(__name__)


class StreamingSession(StatuslineMixin, CallbacksMixin, ConcurrencyMixin):
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
        discover: Callable[[], list[DiscoveredSession]] = discover_sessions,
        probe_one: Optional[Callable[[str, Optional[str]], tuple[bool, bool]]] = None,
    ) -> None:
        self.config = config
        self.store = session_store
        # P13 T-AUDIT: the ONE process-wide durable, body-free audit log (append-only JSONL,
        # atomic + 0600, size-bounded). Built from config ONLY when an audit path is resolved
        # (``CLAUDE_STATE_FILE``-derived default, or an explicit ``AUDIT_LOG_FILE``); ``None``
        # disables audit entirely → every engine is built with ``audit_sink=None`` (a no-op),
        # so behavior is IDENTICAL to pre-P13. ``_build_engine`` wraps this in a per-chat
        # ``ChatBoundSink`` (which stamps the chat id the substrate-neutral engine cannot see)
        # and the bot-side records (session/policy events) append to it directly.
        _audit_path = getattr(config, "audit_log_file", None)
        self.audit_log: Optional[AuditLog] = (
            AuditLog(_audit_path, max_bytes=config.audit_log_max_bytes)
            if _audit_path is not None
            else None
        )
        # P11 T2 (attach): the machine-wide session discovery seam (id -> cwd + liveness).
        # Injected so attach tests feed a fixed discovered list + a fixed running/idle verdict
        # with NO real SDK / ps / ~/.claude; defaults to the real :func:`discover_sessions`
        # (already RB1-total). ``attach_session`` looks the target id up here to learn its cwd
        # (for the SB2 check) and its composite liveness (which gates fork-vs-continue).
        self._discover = discover
        # P11 T2 (B2+B3): the SINGLE-SESSION liveness re-probe seam, called at the FIRST WRITE
        # of an adopted (fork_pending) session to re-derive fork-vs-continue from a FRESH probe
        # → ``(running, degraded)``. Injected so the restart + race tests feed a deterministic
        # verdict; defaults to a fresh real :meth:`SessionDiscovery.probe_one` (its own ps /
        # registry / mtime snapshot). _ensure_engine forks on ``running or degraded`` (never
        # co-driving) and continues only on a confident idle.
        self._probe_one: Callable[[str, Optional[str]], tuple[bool, bool]] = (
            probe_one if probe_one is not None else SessionDiscovery().probe_one
        )
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
            # T4 (P9): an INJECTED factory keeps the proven 3-kwarg contract
            # (cwd/backstop_seconds/permission_policy) — every existing test factory uses
            # exactly that signature. So _ensure_engine must NOT pass the per-project `model`
            # to an injected factory (it would TypeError on the unexpected kwarg). Only the
            # DEFAULT bound factory below accepts (and threads) `model`; this flag gates that.
            self._factory_accepts_model = False
        else:

            def _bound_factory(
                *,
                cwd: str,
                backstop_seconds: float,
                permission_policy: PermissionPolicy,
                model: Optional[str] = None,
                permission_mode: str = "default",
                thinking: bool = False,
                effort: Optional[str] = None,
                audit_sink: Optional[AuditSink] = None,
            ) -> Engine:
                return _default_engine_factory(
                    cwd=cwd,
                    backstop_seconds=backstop_seconds,
                    permission_policy=permission_policy,
                    allowed_roots=config.allowed_roots,
                    allow_any_path=config.allow_any_path,
                    send_timeout=float(config.stream_message_timeout_seconds),
                    model=model,
                    permission_mode=permission_mode,
                    thinking=thinking,
                    effort=effort,
                    audit_sink=audit_sink,
                    # P13 T-BASH: bind the live config's Bash policy (default flag) into the
                    # production engine — consulted ADDITIVELY for Bash in on_tool_request. A
                    # bare/injected factory keeps the "off" default, so only the real bot turns
                    # the guardrail on. Closed over `config` (not a per-build param).
                    bash_policy_mode=config.bash_policy_mode,
                    bash_policy_extra_patterns=config.bash_policy_extra_patterns,
                )

            self._engine_factory = _bound_factory
            # T4 (P9): the default factory accepts the per-project `model` kwarg, so
            # _ensure_engine passes the resolved override into it. `model` stays OPTIONAL so a
            # bare 3-kwarg call (the C2/H2 live-factory-wiring tests + the engine-build path
            # with no override) is unchanged.
            self._factory_accepts_model = True
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
        # P11 T3 (live-mirror): the ONE active ``/watch`` per chat — chat_id -> the running
        # read-only tail task. A new ``/watch`` REPLACES the prior (cancel the old task first,
        # tell the operator); ``/unwatch`` cancels it; :meth:`shutdown` cancels ALL of them so
        # no mirror task outlives the bot. Transient in-memory (RB3): a restart loses every
        # watch (read-only — nothing to persist; the operator re-issues ``/watch``).
        self._watches: dict[int, asyncio.Task[None]] = {}
        # The poll cadence + the injected sleep the watch loop uses, so the live bot tails at
        # ~0.25 s and tests drive it deterministically with the SAME injected ``sleep`` the
        # send gate uses (no real time). The interval is small; the per-chat send gate (D8) is
        # the hard rate limiter, so a fast-writing session never bursts past ~1 msg/s/chat.
        self._watch_poll_interval = float(
            getattr(config, "mirror_poll_interval_seconds", 0.25) or 0.25
        )
        # Flood-control budget: the max messages one poll surfaces before the mirror sheds
        # tool-line NOISE (keeping text + result indicators) + emits a coalesced "… N events
        # skipped" marker. The per-chat send gate (D8) is still the hard rate limiter; this
        # bounds the burst handed to it per poll so a fast-writing session degrades gracefully.
        from ..session_mirror import DEFAULT_WATCH_QUEUE_MAX
        self._watch_queue_max = int(
            getattr(config, "mirror_queue_max", DEFAULT_WATCH_QUEUE_MAX) or DEFAULT_WATCH_QUEUE_MAX
        )

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
        self,
        state: _ChatState,
        send: SendFn,
        *,
        verbatim: bool,
        disable_link_preview: bool = False,
        **kwargs,
    ) -> Optional[int]:
        """Send through the per-chat gate: reserve a slot, await the wait, then send (D8).

        Reserves the next send slot from the chat's :class:`~claude_tg.render.ChatSendGate`
        (``verbatim`` prioritizes a final answer / error / prompt / notification over
        coalesced status churn — D8), awaits the gate's computed wait via the injected
        ``self._sleep`` (the gate decides timing; the session does the awaiting — the
        Coalescer pattern), then performs the real ``send``. Returns the sent message id.

        **T6/P9 — no link previews on notifications.** ``disable_link_preview=True`` threads a
        :class:`~telegram.LinkPreviewOptions` ``is_disabled=True`` into the send so a path /
        URL in a background ping does not balloon into a Telegram preview card. It is passed
        as a kwarg the bot's ``send`` closure forwards to ``Bot.send_message``
        (``link_preview_options`` is the PTB 21.x API; the deprecated
        ``disable_web_page_preview`` is avoided). Only the notification sends set it; ordinary
        verbatim/status sends leave Telegram's default preview behavior unchanged.
        """
        wait = self._gate(state).reserve(verbatim=verbatim)
        if wait > 0:
            await self._sleep(wait)
        if disable_link_preview:
            kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
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

    # -- queued counter for notifications + /status (T6/P9) -----------------

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

    @staticmethod
    def _with_open_button(name: str, base: Optional[InlineKeyboardMarkup]):
        """Append an ``[Open <name>]`` switch row to ``base`` (or build it standalone — T6/P9).

        A background needs-attention ping carries a ``📂 Open <name>`` switch button
        (:func:`~claude_tg.render.open_project_keyboard`) so the operator can jump to the
        project from the ping. When the ping also carries the hold's verdict/approve keyboard
        (permission/plan ``base``), the switch button is appended as an EXTRA ROW beneath it
        (one inline keyboard per message — the two can't be separate keyboards). When there is
        no base keyboard (the ask bell line), the switch button stands alone. The switch tap's
        ``callback_data`` (``w|<name>|s``) is a distinct kind, so it never collides with the
        hold rows' ask/plan/permission ``callback_data`` on the same keyboard.
        """
        open_kb = open_project_keyboard(name)
        if base is None:
            return open_kb
        return InlineKeyboardMarkup(
            list(base.inline_keyboard) + list(open_kb.inline_keyboard)
        )

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
        # T6/P9: the ping carries the hold's verdict/approve keyboard PLUS an [Open <name>]
        # switch row (the operator can act on the hold OR jump to the project), a queued
        # counter, and no link preview (a path/URL must not balloon into a card).
        await self._gated_send(
            state, send, verbatim=True,
            text=notify_attention(name, kind, queued_waiting=self._queued_waiting(state)),
            reply_markup=self._with_open_button(name, self._keyboard_for(event)),
            parse_mode=None,
            disable_link_preview=True,
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
        # T6/P9: the bell line carries the [Open <name>] switch button (the per-question
        # keyboards below carry the option taps, so the switch button rides the bell), the
        # queued counter, and no link preview.
        await self._gated_send(
            state, send, verbatim=True,
            text=notify_attention(name, "ask", queued_waiting=self._queued_waiting(state)),
            reply_markup=open_project_keyboard(name),
            parse_mode=None,
            disable_link_preview=True,
        )
        for q_idx in range(len(ask.questions)):
            keyboard = ask_question_keyboard(ask, q_idx)
            try:
                await self._gated_send(
                    state, send, verbatim=True,
                    text=ask_question_body_html(ask, q_idx),
                    reply_markup=keyboard,
                    parse_mode="HTML",
                    disable_link_preview=True,
                )
            except Exception:
                await self._gated_send(
                    state, send, verbatim=True,
                    text=ask_question_body(ask, q_idx),
                    reply_markup=keyboard,
                    parse_mode=None,
                    disable_link_preview=True,
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
            # T6/P9: queued counter + no link preview (the error ping carries no switch button
            # per the T6 scope — that is on the attention + done pings).
            await self._gated_send(
                state, send, verbatim=True,
                text=notify_error(
                    name, event.kind_of_error, queued_waiting=self._queued_waiting(state)
                ),
                reply_markup=None,
                parse_mode=None,
                disable_link_preview=True,
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
                    text=notify_error(
                        name, "turn_error", queued_waiting=self._queued_waiting(state)
                    ),
                    reply_markup=None,
                    parse_mode=None,
                    disable_link_preview=True,
                )
                return
            if not self._should_notify(state, name, "done"):
                return
            # T6/P9: the done ping carries the [Open <name>] switch button (jump to the
            # finished project), a queued counter (a freed slot may unblock waiters), and no
            # link preview.
            await self._gated_send(
                state, send, verbatim=True,
                text=notify_done(name, queued_waiting=self._queued_waiting(state)),
                reply_markup=open_project_keyboard(name),
                parse_mode=None,
                disable_link_preview=True,
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

    def _override_runtime(
        self, chat_id: int, name: str
    ) -> tuple[str, _ProjectRuntime]:
        """Resolve a NAMED project's ``(name, runtime)`` for a pinned turn (P14 T-FIRE).

        Used by :meth:`handle_message` when ``project_override`` is set (a scheduled task
        targets ITS project, pinned at create). Looks the project up in the store
        case-insensitively; on a hit it builds/reuses that project's runtime at its stored cwd
        (the SAME runtime a typed turn for that project would use, so the fire shares the
        engine + policy + busy-guard). **Fail-soft (RB1):** an unknown/blank name, or no store,
        falls back to the chat's ACTIVE project (``create_default=True`` — a fire always has a
        project) so a stale/renamed schedule never silently drops a turn; the scheduler
        validates the name at create, so the fallback is purely defensive. Returns
        ``(name, runtime)`` (both non-None — the active fallback guarantees it).
        """
        store = self.store
        if store is not None and name:
            try:
                record = store.get_project(chat_id, name)
            except Exception:  # a misbehaving store must not drop the fire (RB1)
                record = None
            if record is not None:
                # The record is keyed by name in the store (no ``name`` field on the record
                # itself); the scheduler pins the project name from ``get_active`` at create,
                # so ``name`` is already canonical. Build/reuse that project's runtime at its
                # stored cwd (the SAME runtime + policy a typed turn for it would use).
                cwd = record.get("cwd") if isinstance(record, dict) else None
                return name, self._runtime(chat_id, name, cwd)
        # Unknown/blank/no-store → the active project (a fire always lands on a project).
        active_name, active_rt = self._active_runtime(chat_id, create_default=True)
        assert active_name is not None and active_rt is not None
        return active_name, active_rt

    def _ensure_default_active(self, chat_id: int) -> str:
        """Auto-create (or switch to) a ``default`` project for a chat with no active one.

        ADR-004 D6: a fresh streaming chat with no active project gets a ``default`` at
        ``config.workdir`` (symmetric with the v1→v2 migration) so the operator can just
        send a message. If ``default`` already exists but isn't active, switch to it
        rather than failing on the duplicate. Returns the now-active project name.
        """
        from ..session_store import DuplicateProject

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
            # P13 T-AUDIT: record the bypass toggle (body-free policy_event) — /yolo widens
            # the gate to allow-all, so it is a security-relevant decision worth a durable
            # record. ``decision`` carries the new posture; no body. Best-effort (RB1).
            self.record_audit(
                KIND_POLICY_EVENT,
                chat_id=chat_id,
                summary="yolo_on" if on else "yolo_off",
                decision="on" if on else "off",
                name=_name,
            )

    def get_yolo(self, chat_id: int) -> bool:
        """Whether the chat's ACTIVE project is in ``/yolo`` allow-all mode (T2 /status).

        Read-only (RB1): never creates a project or runtime — a chat with no active project
        (or no runtime yet) reports ``False`` (the fail-closed default; the gate is ON). The
        bypass is per-project + transient (a fresh process / ``/reset`` clears it), so this
        reflects the live posture of whichever project is currently active.
        """
        _name, rt = self._active_runtime(chat_id, create_default=False)
        return bool(rt.policy.yolo) if rt is not None else False

    def get_project_yolo(self, chat_id: int, name: str) -> bool:
        """Whether the NAMED project is in ``/yolo`` allow-all mode (T2 /status, P9 fix).

        Read-only (RB1): never creates a project or runtime. Mirrors :meth:`project_status` —
        a project with **no in-memory runtime** (never run this process, e.g. just after a
        restart) reports ``False`` (the fail-closed default; ``/yolo`` is transient + per
        process). The name is matched case-insensitively against the stored runtime key
        (like the store), so ``/status`` can mark EACH project's yolo posture independently —
        a wide-open BACKGROUND project is no longer hidden behind the active project's gate.
        """
        state = self._chats.get(chat_id)
        if state is None:
            return False
        key = self._resolve_runtime_key(state.runtimes, name)
        if key is None:
            return False
        return bool(state.runtimes[key].policy.yolo)

    def set_model(self, chat_id: int, model: Optional[str]) -> Optional[str]:
        """Set (or clear) the ACTIVE project's per-project model override (T4 / P9).

        ``/fast`` → the fast id, ``/deep`` → the deep id, ``/auto`` → ``None`` (clear the
        override back to ``CLAUDE_MODEL`` / the SDK default). Persisted on the active project
        via the store (atomic + ``0600``, RB6) so it survives a restart and a store reload;
        with no store it is a no-op (a single implicit project, no persistence) — returns the
        requested ``model`` regardless so the bot can confirm. Auto-creates ``default`` if
        there is no active project (consistent with starting a turn / ``set_yolo``).

        **Applies on the NEXT fresh session, never mid-turn.** The model is a session-creation
        param (baked into ``ClaudeAgentOptions`` when the engine's client is built). A project
        with a live engine/session keeps running its current model until that session ends; the
        new model takes effect when the next fresh session is built (a ``/reset`` or a
        dead-resume rebuild). We deliberately do NOT hot-swap a live session. Returns the
        normalized override that was stored (``None`` for ``/auto``).
        """
        normalized = model.strip() if isinstance(model, str) and model.strip() else None
        # Resolve (and if needed auto-create) the active project so /fast before any turn works.
        name, _rt = self._active_runtime(chat_id, create_default=True)
        if self.store is not None and name is not None:
            try:
                self.store.set_model(chat_id, name, normalized)
            except Exception:
                # RB1: never crash the command over a persist failure (e.g. the project was
                # /rm'd in a race). The override simply isn't recorded; the next turn uses the
                # default. Mirrors _persist's swallow-and-log discipline.
                log.exception("failed to persist model override for chat %s", chat_id)
        return normalized

    def set_effort(self, chat_id: int, level: Optional[str]) -> Optional[str]:
        """Set (or clear) the ACTIVE project's per-project reasoning-EFFORT override (T-EFFORT).

        ``/effort <low|medium|high|xhigh|max>`` stores the level; a bare ``/effort`` (or
        ``/effort default``) clears it (``None``) back to the SDK default (``high``). Exactly
        parallel to :meth:`set_model`: persisted on the active project via the store (atomic +
        ``0600``, RB6) so it survives a restart and a store reload; with no store it is a no-op
        (a single implicit project, no persistence) — returns the NORMALIZED level regardless so
        the bot can confirm. Auto-creates ``default`` if there is no active project (consistent
        with ``set_model`` / ``set_yolo`` / ``arm_plan``).

        The store VALIDATES the level against ``{low, medium, high, xhigh, max}`` and normalizes
        anything else to ``None`` (a cleared override) — but the bot's ``cmd_effort`` rejects a
        bad level with a clean error *before* calling this, so a stored garbage level can't arise
        from the command path; this method simply returns what was persisted. **Applies on the
        NEXT fresh session, never mid-turn** (effort is a session-creation param baked into
        ``ClaudeAgentOptions``; a turn in flight keeps its current effort, and the warm fast-path
        rebuilds on the next turn because ``engine_effort`` no longer matches). Returns the
        normalized override that was stored (``None`` for a clear).
        """
        normalized = (
            level.strip().lower()
            if isinstance(level, str) and level.strip().lower() in _EFFORT_LEVELS
            else None
        )
        # Resolve (and if needed auto-create) the active project so /effort before any turn works.
        name, _rt = self._active_runtime(chat_id, create_default=True)
        if self.store is not None and name is not None:
            try:
                self.store.set_effort(chat_id, name, normalized)
            except Exception:
                # RB1: never crash the command over a persist failure (e.g. the project was
                # /rm'd in a race). The override simply isn't recorded; the next turn uses the
                # SDK default. Mirrors set_model / _persist's swallow-and-log discipline.
                log.exception("failed to persist effort override for chat %s", chat_id)
        return normalized

    def arm_plan(self, chat_id: int) -> None:
        """Arm the ACTIVE project's NEXT turn as a plan turn (``/plan``; P12 T-PLAN-2).

        Sets the per-project, ONE-SHOT, in-memory ``plan_next`` marker on the active project's
        runtime: the next turn for that project is driven in ``permission_mode="plan"``, so
        Claude reasons + proposes a plan and surfaces ``ExitPlanMode`` through the SHIPPED P6
        hold/keyboard (Approve → execution resumes; Reject + feedback → revise). The marker is
        consumed (read + cleared) by :meth:`_ensure_engine` on that one turn, so the turn AFTER
        is a normal (``"default"``) session again — the operator opts in deliberately, per turn.

        Auto-creates ``default`` if there is no active project (consistent with ``set_yolo`` /
        ``set_model`` / starting a turn — a ``/plan`` before any turn arms the implicit default
        project). **RB3 (transient):** the marker is in-memory only and NEVER persisted — a
        process restart drops it (the supervision posture never silently survives a restart).
        **ADR-001 C4:** arming plan mode greenlights NOTHING about tools — every risky tool the
        approved plan later runs still hits the permission gate independently (unchanged here).
        SB1 is enforced by the bot's ``_ok`` recheck before this is reached.
        """
        _name, rt = self._active_runtime(chat_id, create_default=True)
        if rt is not None:
            rt.plan_next = True

    def set_thinking(self, chat_id: int, on: bool) -> bool:
        """Toggle the ACTIVE project's live-thinking flag (``/thinking on|off``; P12 T-THINK).

        Sets the STICKY per-project ``thinking`` marker on the active project's runtime (default
        OFF). When on, that project's NEXT fresh session streams Claude's readable reasoning as
        the capped ``🧠`` status line (built with ``thinking={"type":"adaptive",
        "display":"summarized"}`` + ``include_partial_messages=True`` — :meth:`_ensure_engine`);
        when off, neither option is set and there is no partial-message wire traffic (the pre-P12
        behavior). **Applies on the NEXT fresh session, never mid-turn** (thinking is a
        session-creation knob, mirroring ``/fast``·``/deep`` — a turn in flight keeps streaming
        as it was built; the warm fast-path rebuilds on the next turn because ``engine_thinking``
        no longer matches). Returns the new flag so the bot can confirm the state.

        Auto-creates ``default`` if there is no active project (consistent with ``set_yolo`` /
        ``arm_plan``). **RB3 (transient):** in-memory only, NEVER persisted — a restart drops it
        back to OFF (the supervision posture never silently survives a restart). SB1 is enforced
        by the bot's ``_ok`` recheck before this is reached.
        """
        _name, rt = self._active_runtime(chat_id, create_default=True)
        if rt is not None:
            rt.thinking = bool(on)
            return rt.thinking
        return False

    def get_model(self, chat_id: int) -> Optional[str]:
        """The ACTIVE project's effective model id (override, else the configured default).

        Read-only (RB1): never creates a project/runtime. Returns the per-project override if
        one is set (``/fast``/``/deep``), else the configured ``CLAUDE_MODEL`` (``config.model``),
        else ``None`` (the SDK default). Used by ``/status`` to show the active model. The
        per-project override is stored on the active project; with no store / no active project
        it falls back to the configured default.
        """
        if self.store is not None:
            active = self.store.get_active(chat_id)
            if active is not None:
                override = self.store.get_model(chat_id, active)
                if override:
                    return override
        return self.config.model

    def _resolve_knob(
        self, chat_id: int, name: str, knob_name: str
    ) -> Optional[object]:
        """The value to bake into ``name``'s next session for ONE persisted knob (override → default).

        The single generalized resolver the parallel ``_resolve_project_*`` methods collapse
        into (driven by :data:`PROJECT_KNOBS`). For the named PERSISTED knob (``"model"`` /
        ``"effort"``): read the per-project override from the store (the project record's
        ``knob.field``), normalize it with the knob's normalizer (the SAME rule the typed
        ``store.get_model``/``get_effort`` apply on read — so this is byte-for-byte that read),
        and if an override is set return it; else fall back to ``knob.config_default(config)``
        (model → ``config.model``; effort → ``None`` — the no-global-default asymmetry).

        Read-only + fail-safe (RB1): a missing store / project / field (or a garbage stored
        value) reads as no override → the configured default. ``_ensure_engine`` calls this for
        the project it is building, via the named ``_resolve_project_*`` wrappers below.
        """
        knob = PROJECT_KNOBS[knob_name]
        if self.store is not None:
            try:
                record = self.store.get_project(chat_id, name)
                raw = record.get(knob.field) if isinstance(record, dict) else None
                override = knob.normalize(raw)
            except Exception:  # RB1: a bad/odd record never wedges the build
                override = None
            if override:
                return override
        return knob.config_default(self.config)

    def _resolve_project_model(self, chat_id: int, name: str) -> Optional[str]:
        """The model id to bake into ``name``'s next session (override → CLAUDE_MODEL → None).

        T4 (P9): the per-project override (``/fast``/``/deep``) wins; absent that, the
        configured ``CLAUDE_MODEL`` (``config.model``); absent that, ``None`` (omit ``model``
        → the SDK default). Read-only + fail-safe (RB1): a missing store / project / field
        reads as no override. Called by :meth:`_ensure_engine` for the project it is building.

        Thin wrapper over :meth:`_resolve_knob` (the ``"model"`` row of :data:`PROJECT_KNOBS`)
        — the name + 2-arg signature are kept because the bot/tests call it directly.
        """
        return self._resolve_knob(chat_id, name, "model")  # type: ignore[return-value]

    def _resolve_project_effort(self, chat_id: int, name: str) -> Optional[str]:
        """The reasoning-EFFORT level to bake into ``name``'s next session (override → ``None``).

        T-EFFORT (STATUSLINE): the per-project override (``/effort low…max``) if set, else
        ``None`` (omit ``effort`` → the SDK's own default, ``high``). UNLIKE
        :meth:`_resolve_project_model` there is NO ``CLAUDE_*`` global default for effort — when
        unset we return ``None`` so the kwarg is omitted entirely (encoded as the ``"effort"``
        row's ``config_default = lambda c: None``). Read-only + fail-safe (RB1): a missing store
        / project / field (or a garbage stored level) reads as no override. Called by
        :meth:`_ensure_engine` for the project it is building.

        Thin wrapper over :meth:`_resolve_knob` (the ``"effort"`` row of :data:`PROJECT_KNOBS`)
        — the name + 2-arg signature are kept because the tests call it directly.
        """
        return self._resolve_knob(chat_id, name, "effort")  # type: ignore[return-value]

    # -- engine lifecycle ----------------------------------------------------

    async def _ensure_engine(
        self,
        chat_id: int,
        *,
        target: Optional[tuple[str, _ProjectRuntime]] = None,
        plan_turn: bool = False,
    ) -> tuple[Engine, bool]:
        """Lazily start (or resume) a project's engine. Idempotent per project.

        Returns ``(engine, resume_failed)`` — ``resume_failed`` is True iff a persisted
        ``session_id`` was present but ``resume`` raised and we fell back to a fresh
        ``start`` THIS call (so the caller can post the RB3 operator notice). It is False
        for a fresh start, a clean resume, and the already-started fast path.

        **P12 T-PLAN-2 (/plan).** ``plan_turn`` is passed in by the caller, which ALREADY
        consumed (read + cleared) the project's one-shot ``plan_next`` marker BEFORE this call
        — so this method NEVER reads or clears the marker itself (round-2 QA fix). When True
        this turn's session is built in ``permission_mode="plan"`` (mechanism (a) — a FRESH
        plan-mode session, mirroring how ``model`` is baked at session creation); when False it
        is the unchanged ``"default"``. Because the caller consumes the marker before BOTH the
        pre-engine abort guards AND the SB2 ``resolve_within_roots`` check below, the one-shot
        contract holds on every exit path — a plan turn that aborts or is refused fail-closed
        still consumed the marker, so the NEXT turn is normal (never a surprise plan prompt).

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
        # P12 T-PLAN-2 (/plan), round-2 QA fix: the one-shot plan marker was ALREADY consumed
        # (read + cleared) by the caller (``handle_message``) at the earliest commit point —
        # BEFORE the pre-engine abort guards AND before the SB2 ``resolve_within_roots`` check
        # above — so this method just RECEIVES the verdict as ``plan_turn`` and never touches
        # ``rt.plan_next`` itself. (Consuming inside here was the bug: the SB2 raise above and
        # the two pre-engine ``return``s in the caller skipped it, leaving the marker armed →
        # a later unrelated message got a surprise plan prompt.) When armed, this turn's session
        # must run in ``permission_mode="plan"`` (mechanism (a) — baked at session creation), so
        # the ``"default"``-mode warm engine below is NOT reused: we force a FRESH plan-mode
        # session for exactly this turn (which RESUMES the persisted id, so the conversation
        # continues). A normal turn keeps ``"default"`` and the warm fast-path, byte-for-byte.
        plan_mode = plan_turn
        permission_mode = "plan" if plan_mode else "default"
        # P12 T-THINK: this project's STICKY live-reasoning flag (set by /thinking; default
        # OFF). UNLIKE the one-shot plan marker it is NOT consumed/cleared — it stays on until
        # /thinking off. The session is built with the live-reasoning options when on; the warm
        # fast-path below reuses the engine only when its built-with flag matches, so a toggle
        # rebuilds the session on the next turn (thinking is a session-creation knob).
        thinking = rt.thinking
        # T-EFFORT (STATUSLINE): resolve THIS project's reasoning-EFFORT override (/effort
        # low…max), else None (SDK default — no CLAUDE_* global). Like ``model`` it is fixed for
        # the life of the fresh session built below (a session-creation param); the warm
        # fast-path reuses the engine only when its built-with level matches, so an /effort
        # change rebuilds on the next turn (never hot-swapped). Resolved here (not on the
        # runtime) so the persisted override is read fresh each build (it survives a restart).
        effort = self._resolve_project_effort(chat_id, name)
        # The freshly-resolved value of each SESSION-IDENTITY knob, keyed by knob name — the
        # input to the warm-engine match-key loop below. ``thinking`` is the runtime's sticky
        # flag (read above); ``effort`` is the per-project override (resolved above). ``model``
        # is deliberately ABSENT (it is not a session-identity knob — a model change does not
        # rebuild the warm engine; it is re-resolved at each fresh build instead). Driving the
        # match-key off this map + ``SESSION_KEY_KNOBS`` replaces the three hand-written
        # ``engine_* == …`` comparisons with one loop — byte-for-byte the same decision.
        resolved_knobs: dict[str, object] = {"thinking": thinking, "effort": effort}
        # P5 / ADR-005 D1 (T5): no cross-project stop here. A different project's started
        # engine is left running so N runs can be concurrent (T5 removed P4's
        # _stop_other_started). Only the SAME project's stale/non-started engine is handled
        # by the QF5 discard below.
        #
        # P12 T-PLAN: the warm fast-path is taken ONLY when the warm engine's permission mode
        # already MATCHES the turn's requested mode. This rebuilds the session on a mode change
        # in EITHER direction — a session built in ``"default"`` can't be hot-switched to plan
        # mode (mechanism (a) is session-creation), AND a plan-mode session built for the one
        # ``/plan`` turn must NOT linger onto the next (default) turn (the marker is one-shot).
        # When the requested mode differs, the started engine is torn down + rebuilt fresh in
        # the requested mode just below (the same discard the QF5 stale-engine path uses, which
        # RESUMES the persisted id so the conversation continues). A back-to-back normal turn
        # keeps the warm fast-path byte-for-byte: both modes are ``"default"`` → matched → reuse.
        #
        # P12 T-THINK: the warm fast-path ALSO requires the built-with ``thinking`` flag to match
        # the turn's requested flag — for the same reason (thinking is a session-creation knob,
        # not hot-switchable). So /thinking on→off (or off→on) rebuilds the session on the next
        # turn; a back-to-back same-thinking turn still reuses the warm engine byte-for-byte
        # (both False pre-P12 → matched → reuse, so a thinking-OFF project is unchanged).
        #
        # T-EFFORT (STATUSLINE): and the built-with reasoning-EFFORT level must match too — for
        # the SAME reason (effort is a session-creation knob baked into ClaudeAgentOptions, not
        # hot-switchable). So changing /effort (e.g. high→max, or set→cleared) rebuilds the
        # session on the next turn; a back-to-back same-effort turn still reuses the warm engine
        # byte-for-byte (None == None for a no-override project → matched → reuse, so the
        # default-turn path is unchanged).
        if (
            rt.engine is not None
            and rt.started
            and rt.engine_permission_mode == permission_mode
            and all(
                getattr(rt, knob.runtime_marker) == resolved_knobs[knob.name]
                for knob in SESSION_KEY_KNOBS
            )
        ):
            return rt.engine, False
        # Past the warm fast-path: rt is either fresh (engine None), holds a NON-started
        # engine — a prior start()/resume() that raised AFTER the adapter allocated its
        # client (so the engine is non-None but unusable) — OR holds a STARTED engine we are
        # rebuilding because this is an armed plan turn (``plan_mode``; the warm fast-path was
        # skipped above so the fresh session can be built in plan mode). Never REUSE such an
        # engine: a start()/resume() on it hits the adapter's "already started" guard → the
        # turn wedges (the same coupling the QF4 resume-raises path recovers from). So if ANY
        # engine is present here, best-effort stop() it (free its client) and build a FRESH one
        # — it is always discarded + replaced, never reused. This is the SAME-project QF5
        # hardening, kept under concurrency, now also the plan-mode rebuild path. The plan
        # rebuild RESUMES the persisted (session_id, cwd) below, so the conversation continues
        # — only the permission mode of the fresh session differs.
        if rt.engine is not None:
            try:
                await rt.engine.stop()
            except Exception:
                log.debug(
                    "stop of replaced engine raised for chat %s project %s "
                    "(ignored — building fresh%s)",
                    chat_id,
                    name,
                    " in plan mode" if plan_mode else "",
                    exc_info=True,
                )
            # A started engine being torn down for a plan rebuild leaves ``started`` True; drop
            # it so a downstream failure can't mistake the discarded engine for a live one.
            rt.started = False
        # T4 (P9): resolve THIS project's model (override → CLAUDE_MODEL → SDK default) and
        # bake it into the engine being built. Passed only to the DEFAULT factory (an injected
        # test factory keeps the 3-kwarg contract — see _factory_accepts_model). The model is
        # fixed for the life of THIS fresh session (session-creation param); a later
        # /fast·/deep·/auto takes effect on the next session this project builds.
        model = self._resolve_project_model(chat_id, name)
        # P12 T-PLAN: build the session in the resolved permission mode (``"plan"`` for the one
        # armed turn, else the unchanged ``"default"``). Threaded to the DEFAULT factory only,
        # alongside ``model`` (an injected test factory keeps its 3-kwarg contract). Record the
        # mode on the runtime so the warm fast-path reuses this engine only for a same-mode turn
        # and rebuilds back to ``"default"`` after the one-shot plan turn (the mismatch path).
        engine = self._build_engine(
            chat_id, rt.cwd, rt.policy, model,
            permission_mode=permission_mode, thinking=thinking, effort=effort,
        )
        rt.engine = engine
        rt.engine_permission_mode = permission_mode
        rt.engine_thinking = thinking  # P12 T-THINK: track the built-with thinking flag
        rt.engine_effort = effort  # T-EFFORT: track the built-with reasoning-effort level
        resume_id = self._resume_id(chat_id, name)
        # ⭐ P11 T2 (B2+B3) — the BINDING fork-vs-continue decision, made HERE at the first
        # write from a FRESH liveness re-probe (not frozen at attach time). When this project
        # is an ADOPTED-not-yet-resumed session (the PERSISTED ``fork_pending`` marker — which
        # survives a restart, unlike the in-memory runtime), RE-PROBE the base id's CURRENT
        # liveness and FORK on live-OR-uncertain; CONTINUE only on a confident idle. This
        # closes:
        #   * B2 (restart before first turn): the in-memory intent is gone but the persisted
        #     marker triggers a re-probe, so a restart re-decides instead of co-driving.
        #   * B3 (attach→first-write race): an idle-at-attach session that has since gone live
        #     is caught by the re-probe NOW, at the moment of the write — never co-driven.
        # The fork forks the FIRST resume only; ``fork_pending`` is cleared (persisted) after
        # the first successful turn (in _drive_turn) and on the resume-failure rebuild below.
        fork = False
        fork_pending = bool(resume_id) and self._fork_pending(chat_id, name)
        if fork_pending:
            assert resume_id is not None  # guarded by ``bool(resume_id) and`` above
            running, degraded = self._reprobe_liveness(resume_id, rt.cwd)
            fork = running or degraded  # fork on doubt — never co-drive a possibly-live session
            log.info(
                "attach first-write re-probe for chat %s project %s: running=%s degraded=%s "
                "→ fork=%s",
                chat_id, name, running, degraded, fork,
            )
        resume_failed = False
        if resume_id:
            try:
                # Pass ``fork`` ONLY when forking so the IDLE-continue path (and every
                # pre-P11 resume) calls ``engine.resume(id)`` byte-for-byte as before — an
                # injected engine fake that omits the kwarg is unaffected; only the new
                # attach-fork path exercises the grown signature.
                if fork:
                    await engine.resume(resume_id, fork=True)
                else:
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
                #     engine, replacing the failed one. T4: same per-project model as the
                #     first build (resolved once above) — the fresh fallback session honors
                #     the project's /fast·/deep override too. P12 T-PLAN: and the SAME
                #     permission mode — a plan turn whose resume failed still starts fresh in
                #     plan mode (the marker was already consumed above; this re-uses the value).
                #     P12 T-THINK: and the SAME thinking flag — a thinking-ON project whose
                #     resume failed still starts fresh with live reasoning on (sticky flag).
                #     T-EFFORT: and the SAME reasoning-effort level (resolved once above) — a
                #     project with an /effort override starts fresh at that effort too.
                engine = self._build_engine(
                    chat_id, rt.cwd, rt.policy, model,
                    permission_mode=permission_mode, thinking=thinking, effort=effort,
                )
                rt.engine = engine
                rt.engine_permission_mode = permission_mode  # P12 T-PLAN: track the fresh mode
                rt.engine_thinking = thinking  # P12 T-THINK: track the fresh thinking flag
                rt.engine_effort = effort  # T-EFFORT: track the fresh reasoning-effort level
                # (d) Start the FRESH engine — a clean fresh session (the dead id is gone).
                await engine.start()
                # (e) Signal the caller so handle_message posts the T7 "couldn't resume,
                #     started fresh" notice. The session is fresh (start, not resume), so
                #     it is NOT resumed_unverified — a fresh-session error is an ordinary
                #     turn error, never mistaken for a resume failure.
                resume_failed = True
                # P11 T2: the (possibly forked) resume failed and we recovered onto a FRESH
                # session whose dead id was cleared — there is no longer a base id to fork
                # from, so clear BOTH the in-memory hint AND the PERSISTED fork_pending marker
                # (the next resume of THIS project is a plain continue of the fresh id). This
                # matches the existing attach_fork-clearing on the resume-failure rebuild path.
                rt.attach_fork = False
                self._clear_fork_pending(chat_id, name)
            else:
                # Resume CONNECTED. It is not yet CONFIRMED good — a stale/aged/torn
                # session can connect and then error on the first turn (B3). Mark the
                # runtime so _drive_turn applies the resume-failure heuristic to this
                # first turn only (cleared once a turn completes clean — QF3/RB3).
                rt.resumed_unverified = True
                # P11 T2: the fork-or-continue resume CONNECTED — clear the in-memory hint so a
                # subsequent resume WITHIN THIS PROCESS continues. But DO NOT clear the PERSISTED
                # ``fork_pending`` yet: the turn has not completed, and a restart between connect
                # and a successful turn must STILL re-probe (a forked id is captured + persisted
                # only when the first turn's result lands — until then the persisted id is still
                # the base id). _drive_turn clears the persisted marker after the first
                # SUCCESSFUL turn (by which point the forked/continued id is persisted + ours).
                rt.attach_fork = False
        else:
            await engine.start()
        rt.started = True
        return engine, resume_failed

    def _audit_sink_for(self, chat_id: int) -> Optional[AuditSink]:
        """Build a per-chat :class:`~claude_tg.audit.ChatBoundSink`, or ``None`` (P13 T-AUDIT).

        Returns ``None`` when no process audit log is configured (audit disabled) — the
        engine is then built with ``audit_sink=None`` (a no-op), so behavior is unchanged.
        Otherwise wraps the ONE process :class:`~claude_tg.audit.AuditLog` in a sink that
        STAMPS ``chat_id`` on every record the substrate-neutral engine emits (the engine
        does not know the chat id). Best-effort downstream (the sink/log never raise).
        """
        if self.audit_log is None:
            return None
        return ChatBoundSink(self.audit_log, chat_id)

    def _session_tag(self, chat_id: int, name: Optional[str]) -> Optional[str]:
        """The named project's REDACTED session tag for an audit record, or ``None`` (P13).

        Read-only (never raises, RB1): looks up the project's persisted ``session_id`` and
        runs it through :func:`~claude_tg.util._redact_sid` so the audit record carries a
        correlatable tag (``sid:ab12cd``) — NEVER the raw resumable id (SB3/H1). A missing
        store / project / id yields the fixed ``sid:none`` sentinel.
        """
        from ..util import _redact_sid

        sid: Optional[str] = None
        if self.store is not None and name is not None:
            try:
                record = self.store.get_project(chat_id, name)
                sid = (record or {}).get("session_id")
            except Exception:  # pragma: no cover - store reads are RB1 already
                sid = None
        return _redact_sid(sid)

    def record_audit(
        self,
        kind: str,
        *,
        chat_id: int,
        summary: Optional[str] = None,
        decision: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        """Append a BODY-FREE session/policy audit event to the process log (P13 T-AUDIT).

        The bot-side counterpart of the engine's tool/plan records — used for events the
        substrate-neutral engine never sees (``/attach`` · ``/watch`` · ``/unwatch`` ·
        ``/reset`` · ``/switch`` → ``session_event``; ``/yolo`` · ``/unyolo`` →
        ``policy_event``). ``summary`` is a short fixed ACTION token (e.g. ``"attach"`` /
        ``"yolo_on"``) — NEVER a body — and ``name`` (a project name, used only to resolve
        the redacted session tag) is the sole free value, which is SB4-validated. No-op when
        no audit log is configured; best-effort otherwise — :meth:`AuditLog.append` never
        raises (RB1), so a bot-side record can never break a command.
        """
        if self.audit_log is None:
            return
        from ..util import _now_iso

        self.audit_log.append(
            AuditEvent(
                ts=_now_iso(),
                kind=kind,
                summary=summary,
                decision=decision,
                chat_id=chat_id,
                session_tag=self._session_tag(chat_id, name),
            )
        )

    def _build_engine(
        self,
        chat_id: int,
        cwd: str,
        policy: PermissionPolicy,
        model: Optional[str],
        *,
        permission_mode: str = "default",
        thinking: bool = False,
        effort: Optional[str] = None,
    ) -> Engine:
        """Call the engine factory, passing the T4 per-project ``model`` only when supported.

        The DEFAULT bound factory accepts an optional ``model`` kwarg (threaded into
        ``ClaudeAgentOptions``); an INJECTED test factory keeps the proven 3-kwarg contract
        (``cwd``/``backstop_seconds``/``permission_policy``) and must NOT receive ``model``
        (it would ``TypeError`` on the unexpected kwarg). ``_factory_accepts_model`` (set in
        ``__init__``) gates this so every existing test factory keeps working unchanged.

        **P12 T-PLAN-1:** ``permission_mode`` rides the SAME default-factory-only gate as
        ``model`` — mechanism (a) bakes ``"plan"`` into the FRESH session built for the one
        armed ``/plan`` turn, ``"default"`` otherwise (a normal turn is byte-for-byte
        unchanged: ``"default"`` was always passed). An injected test factory keeps its 3-kwarg
        contract and never receives it, so every existing test factory is unaffected.

        **P12 T-THINK:** ``thinking`` rides the SAME default-factory-only gate — True bakes the
        live-reasoning options (partials + ``display="summarized"``) into the FRESH session for
        a thinking-ON project, ``False`` (the default, always passed pre-P12) keeps a normal
        turn byte-for-byte unchanged. An injected test factory never receives it (3-kwarg
        contract), so every existing test factory is unaffected.

        **P13 T-AUDIT:** the per-chat ``audit_sink`` rides the SAME default-factory-only gate —
        the engine records every gate decision through it (body-free). ``None`` (no audit log
        configured) makes the engine's hook a no-op. An injected test factory keeps its 3-kwarg
        contract and never receives it, so every existing test factory is unaffected (and the
        no-op default keeps the 1288 floor).

        **T-EFFORT (STATUSLINE):** ``effort`` rides the SAME default-factory-only gate — a level
        bakes ``ClaudeAgentOptions(effort=…)`` into the FRESH session for a project with an
        ``/effort`` override, ``None`` (the default) omits it so a no-override turn is
        byte-for-byte unchanged (the SDK default effort applies). An injected test factory keeps
        its 3-kwarg contract and never receives it, so every existing test factory is unaffected.
        """
        if self._factory_accepts_model:
            return self._engine_factory(
                cwd=cwd,
                backstop_seconds=float(self.config.answer_backstop_seconds),
                permission_policy=policy,
                model=model,  # type: ignore[call-arg]  # default factory accepts model (T4)
                permission_mode=permission_mode,  # default factory accepts it too (P12 T-PLAN-1)
                thinking=thinking,  # default factory accepts it too (P12 T-THINK)
                effort=effort,  # default factory accepts it too (T-EFFORT)
                audit_sink=self._audit_sink_for(chat_id),  # default factory accepts it too (P13)
            )
        return self._engine_factory(
            cwd=cwd,
            backstop_seconds=float(self.config.answer_backstop_seconds),
            permission_policy=policy,
        )

    def _resume_id(self, chat_id: int, name: str) -> Optional[str]:
        """The active project's persisted ``session_id`` to resume from, if any."""
        if self.store is None:
            return None
        record = self.store.get_project(chat_id, name)
        session_id = (record or {}).get("session_id")
        return session_id if isinstance(session_id, str) and session_id else None

    def _fork_pending(self, chat_id: int, name: str) -> bool:
        """Whether ``name`` is an adopted-not-yet-resumed session (persisted marker, B2+B3).

        Read-only (RB1): no store, or a missing/false marker → ``False`` (an ordinary
        continue). ``_ensure_engine`` reads this to decide whether to RE-PROBE the base id's
        liveness at the first write (forking on doubt). Survives a restart (it is persisted),
        so a restart before the first turn re-probes instead of co-driving a stale continue.
        """
        if self.store is None:
            return False
        try:
            return bool(self.store.get_fork_pending(chat_id, name))
        except Exception:  # a misbehaving store must not crash the turn (RB1)
            log.debug("get_fork_pending failed for chat %s project %s", chat_id, name, exc_info=True)
            return False

    def _clear_fork_pending(self, chat_id: int, name: str) -> None:
        """Clear the PERSISTED ``fork_pending`` marker (B2+B3); swallow any store error (RB1).

        Called after the first SUCCESSFUL turn of an adopted session (the forked/continued id is
        then persisted + ours alone, so subsequent resumes are ordinary continues) and on the
        resume-failure rebuild path (a fresh session, no base to fork). Never crashes the turn
        over a write — an :class:`~claude_tg.session_store.UnknownProject` (``/rm``'d mid-turn)
        or any other store error is logged and ignored.
        """
        if self.store is None:
            return
        try:
            self.store.set_fork_pending(chat_id, name, False)
        except Exception:
            log.debug("clear fork_pending failed for chat %s project %s", chat_id, name, exc_info=True)

    def _reprobe_liveness(self, session_id: str, cwd: Optional[str]) -> tuple[bool, bool]:
        """Re-probe a base id's CURRENT liveness at the first write → ``(running, degraded)``.

        Delegates to the injected single-session probe seam (``self._probe_one`` →
        :meth:`SessionDiscovery.probe_one` by default), which runs a FRESH composite liveness
        check (its own ps / registry / mtime snapshot). **Never raises (RB1):** any unexpected
        error degrades to ``(False, True)`` — uncertain — so :meth:`_ensure_engine` forks on
        doubt rather than risk co-driving. The probe itself is already RB1-total; this is the
        belt-and-braces wrapper at the call boundary.
        """
        try:
            running, degraded = self._probe_one(session_id, cwd)
            return bool(running), bool(degraded)
        except Exception:  # RB1: a probe hiccup → uncertain (fork on doubt), never crash the turn
            log.debug("first-write liveness re-probe failed; treating as uncertain", exc_info=True)
            return False, True

    # -- attach: adopt ANY discovered Claude session as a project (P11 T2) ----

    def attach_session(self, chat_id: int, session_id: str) -> AttachOutcome:
        """Adopt the discovered session ``session_id`` as a controllable bot project (P11 T2).

        The "drive any session from your phone" core: look the discovered session up (id →
        cwd + composite liveness), then create/adopt it as a bot project pinned to that
        ``(session_id, cwd)`` and make it active, so the operator's NEXT message resumes +
        drives it through the **normal turn + permission gate** path (no bypass). Returns an
        :class:`AttachOutcome` the bot replies (the session owns all the policy; the bot is a
        pure renderer, like ``/sessions``).

        **⭐ The hard safety rules (this is the risky write part):**

        1. **Fork-if-live (never co-drive a live session).** The target's composite liveness
           (the T1 epoch-validated ``running`` hint) gates the adopt: if it is RUNNING in
           another process the project is marked to **FORK** on its first resume
           (``attach_fork=True`` → ``_ensure_engine`` passes ``fork=True`` → the SDK resumes
           into a NEW id with the transcript copied, never writing the live id) and the
           operator is TOLD why; if IDLE it continues the same id (``attach_fork=False``).
           Two writers on one ``(id, cwd)`` silently corrupt the transcript — this is THE
           rule that prevents it.
        2. **SB2 on the discovered cwd.** A discovered session's cwd can be ANYWHERE on the
           Mac. The cwd is canonicalized + confined via :func:`resolve_within_roots`; an
           out-of-``ALLOWED_ROOTS`` cwd (with ``ALLOW_ANY_PATH`` off) is **refused** with a
           clear message — we never silently adopt + drive a session in an arbitrary dir.
        3. **SB1** is enforced by the bot (``/attach`` rides the ``allowed`` filter + the
           ``_authorized`` recheck; the attach callback rechecks ``_authorized`` in
           ``on_callback``) BEFORE this is reached — an unauthorized chat never adopts.
        4. The adopted project drives through the normal path and is **persisted** in the
           registry under a generated SB4-valid name (RB1/RB2: unknown id / out-of-root / a
           resume that fails → a clean message, no crash; the resume-fail recovery is the
           existing ``_ensure_engine`` RB3 path).

        Steps (fail-fast + secure):

        * **No store →** projects need persistence — reply a clean notice (RB1: never deref a
          None store).
        * **Look up** ``session_id`` in the (injected) discovery. An id absent from discovery
          → a clean "unknown session" refusal (RB2 — the operator typed/tapped a stale id).
        * **SB2** the discovered cwd; out-of-root → refuse (rule 2).
        * **Already adopted?** If a project already points at this ``session_id`` for the
          chat, just switch to it (idempotent — re-attaching the same id never forks a
          duplicate project, and never spuriously re-forks a session we already own).
        * **Adopt:** derive an SB4-valid, deduped project name, ``store.create`` it at the
          discovered cwd + ``set_session_id`` to the discovered id, make it active, and seed
          the in-memory runtime's ``attach_fork`` from the liveness (rule 1).
        """
        if self.store is None:
            return AttachOutcome(
                ok=False,
                message="Attaching a session needs persistence — set CLAUDE_STATE_FILE.",
            )
        sid = (session_id or "").strip()
        if not sid:
            return AttachOutcome(ok=False, message="Usage: /attach <session-id>")

        discovered = self._find_discovered(sid)
        if discovered is None:
            # RB2: the id is not among the machine's discovered sessions (stale / mistyped /
            # never existed). A clean refusal — no crash, no adopt. SB3: echo only a SHORT id
            # prefix (the full id is a resumable credential; the operator typed it, but we
            # keep the reply body-free of the full id, mirroring the render discipline).
            return AttachOutcome(
                ok=False,
                message=(
                    f"❌ No Claude session found with id <code>{html.escape(sid[:12], quote=False)}</code>. "
                    "Use /sessions to see what's on this machine."
                ),
                parse_mode="HTML",
            )

        cwd = discovered.cwd
        if not cwd:
            # A discovered session with no recorded cwd can't be resumed (the resume is
            # cwd-scoped — ADR-001/C6) — refuse cleanly rather than adopt an un-runnable one.
            return AttachOutcome(
                ok=False,
                message="❌ That session has no recorded working directory — can't attach it.",
            )

        # SB2 (rule 2): the discovered cwd can be ANYWHERE — confine it to the permitted roots
        # BEFORE adopting. resolve_within_roots canonicalizes (~, .., symlinks); an out-of-root
        # cwd raises PathNotAllowed and we refuse — we never silently adopt + drive a session
        # in an arbitrary dir. ALLOW_ANY_PATH=true no-ops the check (the resolver returns the
        # canonical path), the same opt-out /new + /cd honor.
        try:
            resolved = resolve_within_roots(
                cwd,
                cwd=cwd,
                allowed_roots=self.config.allowed_roots,
                allow_any=self.config.allow_any_path,
            )
        except PathNotAllowed:
            # R6: wrap the (discovered) path in <code> so Telegram renders it inert monospace,
            # not tappable fake command-links; code_path HTML-escapes it.
            return AttachOutcome(
                ok=False,
                message=(
                    f"❌ That session's directory {code_path(cwd)} is outside the permitted "
                    "roots — not attaching. Widen ALLOWED_ROOTS (or set ALLOW_ANY_PATH) to "
                    "drive a session there."
                ),
                parse_mode="HTML",
            )
        # Use the CANONICAL path as the project's cwd (never the raw discovered string) so the
        # stored cwd is the resolved, contained path — consistent with /new + /cd.
        canonical_cwd = str(resolved)

        # Idempotent re-attach: if a project already points at this id, just switch to it
        # (case-insensitive on the value match is unnecessary — session ids are exact). This
        # avoids forking a duplicate project AND never re-forks a session we already own.
        existing = self._project_for_session(chat_id, sid)
        if existing is not None:
            self.store.switch(chat_id, existing)
            # P13 T-AUDIT: an idempotent re-attach is still a (re-)attach — record it.
            self.record_audit(
                KIND_SESSION_EVENT, chat_id=chat_id, summary="attach", name=existing
            )
            name_html = html.escape(existing, quote=False)
            return AttachOutcome(
                ok=True,
                message=(
                    f"✅ Already attached as <b>{name_html}</b> — switched to it; your next "
                    "message resumes it."
                ),
                parse_mode="HTML",
                project_name=existing,
                forked=False,
            )

        # Derive an SB4-valid, deduped project name from the session's title / cwd basename.
        name = self._attach_project_name(chat_id, discovered, sid)

        try:
            self.store.create(chat_id, name, canonical_cwd, make_active=True)
        except (InvalidProjectName, DuplicateProject):
            # Defensive (RB1): _attach_project_name already validated + deduped, so neither
            # should fire — but never crash the attach over a registry write. Re-derive once
            # with a guaranteed-unique fallback and retry; if THAT fails, refuse cleanly.
            name = self._fallback_attach_name(chat_id, sid)
            try:
                self.store.create(chat_id, name, canonical_cwd, make_active=True)
            except (InvalidProjectName, DuplicateProject):
                return AttachOutcome(
                    ok=False,
                    message="❌ Couldn't adopt that session as a project — please try again.",
                )
        # Pin the discovered id onto the new (active) project so the next turn resumes it.
        self.store.set_session_id(chat_id, name, sid)
        # ⭐ B2+B3: PERSIST the fork-pending marker so the BINDING fork-vs-continue decision is
        # made at the FIRST WRITE, from a FRESH liveness re-probe — NOT frozen here at attach
        # time. This survives a restart (the in-memory runtime is lost on restart, but the
        # persisted base id + this marker are not), so a restart before the first turn re-probes
        # and re-decides (closing B2's co-drive-after-restart). And because the re-probe runs at
        # first write, an idle-at-attach session that has since gone live is caught then (B3).
        # _ensure_engine reads this, re-probes, forks on live-or-uncertain, and clears it
        # (persisted) after the first successful turn (never re-forking thereafter).
        self.store.set_fork_pending(chat_id, name, True)
        # P13 T-AUDIT: record the adopt (body-free session_event). The session tag resolves
        # from the just-pinned id (redacted — never the raw resumable id). Best-effort (RB1).
        self.record_audit(KIND_SESSION_EVENT, chat_id=chat_id, summary="attach", name=name)

        # Build the runtime (fresh — brand-new name) and ALSO mirror the marker in memory so a
        # turn within THIS process doesn't need a store round-trip; _ensure_engine consults the
        # persisted marker as the source of truth (the in-memory one is just a fast-path /
        # restart-survivable mirror). attach_fork stays for the in-process hint; the persisted
        # fork_pending is authoritative.
        rt = self._runtime(chat_id, name, canonical_cwd)
        rt.attach_fork = True  # adopted-pending; the actual fork-vs-continue is decided at write

        # The attach-time liveness is only a PREVIEW hint for the message — NOT a promise (the
        # binding decision is the first-write re-probe). Phrase honestly so we never over-promise
        # an outcome that the re-probe could change between now and the first message.
        name_html = html.escape(name, quote=False)
        message = (
            f"✅ Attached <b>{name_html}</b>. On your next message I'll resume it — forking "
            "automatically if it's active elsewhere, so I never corrupt a live session.\n"
            f"{code_path(canonical_cwd)}"
        )
        return AttachOutcome(
            ok=True,
            message=message,
            parse_mode="HTML",
            project_name=name,
            # ``forked`` is not yet known (decided at first write); report False here and surface
            # the ACTUAL outcome when the first turn starts. The bot relays only ``message``.
            forked=False,
        )

    def _find_discovered(self, session_id: str) -> Optional[DiscoveredSession]:
        """The discovered session whose id matches ``session_id``, or ``None`` (RB1-total).

        Runs the (injected) machine-wide discovery and returns the matching
        :class:`~claude_tg.sessions_discovery.DiscoveredSession` (id is exact — session ids
        are UUIDs). Discovery is already best-effort/total (an empty/odd ``~/.claude`` →
        ``[]``); a misbehaving injected discover is swallowed to ``None`` so an attach can
        never crash the handler. The match carries the cwd (for SB2) + the ``running`` hint
        (for fork-vs-continue).
        """
        try:
            sessions = self._discover() or []
        except Exception:  # a custom discover that misbehaves must not crash attach (RB1)
            log.warning("session discovery failed during attach", exc_info=True)
            return None
        for s in sessions:
            if getattr(s, "session_id", None) == session_id:
                return s
        return None

    def _project_for_session(self, chat_id: int, session_id: str) -> Optional[str]:
        """The chat's project (stored name) already pinned to ``session_id``, or ``None``.

        Makes attach idempotent: a re-attach of an id the chat already adopted just switches
        to the existing project instead of forking a duplicate (and never re-forks a session
        we already own). Read-only; never raises (RB1).
        """
        if self.store is None:
            return None
        try:
            projects = self.store.list_projects(chat_id)
        except Exception:
            return None
        for pname, record in projects.items():
            if isinstance(record, dict) and record.get("session_id") == session_id:
                return str(pname)
        return None

    # -- live-mirror: /watch <id> and /unwatch (P11 T3, READ-ONLY) -----------

    def watch_session(
        self, chat_id: int, session_id: str, *, send: SendFn
    ) -> "WatchOutcome":
        """Start a READ-ONLY live mirror of ``session_id``'s transcript onto this chat (P11 T3).

        Resolves the target id → its cwd → its append-only transcript path (via the SAME
        machine-wide discovery ``/attach`` uses), then starts a background asyncio task that
        TAILS that file, maps each line to the bot's body-free events
        (:func:`~claude_tg.session_mirror.normalize_line`), renders them through the EXISTING
        :func:`~claude_tg.render.render_event`, and SENDS them through this chat's per-chat
        send gate (so a fast-writing session can never burst past Telegram's ~1 msg/s/chat
        ceiling — flood control in :func:`~claude_tg.session_mirror.render_batch`). It NEVER
        writes the watched transcript.

        **ONE watch per chat.** A new ``/watch`` REPLACES any prior one for the chat (the old
        task is cancelled first and the operator is told). ``/unwatch`` (:meth:`unwatch`)
        stops it; :meth:`shutdown` cancels every watch on bot shutdown so no task outlives the
        bot.

        **⭐ SB3** is enforced in the normalizer (raw tool bodies never reach an event, so they
        never reach Telegram). **SB2-ish:** :func:`~claude_tg.session_mirror.transcript_path`
        resolves the path canonically and refuses to follow a symlink OUT of ``~/.claude/
        projects``; mirroring is read-only so the SB2 adopt-confinement is not the driver here.
        **SB1** is enforced by the bot (``/watch`` rides the ``allowed`` filter + ``_authorized``)
        BEFORE this is reached.

        Returns a :class:`WatchOutcome` the bot replies. RB1/RB2: an unknown/odd id, a session
        with no cwd, or an unresolvable transcript path → a clean refusal (no task, no crash).
        ``send`` is the bot's per-chat send closure (it captures the persistent ``Bot`` + the
        chat id, so the background task can send after the handler returns); injected so tests
        capture what would be sent.
        """
        sid = (session_id or "").strip()
        if not sid:
            return WatchOutcome(ok=False, message="Usage: /watch <session-id>")

        discovered = self._find_discovered(sid)
        if discovered is None:
            # RB2: the id is not among the machine's discovered sessions (stale / mistyped).
            return WatchOutcome(
                ok=False,
                message=(
                    f"❌ No Claude session found with id <code>{html.escape(sid[:12], quote=False)}</code>. "
                    "Use /sessions to see what's on this machine."
                ),
                parse_mode="HTML",
            )
        cwd = discovered.cwd
        if not cwd:
            return WatchOutcome(
                ok=False,
                message="❌ That session has no recorded working directory — can't mirror it.",
            )
        path = transcript_path(sid, cwd)
        if path is None:
            # No resolvable, in-tree transcript path (missing cwd, or a symlink escaping
            # ~/.claude/projects). Refuse cleanly — never follow it.
            return WatchOutcome(
                ok=False,
                message="❌ Can't locate that session's transcript to mirror it.",
            )

        # ONE watch per chat: replace any prior one (cancel the old task; tell the operator).
        replaced = self._cancel_watch(chat_id)

        state = self._chat(chat_id)
        tailer = TranscriptTailer(path=path)
        emit = self._make_watch_emit(state, send)

        async def _on_gone() -> None:
            # The transcript vanished (session ended / file removed) — tell the operator the
            # mirror stopped, body-free. Drop the registry entry so a later /unwatch is a clean
            # no-op. Best-effort through the gate (verbatim — a terminal notice).
            self._watches.pop(chat_id, None)
            try:
                await self._gated_send(
                    state, send, verbatim=True,
                    text=f"👁 Mirror ended — session {html.escape(sid[:8], quote=False)} transcript is gone.",
                    parse_mode="HTML",
                )
            except Exception:
                log.debug("watch on_gone notice failed (ignored)", exc_info=True)

        async def _runner() -> None:
            try:
                await run_mirror(
                    tailer,
                    emit=emit,
                    sleep=self._sleep,
                    poll_interval=self._watch_poll_interval,
                    queue_max=self._watch_queue_max,
                    should_stop=lambda: self._watches.get(chat_id) is not task,
                    on_gone=_on_gone,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # a watch loop must never crash the process (RB1)
                log.exception("live-mirror watch loop failed for chat %s", chat_id)

        task: asyncio.Task[None] = asyncio.ensure_future(_runner())
        self._watches[chat_id] = task
        # Best-effort: drop the registry entry when the task finishes on its own (gone /
        # error) so a stale done-task never lingers as "the active watch". ``chat_id`` is
        # captured by closure (each call has its own); the callback gets the finished task.
        def _done(finished: "asyncio.Future[None]") -> None:
            self._on_watch_done(chat_id, finished)

        task.add_done_callback(_done)

        # P13 T-AUDIT: record the live-mirror start (body-free session_event). The watched id
        # is NOT a project session — pass name=None so the tag is sid:none (the record marks
        # that a /watch happened; the watched id is not logged). Best-effort (RB1).
        self.record_audit(KIND_SESSION_EVENT, chat_id=chat_id, summary="watch")
        short = html.escape(sid[:8], quote=False)
        prefix = "🔁 Replaced the previous mirror. " if replaced else ""
        return WatchOutcome(
            ok=True,
            message=(
                f"👁 {prefix}Now mirroring session <code>{short}</code> (read-only). "
                "Send /unwatch to stop."
            ),
            parse_mode="HTML",
            session_id=sid,
        )

    def unwatch(self, chat_id: int) -> str:
        """Stop this chat's active live-mirror, if any (P11 T3). Returns an operator reply.

        Cancels the running tail task + drops the registry entry (the task releases the file
        as it unwinds — the tailer holds no open handle between polls anyway). Idempotent: no
        active watch → a clean "nothing to stop" notice. Never raises (RB1).
        """
        if self._cancel_watch(chat_id):
            # P13 T-AUDIT: record the mirror stop only when one was actually active (an
            # idempotent no-op /unwatch records nothing). Body-free session_event (RB1).
            self.record_audit(KIND_SESSION_EVENT, chat_id=chat_id, summary="unwatch")
            return "🛑 Stopped mirroring."
        return "There's no active mirror to stop (use /watch <session-id> to start one)."

    def is_watching(self, chat_id: int) -> bool:
        """Whether ``chat_id`` has a live (not-yet-finished) mirror task (read-only; RB1)."""
        task = self._watches.get(chat_id)
        return task is not None and not task.done()

    def _make_watch_emit(self, state: _ChatState, send: SendFn) -> "Callable[..., Awaitable[Optional[int]]]":
        """Build the watch's send-one-message-through-the-gate coroutine (D8 flood control).

        Every mirror message funnels through the chat's :class:`~claude_tg.render.ChatSendGate`
        (via :meth:`_gated_send`) so the combined per-chat rate stays bounded — the mirror can
        never burst past Telegram's ~1 msg/s/chat ceiling even when the watched session writes
        fast. ``verbatim`` (assistant/operator/result text) keeps gate priority; a tool-use
        line is non-verbatim and yields. A send failure propagates to ``run_mirror`` which logs
        + continues (one bad send never kills the mirror).
        """

        async def emit(*, text: str, parse_mode: Optional[str], verbatim: bool) -> Optional[int]:
            return await self._gated_send(
                state, send, verbatim=verbatim, text=text, parse_mode=parse_mode
            )

        return emit

    def _cancel_watch(self, chat_id: int) -> bool:
        """Cancel + drop this chat's watch task if present; return whether one existed.

        The single place a watch is torn down (``/watch`` replacement, ``/unwatch``,
        shutdown). Removes the registry entry FIRST (so the loop's ``should_stop`` sees it is
        no longer the active task and the done-callback no-ops) then cancels the task. Never
        raises (RB1).
        """
        task = self._watches.pop(chat_id, None)
        if task is None:
            return False
        if not task.done():
            task.cancel()
        return True

    def _on_watch_done(self, chat_id: int, task: "asyncio.Future[None]") -> None:
        """Drop the registry entry when a watch task finishes on its own (gone / error / cancel).

        Guards against clobbering a REPLACEMENT watch: only removes the entry if it is STILL
        this exact task (a new /watch may have already replaced it). Pure bookkeeping; never
        raises (a cancelled task's exception is intentionally not retrieved here). Takes a
        ``Future`` (what ``add_done_callback`` passes); the ``is`` identity check is exact.
        """
        if self._watches.get(chat_id) is task:
            self._watches.pop(chat_id, None)

    def _attach_project_name(
        self, chat_id: int, discovered: DiscoveredSession, session_id: str
    ) -> str:
        """Derive an SB4-valid, deduped project name for an adopted session (P11 T2 naming).

        A project name must satisfy SB4 (``^[A-Za-z0-9_-]{1,32}$``) so ``/projects`` /
        ``/switch`` work on it. We build a friendly base from the session's title (the first
        prompt / custom title) or its cwd basename, sanitize every non-``[A-Za-z0-9_-]`` char
        to ``-``, collapse/trim, and clamp to the length budget; an empty result falls back to
        ``attached-<shortid>``. Then we DEDUPE against the chat's existing projects
        (case-insensitive, mirroring the store) by appending ``-2``, ``-3``, … so two attaches
        of similarly-named sessions never collide. Pure of I/O beyond the read-only store list.
        """
        base = _sanitize_attach_name(discovered.title) or _sanitize_attach_name(
            _basename_of(discovered.cwd)
        )
        if not base:
            base = self._fallback_attach_name(chat_id, session_id)
        return self._dedupe_attach_name(chat_id, base)

    def _fallback_attach_name(self, chat_id: int, session_id: str) -> str:
        """An always-valid ``attached-<shortid>`` name (deduped), the last-resort base.

        Used when the title + cwd basename both sanitize to nothing, or as the retry base if a
        derived name somehow collided. ``<shortid>`` is the session id's first 8 chars
        (sanitized to the SB4 charset), so it is recognizable + unique-ish; the dedupe suffix
        guarantees uniqueness within the chat.
        """
        short = _sanitize_attach_name(session_id[:8]) or "x"
        return self._dedupe_attach_name(chat_id, f"attached-{short}")

    def _dedupe_attach_name(self, chat_id: int, base: str) -> str:
        """``base`` (or ``base-2``/``base-3``/…) — the first not already used by the chat.

        Matches the store's case-insensitive name comparison so the returned name is
        guaranteed to pass ``store.create`` without a :class:`DuplicateProject`. Clamps each
        candidate to the SB4 32-char budget (trimming the BASE, never the numeric suffix, so
        the suffix always survives). Read-only on the store (RB1).
        """
        existing = set()
        if self.store is not None:
            try:
                existing = {n.casefold() for n in self.store.list_projects(chat_id)}
            except Exception:
                existing = set()
        candidate = base[:32] or "attached"
        if candidate.casefold() not in existing:
            return candidate
        i = 2
        while True:
            suffix = f"-{i}"
            trimmed = base[: 32 - len(suffix)] or "attached"[: 32 - len(suffix)]
            candidate = f"{trimmed}{suffix}"
            if candidate.casefold() not in existing:
                return candidate
            i += 1

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
            # P13 T-AUDIT: record the reset (body-free session_event). Recorded only when a
            # project was actually reset (name is not None — a no-project /reset is a no-op,
            # nothing to audit). The session id is now cleared, so the tag reads sid:none —
            # an honest "fresh session" marker. Best-effort (RB1).
            self.record_audit(
                KIND_SESSION_EVENT, chat_id=chat_id, summary="reset", name=name
            )

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

    # -- proactive (scheduler-fired) turn (P14 T-FIRE ⭐) --------------------

    async def _proactive_skip_busy(
        self, state: "_ChatState", schedule: Schedule, send: SendFn
    ) -> None:
        """Body-free busy-skip for a proactive fire: ``⏰ skipped`` notice + ``proactive_skip`` audit.

        Shared by the fire-ability PRE-check (before any header is sent) and the
        :class:`StreamingBusy` RACE branch in :meth:`fire_schedule`, so a busy skip is uniform
        whichever path hit it. The notice is verbatim through the D8 gate (best-effort, RB1);
        the audit is body-free (the task name only, ``decision="busy"``). Never raises.
        """
        try:
            await self._gated_send(
                state, send, verbatim=True,
                text=f"⏰ skipped <b>{html.escape(schedule.name, quote=False)}</b> — still working.",
                reply_markup=None, parse_mode="HTML",
            )
        except Exception:
            log.debug("proactive skip notice send failed for %s (ignored)", schedule.name, exc_info=True)
        self.record_audit(
            KIND_SESSION_EVENT,
            chat_id=schedule.chat_id,
            summary=f"proactive_skip ({schedule.name})",
            decision="busy",
            name=schedule.project,
        )

    async def fire_schedule(
        self,
        schedule: Schedule,
        *,
        send: SendFn,
        edit: EditFn,
        delete: Optional[DeleteFn] = None,
        pin: Optional[PinFn] = None,
        unpin: Optional[UnpinFn] = None,
    ) -> bool:
        """Fire ONE scheduled task as a fully-gated PROACTIVE turn (P14 T-FIRE ⭐).

        The single entry point both the firing driver and ``/runnow`` use. It REUSES the
        entire turn stack — it just calls :meth:`handle_message` with the security flags set:

        * ``proactive=True`` → the engine FORCES the permission gate ON for the turn
          (``/yolo`` + grants treated as off; every risky tool HOLDS and, unattended,
          backstop-DENIES — RB4). The unattended-action core (design §5.1/§5.2).
        * ``project_override=schedule.project`` → the turn targets the task's PINNED project
          (not whatever is active), or the active project if the task pinned none / it is gone.
        * ``command_initiated=True`` → a FRESH turn that can never be swallowed as the answer
          to some other project's pending free-text hold (design §2.2 — load-bearing).

        Flow + posture:

        0. **⭐ SB1 re-check AT FIRE TIME (§5.4).** Before anything else, re-check that
           ``schedule.chat_id`` is STILL in the current allowlist (``config.allowed_chat_ids``).
           SB1 is enforced at ``/every`` creation, but a chat REMOVED from
           ``TELEGRAM_ALLOWED_CHAT_IDS`` since then must NEVER receive a proactive fire — so a
           de-authorized chat's schedule is SKIPPED (no header, no turn) with a body-free
           ``proactive_skip`` (``decision="unauthorized"``). SB1 holds at fire, not just create.
        1. **Fire-ability (no-stacking) pre-check (design §3.2).** If the target project/chat is
           already BUSY, SKIP this tick — body-free ``⏰ skipped <name> — still working`` notice
           + a ``proactive_skip`` (``decision="busy"``) — WITHOUT sending a start header (so a
           busy-skip shows only the skip notice, never a confusing header-then-skip pair).
        2. **Notify + audit the fire.** Only when the turn WILL run: a small body-free
           ``⏰ <name> (scheduled)`` header through the per-chat send gate (SB3: the schedule
           NAME only, never the prompt) + a body-free ``proactive_fire`` ``session_event`` (the
           task name, never the prompt) so the owner can reconstruct what fired while away (§5.3).
        3. **Drive** via :meth:`handle_message` (the full gated path). The turn's own streamed
           output IS the result notification (renders through the gate like any turn).
        4. **RB1-total.** ``handle_message`` is still the AUTHORITATIVE concurrency guard: if a
           race slips a turn past the pre-check it raises :class:`StreamingBusy`, caught here →
           ``proactive_skip`` (busy). ANY other turn exception is caught, logged body-free, and
           audited as a ``proactive_skip`` (``decision="error"``) — a single bad fire NEVER
           propagates (so it can never kill the firing loop or the bot). Returns ``True`` iff the
           turn was driven (not skipped/errored). The audit/notify writes are best-effort (RB1).
        """
        name = schedule.name
        state = self._chat(schedule.chat_id)
        # 0) ⭐ SB1 AT FIRE TIME: a chat de-authorized since /every must NEVER receive a fire.
        #    Re-check the live allowlist; if gone, skip (no header, no turn) + audit unauthorized.
        if schedule.chat_id not in self.config.allowed_chat_ids:
            log.warning(
                "proactive fire of %s skipped — chat %s is no longer allowlisted (SB1)",
                name,
                schedule.chat_id,
            )
            self.record_audit(
                KIND_SESSION_EVENT,
                chat_id=schedule.chat_id,
                summary=f"proactive_skip ({name})",
                decision="unauthorized",
                name=schedule.project,
            )
            return False
        # 1) Fire-ability pre-check: if the target project/chat is already BUSY, skip this tick
        #    BEFORE sending any header (so a busy-skip shows only the skip notice — not a
        #    header-then-skip pair). ``project=None`` checks "any project busy" (conservative —
        #    never stack a fire into a chat with a turn already in flight). handle_message's own
        #    StreamingBusy below remains the authoritative guard for the pre-check→drive race.
        if self.is_busy(schedule.chat_id, schedule.project):
            await self._proactive_skip_busy(state, schedule, send)
            return False
        # 2) Body-free "a scheduled run started" header (only now that the turn WILL run) +
        #    the proactive_fire audit (the task name, never the prompt). Verbatim through D8.
        try:
            await self._gated_send(
                state, send, verbatim=True,
                text=f"⏰ <b>{html.escape(name, quote=False)}</b> (scheduled)…",
                reply_markup=None, parse_mode="HTML",
            )
        except Exception:
            log.debug("proactive fire header send failed for %s (ignored)", name, exc_info=True)
        self.record_audit(
            KIND_SESSION_EVENT,
            chat_id=schedule.chat_id,
            summary=f"proactive_fire ({name})",
            name=schedule.project,
        )
        # 3) Drive the proactive turn through the FULL gated path (force-gate ON).
        try:
            await self.handle_message(
                schedule.chat_id,
                schedule.prompt,
                send=send,
                edit=edit,
                delete=delete,
                pin=pin,
                unpin=unpin,
                command_initiated=True,
                proactive=True,
                project_override=schedule.project,
            )
            return True
        except StreamingBusy:
            # Overlap RACE: the pre-check passed but the project/chat became busy before this
            # turn acquired (handle_message is the authoritative guard). Skip this tick (the
            # driver reschedules), notice + audit body-free — same path as the pre-check skip.
            log.info("proactive fire of %s skipped — project busy (race)", name)
            await self._proactive_skip_busy(state, schedule, send)
            return False
        except Exception:
            # RB1-total: ANY other fire failure (a render bug, an engine build error, a store
            # hiccup mid-turn) is caught here so it can NEVER propagate to the firing loop /
            # bot. Logged body-free + audited as a skip so the owner can see the fire faltered.
            log.exception("proactive fire of %s failed (skipping this tick)", name)
            self.record_audit(
                KIND_SESSION_EVENT,
                chat_id=schedule.chat_id,
                summary=f"proactive_skip ({name})",
                decision="error",
                name=schedule.project,
            )
            return False

    # -- the turn driver (LOCK-GUARDED: one turn per PROJECT) ----------------

    async def handle_message(
        self,
        chat_id: int,
        text: str,
        *,
        send: SendFn,
        edit: EditFn,
        delete: Optional[DeleteFn] = None,
        pin: Optional[PinFn] = None,
        unpin: Optional[UnpinFn] = None,
        reply_to_message_id: Optional[int] = None,
        command_initiated: bool = False,
        images: Optional[Sequence[ImageInput]] = None,
        proactive: bool = False,
        project_override: Optional[str] = None,
    ) -> bool:
        """Drive ONE operator turn (or capture a free-text answer) for ``chat_id``.

        **P10 T1 — optional ``images`` (multimodal).** When an operator sends a photo /
        image-document, the bot passes the decoded :class:`~claude_tg.engine.ImageInput`
        list here and the caption (or a default look-at-this prompt) as ``text``. An image
        turn is ALWAYS a fresh turn — like ``command_initiated``, it never satisfies a
        pending free-text "Other"/reject hold (you do not answer a question with a
        screenshot) — so the free-text routing is skipped when ``images`` is present, and
        the pixels are threaded through ``_drive_turn`` → ``engine.send(images=…)``. The
        per-project busy-guard / slot / lock path is otherwise identical to a text turn.

        **P14 T-FIRE — ``proactive`` + ``project_override`` (a SCHEDULER-fired turn).** Both
        default off, so every existing caller + test is byte-for-byte unchanged. ⭐
        ``proactive=True`` marks this turn as machine-initiated (no human at fire time): it is
        threaded down to ``engine.send(proactive=True)`` so the engine FORCES the permission
        gate ON for the turn — ``/yolo`` + allow-session grants are treated as OFF, every risky
        tool HOLDS (and, unattended, backstop-DENIES — RB4), without mutating the project's
        persistent policy (the operator's interactive bypass survives for their own typed
        turns). A proactive turn is ALWAYS a fresh turn (like ``command_initiated`` — which the
        scheduler also sets — it never satisfies a pending free-text hold). ``project_override``
        PINS the target project by name (a scheduled task targets ITS project, not whatever is
        active) — it is resolved to that project's ``(name, runtime)`` and used as the turn's
        pinned ``target``; an unknown/blank name falls back to the active project (the
        scheduler validates the name at create, so this is defensive). The per-project
        busy-guard / slot / lock / gate / audit path is otherwise identical to a typed turn —
        the whole point: a proactive fire REUSES the entire turn stack, adding only "force the
        gate on".

        Free-text capture takes precedence: if any project is awaiting an "Other" answer /
        plan-reject feedback, this text is routed to ``engine.resolve`` (NOT a new turn)
        and the held turn — still inside ``engine.send`` — continues. Otherwise it opens
        a new turn via ``engine.send`` and renders the event stream against the **active
        project's** engine (auto-creating ``default`` on the first turn — ADR-004 D6).

        **Returns** ``True`` iff this message was CONSUMED as a free-text capture (a reply to
        an "Other"/reject prompt — whether or not its target was still live), ``False`` for a
        normal new turn. T6/P9: the bot uses this to dismiss the one-time quick-reply chips
        (``ReplyKeyboardRemove``) once the free-text prompt is answered, so the chips don't
        linger over the next, unrelated turn. Existing callers that ignore the return value
        are unaffected (Python discards it).

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

        **Command-initiated turns bypass free-text capture (P9 fix).** A macro ``/run``
        expands to text and routes here, but it is a DELIBERATE command to START a fresh
        turn — it must NEVER be swallowed as the answer to an outstanding "Other"/plan-reject
        free-text hold. ``command_initiated=True`` therefore skips the free-text routing below
        so the expanded macro always opens a new turn (through the same permission path a
        plain message does), against the active project. A PLAIN typed message keeps
        ``command_initiated=False`` and answers a pending capture EXACTLY as before. (If the
        active project is the one parked awaiting free text it is ``inflight``, so the fresh
        ``/run`` turn cleanly raises :class:`StreamingBusy` — the bot replies "still working"
        — rather than misrouting.)
        """
        state = self._chat(chat_id)

        # Free-text capture for a prior "Other"/reject tap routes to resolve(), not a new
        # turn — and must NOT take the turn lock (the awaiting turn holds it). The capture
        # marker lives on the OWNING project's runtime (ADR-005 D7); under concurrency
        # several projects can be armed, so the target is chosen by the D5 precedence
        # (reply-to > most-recent) in one resolver. ``routed`` is True iff free-text routing
        # CLAIMED this message (it was a free-text reply, even if the target turned out gone
        # — so a stale reply-to never silently falls through to a NEW turn / a misroute).
        # P9 fix: a command-initiated turn (a macro /run) NEVER captures a pending free-text
        # hold — it always opens a fresh turn — so the routing is skipped entirely for it.
        # P10 T1: an image turn (images present) is ALWAYS a fresh turn — like a macro
        # /run it must never be swallowed as the answer to an outstanding free-text hold,
        # so the routing is skipped for it too.
        # P14 T-FIRE: a proactive (scheduler-fired) turn is ALWAYS a fresh turn — like a
        # command-initiated /run or an image turn it must NEVER be swallowed as the answer to
        # an outstanding free-text hold (no human is replying), so the routing is skipped for
        # it too. (The scheduler also passes command_initiated=True, so this is belt-and-braces.)
        armed_name, armed_rt, routed = (
            (None, None, False)
            if (command_initiated or images or proactive)
            else self._route_free_text_target(state, reply_to_message_id)
        )
        if routed:
            if armed_rt is not None:
                self._resolve_free_text(state, chat_id, armed_name, armed_rt, text)
            # else: a free-text reply whose target is gone/ambiguous — no-op (never a
            # misroute, never silently a new turn). The marker (if any) was already cleared
            # by _resolve_free_text on a prior attempt; nothing else to do.
            # T6/P9: a free-text reply was consumed (resolved or a stale no-op) — return True
            # so the bot dismisses the one-time quick-reply chips it attached to the prompt.
            return True

        # P5 / ADR-005 D1 (T5): lock the TARGET project — the active project at message
        # time — NOT the chat. Resolving it (create_default=True) auto-creates `default` on
        # the first turn (ADR-004 D6), exactly as a turn always must. A second message to
        # the SAME project while its lock is held raises StreamingBusy; a message to a
        # DIFFERENT idle project takes its own lock and runs concurrently. _ensure_engine /
        # _drive_turn re-resolve + pin the active project (the busy-guards keep it stable for
        # the turn in T5; T7 frees /switch but _drive_turn still pins the turn's project).
        # P14 T-FIRE: ``project_override`` PINS the target project by name (a scheduled task
        # targets ITS project, not whatever the chat has active). Resolve it to that project's
        # (name, runtime); an unknown/blank name (defensive — the scheduler validates at create)
        # falls back to the active project so a fire is never silently dropped.
        target_name: Optional[str]
        target_rt: Optional[_ProjectRuntime]
        if project_override:
            target_name, target_rt = self._override_runtime(chat_id, project_override)
        else:
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
        # P12 T-PLAN-2 (/plan), round-2 QA BLOCKER — CONSUME the one-shot plan marker HERE, at
        # the EARLIEST point this PROMPT message is committed to being driven as a turn: after
        # the busy-guard passed (so we won't reject + leave it armed) and BEFORE _acquire_slot,
        # the two pre-engine abort guards (the slot-transfer + lock-wait windows below), and
        # _ensure_engine's SB2 PathNotAllowed check. Read + CLEAR atomically into a local
        # ``plan_turn`` that is threaded DOWN to _ensure_engine (which no longer reads/clears
        # the marker). Consuming it this early makes the one-shot contract hold on EVERY exit:
        # a turn that aborts (abort.is_set → return) or is refused fail-closed (SB2 raise) STILL
        # consumed the marker, so the NEXT message is ALWAYS a normal turn — never a surprise
        # plan prompt (the bug: the late read/clear in _ensure_engine was skipped by both the
        # pre-engine ``return``s and the SB2 raise). PROMPT-turn-scoped: commands (/status,
        # /plan itself) go through their cmd_* handlers, never handle_message, so they never
        # reach here and never consume the marker (/plan → /status → a prompt = the PROMPT runs
        # in plan mode); a free-text "Other"/reject reply returned above (it resolves a hold,
        # not a new turn) so it doesn't consume either. RB3: ``plan_turn`` is a local; the
        # marker stays in-memory + un-persisted.
        plan_turn = target_rt.plan_next
        target_rt.plan_next = False
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
                    return False
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
                        return False
                    try:
                        engine, resume_failed = await self._ensure_engine(
                            chat_id, target=target, plan_turn=plan_turn
                        )
                    except PathNotAllowed:
                        # SB2 (T7): the active project's stored cwd drifted out of the permitted
                        # roots (config narrowed, or a path component became an out-of-root
                        # symlink). Refuse the turn fail-closed WITHOUT starting the engine; the
                        # lock releases on return AND the finally releases the slot (no leak).
                        # Operator-facing refusal → verbatim priority through the D8 gate.
                        # R6: wrap the cwd in <code> (HTML) so Telegram renders the path as
                        # inert monospace, not a row of tappable fake /segment command-links;
                        # code_path HTML-escapes it so a stray &/</> can't break the message.
                        # The literal "<name> <path>" placeholders are written ESCAPED
                        # (&lt;…&gt;) because this is now an HTML message — unescaped "<name>"
                        # would be parsed as a (broken) tag and Telegram would reject the send.
                        # The send closure passes parse_mode straight through; with the path
                        # escaped + the placeholders escaped, the content is always valid HTML.
                        await self._gated_send(
                            state, send, verbatim=True,
                            text=(
                                f"❌ This project's directory {code_path(self.get_cwd(chat_id))} "
                                "is no longer within the permitted roots — use "
                                "/new &lt;name&gt; &lt;path&gt; to create one inside them."
                            ),
                            reply_markup=None,
                            parse_mode="HTML",
                        )
                        return False
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
                        send=send, edit=edit, delete=delete,
                        pin=pin, unpin=unpin, target=target,
                        images=images, proactive=proactive, plan_turn=plan_turn,
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
        # T6/P9: the normal-turn path was taken (not a free-text capture) → False, so the bot
        # leaves any quick-reply chips alone (they belong to a pending free-text prompt, not a
        # new turn). Reached only on the clean end of a driven turn; the early returns above
        # (abort, SB2 refusal) also return False (all non-free-text).
        return False

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
        pin: Optional[PinFn] = None,
        unpin: Optional[UnpinFn] = None,
        target: Optional[tuple[str, _ProjectRuntime]] = None,
        images: Optional[Sequence[ImageInput]] = None,
        proactive: bool = False,
        plan_turn: bool = False,
    ) -> None:
        """Iterate ``engine.send`` → render → Telegram send/edit (coalesced).

        **P10 T1:** ``images`` (default ``None`` → text turn) is forwarded to
        ``engine.send`` so a multimodal turn streams the prompt + pixels; the render /
        coalesce / QF3-recovery machinery below is identical for both.

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
        # STATUSLINE T-SL-WIRE (B3 fix): mark the LIVE plan-mode flag for the statusline's
        # duration so the line shows 🔒 plan WHILE the plan turn runs. ``plan_turn`` is the value
        # ``handle_message`` consumed from the one-shot ``plan_next`` (already cleared there), so
        # this transient flag is the only honest "this turn is a plan turn" signal at render
        # time. Cleared in the finally (turn end → back to gate/yolo). Set BEFORE the turn-start
        # statusline trigger so that first render already reads ``plan``.
        turn_rt.in_plan_turn = plan_turn
        # STATUSLINE T-SL-WIRE (design §3.1): turn START → flip the working ⚙️ marker ON (and
        # refresh model/effort/mode/worktree). FOREGROUND-ONLY — gated on ``turn_name`` so a
        # BACKGROUND concurrent turn never stomps the foreground line (the make-or-break
        # invariant). Best-effort (RB1): pins/edits can't break the turn (the helper swallows).
        await self._maybe_update_statusline(
            chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=turn_name,
        )
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
        # P10 T1: pass ``images`` to ``engine.send`` ONLY when present, so a pure TEXT turn
        # calls ``engine.send(prompt)`` with the EXACT pre-P10 signature — every existing
        # injected fake engine (whose ``send`` has no ``images`` kwarg) keeps working
        # verbatim. The image path supplies the kwarg to the real Engine (which accepts it).
        # P14 T-FIRE: ⭐ pass ``proactive=True`` to ``engine.send`` ONLY for a proactive turn
        # (the same additive-kwarg discipline), so the engine FORCES the gate on for it; a
        # normal turn omits it entirely (the pre-P14 signature is preserved for every fake).
        send_kwargs: dict[str, Any] = {}
        if images:
            send_kwargs["images"] = images
        if proactive:
            send_kwargs["proactive"] = True
        try:
            async for event in engine.send(prompt, **send_kwargs):
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
                        # T3 (P9): accumulate this turn's SDK-reported cost into the
                        # project's durable cumulative total (shown by /status). Only when
                        # the SDK gave a cost (oneshot / a partial result may not) and a
                        # store + named project exist; swallowed like _persist (RB1 — never
                        # crash a turn over a write). Persisted to THIS turn's CAPTURED
                        # project (turn_name), same per-project discipline as the session_id.
                        if event.total_cost_usd is not None and self.store is not None:
                            try:
                                self.store.add_cost(
                                    chat_id, turn_name, event.total_cost_usd
                                )
                            except Exception:
                                log.exception(
                                    "failed to accumulate project cost for chat %s", chat_id
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
            # STATUSLINE T-SL-WIRE (B3 fix): the plan turn is over → clear the live plan flag so
            # the turn-end render (below) and every idle refresh show 🔒 gate/yolo again, not a
            # lingering 🔒 plan. Cleared BEFORE the turn-end statusline trigger. (A freshly-armed
            # /plan for the NEXT turn re-shows 🔒 plan via the command refresh's ``plan_next``.)
            turn_rt.in_plan_turn = False
            # STATUSLINE T-SL-WIRE (design §3.1): turn END → flip the working ⚙️ marker OFF and
            # refresh ctx % (the context just grew, and the engine is still alive here — its
            # teardown for a driver_error/resume-failure happens AFTER this finally — so
            # _statusline_text's engine.context_percentage() reads the fresh figure). FOREGROUND-
            # ONLY (``turn_name``) so a background turn's end never stomps the foreground line.
            # In the finally + fully best-effort (RB1), so it fires on EVERY exit path (clean
            # end, mid-stream raise, cancel) and can never mask the turn's own exception.
            await self._maybe_update_statusline(
                chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=turn_name,
            )
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
                # ⭐ P11 T2 (B2+B3): the first turn of an ADOPTED session completed cleanly, so
                # the forked/continued id is now persisted (the result event's session_id landed
                # via _persist) and is OURS alone — clear the PERSISTED fork_pending so every
                # SUBSEQUENT resume of this project is an ordinary continue (never re-forking).
                # Cleared HERE (after a clean turn), NOT at resume-connect, so a restart between
                # connect and a successful turn STILL re-probes (the persisted id is still the
                # base id until the turn's result lands). Best-effort (RB1) — never crash the
                # turn's teardown over the write.
                turn_rt.attach_fork = False
                self._clear_fork_pending(chat_id, turn_name)

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
        # P11 T2: ALSO clear the persisted ``fork_pending`` here — the recovery drops the dead
        # base id, so the NEXT turn starts FRESH and persists a brand-new id that is OURS alone.
        # A leftover ``fork_pending`` would, after a restart, trigger a needless re-probe/fork of
        # the bot's OWN fresh session (self-healing, never a co-drive — there is no live base id
        # to corrupt — but untidy). Clear it wherever the dead id is cleared (mirrors the
        # resume-raises rebuild path, which already clears both). Must come BEFORE the
        # best-effort notice send below so a send failure can't leave the marker stale.
        if name is not None:
            self._clear_fork_pending(chat_id, name)
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

    # -- shutdown ------------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop every project's engine + cancel every live-mirror watch (idempotent).

        For a clean exit: cancels all read-only ``/watch`` tail tasks (P11 T3 — no mirror task
        outlives the bot) and stops every started engine across every chat. Best-effort
        throughout (RB1): a failure stopping one engine / cancelling one watch never blocks the
        rest.
        """
        # P11 T3: cancel every chat's live-mirror watch so no tail task survives shutdown.
        for chat_id in list(self._watches):
            self._cancel_watch(chat_id)
        for state in self._chats.values():
            for rt in state.runtimes.values():
                if rt.engine is not None:
                    try:
                        await rt.engine.stop()
                    except Exception:
                        log.exception("error stopping engine during shutdown")
