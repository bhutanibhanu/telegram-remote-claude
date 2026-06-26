"""Leaf types, constants, and pure helpers for the streaming session package.

These symbols carry **no** ``self`` and **no** dependency on the runtime dataclasses
(``_ProjectRuntime`` & co.) or :class:`StreamingSession`, so they sit at the bottom of the
package's import graph: ``runtime.py``, the mixins, and ``core.py`` all import from here
without any cycle. Relocated verbatim from the original single-file ``stream_session.py``
(behavior-preserving — see ``docs/features/core-refactor/design.md`` §2/§4).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from ..render import ProjectStatus

#: A coroutine that sends a NEW message and returns the sent message id (or None).
#: ``reply_markup`` is the inline keyboard for an ask/plan (None otherwise).
SendFn = Callable[..., Awaitable[Optional[int]]]
#: A coroutine that edits an existing message's text in place (best-effort).
EditFn = Callable[..., Awaitable[None]]
#: A coroutine that deletes a message by id (best-effort; used to clear the transient
#: "💭 Claude is thinking…" status line at the end of a turn so it does not linger).
DeleteFn = Callable[..., Awaitable[None]]
#: A coroutine that PINS a message by id (STATUSLINE T-SL-CORE). The bot's closure forwards
#: to ``Bot.pin_chat_message`` with ``disable_notification=True`` (a silent pin — design §3.1).
#: Best-effort: a failure is swallowed (RB1) and never breaks a turn.
PinFn = Callable[..., Awaitable[None]]
#: A coroutine that UNPINS a message by id (STATUSLINE T-SL-CORE). Used on orphan-recovery to
#: best-effort drop the stale pin before re-pinning the fresh one (the "one pinned message"
#: invariant; Telegram's current pin is the newest, so the bar self-corrects). Best-effort (RB1).
UnpinFn = Callable[..., Awaitable[None]]

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

#: P11 T2 (attach naming): every char NOT in the SB4 project-name charset
#: (``[A-Za-z0-9_-]`` — ``session_store._NAME_RE``) collapses to ``-`` so a derived name (from
#: a session title / cwd basename, which may carry spaces, slashes, dots, unicode) is rendered
#: SB4-valid. Runs of separators collapse to ONE ``-`` and leading/trailing ``-`` are trimmed.
_ATTACH_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _sanitize_attach_name(text: object) -> str:
    """Reduce arbitrary text to the SB4 project-name charset (P11 T2 attach naming).

    Maps every non-``[A-Za-z0-9_-]`` run to a single ``-``, strips leading/trailing ``-``/``_``,
    and clamps to 32 chars (the SB4 budget). Returns ``""`` when nothing usable survives (the
    caller falls back to ``attached-<shortid>``). Pure; defensive against a non-``str`` input.
    """
    if text is None:
        return ""
    raw = str(text).strip()
    if not raw:
        return ""
    cleaned = _ATTACH_NAME_SANITIZE_RE.sub("-", raw).strip("-_")
    return cleaned[:32]


def _basename_of(path: object) -> str:
    """The final path component of ``path`` (the dir name), or ``""`` (P11 T2 attach naming).

    Used to derive a friendly project name from a session's cwd when it has no title. Pure;
    uses :class:`pathlib.PurePosixPath`-style ``Path.name`` (a discovered cwd is a Mac path).
    Defensive: a None/empty/odd value → ``""`` so the caller falls back.
    """
    if not path:
        return ""
    try:
        return Path(str(path)).name
    except Exception:
        return ""


#: The kind of interactive request a pending-index entry holds open.
PendingKind = Literal["ask", "plan", "permission"]


#: A held request of each kind maps the OWNING project's status to the matching
#: ``awaiting_<kind>`` for the /projects column (ADR-005 D7). These values MUST match the
#: render-layer :data:`~claude_tg.render.ProjectStatus` enum (permission->awaiting_approval,
#: ask->awaiting_answer, plan->awaiting_plan).
_AWAITING_STATUS: dict[PendingKind, ProjectStatus] = {
    "permission": "awaiting_approval",
    "ask": "awaiting_answer",
    "plan": "awaiting_plan",
}


class StreamingBusy(Exception):
    """Raised when a chat already has a streaming turn in flight (harvested ClaudeBusy)."""


@dataclass(frozen=True)
class AttachOutcome:
    """Result of an ``/attach <id>`` / ``[Attach]`` adopt (so the bot can reply, P11 T2).

    The session decides everything (lookup, SB2, fork-vs-continue, the registry write) and
    returns this for the bot to render — the bot adds no policy, exactly as ``/sessions`` is a
    pure render of the session's discovery. Fields:

    * ``ok``       — True iff a project was adopted + made active (the next message resumes it).
    * ``message``  — the operator-facing reply (already styled; HTML when ``parse_mode``='HTML').
    * ``parse_mode`` — the reply's Telegram parse mode (``"HTML"`` for the styled replies,
                       ``None`` for a plain one).
    * ``project_name`` — the adopted project's STORED (SB4-validated) name, or ``None`` on a
                       refusal/no-op (unknown id, out-of-root cwd, no store).
    * ``forked``   — True iff the target was LIVE elsewhere and we adopted a FORK (a fresh id,
                       transcript copied — never the live id); False for an idle continue. Only
                       meaningful when ``ok``. Surfaced so the reply can tell the operator why.
    """

    ok: bool
    message: str
    parse_mode: Optional[str] = None
    project_name: Optional[str] = None
    forked: bool = False


@dataclass(frozen=True)
class WatchOutcome:
    """Result of a ``/watch <id>`` start / replacement (so the bot can reply, P11 T3).

    The session owns everything (id lookup, transcript-path resolution, the task lifecycle);
    the bot is a pure renderer of this, exactly like :class:`AttachOutcome`. Fields:

    * ``ok``         — True iff a read-only mirror task was started (or replaced) for the chat.
    * ``message``    — the operator-facing reply (already styled; HTML when ``parse_mode``='HTML').
    * ``parse_mode`` — the reply's Telegram parse mode (``"HTML"`` for the styled replies).
    * ``session_id`` — the mirrored session's id, or ``None`` on a refusal (unknown id / no
                       cwd / unresolvable transcript). Only meaningful when ``ok``.
    """

    ok: bool
    message: str
    parse_mode: Optional[str] = None
    session_id: Optional[str] = None


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
    * ``switch_to``    — (T6/P9) the TARGET project name of a ``[Open <project>]`` switch tap.
                         The session does NOT touch the store for a switch (it needs the bot's
                         SB2 path re-validation, the same as ``/switch``); it decodes + routes
                         and returns the name so the bot performs the switch via its shared
                         ``/switch`` helper. ``None`` for every non-switch outcome.
    * ``attach_session_id`` — (P11 T2) the TARGET session id of an ``[Attach]`` tap. The
                         session decodes + routes it and returns it; the bot calls
                         :meth:`attach_session` (which does the SB2 cwd check + fork-vs-continue).
                         ``None`` for every non-attach outcome.

    ``project_name`` / ``tool_use_id`` are populated only for an ``expects_text`` outcome
    (the "Other"/"Reject" arm); ``switch_to`` only for a switch tap; ``attach_session_id``
    only for an attach tap; all are ``None`` otherwise.
    """

    handled: bool
    note: str = ""
    expects_text: bool = False
    project_name: Optional[str] = None
    tool_use_id: Optional[str] = None
    switch_to: Optional[str] = None
    attach_session_id: Optional[str] = None
