"""Activity mixin — the TRANSIENT "what's running right now" line (OBSERVABILITY T5).

A best-effort, foreground-only, throttled message lifecycle modelled EXACTLY on
:class:`~claude_tg.stream_session.statusline.StatuslineMixin` (the blueprint for a gated
message that is posted, edited-in-place, and removed — see design.md §3 + progress.md T5):

* :meth:`_render_activity` — a PURE, body-free, SHORT line from an
  :class:`~claude_tg.engine.adapter_sdk.ActivitySnapshot` (the current tool NAME + active
  subagent TYPE-names, SB3 — never args/paths/prompts). ``None`` when there is nothing to show.
* :meth:`_maybe_update_activity` — read the FOREGROUND engine's ``last_activity()``
  (getattr/try-guarded → ``None``), render, then POST the message on first activity or EDIT it
  in place thereafter — **skip-identical** (no edit when the body is unchanged) AND a
  **time-throttle** (≲1 edit/sec; a change that lands inside the interval is coalesced — the
  in-memory state stays current and the next change past the interval shows it). Foreground-only,
  with the B2 SYNC foreground re-check immediately before any write (no await between), and the
  total RB1 swallow (any send/edit failure / a raising ``last_activity()`` never breaks a turn).
* :meth:`_finalize_activity` — at turn end, best-effort DELETE the transient message and clear its
  id/throttle state. No lingering ⚙️, and NOT a per-turn "done" footer (the owner disliked that —
  the pinned statusline is the persistent summary).

⭐ The B2 foreground re-check (``_is_foreground(built_for)`` immediately before the raw
``edit``/``send``, with NO await between) is preserved EXACTLY, as in the statusline — it is the
guard against a stale line surviving a ``/switch``.

:class:`ActivityMixin` reaches the foundation
(``_chat``/``_gate``/``_sleep``/``_is_foreground``/``_active_runtime``/``_clock``) through
``self`` at runtime via the composed
:class:`~claude_tg.stream_session.core.StreamingSession`'s MRO — so there is no module-level
import of ``core`` (no cycle). It does its OWN gate reservation (``_gate(state).reserve(verbatim=
False)`` + ``await self._sleep(wait)``) and raw ``send``/``edit`` rather than going through the
``_gated_send``/``_gated_edit`` helpers — it needs the B2 SYNC foreground re-check to land
between the awaited gate wait and the raw write (no await between), which the gated helpers don't
expose. The ``TYPE_CHECKING`` block declares exactly that consumed surface for the type-checker
only (behavior-neutral; mirrors the statusline mixin's discipline).
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Optional

from .runtime import _ChatState
from .types import DeleteFn, EditFn, SendFn

#: The minimum interval (seconds) between two activity-line EDITS — the time-throttle that
#: coalesces a rapid tool/subagent burst into ≲1 edit/sec (within Telegram's edit limits + the
#: per-chat send gate). A change landing inside this window is SKIPPED (the in-memory state stays
#: current; the next change past the interval shows it). The first POST is never throttled.
_ACTIVITY_EDIT_INTERVAL = 1.0

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..engine.adapter_sdk import ActivitySnapshot
    from ..render import ChatSendGate
    from .runtime import _ProjectRuntime

log = logging.getLogger(__name__)


class ActivityMixin:
    """The transient activity-line surface (post-at-first-activity / edit-throttled / remove@end).

    Mixed into :class:`~claude_tg.stream_session.core.StreamingSession`. Every method references
    the orchestration root's state/foundation through ``self``; the annotations below exist only
    for the type-checker (mirroring :class:`StatuslineMixin`).
    """

    if TYPE_CHECKING:
        _chat: Callable[[int], _ChatState]
        _gate: Callable[[_ChatState], ChatSendGate]
        _is_foreground: Callable[[int, Optional[str]], bool]
        _active_runtime: Callable[..., tuple[Optional[str], Optional[_ProjectRuntime]]]
        _sleep: Callable[[float], Awaitable[None]]
        _clock: Callable[[], float]

    @staticmethod
    def _render_activity(snapshot: Optional["ActivitySnapshot"]) -> Optional[str]:
        """A SHORT, body-free activity line from ``snapshot``, or ``None`` when nothing to show.

        Pure. The :class:`~claude_tg.engine.adapter_sdk.ActivitySnapshot` is SB3-clean by
        construction (NAMES ONLY — ``current_tool`` is a tool name; ``subagents`` are agent
        TYPE-names; there is nowhere to put args/paths/prompts/output). We still HTML-escape each
        name once (``parse_mode="HTML"`` safety) and assemble:

        * no subagents, a tool → ``⚙️ <tool>``
        * subagents (≤ 3) + a tool → ``⚙️ <type[, type…]> · <tool>``
        * many subagents (> 3) + a tool → ``⚙️ N agents · <tool>`` (a count, not a wall of names)
        * subagents only (no tool) → ``⚙️ <type[, type…]>`` (or ``⚙️ N agents``)
        * tool only → ``⚙️ <tool>``

        Returns ``None`` for a ``None`` snapshot OR a snapshot that, defensively, carries nothing
        renderable (no tool + no subagents) — the caller then removes/skips the line. NEVER raises.
        """
        if snapshot is None:
            return None
        # Read the two NAMES-ONLY fields defensively (a real ActivitySnapshot always has them; an
        # odd duck-typed value degrades to nothing rather than raising — RB1-adjacent).
        tool = getattr(snapshot, "current_tool", None)
        subagents = getattr(snapshot, "subagents", ()) or ()
        tool_part = (
            html.escape(tool.strip(), quote=False)
            if isinstance(tool, str) and tool.strip()
            else None
        )
        # Only keep non-empty string type-names (SB3: they are agent-type classifiers, escaped once).
        names = [
            html.escape(s.strip(), quote=False)
            for s in subagents
            if isinstance(s, str) and s.strip()
        ]
        if names:
            agents_part = (
                ", ".join(names) if len(names) <= 3 else f"{len(names)} agents"
            )
        else:
            agents_part = None
        if agents_part and tool_part:
            return f"⚙️ {agents_part} · {tool_part}"
        if agents_part:
            return f"⚙️ {agents_part}"
        if tool_part:
            return f"⚙️ {tool_part}"
        return None

    async def _maybe_update_activity(
        self,
        chat_id: int,
        *,
        send: Optional[SendFn],
        edit: Optional[EditFn],
        for_project: Optional[str] = None,
    ) -> None:
        """Post / edit-in-place the transient activity line for the FOREGROUND turn (T5).

        Reads the chat's ACTIVE (foreground) engine's ``last_activity()`` (getattr/try-guarded →
        ``None``), renders it (:meth:`_render_activity`), and reconciles it with the chat's single
        transient activity message:

        * **nothing to show** (``None`` render — idle, or no foreground engine) → skip (the line is
          removed at turn end by :meth:`_finalize_activity`, not here, so a brief idle gap mid-turn
          doesn't churn a delete+resend).
        * **first activity** (no id held) → POST the line (gated, non-verbatim).
        * **subsequent change** → EDIT that SAME message in place (NEVER a new message per change).

        Two throttles keep this within Telegram's edit limits + the per-chat send gate (anti-spam):

        * **skip-identical** — if the rendered text equals what's already shown, do nothing (a
          no-op Telegram edit raises "message is not modified" AND wastes a send slot; mirrors the
          statusline's identical-text skip).
        * **time-throttle** — at most ~1 EDIT/sec: an edit landing within ``_ACTIVITY_EDIT_INTERVAL``
          of the last is SKIPPED and coalesced. The in-memory ``activity_text`` is NOT advanced on a
          throttled skip, so the NEXT change past the interval still renders the latest state (no
          lost final state). The FIRST post is never throttled.

        **Foreground-only** (``for_project`` must be the chat's foreground, mirroring the
        statusline's make-or-break invariant: a BACKGROUND concurrent turn never writes the
        foreground line). **B2** — a SYNC foreground re-check immediately precedes the raw send/edit
        with NO await between (the gate wait is a ``/switch`` window). **RB1** — the WHOLE body is
        wrapped so ANY failure (a raising ``last_activity()`` / send / edit, odd state) is swallowed
        and NEVER breaks the turn (an observer off the critical path).
        """
        if send is None or edit is None:
            return  # no closures injected (a test / a caller that didn't wire them) → no-op.
        try:
            # ⭐ Foreground-only: a BACKGROUND turn never writes the foreground activity line.
            if for_project is not None and not self._is_foreground(chat_id, for_project):
                return
            _name, rt = self._active_runtime(chat_id, create_default=False)
            if rt is None:
                return  # no foreground project to describe.
            engine = rt.engine
            if engine is None:
                return
            # Best-effort read of the foreground engine's activity (getattr/try-guarded so a
            # predating/fake engine — or a raising read — yields None; the line just isn't driven).
            snapshot: Optional[ActivitySnapshot] = None
            getter = getattr(engine, "last_activity", None)
            if callable(getter):
                try:
                    snapshot = getter()
                except Exception:  # the engine read is already best-effort (RB1)
                    snapshot = None
            body = self._render_activity(snapshot)
            if body is None:
                return  # idle / nothing to show — removal is the finalize's job, not here.
            state = self._chat(chat_id)
            if body == state.activity_text:
                # Identical to what's shown — skip BEFORE the gate so an unchanged snapshot never
                # consumes a send slot and never triggers a no-op "not modified" edit.
                return
            if state.activity_message_id is None:
                # First activity this turn → POST the line. The B2 re-check happens INSIDE the
                # gated send (below) right before the raw send. The first post is NOT throttled.
                await self._activity_send(chat_id, state, body, for_project, send=send)
                return
            # A subsequent CHANGE → EDIT in place, time-throttled (≲1 edit/sec). A change inside
            # the interval is coalesced: skip the edit WITHOUT advancing activity_text, so the
            # next change past the interval still shows the latest state.
            now = self._clock()
            if (now - state.activity_last_edit_ts) < _ACTIVITY_EDIT_INTERVAL:
                return
            await self._activity_edit(chat_id, state, body, for_project, edit=edit)
        except Exception:
            # ⭐ The make-or-break swallow (RB1): NOTHING the activity line does may escape to the
            # turn. A read/render/gate/closure failure is logged at debug and dropped.
            log.debug("activity update failed for chat %s (ignored)", chat_id, exc_info=True)

    async def _activity_send(
        self,
        chat_id: int,
        state: _ChatState,
        body: str,
        for_project: Optional[str],
        *,
        send: SendFn,
    ) -> None:
        """POST the transient activity line (gated, non-verbatim) with the B2 foreground re-check.

        The gated send awaits the gate's wait (a ``/switch`` window), so immediately before the raw
        send we re-confirm SYNCHRONOUSLY that ``for_project`` is STILL the chat's foreground — no
        await between the check and the send. A ``/switch`` during the wait drops the stale post. The
        id/text/throttle-ts are stored ONLY when the send returns an id (so a ``None`` send leaves no
        half-set state). Called inside :meth:`_maybe_update_activity`'s best-effort guard.
        """
        wait = self._gate(state).reserve(verbatim=False)
        if wait > 0:
            await self._sleep(wait)
        # ⭐ B2 sync re-check (no await between here and the send): a /switch during the gate wait
        # makes ``for_project`` no longer foreground → drop the stale post.
        if for_project is not None and not self._is_foreground(chat_id, for_project):
            return
        mid = await send(text=body, reply_markup=None, parse_mode="HTML")
        if mid is None:
            return  # the closure produced no id — don't store a half state.
        state.activity_message_id = mid
        state.activity_text = body
        state.activity_last_edit_ts = self._clock()

    async def _activity_edit(
        self,
        chat_id: int,
        state: _ChatState,
        body: str,
        for_project: Optional[str],
        *,
        edit: EditFn,
    ) -> None:
        """EDIT the transient activity line in place (gated, non-verbatim) with the B2 re-check.

        Mirrors :meth:`_activity_send`: reserve + await the gate slot, then a FINAL SYNC foreground
        re-check (no await between it and the raw edit) so a ``/switch`` during the wait drops the
        stale edit. Advances ``activity_text`` + the throttle ts only after the edit issues. A
        raising edit propagates to the caller's RB1 swallow (the message may be gone — the next
        change re-posts on a fresh turn; mid-turn we simply leave it).
        """
        wait = self._gate(state).reserve(verbatim=False)
        if wait > 0:
            await self._sleep(wait)
        # ⭐ B2 sync re-check (no await between here and the edit).
        if for_project is not None and not self._is_foreground(chat_id, for_project):
            return
        if state.activity_message_id is None:
            return  # cleared underneath us (turn-end finalize raced) — nothing to edit.
        await self._gated_edit_raw(state, body, edit=edit)

    async def _gated_edit_raw(self, state: _ChatState, body: str, *, edit: EditFn) -> None:
        """Issue the raw edit and advance the in-memory text + throttle ts (no gate reserve here).

        The gate slot was already reserved+awaited by :meth:`_activity_edit` (which also did the B2
        re-check); this just performs the edit and records that it happened so skip-identical + the
        time-throttle see the new state.
        """
        await edit(message_id=state.activity_message_id, text=body, parse_mode="HTML")
        state.activity_text = body
        state.activity_last_edit_ts = self._clock()

    async def _finalize_activity(
        self,
        chat_id: int,
        *,
        delete: Optional[DeleteFn],
    ) -> None:
        """At turn END, REMOVE the transient activity line + clear its id/throttle state (T5).

        Best-effort DELETE of the activity message (no lingering ⚙️), then clear
        ``activity_message_id``/``activity_text``/``activity_last_edit_ts`` so the NEXT turn starts
        fresh. NOT a per-turn "done" footer — the owner explicitly disliked that; the pinned
        statusline is the persistent summary. Called from ``_drive_turn``'s ``finally`` (alongside
        the statusline refresh + the limit warning) so the line is ALWAYS removed, even on a
        mid-stream raise. **RB1** — the WHOLE body is wrapped; a failed/absent delete never breaks
        the turn (the state is cleared regardless, so a stale id can't leak into the next turn).
        """
        state = self._chat(chat_id)
        try:
            if delete is not None and state.activity_message_id is not None:
                try:
                    await delete(message_id=state.activity_message_id)
                except Exception:
                    log.debug("activity-line delete failed at turn end (ignored)", exc_info=True)
        finally:
            # Clear unconditionally (even if the delete raised / no delete was injected) so a stale
            # id/text never survives into the next turn — the line is transient per turn (RB3).
            state.activity_message_id = None
            state.activity_text = None
            state.activity_last_edit_ts = 0.0
