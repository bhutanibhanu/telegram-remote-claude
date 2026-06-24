"""Durable, **body-free** AUDIT TRAIL for security-relevant events (P13 T-AUDIT).

A pure-ish persistence module (no telegram, no SDK — mirrors
:mod:`claude_tg.session_store`'s isolation) that records *what happened* on the
permission/decision chokepoints to a durable, append-only, ``0600`` JSONL log the
operator can review with ``/audit``. It reuses the proven body-free discipline (SB3,
H1-remediated): every record is built **only** from values that are *already* safe —
:func:`~claude_tg.engine.types.safe_input_summary` outputs (lengths / truncated idents,
the same string the permission prompt shows) and the redacted session tag
(:func:`~claude_tg.util._redact_sid`).

**SB3 is STRUCTURAL, not a convention.** :class:`AuditEvent` has **no field that can
carry a raw body** — no file content, no command output, no prompt text, no plan
feedback, no raw session id, no secret. The writer's only input type is this frozen
dataclass, so a body cannot be logged "by mistake" (mirrors how
:class:`~claude_tg.engine.types.ThinkingEvent` has no ``signature`` field and
:class:`~claude_tg.engine.types.ImageInput.__repr__` elides the base64). A leak is
impossible *by construction*.

**RB1 is TOTAL.** Every write / rotate is best-effort: any exception (disk full, bad
perms, bad path) is caught, logged once at WARNING (body-free), and **swallowed** — it
NEVER propagates. The audit log is an *observer*, never on a turn's critical path; an
audit failure must never break a turn (mirrors ``JsonSessionStore.add_cost`` and
``StreamingSession._persist``: "never wedge a turn over a write").

**Persistence discipline (reused from ``JsonSessionStore._save_raw``).** The log file
is created/opened ``0600`` with its parent ``mkdir -p``'d, and each append is a single
line written under an OS append-mode handle (atomic-enough for a local single-writer
log — one bot per token, one asyncio loop; this is NOT designed for concurrent-process
appends). A **size-bounded 1-keep rotation** caps disk: before an append, if the file
exceeds the byte bound it is rotated once to ``<file>.1`` (replacing any prior ``.1``)
and a fresh file is started — bounded growth, no unbounded log.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The record — body-free BY CONSTRUCTION (SB3 is structural)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    """One security-relevant event — **only** non-sensitive fields (SB3, structural).

    Every field is either a fixed/short discriminator or an ALREADY-SAFE string. There
    is deliberately **no** field that could carry a raw body — no file content, no
    command output, no prompt / plan text, no plan feedback, no raw session id, no
    secret. The ONLY free-ish string is :attr:`summary`, and the contract is that the
    caller passes a value built by :func:`audit_safe_summary` — the audit-specific,
    STRONGLY body-free renderer that collapses BOTH free-text body fields AND ident fields
    (``command`` / ``path`` / ``url`` / ``pattern``) to a length/shape, so even a secret
    early in a Bash command is never persisted (this is STRICTER than the ephemeral prompt's
    ``safe_input_summary``, which keeps 160 raw chars of an ident — correct for the prompt,
    a leak for the durable log). Because the writer's only input is this type and the only
    summary source is the strict renderer, a body cannot reach the log even by mistake — a
    leak is impossible by construction (the SB3 gate-blocking bar).

    Fields:

    * ``ts`` — an ISO-8601 UTC timestamp (when the event was recorded).
    * ``kind`` — the event family: ``tool_decision`` / ``plan_decision`` /
      ``session_event`` / ``policy_event``.
    * ``tool`` — the tool NAME for a ``tool_decision`` (e.g. ``"Bash"``), else ``None``.
      A name only — never the tool input.
    * ``summary`` — for a ``tool_decision`` the STRONGLY body-free
      :func:`audit_safe_summary` string (idents collapsed to length/shape, not raw); for the
      other kinds a short action token (e.g. ``"attach"`` / ``"yolo_on"`` /
      ``"bash_policy_flag"``) optionally with a body-free pattern label. NEVER a body.
    * ``decision`` — the outcome: a verdict (``auto_allow`` / ``allow_once`` /
      ``allow_session`` / ``deny`` / ``backstop_deny`` / ``cancel``) or an action verdict
      (``approve`` / ``reject``), or ``None`` where not applicable.
    * ``chat_id`` — the originating chat id (stamped by the :class:`ChatBoundSink`), or
      ``None`` when recorded outside a chat context.
    * ``session_tag`` — the **redacted** session tag (``sid:ab12cd`` from
      :func:`~claude_tg.util._redact_sid`) — NEVER the raw resumable session id.
    """

    ts: str
    kind: str
    tool: Optional[str] = None
    summary: Optional[str] = None
    decision: Optional[str] = None
    chat_id: Optional[int] = None
    session_tag: Optional[str] = None

    def to_json_line(self) -> str:
        """Serialize to one compact JSON line (no embedded newline) for the JSONL log.

        ``ensure_ascii`` keeps the line single-byte-safe; ``separators`` keeps it
        compact. The dataclass holds only short scalars, so this never embeds a body.
        """
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> "AuditEvent":
        """Parse one JSONL line back into an :class:`AuditEvent`.

        Tolerant of unknown / missing keys (a hand-edited or future-version line): only
        the known fields are taken, extras are ignored, absent ones default. Raises on
        non-object / unparseable JSON — :meth:`AuditLog.tail` catches that per line so one
        bad line never breaks the whole read (RB1).
        """
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("audit line is not a JSON object")
        return cls(
            ts=str(data.get("ts", "")),
            kind=str(data.get("kind", "")),
            tool=_opt_str(data.get("tool")),
            summary=_opt_str(data.get("summary")),
            decision=_opt_str(data.get("decision")),
            chat_id=_opt_int(data.get("chat_id")),
            session_tag=_opt_str(data.get("session_tag")),
        )


def _opt_str(value: object) -> Optional[str]:
    return str(value) if isinstance(value, str) else None


def _opt_int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# Event-kind discriminators (the small fixed set — body-free by design).
KIND_TOOL_DECISION = "tool_decision"
KIND_PLAN_DECISION = "plan_decision"
KIND_SESSION_EVENT = "session_event"
KIND_POLICY_EVENT = "policy_event"


# ---------------------------------------------------------------------------
# Audit-specific summary — STRICTER than the prompt's (the durable-log SB3 bar)
# ---------------------------------------------------------------------------
#
# The live permission PROMPT uses ``engine.types.safe_input_summary``, which keeps the
# first 160 RAW chars of an IDENT field (``command`` / ``path`` / ``url`` / ``pattern``) —
# correct there, because the operator must SEE the command to approve it, and the prompt is
# EPHEMERAL (it scrolls off the chat). The DURABLE audit log is different: persisting 160 raw
# chars of a Bash command would write a secret early in the command to disk. So the audit log
# uses THIS stricter summary, which collapses IDENT fields to a length/shape too — never raw
# text. (BLOCKER 1, Codex QA: the durable log must be STRONGLY body-free.)

#: IDENT fields the PROMPT shows verbatim (truncated) but the AUDIT log must NOT — a command
#: / path / url / regex can carry a secret or sensitive target. Collapsed to length/shape.
_AUDIT_IDENT_FIELDS = frozenset({"file_path", "path", "command", "pattern", "url"})

#: Free-text body fields — collapsed to a length in BOTH the prompt and the audit summary.
_AUDIT_BODY_FIELDS = frozenset({"content", "new_string", "old_string"})

#: Per-tool argv[0]-bearing field, and the max chars of it the audit summary may keep. For a
#: ``Bash`` command we surface ONLY the first whitespace-delimited token (the binary name,
#: e.g. ``rm`` / ``git`` / ``curl``) — useful for review ("a curl command was denied") and
#: low-risk (argv[0] is the program, not its secret-bearing arguments) — then the LENGTH of
#: the whole command. Capped so even a pathological no-space "token" can't dump the command.
_AUDIT_ARGV0_MAX = 16


def _audit_ident_value(field: str, value: str) -> str:
    """Collapse an IDENT field to a length/shape for the AUDIT log — never raw text (SB3).

    * ``command`` → ``<bin> …<N chars>`` where ``<bin>`` is ONLY argv[0] (the first
      whitespace-delimited token, capped at :data:`_AUDIT_ARGV0_MAX`, dropped if it looks
      non-trivial) and ``N`` is the FULL command length. argv[0] is the program name (e.g.
      ``rm`` / ``git`` / ``curl``), not a secret-bearing argument, so it is review-useful and
      low-risk; everything after it is replaced by a length. If argv[0] is suspiciously long
      / non-word-ish (could be an inline assignment like ``SECRET=…`` or a data blob), it is
      dropped and only the length is shown.
    * ``path`` / ``file_path`` / ``url`` / ``pattern`` → ``<N chars>`` (length only — a path
      or url can encode a token or a sensitive location; a regex is the owner's, not a body,
      but length-only keeps the rule uniform + the log strongly body-free).
    """
    if field == "command":
        token = value.split(maxsplit=1)[0] if value.split(maxsplit=1) else ""
        # Keep argv[0] only when it is a short, plausible program name (word-ish chars +
        # path separators / dots / hyphens) — NOT an inline ``VAR=value`` assignment or a
        # long blob, which could carry a secret. Otherwise show length only.
        if token and len(token) <= _AUDIT_ARGV0_MAX and "=" not in token and all(
            ch.isalnum() or ch in "._-/" for ch in token
        ):
            return f"<{token} …{len(value)} chars>"
        return f"<{len(value)} chars>"
    return f"<{len(value)} chars>"


def audit_safe_summary(tool_name: str, tool_input: "dict[str, object] | None") -> str:
    """Render a tool's input for the DURABLE audit log — STRONGLY body-free (SB3).

    Stricter than :func:`~claude_tg.engine.types.safe_input_summary` (the ephemeral prompt's
    renderer): in ADDITION to collapsing free-text BODY fields (``content`` / ``new_string``
    / ``old_string``) to ``<N chars>``, this also collapses IDENT fields (``command`` /
    ``path`` / ``file_path`` / ``url`` / ``pattern``) to a length/shape via
    :func:`_audit_ident_value` — so a secret early in a Bash command, or a token-bearing url
    / path, is **never persisted to disk** (only its length + argv[0] binary name). Every
    other field is truncated to 40 chars (a short scalar like a flag is harmless and keeps the
    line review-useful). Pure; SDK-free (the audit module stays isolated).

    A Write's ``content`` shows ``content=<500 chars>``; a Bash ``rm -rf /etc … <secret>``
    shows ``command=<rm …40 chars>`` — the verb, then the length, never the body.
    """
    if not isinstance(tool_input, dict):
        return f"{tool_name}(<{len(str(tool_input))} chars>)"
    parts: list[str] = []
    for k, v in tool_input.items():
        sv = str(v)
        if k in _AUDIT_BODY_FIELDS:
            parts.append(f"{k}=<{len(sv)} chars>")
        elif k in _AUDIT_IDENT_FIELDS:
            parts.append(f"{k}={_audit_ident_value(k, sv)}")
        else:
            parts.append(f"{k}={sv[:40]}")
    return f"{tool_name}({', '.join(parts)})"




#: Default size bound (bytes) before a 1-keep rotation: 5 MB. Bounds disk to ~2x this
#: (the live file + the single ``.1`` keep). Configurable via ``AUDIT_LOG_MAX_BYTES``.
DEFAULT_AUDIT_LOG_MAX_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# The durable log — append-only JSONL, 0600, size-bounded, RB1-total
# ---------------------------------------------------------------------------


class AuditLog:
    """Append-only, ``0600``, size-bounded JSONL audit log (RB1-total best-effort).

    Holds the path; exposes :meth:`append` (one line per event, best-effort, never
    raises) and :meth:`tail` (the last ``n`` parsed events, never raises). The file is
    created ``0600`` with its parent ``mkdir -p``'d, and a **1-keep rotation** caps disk
    at the configured byte bound. There is no cross-process locking — one writer is
    assumed (one bot per token, one asyncio loop), consistent with the project's
    one-instance invariant.
    """

    def __init__(self, path: str | Path, *, max_bytes: int = DEFAULT_AUDIT_LOG_MAX_BYTES):
        self.path = Path(path)
        # A non-positive bound would rotate on every write (or never) — clamp to the
        # default so an odd value can't wedge the log. Config validates the env value
        # loudly upstream; this is the defensive floor (RB1).
        self.max_bytes = max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else DEFAULT_AUDIT_LOG_MAX_BYTES

    def append(self, event: AuditEvent) -> None:
        """Append one event as a JSONL line — best-effort, NEVER raises (RB1-total).

        Rotates first if the file is at/over the size bound (1-keep), then writes the
        single line under a ``0600`` append-mode handle and best-effort ``flush``+
        ``fsync``. ANY failure (disk full, perms, bad path, a rotation error) is caught,
        logged once at WARNING (body-free — only the path + exception class), and
        swallowed: the caller's turn is unaffected. The audit log is an observer, never
        on the critical path.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            line = event.to_json_line() + "\n"
            # O_APPEND makes the single-line write atomic enough for one local writer;
            # mode 0o600 on O_CREAT sets the perms at creation (no create-then-chmod
            # window). umask can only REMOVE bits, so an existing-file mode is left as-is
            # — we re-assert 0600 below to be safe on a pre-existing looser file.
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                try:
                    os.fsync(fd)
                except OSError:
                    pass  # fsync is best-effort (some filesystems/CI don't support it)
            finally:
                os.close(fd)
            # Re-assert 0600 in case the file pre-existed with looser perms (O_CREAT only
            # sets the mode when CREATING the file). Best-effort.
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            # RB1-total: an audit write must NEVER break a turn. Log body-free (the path
            # + the exception type only — never the event payload, which is already
            # body-free anyway) and swallow.
            log.warning("audit append failed for %s (ignored)", self.path, exc_info=True)

    def tail(self, n: int = 20) -> list[AuditEvent]:
        """Return the last ``n`` parsed events (oldest→newest) — NEVER raises (RB1).

        A missing/empty file reads as ``[]``. Each line is parsed independently: a single
        malformed line is skipped (logged once at DEBUG) rather than failing the whole
        read, so a partially-written tail or a hand-edit can't break ``/audit``. ``n`` is
        clamped to ``>= 0`` (``0`` → empty list).
        """
        if n <= 0:
            return []
        try:
            if not self.path.is_file():
                return []
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            log.debug("audit tail read failed for %s (ignored)", self.path, exc_info=True)
            return []
        events: list[AuditEvent] = []
        # Take the last n NON-EMPTY lines, then parse. Reading the whole file is fine for
        # a size-bounded log (≤ max_bytes); a future optimization could seek from the end.
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        for line in lines[-n:]:
            try:
                events.append(AuditEvent.from_json_line(line))
            except Exception:
                log.debug("skipping unparseable audit line in %s", self.path, exc_info=True)
        return events

    def _rotate_if_needed(self) -> None:
        """Rotate once to ``<file>.1`` if the live file is at/over the byte bound.

        A simple 1-keep rotation (replace any prior ``.1``) so disk is bounded to ~2x
        ``max_bytes``. Best-effort and called from inside :meth:`append`'s try-block, so a
        rotation failure is swallowed like any other write failure (RB1) — at worst the
        live file grows past the bound until the next successful rotation; it never raises.
        """
        try:
            if not self.path.is_file():
                return
            if self.path.stat().st_size < self.max_bytes:
                return
            rotated = self.path.with_name(self.path.name + ".1")
            # os.replace is atomic on the same filesystem and overwrites any prior .1
            # (the single keep) without a separate unlink race.
            os.replace(self.path, rotated)
            try:
                rotated.chmod(0o600)
            except OSError:
                pass
        except Exception:
            # Swallowed (RB1): a rotation failure must not break the append/turn. The
            # append's own try-block also guards this, but rotation has its own log line.
            log.warning("audit rotation failed for %s (ignored)", self.path, exc_info=True)


# ---------------------------------------------------------------------------
# The sink — the engine/bot-facing seam (best-effort, chat-id-stamping)
# ---------------------------------------------------------------------------


class AuditSink(Protocol):
    """The tiny seam the engine/bot record through (best-effort; never raises).

    A :meth:`record` that takes a fully-built body-free :class:`AuditEvent`. Production
    passes a :class:`ChatBoundSink` (an adapter over :class:`AuditLog` that stamps the
    chat id + redacted session tag the substrate-neutral engine does not know); tests pass
    a list-collecting fake or ``None`` (the engine's no-op default — behavior identical).
    Implementations MUST be best-effort: a sink that raised would re-introduce the RB1
    hazard the log itself avoids, so every implementation swallows its own failures.
    """

    def record(self, event: AuditEvent) -> None: ...


class FileAuditSink:
    """The default sink: write each event straight to an :class:`AuditLog` (best-effort).

    A thin pass-through used where the chat id is already on the event (or absent). The
    :class:`ChatBoundSink` is preferred on the engine path because it stamps the chat id
    the engine cannot see; this plain sink is the simplest adapter and the building block
    the bot can also use directly.
    """

    def __init__(self, audit_log: AuditLog):
        self._log = audit_log

    def record(self, event: AuditEvent) -> None:
        # AuditLog.append is already RB1-total (never raises); the extra guard is
        # defense-in-depth so NO sink implementation can ever propagate.
        try:
            self._log.append(event)
        except Exception:  # pragma: no cover - append already swallows
            log.warning("audit sink record failed (ignored)", exc_info=True)


class ChatBoundSink:
    """An :class:`AuditSink` that STAMPS the chat id (+ a redacted session tag) on records.

    The substrate-neutral :class:`~claude_tg.engine.engine.Engine` does not know the chat
    id; the production factory binds ONE of these per chat/project (closing over the
    ``chat_id``) and hands it to the engine, so the engine can ``record(...)`` an event
    with ``chat_id=None`` and this sink fills it in before the write. It also redacts a
    raw session id passed via :attr:`AuditEvent.session_tag` (defense-in-depth — the
    engine already passes a redacted tag, but if a raw id ever arrived this re-redacts it,
    so a resumable id can never reach the log; SB3/H1).

    Best-effort: stamping + the underlying append never raise (RB1-total).
    """

    def __init__(self, audit_log: AuditLog, chat_id: int):
        self._log = audit_log
        self._chat_id = chat_id

    def record(self, event: AuditEvent) -> None:
        try:
            stamped = self._stamp(event)
            self._log.append(stamped)
        except Exception:  # pragma: no cover - append already swallows
            log.warning("chat-bound audit sink record failed (ignored)", exc_info=True)

    def _stamp(self, event: AuditEvent) -> AuditEvent:
        """Return ``event`` with ``chat_id`` filled in and ``session_tag`` re-redacted.

        ``chat_id`` is stamped only when absent (an event that already carries one — e.g.
        a bot-side record — is respected). ``session_tag`` is run through
        :func:`~claude_tg.util._redact_sid_in_text` so any UUID-shaped raw id embedded in
        it is replaced with its redacted tag (a no-op for an already-redacted ``sid:...``
        tag, which contains no UUID), guaranteeing the log never carries a raw resumable id.
        """
        from .util import _redact_sid_in_text

        chat_id = event.chat_id if event.chat_id is not None else self._chat_id
        session_tag = (
            _redact_sid_in_text(event.session_tag) if event.session_tag is not None else None
        )
        if chat_id == event.chat_id and session_tag == event.session_tag:
            return event  # nothing to stamp/redact — avoid a needless copy
        from dataclasses import replace

        return replace(event, chat_id=chat_id, session_tag=session_tag)


__all__ = [
    "AuditEvent",
    "AuditLog",
    "AuditSink",
    "FileAuditSink",
    "ChatBoundSink",
    "DEFAULT_AUDIT_LOG_MAX_BYTES",
    "audit_safe_summary",
    "KIND_TOOL_DECISION",
    "KIND_PLAN_DECISION",
    "KIND_SESSION_EVENT",
    "KIND_POLICY_EVENT",
]
