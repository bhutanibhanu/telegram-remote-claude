"""Callbacks mixin — the LOCK-FREE decision-resolution surface (ADR-005 D3/D5/D7/D9).

A behavior-preserving relocation of the callback/resolve + pending-index method-group out of the
former single-file ``StreamingSession`` (see ``docs/features/core-refactor/design.md`` §6 T4).
Three surfaces live here:

* the **resolve path** — :meth:`resolve_callback` + the ``_resolve_*`` family (switch/attach/ask/
  plan/permission/free-text) + :meth:`resolve_to`. Intentionally lock-free: it resolves the
  pending decision a HELD turn is awaiting (parked inside ``engine.send``), so it must run
  concurrently with that turn — taking the turn lock would deadlock it.
* **cancel** — :meth:`handle_cancel` + :meth:`_cancel_project` (the lock-free abort; placed here
  per design §6 T4 as part of the decision-resolution surface). ``_cancel_project`` calls
  ``self._drain_queued`` — a :class:`~claude_tg.stream_session.concurrency.ConcurrencyMixin`
  method — through ``self`` at runtime (MRO), so there is NO module import edge between the two
  mixins (no cycle).
* the **pending-request index** (ADR-005 D3) — ``_register_pending``/``_drop_pending``/… and the
  per-runtime free-text markers (``_clear_runtime_text``/``_clear_runtime_turn_state``).

:class:`CallbacksMixin` holds the methods; they reach core's foundation
(``_chat``/``_chats``/``store``/``_resolve_runtime_key``/``_active_runtime``) and the sibling
concurrency mixin (``_drain_queued``) through ``self`` at runtime via the composed
:class:`~claude_tg.stream_session.core.StreamingSession`'s MRO — so there is no module-level
import of ``core`` (no cycle). It imports only the leaf modules + the ``engine``/``render`` leaf
types.

The few intra-group ``@staticmethod`` calls that the original made by class name
(``StreamingSession._prune_reply_to`` / ``StreamingSession._clear_runtime_text``) are rewritten to
``CallbacksMixin.…`` — both targets relocated here, so the same-module class name resolves them
with no ``core`` import. Behavior-identical (same static functions).
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any, Optional

from ..engine import (
    AskEvent,
    Engine,
    Event,
    PermissionDecision,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
)
from ..render import (
    Callback,
    answers_from_ask,
    decode_callback,
)
from ..util import _redact_sid
from .runtime import _ChatState, _pending_kind_of, _PendingRef, _ProjectRuntime
from .types import (
    _PERMISSION_NOTES,
    _PERMISSION_VERDICTS,
    CallbackOutcome,
)

if TYPE_CHECKING:
    # The foundation surface these methods consume — defined on ``StreamingSession`` (core.py),
    # or on the sibling ``ConcurrencyMixin`` (``_drain_queued``). Declared here as bare attribute
    # annotations (``Callable`` for the consumed methods, NOT ``def`` stubs — those would create
    # spurious override-compatibility checks against the real signatures) so the type-checker
    # resolves ``self._active_runtime`` etc. on the mixin without runtime cost (the composed
    # instance carries them via the MRO). Behavior-neutral; see design.md §1.
    from collections.abc import Callable

log = logging.getLogger(__name__)


class CallbacksMixin:
    """Decision-resolution + the pending-request index (ADR-005), relocated intact from core.

    Mixed into :class:`~claude_tg.stream_session.core.StreamingSession` ahead of the base in the
    MRO. Every method here references the orchestration root's state/foundation (and the sibling
    concurrency mixin's ``_drain_queued``) through ``self``; the annotations below exist only for
    the type-checker.
    """

    if TYPE_CHECKING:
        store: Any
        _chats: dict[int, _ChatState]
        _chat: Callable[[int], _ChatState]
        _resolve_runtime_key: Callable[..., Optional[str]]
        _active_runtime: Callable[..., tuple[Optional[str], Optional[_ProjectRuntime]]]
        # Defined on ConcurrencyMixin (sibling) — reached via self at runtime through the MRO.
        _drain_queued: Callable[[_ChatState, _ProjectRuntime], int]

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
        # T6/P9: a [Open <project>] switch tap routes by PROJECT NAME, not a tool_use_id, and
        # touches no pending hold — handle it BEFORE the pending-index lookup. The bot has
        # already enforced SB1 (the _authorized recheck in on_callback) before reaching here,
        # so an unauthorized tap never gets this far. The session does NOT mutate the store
        # for a switch (the bot's /switch helper does the SB2 path re-validation + the store
        # write); we just decode + return the target name. A switch never resolves a decision.
        if decoded.kind == "switch":
            return self._resolve_switch(chat_id, decoded)
        # P11 T2: an [Attach] tap routes by SESSION ID (not a tool_use_id) and touches no
        # pending hold — handle it BEFORE the pending-index lookup, like switch. The bot has
        # already enforced SB1 (the _authorized recheck) before reaching here. We decode +
        # return the session id; the bot calls attach_session (which does the SB2 cwd check +
        # fork-vs-continue). An attach never resolves a held decision.
        if decoded.kind == "attach":
            return self._resolve_attach(chat_id, decoded)
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

    def _resolve_switch(self, chat_id: int, decoded: Callback) -> "CallbackOutcome":
        """Route a ``[Open <project>]`` switch tap (T6/P9) — decode-only; bot does the switch.

        The switch tap carries the TARGET PROJECT NAME (``decoded.switch_to``), already
        lexically validated by :func:`~claude_tg.render.decode_callback` (the SB4 name shape).
        This returns a :class:`CallbackOutcome` with ``switch_to`` set so the bot's
        ``on_callback`` performs the actual switch through its shared ``/switch`` helper —
        which does the SB2 path re-validation (the target project's cwd must still be within
        the permitted roots) the session has no access to. The session deliberately does NOT
        mutate the store here (no path check available) and resolves no held decision (a
        switch is navigation, not an answer). With no store there is nothing to switch within
        (single implicit project) → a benign no-op note. The bot's SB1 ``_authorized`` recheck
        already gated this call (a non-allowlisted tap never reaches the session).
        """
        name = decoded.switch_to
        if not name:
            return CallbackOutcome(handled=False, note="ignored")
        if self.store is None:
            # No registry to switch within (single implicit project) — benign no-op (RB1).
            return CallbackOutcome(handled=False, note="no projects")
        # Hand the (decoded) name to the bot to switch + path-revalidate; the toast is set by
        # the bot after the switch. ``handled`` is True (we recognized + routed the tap).
        return CallbackOutcome(handled=True, note=f"Opening {name}…", switch_to=name)

    def _resolve_attach(self, chat_id: int, decoded: Callback) -> "CallbackOutcome":
        """Route an ``[Attach]`` tap (P11 T2) — decode-only; the bot calls ``attach_session``.

        The attach tap carries the TARGET SESSION ID (``decoded.attach_session_id``), already
        lexically validated by :func:`~claude_tg.render.decode_callback` (the session-id shape).
        This returns a :class:`CallbackOutcome` with ``attach_session_id`` set so the bot's
        ``on_callback`` performs the adopt through :meth:`attach_session` — which does the SB2
        cwd confinement + the fork-vs-continue decision (the same code path ``/attach`` uses).
        The session does the actual work in ``attach_session``; this is just the decode + route
        (mirroring ``_resolve_switch``). With no store there is nothing to attach into (single
        implicit project) → a benign no-op note. The bot's SB1 ``_authorized`` recheck already
        gated this call. An attach resolves no held decision.
        """
        sid = decoded.attach_session_id
        if not sid:
            return CallbackOutcome(handled=False, note="ignored")
        if self.store is None:
            # No registry to attach into (single implicit project) — benign no-op (RB1).
            return CallbackOutcome(handled=False, note="no projects")
        return CallbackOutcome(handled=True, note="Attaching…", attach_session_id=sid)

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

        **P9 styling.** The returned reply names the project as ``<b>{html.escape(name)}</b>``
        and the bot (``cmd_to``) sends it ``parse_mode="HTML"`` — uniform with every other
        name-bearing operator reply. ``name`` is operator input here (it may have FAILED the
        runtime lookup), so escaping is both consistency AND defense-in-depth.
        """
        state = self._chats.get(chat_id)
        if state is None:
            return (
                f"❌ No project named <b>{html.escape(name, quote=False)}</b> "
                "is awaiting a reply."
            )
        key = self._resolve_runtime_key(state.runtimes, name)
        rt = state.runtimes.get(key) if key is not None else None
        if rt is None or rt.awaiting_text_for is None:
            # Unknown name, or the project has no pending "Other"/reject to answer. Clear,
            # body-free no-op — do NOT fall back to the most-recent default (never misroute).
            return (
                f"❌ <b>{html.escape(name, quote=False)}</b> is not awaiting a free-text reply "
                "(tap “Other”/“Reject” on its prompt first)."
            )
        self._resolve_free_text(state, chat_id, key, rt, text)
        # ``key`` is non-None here (rt is None whenever key is None, and that path returned
        # above) — narrow for mypy. It is the stored (SB4-validated) project key; escape it
        # uniformly anyway (consistency + defense-in-depth).
        assert key is not None
        return f"✅ Sent your reply to <b>{html.escape(key, quote=False)}</b>."

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
        CallbacksMixin._prune_reply_to(state, tool_use_id)
        ref = state.pending_index.pop(tool_use_id, None)
        if ref is None:
            return
        rt = state.runtimes.get(ref.project_name)
        if rt is not None and rt.awaiting_text_for == tool_use_id:
            CallbacksMixin._clear_runtime_text(rt)

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
            CallbacksMixin._prune_reply_to(state, tuid)
        rt = state.runtimes.get(project_name)
        if rt is not None and rt.awaiting_text_for in doomed:
            CallbacksMixin._clear_runtime_text(rt)

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
        CallbacksMixin._clear_runtime_text(rt)
