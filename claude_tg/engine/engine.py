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
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional, Sequence

if TYPE_CHECKING:
    # Type-only import (no runtime dependency — keeps the engine substrate-neutral): the body-free
    # activity snapshot returned by :meth:`Engine.last_activity` (OBSERVABILITY T2). The real
    # validation import is lazy, inside the method. ``ActivitySnapshot`` is a plain dataclass and
    # pulls in no SDK.
    from .adapter_sdk import ActivitySnapshot

from ..audit import (
    KIND_PLAN_DECISION,
    KIND_POLICY_EVENT,
    KIND_TOOL_DECISION,
    AuditEvent,
    AuditSink,
    audit_safe_summary,
)
from ..bash_policy import BashPolicyMatch, classify_bash
from ..permissions import PermissionPolicy, is_risky, path_needs_approval
from ..util import _now_iso, _redact_sid
from .pending import DEFAULT_BACKSTOP_SECONDS, PendingRegistry
from .substrate import Substrate
from .types import (
    DENIED_MESSAGE,
    AskEvent,
    Cancel,
    Decision,
    Event,
    ImageInput,
    PermissionDecision,
    PermissionEvent,
    PermissionVerdict,
    PlanEvent,
    PlanVerdict,
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
        audit_sink: AuditSink | None = None,
        bash_policy_mode: str = "off",
        bash_policy_extra_patterns: tuple[str, ...] = (),
    ) -> None:
        self._substrate = substrate
        self._send_timeout = send_timeout
        self._backstop_seconds = backstop_seconds
        # P13 T-BASH: the Bash command-policy mode + owner extra denylist patterns, layered
        # ADDITIVELY on the gate (the C2-residual guardrail). **Default ``"off"`` → NO policy**:
        # an Engine built without it (every pre-P13 construction + all existing tests) behaves
        # byte-for-byte as before — ``off`` is the current gate exactly. The bot's production
        # factory wires ``flag`` (the design default) from config. A non-Bash tool, or any tool
        # when the mode is ``off``, never touches the policy. The policy may only ESCALATE (an
        # auto-allow → a prompt, a prompt → a deny); it NEVER converts a would-prompt/would-deny
        # into an auto-allow (the load-bearing additive invariant — see on_tool_request).
        self._bash_policy_mode = bash_policy_mode
        self._bash_policy_extra_patterns = bash_policy_extra_patterns
        # P13 T-AUDIT: the optional, BODY-FREE audit sink the gate records every decision
        # to. **Default None → a NO-OP**: when unset, ``_record_*`` returns immediately, so
        # an Engine built without it (every pre-P13 construction + all 1288 tests) behaves
        # byte-for-byte as before — same pattern as the optional cwd/allowed_roots C2 params.
        # The production sink is a ChatBoundSink (stamps the chat id the substrate-neutral
        # engine does not know); it is best-effort (an audit write never breaks a turn, RB1).
        self._audit_sink = audit_sink
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
        # P14 T-FIRE ⭐ THE SECURITY CORE — the per-turn FORCE-GATE flag. False for every
        # normal (operator-typed) turn (so the interactive ``/yolo`` + allow-session grants
        # behave EXACTLY as before — zero behavior change). Set to True for the duration of a
        # PROACTIVE (scheduler-fired) turn by :meth:`send` (``proactive=True``) and reset in
        # its ``finally`` — turn-scoped, like ``_out_queue``. When set, :meth:`on_tool_request`
        # treats ``policy.yolo`` AND every allow-session grant as OFF for that turn, so a risky
        # tool ALWAYS holds for approval (and, unattended, the 60-min backstop auto-DENIES it —
        # RB4 fail-safe). It is NEVER persisted and NEVER mutates the policy object: the
        # operator's interactive ``/yolo``/grants survive untouched for their own later typed
        # turns. The per-project turn lock means a proactive and a normal turn never run on the
        # SAME engine at once, so this single per-engine flag has no cross-turn race (mirrors
        # ``_out_queue``'s single-in-flight-turn invariant). The make-or-break invariant: an
        # unattended fire can NEVER inherit allow-all.
        self._force_gate: bool = False

    # -- audit hook (P13 T-AUDIT — body-free, best-effort, no-op when unset) --

    def _record_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        decision: str,
    ) -> None:
        """Record a body-free ``tool_decision`` audit event for ``tool_name`` (SB3).

        Called at EVERY gate outcome in :meth:`on_tool_request` / :meth:`_permission_hold`
        — auto-allow (safe / live grant / ``/yolo``), the out-of-root and risky holds'
        resolved verdicts, and the fail-closed no-id deny — so a tool that auto-runs under
        ``/yolo`` (and never reaches the bot) is still audited. The summary is
        :func:`audit_safe_summary` — the STRONGLY body-free audit renderer that collapses
        BOTH body fields AND idents (``command`` / ``path`` / ``url`` / ``pattern``) to a
        length/shape, so a secret early in a Bash command is **never persisted to the durable
        log** (stricter than the ephemeral prompt's ``safe_input_summary``; BLOCKER 1). The
        session tag is :func:`_redact_sid` (never the raw resumable id). When no sink is wired
        this is a no-op (the floor holds). Best-effort: the sink swallows its own failures
        (RB1) — an audit write never breaks a turn.
        """
        sink = self._audit_sink
        if sink is None:
            return  # no-op default — behavior identical to pre-P13
        try:
            sink.record(
                AuditEvent(
                    ts=_now_iso(),
                    kind=KIND_TOOL_DECISION,
                    tool=tool_name,
                    summary=audit_safe_summary(tool_name, tool_input),
                    decision=decision,
                    session_tag=_redact_sid(self.session_id),
                )
            )
        except Exception:  # pragma: no cover - the sink is already best-effort
            log.debug("audit record (tool) failed (ignored)", exc_info=True)

    def _record_plan(self, decision: str) -> None:
        """Record a body-free ``plan_decision`` audit event (``approve`` / ``reject``).

        Emitted from :meth:`_answer_hold` for an ``ExitPlanMode`` verdict — NO plan text
        and NO reject feedback ride the record (SB3 structural: :class:`AuditEvent` has no
        body field), only the verdict + the redacted session tag. No-op when no sink is
        wired; best-effort otherwise (RB1).
        """
        sink = self._audit_sink
        if sink is None:
            return
        try:
            sink.record(
                AuditEvent(
                    ts=_now_iso(),
                    kind=KIND_PLAN_DECISION,
                    decision=decision,
                    session_tag=_redact_sid(self.session_id),
                )
            )
        except Exception:  # pragma: no cover - the sink is already best-effort
            log.debug("audit record (plan) failed (ignored)", exc_info=True)

    def _record_policy(self, action: str, tool_name: str, *, label: str | None = None) -> None:
        """Record a body-free ``policy_event`` for a Bash-policy outcome (P13 T-BASH).

        ``action`` is ``bash_policy_flag`` (flag mode escalated the prompt) or
        ``bash_policy_block`` (deny mode auto-denied). **Clean semantics (Codex QA non-block):**
        ``summary`` carries the action token PLUS the body-free matched-pattern ``label`` (e.g.
        ``"bash_policy_block (chmod-777-recursive)"``) — so ``/audit`` reads "a chmod-777 Bash
        command was denied" WITHOUT the command text — and ``decision`` carries the verdict
        token (``deny`` for both modes: flag denies on the no-id edge / on operator deny, deny
        mode auto-denies). The matched command's body-free summary is recorded SEPARATELY by
        the paired ``_record_tool`` call (the ``tool_decision``), via the STRICT
        :func:`audit_safe_summary` — so NO command text (raw or 160-char) is persisted on EITHER
        record (BLOCKER 1). No-op when no sink is wired; best-effort otherwise (RB1) — a
        policy-audit write never breaks a turn.
        """
        sink = self._audit_sink
        if sink is None:
            return
        summary = f"{action} ({label})" if label else action
        try:
            sink.record(
                AuditEvent(
                    ts=_now_iso(),
                    kind=KIND_POLICY_EVENT,
                    tool=tool_name,
                    summary=summary,
                    decision="deny",
                    session_tag=_redact_sid(self.session_id),
                )
            )
        except Exception:  # pragma: no cover - the sink is already best-effort
            log.debug("audit record (policy) failed (ignored)", exc_info=True)

    def _bash_policy_match(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> BashPolicyMatch | None:
        """Classify a Bash command against the policy — FAIL-CLOSED (P13 T-BASH).

        Returns a :class:`~claude_tg.bash_policy.BashPolicyMatch` when the RAW command matches
        the denylist (scanning ``tool_input["command"]`` verbatim — NOT the 160-char summary,
        so a long dangerous command can't slip past the truncation), else ``None``. **Only**
        consulted for ``Bash`` when the mode is not ``off`` (the caller guards both, so a
        non-Bash tool / ``off`` mode never reaches here — zero behavior change).

        **FAIL-CLOSED:** if :func:`classify_bash` raises (a policy bug, an unexpected input),
        this returns a synthetic ``error``/``classifier-error`` match — i.e. the command is
        treated as FLAGGED — never ``None``. A policy error therefore escalates (flag mode) or
        denies (deny mode) at the call site; it can **never** silent-allow (the SB6 invariant).
        Mutation-probe: make this swallow the exception and ``return None`` and the
        fail-closed test flips to an auto-allow and FAILS.
        """
        command = tool_input.get("command", "")
        try:
            return classify_bash(
                command if isinstance(command, str) else str(command),
                extra_patterns=self._bash_policy_extra_patterns,
            )
        except Exception:
            # FAIL-CLOSED: a classifier error is treated as a hit (escalate/deny), NEVER a
            # silent allow. Log body-free (no command) and synthesize a generic match.
            log.warning(
                "bash policy classifier raised for %s — failing closed (treat as flagged)",
                tool_name,
                exc_info=True,
            )
            return BashPolicyMatch(
                pattern="error",
                label="policy check failed (treated as dangerous)",
                severity="high",
            )

    # -- session id ----------------------------------------------------------

    @property
    def session_id(self) -> Optional[str]:
        """The current Claude session id (None before the substrate reports one)."""
        return self._substrate.session_id

    # -- ctx % for the statusline (STATUSLINE T-SL-CORE) ---------------------

    async def context_percentage(self) -> Optional[int]:
        """Best-effort % of the context window currently used, or ``None`` (design §2.1/§5 T5).

        Delegates to the substrate's ``context_percentage`` (live ``get_context_usage()`` →
        honest usage-derived fallback). The statusline shows ``🧠 ctx <X>%`` when this is an
        int and ``🧠 ctx —`` when it is ``None`` — NEVER a fabricated number. Read defensively
        via ``getattr`` so a substrate that predates this method (or a fake in a test) simply
        yields ``None`` (the additive-seam discipline, mirroring the optional ``fork`` keyword);
        the call is fully best-effort and NEVER raises — it is an observer off the turn's
        critical path (RB1).

        ⭐ **ASYNC (B1 fix):** the substrate awaits the SDK's coroutine ``get_context_usage()``,
        so this is async too. We accept either a coroutine (await it — the real path) or a plain
        ``int``/``None`` (a sync fake / a predating substrate), so every existing seam keeps
        working while the real awaited SDK percentage is actually read.
        """
        getter = getattr(self._substrate, "context_percentage", None)
        if getter is None:
            return None
        try:
            value = getter()
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # pragma: no cover - the substrate is already best-effort
            log.debug("context_percentage() failed (ignored)", exc_info=True)
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def last_model(self) -> Optional[str]:
        """The actual model id the substrate reports for this session, or ``None`` (statusline).

        Delegates to the substrate's ``last_model`` (the model the SDK actually used — captured
        from the ``init``/assistant/result messages). The statusline uses this as the model
        fallback so it shows the genuinely-running model instead of the literal ``default`` when
        no per-project override / ``CLAUDE_MODEL`` is configured. Read defensively via ``getattr``
        so a substrate that predates this method (or a fake in a test) simply yields ``None`` (the
        additive-seam discipline, mirroring :meth:`context_percentage`); pure + never raises (RB1).
        """
        getter = getattr(self._substrate, "last_model", None)
        if getter is None:
            return None
        try:
            value = getter()
        except Exception:  # pragma: no cover - the substrate is already best-effort
            log.debug("last_model() failed (ignored)", exc_info=True)
            return None
        return value if isinstance(value, str) and value.strip() else None

    def limit_status(self) -> Optional[tuple[str, Optional[int]]]:
        """The rolling session-limit signal ``(status, pct_or_None)``, or ``None`` (observability).

        Delegates to the substrate's ``limit_status`` (captured from each ``RateLimitEvent`` the
        SDK emits when the rolling rate-limit state changes). ``status`` is the normalized ``ok`` /
        ``approaching`` / ``limited``; the second element is the precise percent of the rolling
        limit when the SDK exposed one (⭐ SPIKE: ``RateLimitInfo.utilization``), else ``None`` (the
        statusline then shows the 🟢/🟡/🔴 badge). ``None`` when no signal has been seen — never a
        fabricated value. Read defensively via ``getattr`` so a substrate that predates this method
        (or a fake in a test) simply yields ``None`` (the additive-seam discipline, mirroring
        :meth:`last_model`); the shape is validated and pure + never raises — an observer off the
        turn's critical path (RB1).
        """
        getter = getattr(self._substrate, "limit_status", None)
        if getter is None:
            return None
        try:
            value = getter()
        except Exception:  # pragma: no cover - the substrate is already best-effort
            log.debug("limit_status() failed (ignored)", exc_info=True)
            return None
        # Validate the shape: a 2-tuple of (non-empty str status, int|None pct). Anything odd → None
        # (never propagate a malformed signal to the renderer).
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], str)
            and value[0].strip()
            and (
                value[1] is None
                or (isinstance(value[1], int) and not isinstance(value[1], bool))
            )
        ):
            return (value[0], value[1])
        return None

    def last_activity(self) -> Optional["ActivitySnapshot"]:
        """A body-free snapshot of what's running right now, or ``None`` when idle (observability T2).

        Delegates to the substrate's ``last_activity`` (the current-tool NAME + active-subagent
        type-names captured from each ``Task*`` / ``tool_use`` message — SB3, names only, never
        args/bodies). The activity line (T5) renders this; ``None`` means fully idle (never a
        fabricated snapshot). Read defensively via ``getattr`` so a substrate that predates this
        method (or a fake in a test) simply yields ``None`` (the additive-seam discipline, mirroring
        :meth:`limit_status`); the shape is validated (an ``ActivitySnapshot`` or ``None`` — anything
        odd → ``None``) and the call is pure + NEVER raises — an observer off the turn's critical
        path (RB1).
        """
        getter = getattr(self._substrate, "last_activity", None)
        if getter is None:
            return None
        try:
            value = getter()
        except Exception:  # pragma: no cover - the substrate is already best-effort
            log.debug("last_activity() failed (ignored)", exc_info=True)
            return None
        # Validate the shape: a real ActivitySnapshot or None — never propagate anything else.
        from .adapter_sdk import ActivitySnapshot  # lazy (no SDK import; a plain dataclass)

        return value if isinstance(value, ActivitySnapshot) else None

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

        # P14 T-FIRE ⭐ the FORCE-GATE for this turn. ``yolo_active`` is the policy's ``/yolo``
        # bit UNLESS this is a proactive turn (``_force_gate``), in which case it is forced
        # OFF — an unattended fire must never inherit allow-all. Everything below that consulted
        # ``self._policy.yolo`` directly now reads ``yolo_active`` so the proactive inversion is
        # honored in ONE place (the out-of-root bypass AND the Bash-policy/name-gate ordering).
        yolo_active = self._policy.yolo and not self._force_gate

        # --- P13 T-BASH: the Bash command policy (ADDITIVE, checked FIRST so it can override
        # grant/yolo for a MATCHED dangerous command — the C2-residual closure). It runs ONLY
        # for Bash and ONLY when the mode is not ``off`` (so a non-Bash tool, or any tool with
        # the policy off, is byte-for-byte the pre-P13 gate below — zero behavior change). The
        # match is FAIL-CLOSED (a classifier raise → treated as flagged; never silent-allow).
        #
        # The load-bearing INVARIANT: the policy may only ESCALATE. ``deny`` mode turns a
        # would-allow/would-prompt into a DENY; ``flag`` mode turns a would-AUTO-ALLOW (a prior
        # grant / /yolo) into a one-time PROMPT (and a would-prompt stays a prompt, just louder
        # + session-button-dropped). It NEVER converts a would-prompt/would-deny into an
        # auto-allow. A NON-matching Bash command falls straight through to today's gate (grant/
        # yolo still auto-allow it) — only a MATCHED command is escalated.
        if self._bash_policy_mode != "off" and tool_name == "Bash":
            bash_match = self._bash_policy_match(tool_name, tool_input)
            if bash_match is not None:
                if self._bash_policy_mode == "deny":
                    # Hard wall: auto-deny the matched command, OVERRIDING any grant / /yolo
                    # (the one place policy beats yolo — the owner opted into a hard wall).
                    # Audited as a policy block + a tool_decision deny (so /audit shows both
                    # the policy trip and the denied tool). Body-free.
                    log.info(
                        "bash policy DENY for %s (%s) — auto-denying (overrides grant/yolo)",
                        tool_name,
                        bash_match.pattern,
                    )
                    self._record_policy("bash_policy_block", tool_name, label=bash_match.label)
                    self._record_tool(tool_name, tool_input, "deny")
                    return decision_to_substrate(
                        PermissionVerdict(behavior="deny", message=DENIED_MESSAGE),
                        tool_input=tool_input,
                    )
                # flag mode: ESCALATE to a deliberate one-time prompt, OVERRIDING any grant /
                # /yolo for THIS command. A flag-mode hold still needs a tool_use_id to route
                # the verdict; if a flagged Bash command somehow arrives without one we CANNOT
                # open a resolvable hold, so we fail CLOSED and DENY (never auto-allow a flagged
                # command — the additive invariant holds even on this edge). Audited.
                if tool_use_id is None:
                    log.warning(
                        "flagged Bash command (%s) arrived with no tool_use_id; cannot route "
                        "approval — failing closed (deny)",
                        bash_match.pattern,
                    )
                    self._record_policy("bash_policy_flag", tool_name, label=bash_match.label)
                    self._record_tool(tool_name, tool_input, "deny")
                    return decision_to_substrate(
                        PermissionVerdict(behavior="deny", message=DENIED_MESSAGE),
                        tool_input=tool_input,
                    )
                log.info(
                    "bash policy FLAG for %s (%s) — escalating to a one-time prompt "
                    "(overrides grant/yolo)",
                    tool_name,
                    bash_match.pattern,
                )
                self._record_policy("bash_policy_flag", tool_name, label=bash_match.label)
                return await self._permission_hold(
                    tool_name,
                    tool_input,
                    tool_use_id,
                    bash_flag=True,
                    bash_flag_label=bash_match.label,
                )

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
        if not yolo_active and self._path_out_of_root(tool_name, tool_input):
            # Out-of-root + not yolo → hold for approval regardless of name/grant. A risky
            # tool with no tool_use_id still can't open a resolvable hold (fail closed →
            # deny, below); an out-of-root SAFE tool with no id would be vanishingly rare on
            # the live path but is handled the same fail-closed way. (Proactive: yolo_active
            # is forced False, so an out-of-root tool always re-prompts under a proactive turn
            # even if the project is /yolo'd — the §5.1 out-of-root fail-safe.)
            log.debug(
                "tool %s target is outside allowed_roots — requiring approval (C2/SB2)",
                tool_name,
            )
        elif not self._needs_approval(tool_name, tool_input):
            log.debug("policy allows tool %s without prompt", tool_name)
            # P13 T-AUDIT: record the AUTO-ALLOW (safe tool / live grant / /yolo) at the
            # chokepoint — this branch never reaches the bot, so the engine is the only
            # place a yolo/grant auto-allow can be audited.
            self._record_tool(tool_name, tool_input, "auto_allow")
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
            # P13 T-AUDIT: record the fail-closed deny (a risky tool we could not gate).
            self._record_tool(tool_name, tool_input, "deny")
            return decision_to_substrate(
                PermissionVerdict(behavior="deny", message=DENIED_MESSAGE),
                tool_input=tool_input,
            )

        return await self._permission_hold(tool_name, tool_input, tool_use_id)

    def _needs_approval(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Whether this tool must HOLD for approval — force-gate-aware (P14 T-FIRE ⭐).

        For a NORMAL turn this is exactly ``self._policy.needs_approval(...)`` — the
        unchanged P2 gate (``/yolo`` on → never; safe tool → never; live allow-session grant
        → never; else hold). For a PROACTIVE turn (``self._force_gate`` set) it treats BOTH
        ``/yolo`` AND every allow-session grant as OFF: a tool holds **iff it is risky**
        (:func:`~claude_tg.permissions.is_risky`), regardless of any stale bypass on the
        project's policy. So a risky tool fired unattended ALWAYS gates (and the 60-min
        backstop then auto-DENIES it — RB4), while a SAFE/read tool still auto-runs (proactive
        is useful for read-only checks).

        Crucially this does **not** mutate the persistent :class:`~claude_tg.permissions.
        PermissionPolicy` — the operator's interactive ``/yolo`` / grants are untouched and
        apply to their own later typed turns. The inversion is purely per-turn (it reads the
        turn-scoped ``_force_gate`` flag), so the make-or-break invariant holds: an unattended
        fire can never inherit allow-all.

        **Mutation-probe:** drop the ``self._force_gate`` guard (let a proactive turn fall
        through to ``self._policy.needs_approval``) and the force-gate test flips — a risky
        tool auto-allows under a yolo'd project — so the test FAILS, proving the gate is pinned.
        """
        if self._force_gate:
            # Proactive turn: ignore yolo + grants entirely. Risk is the ONLY criterion.
            return is_risky(tool_name, tool_input)
        return self._policy.needs_approval(tool_name, tool_input)

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

        # P13 T-AUDIT: record a body-free plan_decision for an ExitPlanMode verdict —
        # approve / reject (a backstop/cancel rejects → recorded as "reject"). NO plan text
        # and NO reject feedback are recorded (SB3 structural). Ask answers are NOT audited
        # (an answer is not a security decision and its content is the operator's). Recorded
        # here at the engine chokepoint so it is captured regardless of the bot's path.
        if tool_name == PLAN_TOOL:
            self._record_plan(_audit_plan_verdict(decision))

        # 3) Map the decision through the single load-bearing mapper (every [FLAG]).
        return decision_to_substrate(decision, tool_input=tool_input)

    async def _permission_hold(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        *,
        bash_flag: bool = False,
        bash_flag_label: str | None = None,
    ) -> SubstrateDecision:
        """Hold a risky tool for operator approval and map the verdict (ADR-003 §2/§4).

        **P13 T-BASH:** ``bash_flag`` / ``bash_flag_label`` (default ``False`` / ``None`` —
        unchanged for every non-policy hold) ride onto the injected :class:`PermissionEvent`
        so the render shows ``⚠️`` + the matched-pattern label and DROPS ``[Allow for
        session]``. A flagged hold can therefore only resolve to ``allow_once`` / ``deny`` —
        and because ``allow_session`` is impossible (no button), ``_verdict_for`` never records
        a session grant for a flagged command, so the NEXT dangerous command re-prompts too.

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
        # 1) Surface the prompt with its tool_use_id; the summary is body-free (SB3). A
        #    P13-flagged Bash command carries bash_flag (+ the body-free label) so the render
        #    shows ⚠️ + the pattern and drops the [Allow for session] button.
        self._inject(
            PermissionEvent(
                tool_name=tool_name,
                tool_input_summary=safe_input_summary(tool_name, tool_input),
                tool_use_id=tool_use_id,
                session_id=self.session_id,
                bash_flag=bash_flag,
                bash_flag_label=bash_flag_label,
            )
        )

        # 2) Hold on the SHARED registry until resolved (operator / backstop / cancel).
        decision = await self._pending.await_decision(tool_use_id, tool_name)

        # P13 T-AUDIT: record the RESOLVED verdict (body-free) for this held tool, covering
        # every resolution: an operator allow_once/allow_session/deny, the 60-min backstop
        # (a PermissionVerdict deny → "backstop_deny"), or a /cancel / turn cancel (a Cancel
        # → "cancel"). Recorded BEFORE the mapping so the audited verdict is the operator's
        # intent (allow_session is distinguished from allow_once, which decision_to_substrate
        # collapses). The grant side effect is still in _verdict_for below.
        self._record_tool(tool_name, tool_input, _audit_verdict(decision))

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

    async def resume(self, session_id: str, *, fork: bool = False) -> None:
        """Re-attach to an existing session by id (cwd-scoped — engine-owned, C6).

        **P11 T2 — ``fork``.** ``fork=False`` (the default) CONTINUES the same session id
        (every pre-P11 resume). ``fork=True`` resumes into a NEW session id with the
        transcript copied, NEVER writing to the resumed id — the safety primitive for
        attaching a session that is LIVE in another process (two writers on one ``(id, cwd)``
        silently corrupt the transcript). The forked id is reported by the substrate on the
        first turn (``self.session_id`` updates then), so the caller persists the forked id,
        not the base one.
        """
        # Pass ``fork`` to the substrate ONLY when forking, so a CONTINUE (the default, every
        # pre-P11 resume) calls ``resume(session_id)`` exactly as before — a substrate that
        # never needs to fork (and a fake that omits the kwarg) is unaffected; only the new
        # attach-fork path exercises the grown signature.
        if fork:
            await self._substrate.resume(session_id, fork=True)
        else:
            await self._substrate.resume(session_id)
        # SB3/H1: redacted tag only (the raw id is a credential — see _redact_sid).
        log.debug("engine resumed %s (fork=%s)", _redact_sid(self.session_id), fork)

    async def send(
        self,
        prompt: str,
        *,
        timeout: Optional[float] = None,
        images: Optional[Sequence[ImageInput]] = None,
        proactive: bool = False,
    ) -> AsyncIterator[Event]:
        """Send one operator turn; async-yield normalized events out.

        **P10 T1 — optional ``images``.** Defaults to ``None`` (the unchanged text turn);
        when supplied it is threaded straight through to the substrate's ``send`` so the
        turn is multimodal (prompt + pixels). The merge/inject/decision machinery below is
        identical for both — only the substrate's ``query`` argument differs.

        **P14 T-FIRE ⭐ — ``proactive`` FORCES THE GATE ON for this turn (the security core).**
        Defaults to ``False`` (every operator-typed turn — unchanged; ``/yolo`` + allow-session
        grants behave exactly as before). When ``True`` (a scheduler-fired turn — no human
        present), the per-turn :attr:`_force_gate` flag is set for the duration of this ``send``
        and reset in its ``finally``, so :meth:`on_tool_request` treats ``policy.yolo`` AND
        every allow-session grant as OFF: a risky tool ALWAYS holds (and, unattended, the 60-min
        backstop auto-DENIES it — RB4 fail-safe), while a safe/read tool still auto-runs. It is
        turn-scoped and NEVER mutates the policy (the operator's interactive bypass is untouched
        for their own later typed turns). The per-project turn lock guarantees a proactive turn
        and a normal turn never overlap on the SAME engine, so this single per-engine flag is
        race-free (same single-in-flight-turn invariant as ``_out_queue``).

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
        # P14 T-FIRE (Codex-QA, defensive): one turn at a time per Engine. The per-turn state
        # below (``_out_queue`` + the ``_force_gate`` flag) is single-flight — it is set here and
        # reset in the ``finally``, so a turn must fully end before the next starts. Production
        # guarantees this via ``StreamingSession``'s per-project turn lock (a proactive and a
        # normal turn never overlap on one engine), but assert it locally so a future caller that
        # tried to drive two concurrent turns on ONE engine fails LOUD here rather than silently
        # corrupting the force-gate / stream merge. (``_out_queue is None`` between turns.)
        assert self._out_queue is None, "Engine.send is single-flight: a turn is already in flight"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._out_queue = queue
        # P14 T-FIRE ⭐ arm the per-turn FORCE-GATE for a proactive turn (reset in the
        # finally). Set BEFORE the producer task starts so the decision callback (which fires
        # mid-``send`` from the substrate's can_use_tool) always observes it.
        self._force_gate = proactive
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
            # P14 T-FIRE: disarm the force-gate so a later turn on this (reused) engine is a
            # normal gated turn unless it too is proactive. Turn-scoped, like _out_queue.
            self._force_gate = False

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
# Small helpers (module-level + pure so they are trivially testable)
# ---------------------------------------------------------------------------


def _audit_verdict(decision: Decision) -> str:
    """Map a resolved permission decision to its body-free audit verdict string (P13).

    Mirrors how :meth:`Engine._permission_hold` resolves: an operator
    :class:`PermissionDecision` carries its own verdict (``allow_once`` / ``allow_session``
    / ``deny``); the 60-min backstop arrives as a :class:`PermissionVerdict` ``deny`` →
    ``backstop_deny``; a ``/cancel`` / turn cancel arrives as a :class:`Cancel` → ``cancel``.
    Any other shape (defensive — should not occur on this path) is recorded as ``deny`` (the
    fail-closed reading). Pure — carries no body, just a fixed verdict token.
    """
    if isinstance(decision, PermissionDecision):
        return decision.verdict  # allow_once | allow_session | deny
    if isinstance(decision, Cancel):
        return "cancel"
    if isinstance(decision, PermissionVerdict):
        # The registry's backstop resolves a held permission as a PermissionVerdict deny.
        return "backstop_deny" if decision.behavior == "deny" else "allow_once"
    return "deny"  # unexpected shape → fail-closed audit reading


def _audit_plan_verdict(decision: Decision) -> str:
    """Map a resolved plan decision to ``approve`` / ``reject`` (body-free; P13).

    A :class:`PlanVerdict` carries ``approve`` (→ ``approve`` / ``reject``); a backstop
    (:class:`PermissionVerdict` ``deny``) or a :class:`Cancel` is a non-approval → ``reject``
    (fail-closed). NO feedback text is read — only the verdict. Pure.
    """
    if isinstance(decision, PlanVerdict):
        return "approve" if decision.approve else "reject"
    return "reject"


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
