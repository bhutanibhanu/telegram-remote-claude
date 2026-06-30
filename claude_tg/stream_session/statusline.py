"""Statusline mixin — the per-project coalesced status line + the ONE pinned mobile statusline.

A behavior-preserving relocation of the statusline method-group out of the former single-file
``StreamingSession`` (see ``docs/features/core-refactor/design.md`` §6 T3). Two surfaces live
here:

* :meth:`_edit_status` — the per-project, per-turn coalesced status line edited in place (ADR-005
  D7), funneled through the chat's non-verbatim send gate (D8).
* the pinned mobile statusline (STATUSLINE T-SL-CORE, design §3.1/§4) — :meth:`_update_statusline`
  + its build/gate/pin helpers, foreground-only and fully best-effort (RB1).

⭐ The B2 foreground re-check (``_is_foreground(built_for)`` immediately before the raw
``edit``/``send``, with NO await between) is preserved EXACTLY — it is the make-or-break guard
against a stale line surviving a ``/switch``.

:class:`StatuslineMixin` holds the methods; they reach the foundation
(``_active_runtime``/``_resolve_project_*``/``_is_foreground``/``_chat``/``_gate``/``_gated_*``/
``_sleep``) through ``self`` at runtime via the composed
:class:`~claude_tg.stream_session.core.StreamingSession`'s MRO — so there is no module-level
import of ``core`` (no cycle). It imports only the leaf modules + the ``render`` leaf helpers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ..render import (
    RenderAction,
    format_statusline,
    model_short_label,
)
from .runtime import _ChatState, _ProjectRuntime
from .types import DeleteFn, EditFn, PinFn, SendFn, UnpinFn

#: The effort shown in the statusline when no per-project ``/effort`` override is set.
#: ``_resolve_project_effort`` returns ``None`` when unset — the deliberate asymmetry: no
#: ``effort`` kwarg is threaded into a default turn, so turn behavior is byte-for-byte unchanged.
#: But the owner wants the bar to ALWAYS show the current effort, and the SDK's own default
#: effort is ``high`` (documented in ``_resolve_project_effort``), so we DISPLAY ``high`` when
#: unset. Display-only — this never changes what is sent to the engine.
_DEFAULT_EFFORT_LABEL = "high"

if TYPE_CHECKING:
    # The foundation surface these methods consume — defined on ``StreamingSession`` (core.py),
    # not in this file. Declared here as bare ``Callable`` attribute annotations (NOT ``def``
    # stubs, which would create spurious override-compatibility checks against core's real
    # signatures) so the type-checker resolves ``self._active_runtime`` etc. on the mixin
    # without runtime cost (the composed instance carries them via the MRO). Behavior-neutral;
    # see design.md §1 "the one honest caveat".
    from collections.abc import Awaitable, Callable

    from ..render import ChatSendGate

log = logging.getLogger(__name__)


class StatuslineMixin:
    """Status-line surfaces (per-turn coalesced + the pinned bar), relocated intact from core.

    Mixed into :class:`~claude_tg.stream_session.core.StreamingSession` ahead of the base in the
    MRO. Every method here references the orchestration root's state/foundation through ``self``;
    the annotations below exist only for the type-checker.
    """

    if TYPE_CHECKING:
        _chat: Callable[[int], _ChatState]
        _gate: Callable[[_ChatState], ChatSendGate]
        _is_foreground: Callable[[int, Optional[str]], bool]
        _active_runtime: Callable[..., tuple[Optional[str], Optional[_ProjectRuntime]]]
        _resolve_project_model: Callable[[int, str], Optional[str]]
        _resolve_project_effort: Callable[[int, str], Optional[str]]
        _sleep: Callable[[float], Awaitable[None]]
        _gated_send: Callable[..., Awaitable[Optional[int]]]
        _gated_edit: Callable[..., Awaitable[None]]

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

    # -- the pinned mobile statusline (STATUSLINE T-SL-CORE, design §3.1/§4) --

    async def _maybe_update_statusline(
        self,
        chat_id: int,
        *,
        send: Optional[SendFn],
        edit: Optional[EditFn],
        pin: Optional[PinFn],
        unpin: Optional[UnpinFn],
        for_project: Optional[str] = None,
    ) -> None:
        """Refresh the pinned statusline IFF this is the chat's FOREGROUND project (T-SL-WIRE).

        ⭐ **The make-or-break wiring invariant (design §3.1).** The pinned line reflects the
        chat's ACTIVE (foreground) project — the one the operator is watching. A BACKGROUND
        concurrent turn (a non-active project running under P5 concurrency) must NEVER rewrite
        the line, or two concurrent turns would stomp each other's state and the single pinned
        line would stop describing "what you're looking at". So the turn-start / turn-end
        triggers route through HERE, which:

        * **skips** when ``for_project`` is not the chat's foreground (:meth:`_is_foreground`) —
          a background turn leaves the foreground line untouched;
        * **skips** when any closure is missing (a caller/test that didn't inject pin/unpin —
          back-compat: the statusline simply isn't driven, the turn is unaffected);
        * otherwise delegates to :meth:`_update_statusline` (itself fully best-effort, RB1).

        ``for_project=None`` means "the caller already knows this is foreground" (the command
        paths: ``/switch`` + the knob setters always act on the active project), so the
        foreground gate is bypassed but the closure-presence gate still applies. The whole call
        is wrapped so a foreground-check / build error can never escape to the turn (RB1) — the
        statusline is an observer off the turn's critical path.
        """
        if send is None or edit is None or pin is None or unpin is None:
            return  # no closures injected (a test / a caller that didn't wire them) → no-op.
        try:
            if for_project is not None and not self._is_foreground(chat_id, for_project):
                # ⭐ Foreground-only: a BACKGROUND turn never rewrites the foreground line.
                return
            await self._update_statusline(
                chat_id, send=send, edit=edit, pin=pin, unpin=unpin
            )
        except Exception:
            # RB1: a foreground-check / dispatch error must never break the turn (the inner
            # _update_statusline already swallows its own I/O; this guards the gate itself).
            log.debug("statusline trigger failed for chat (ignored)", exc_info=True)

    async def _statusline_text(self, chat_id: int) -> Optional[tuple[str, str]]:
        """Build the CURRENT statusline body + the project it was built FOR (``(text, name)``).

        Reads the chat's ACTIVE (foreground) project's live state — the worktree NAME, the
        effective model + effort, the permission mode, the working/idle marker, and the ctx %
        — and renders it through :func:`~claude_tg.render.format_statusline`. Foreground-only
        (design §3.1): a background project's turn never rewrites the line, so the single pinned
        line always describes "what you're looking at".

        **Read-only / fail-safe (RB1):** resolves the active runtime with ``create_default=
        False`` so a statusline refresh NEVER creates a project as a side effect; with no active
        project (nothing run yet) returns ``None`` (nothing to show). Each field read is
        defensive — a missing store / odd record / ctx call that raises degrades to a safe
        default (``ctx —``, ``gate``) rather than raising.

        ⭐ **Returns ``(text, built_for)``** — the rendered body AND the project NAME it describes
        — or ``None`` when there is no foreground project. The caller uses ``built_for`` for the
        FINAL pre-write foreground re-check (B2): the ctx ``await`` below is a switch window, so
        the only safe guarantee is "the project this text was built for is STILL foreground at the
        instant just before the write" — a sync check the write helpers do with no await between
        it and the ``edit``/``send``.

        ⭐ **ASYNC (B1 fix):** the ctx % comes from ``Engine.context_percentage()`` which AWAITS
        the SDK's coroutine ``get_context_usage()`` — so this method is async and awaits it. The
        await is still fully best-effort (any raise → ``ctx —``, never a fabricated number); it
        is the only await here (every other field is a pure in-memory read).

        * ``worktree`` — the active project NAME (SB4-validated charset, so inert — SB3).
        * ``model`` — :meth:`_resolve_project_model` (override → ``CLAUDE_MODEL``), falling back
          to the live engine's ``last_model`` (the model the SDK actually used) when neither is
          configured, reduced by :func:`model_short_label`. Only ``default`` if all are unknown.
        * ``effort`` — :meth:`_resolve_project_effort`, defaulting to ``high`` (the SDK default)
          for DISPLAY when unset so the bar always shows the current effort (turns unchanged).
        * ``mode`` — ``yolo`` if the project's policy is allow-all, else ``plan`` if a plan turn
          is RUNNING (``in_plan_turn`` — B3) OR a ``/plan`` is armed for the next turn
          (``plan_next``), else ``gate`` (the fail-closed default).
        * ``working`` — the per-project status enum is a working state (``running`` /
          ``awaiting_*`` / ``queued``) vs ``idle``.
        * ``ctx_pct`` — the live engine's :meth:`~claude_tg.engine.engine.Engine.context_percentage`
          (``None`` → ``ctx —``, never a fabricated number).
        """
        name, rt = self._active_runtime(chat_id, create_default=False)
        if name is None or rt is None:
            return None
        worktree = name  # the SB4-validated project name (no path; SB3-inert).
        # Model: per-project override (/fast·/deep) → CLAUDE_MODEL → else the model the SDK
        # ACTUALLY reported for the live session (engine.last_model) → else "default". The live
        # fallback means a session with no configured model shows its REAL model (e.g. 🤖 opus)
        # from the first turn instead of the literal word "default". getattr-guarded so a fake /
        # predating engine simply yields no live model (additive-seam discipline; RB1 best-effort).
        model_id = self._resolve_project_model(chat_id, name)
        if not model_id and rt.engine is not None:
            getter = getattr(rt.engine, "last_model", None)
            if callable(getter):
                try:
                    live = getter()
                    if isinstance(live, str) and live.strip():
                        model_id = live
                except Exception:  # pragma: no cover - a telemetry read never breaks the line
                    pass
        model_label = model_short_label(model_id)
        # Effort: the per-project override if set, else the SDK default (high) — the bar always
        # shows the current effort (display-only; the turn-threading resolver is unchanged).
        effort = self._resolve_project_effort(chat_id, name) or _DEFAULT_EFFORT_LABEL
        # mode: yolo (allow-all) wins; else plan — either a plan turn is RUNNING NOW
        # (``in_plan_turn``, B3 — ``plan_next`` is already consumed by the time the turn streams)
        # OR a ``/plan`` is armed for the NEXT turn (``plan_next``); else the fail-closed gate.
        if bool(getattr(rt.policy, "yolo", False)):
            mode = "yolo"
        elif bool(getattr(rt, "in_plan_turn", False)) or bool(getattr(rt, "plan_next", False)):
            mode = "plan"
        else:
            mode = "gate"
        working = rt.status in ("running", "awaiting_approval", "awaiting_answer", "awaiting_plan", "queued")
        ctx_pct: Optional[int] = None
        engine = rt.engine
        if engine is not None:
            try:
                # ⭐ The ONLY await in this builder — and a /switch window (B2): the returned
                # ``built_for`` lets the write helpers re-check foreground AFTER this await.
                ctx_pct = await engine.context_percentage()
            except Exception:  # pragma: no cover - the engine call is already best-effort (RB1)
                ctx_pct = None
        body = format_statusline(
            worktree=worktree,
            model_label=model_label,
            effort=effort,
            ctx_pct=ctx_pct,
            mode=mode,
            working=working,
        )
        return body, name

    async def _update_statusline(
        self,
        chat_id: int,
        *,
        send: SendFn,
        edit: EditFn,
        pin: PinFn,
        unpin: UnpinFn,
    ) -> None:
        """Refresh the chat's ONE pinned statusline — send+pin on first use, edit thereafter.

        STATUSLINE T-SL-CORE (design §3.1/§4). Builds the current foreground statusline body
        (:meth:`_statusline_text`) and reconciles it with the chat's pinned line:

        * **identical text** → skip entirely (no I/O — a no-op edit raises "message is not
          modified" AND wastes a send slot; mirrors :meth:`_edit_status`).
        * **first update** (no id held) → SEND the body then PIN it with the notification
          DISABLED (a silent pin — design §3.1); store the id + text.
        * **subsequent update** → EDIT in place only (no re-pin, no re-send; a pinned message
          edited in place stays pinned and silent).
        * **edit FAILURE** (the operator unpinned/deleted it → "message to edit not found", an
          API hiccup, too old) → ORPHAN RECOVERY: clear the stored id, best-effort UNPIN the
          stale one (the "one pinned message" invariant — Telegram's current pin is the newest,
          so the bar self-corrects), then re-SEND + re-PIN a fresh line (mirrors the orphaned
          status-line recovery in :meth:`_edit_status`).

        **⭐ RB1 — a pin/edit/send failure NEVER breaks or wedges a turn.** This is an observer
        OFF the turn's critical path: the WHOLE body is wrapped so ANY exception (a raising
        ``send``/``edit``/``pin``/``unpin``, a build error) is logged at debug and swallowed —
        the caller (the turn loop / a command) is unaffected. **RB5** — every send/edit funnels
        through the per-chat gate as the **non-verbatim** kind (:meth:`_gated_send`/
        :meth:`_gated_edit`), so the statusline can never flood and never starves a real
        answer/prompt. **One id invariant** — exactly one ``statusline_message_id`` is ever held
        per chat; we only ever edit it, and on recovery re-point it.

        ``send``/``edit``/``pin``/``unpin`` are injected by ``bot.py`` (the same pattern as the
        existing send/edit/delete closures) targeting THIS chat — so the line is SB1-confined to
        the operator's allowlisted chat (no new outbound surface).

        **⭐ B2 fix — no stale line across a ``/switch`` (the FINAL guard).** The body is built
        from the FOREGROUND project's state, but BOTH the gate's wait AND the ctx ``await`` inside
        the rebuild are ``/switch`` windows. So the gated write helpers (1) REBUILD the body from
        CURRENT state after the wait, then (2) do a FINAL **synchronous** foreground re-check — is
        the project the rebuilt text was BUILT FOR still the chat's active/foreground? — with NO
        await between that check and issuing the ``edit``/``send``. If a ``/switch`` happened
        during ANY await, ``built_for`` is no longer foreground → the stale write is SKIPPED (the
        ``/switch``'s own statusline trigger writes the correct line — no loop, no stale write).
        **Pin-retry** — a send that succeeded while its pin RAISED leaves the line UNPINNED
        (``statusline_pinned`` False); a later update RETRIES the pin even if the text is
        unchanged, so a transient pin failure self-heals instead of sticking unpinned forever.
        """
        try:
            built = await self._statusline_text(chat_id)  # async (B1: awaits the SDK ctx %)
            if not built:
                return  # no foreground project to describe — nothing to pin/edit.
            body, _built_for = built  # body for the skip/decision; the helpers rebuild + re-check
            state = self._chat(chat_id)
            # Pin-retry: if we hold a sent id whose pin FAILED, retry the pin even on identical
            # text (the identical-text skip below would otherwise leave it unpinned forever).
            if (
                state.statusline_message_id is not None
                and not state.statusline_pinned
                and body == state.statusline_text
            ):
                await self._statusline_pin(state, state.statusline_message_id, pin=pin)
                return
            if body == state.statusline_text:
                # Identical to what's pinned — skip BEFORE the gate so an unchanged refresh
                # never consumes a send slot and never triggers a no-op "not modified" edit.
                return
            if state.statusline_message_id is None:
                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
                return
            try:
                # B2: rebuild the body AFTER the gate wait (inside _statusline_gated_edit) so a
                # /switch during the wait writes the now-current line, never the stale snapshot.
                await self._statusline_gated_edit(
                    chat_id, state, state.statusline_message_id, edit=edit
                )
            except Exception:
                # Orphan recovery (design §4 RB1): the pinned line is gone (unpinned/deleted by
                # the operator) / too old / an API hiccup. Clear the dead id, best-effort UNPIN
                # the stale one (one-pin invariant), then re-send + re-pin a fresh line. The
                # turn is unaffected either way (this whole method is best-effort).
                log.debug("statusline edit failed for chat; re-sending + re-pinning", exc_info=True)
                stale_id = state.statusline_message_id
                state.statusline_message_id = None
                state.statusline_text = None
                state.statusline_pinned = False
                try:
                    await unpin(message_id=stale_id)
                except Exception:
                    log.debug("stale statusline unpin failed (ignored)", exc_info=True)
                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
        except Exception:
            # ⭐ The make-or-break swallow (RB1): NOTHING the statusline does may escape to the
            # turn. A build/gate/closure failure is logged at debug and dropped — the next state
            # change re-creates the line.
            log.debug("statusline update failed for chat (ignored)", exc_info=True)

    async def _statusline_gated_edit(
        self, chat_id: int, state: _ChatState, message_id: int, *, edit: EditFn
    ) -> None:
        """Edit the pinned line through the gate, REBUILDING + RE-CHECKING foreground (B2).

        Reserves the per-chat gate slot and awaits its wait (non-verbatim — RB5), THEN re-derives
        the statusline body + the project it was built for from CURRENT state. Two awaits precede
        the write — the gate wait AND the ctx ``await`` inside :meth:`_statusline_text` — both
        ``/switch`` windows. So immediately before the raw edit we do a FINAL **synchronous**
        foreground re-check (``_is_foreground(built_for)``) with NO await between it and the
        ``edit``: if a ``/switch`` happened during ANY await, ``built_for`` is no longer
        foreground → SKIP (the switch's own trigger writes the correct line — no stale write, no
        loop). An empty rebuild (foreground vanished — e.g. ``/rm``) or identical text also skips.
        A raise propagates to the caller's orphan-recovery (the message may be gone).
        """
        wait = self._gate(state).reserve(verbatim=False)
        if wait > 0:
            await self._sleep(wait)
        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
        if built is None:
            return  # foreground vanished mid-wait → no stale write.
        body, built_for = built
        if body == state.statusline_text:
            return  # nothing changed → no no-op "not modified" edit.
        # ⭐ FINAL sync guard (B2): only write if the project this body describes is STILL the
        # chat's foreground at THIS instant — no await between here and the edit, so a /switch
        # during any preceding await is caught. A stale body (built_for switched away) is dropped.
        if not self._is_foreground(chat_id, built_for):
            return
        await edit(message_id=message_id, text=body, parse_mode="HTML")
        state.statusline_text = body

    async def _statusline_send_and_pin(
        self,
        chat_id: int,
        state: _ChatState,
        *,
        send: SendFn,
        pin: PinFn,
    ) -> None:
        """Send the statusline body (gated, non-verbatim) then PIN it silently (design §3.1).

        The first-use + orphan-recovery primitive: reserve the gate slot, await its wait, THEN
        rebuild the body + the project it was built for from CURRENT state. Two awaits precede the
        send — the gate wait AND the ctx ``await`` inside :meth:`_statusline_text` — both
        ``/switch`` windows (B2). So immediately before the raw send we do a FINAL **synchronous**
        foreground re-check (``_is_foreground(built_for)``) with NO await between it and the
        ``send``: a ``/switch`` during any preceding await makes ``built_for`` no longer
        foreground → SKIP (the switch's own trigger sends the correct line — no stale send, no
        loop). A best-effort silent pin follows (``disable_notification=True`` — a pin must never
        re-ping). The id/text are stored ONLY when the send returns an id (so a ``None`` send does
        not leave a half-set state). A PIN failure is swallowed (RB1) AND records
        ``statusline_pinned=False`` so the next update retries the pin. Called from
        :meth:`_update_statusline` inside its best-effort guard, so a raising ``send`` propagates
        to that guard's swallow.
        """
        wait = self._gate(state).reserve(verbatim=False)
        if wait > 0:
            await self._sleep(wait)
        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
        if built is None:
            return  # foreground vanished mid-wait — nothing to send.
        body, built_for = built
        # ⭐ FINAL sync guard (B2): only send if the project this body describes is STILL the
        # chat's foreground at THIS instant — no await between here and the send, so a /switch
        # during any preceding await (the gate wait OR the ctx await) is caught and the stale
        # send is dropped (the switch's own statusline trigger sends the correct line).
        if not self._is_foreground(chat_id, built_for):
            return
        mid = await send(text=body, reply_markup=None, parse_mode="HTML")
        if mid is None:
            # The send produced no id (a closure that returns None) — don't store a half state;
            # the next update will try a fresh send.
            return
        state.statusline_message_id = mid
        state.statusline_text = body
        state.statusline_pinned = False  # not pinned until the pin call below succeeds.
        await self._statusline_pin(state, mid, pin=pin)

    async def _statusline_pin(self, state: _ChatState, message_id: int, *, pin: PinFn) -> None:
        """Best-effort SILENT pin of the statusline message; record whether it stuck (pin-retry).

        A pin must never re-ping (``disable_notification=True``) and never break the turn (RB1).
        On success ``statusline_pinned`` is set True; on failure it stays/!becomes False and is
        swallowed — :meth:`_update_statusline` then RETRIES the pin on the next update (even with
        unchanged text) so a transient pin failure self-heals instead of leaving the line unpinned
        forever. Only the pinned-bar placement is ever at stake here, never the turn.
        """
        try:
            await pin(message_id=message_id, disable_notification=True)
            state.statusline_pinned = True
        except Exception:
            state.statusline_pinned = False
            log.debug("statusline pin failed (will retry on next update)", exc_info=True)
