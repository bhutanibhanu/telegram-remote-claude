"""Discovery of ALL Claude Code sessions on this machine (P11 T1).

The bot's own :class:`~claude_tg.session_store.JsonSessionStore` only knows the sessions
the bot *created* (its named projects). This module is the other half — it enumerates
**every** Claude Code session on the Mac (started in a terminal, in an IDE, or by the bot)
so ``/sessions`` can show the operator the whole machine, *including the orchestrator that
is running right now*.

Two pieces, both **dependency-injected** so the whole thing is unit-testable with no
dependency on the real ``~/.claude`` directory, the real SDK, or a real ``ps``:

1. **The SDK adapter (the ONLY place that touches the SDK's ``_internal`` session API).**
   :func:`sdk_list_sessions` calls the SDK's re-exported ``list_sessions`` (which lives in
   ``claude_agent_sdk._internal.sessions`` — see the pin note below) and maps each
   ``SDKSessionInfo`` to our own small, stable :class:`_RawSession` so the rest of the bot
   never imports an SDK type. If the SDK changes shape, **only this function changes.**

2. **The composite liveness probe.** A session is reported ``running`` if ANY of three
   independent signals fires (none is reliable alone — see :func:`probe_liveness`):
   recent transcript ``mtime``, a matching ``claude`` process in ``ps``, or a validated
   entry in the ``~/.claude/sessions/<pid>.json`` process registry. "running" is always a
   HINT, never a lock (this is a read-only listing).

:func:`discover_sessions` glues them: it asks the (injected) lister for the raw sessions
and the (injected) liveness probe for each one's running state, returning a list of
:class:`DiscoveredSession`. **RB1 (never crash):** a missing/odd ``~/.claude``, an SDK
import or call failure, or a ``ps`` failure each degrade to "return what you can" (an empty
list is a fine answer) — discovery is best-effort and never raises.

**SB pinning.** ``list_sessions`` / ``get_session_info`` are re-exported by the SDK from a
``_internal`` module; the dependency is pinned (``claude-agent-sdk==0.2.105``, ADR-001) and
ALL session-SDK use is wrapped here, so an SDK upgrade that moves/renames the API breaks
exactly one import in one file (and degrades cleanly to an empty list at runtime, RB1)
rather than scattering through the bot.

**SB3 (body-free).** This module surfaces only session *metadata* — id, cwd, a title /
first-prompt line, a timestamp — never transcript bodies (no file contents, no tool
output). The title/first-prompt is operator-authored prompt text the owner is entitled to
see, but the render layer still truncates + HTML-escapes it; this module merely carries it.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public + internal data shapes (NO SDK type escapes this module)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawSession:
    """A session as returned by the SDK adapter — our own shape, no SDK type.

    The adapter (:func:`sdk_list_sessions`) maps each SDK ``SDKSessionInfo`` to this so the
    discovery logic + tests depend only on these plain fields, never on the SDK. ``title``
    is the best human label (``custom_title`` → ``first_prompt`` → ``summary``, whichever
    the SDK gave first); ``last_modified`` is epoch seconds (the SDK reports it as an int).
    """

    session_id: str
    cwd: Optional[str]
    title: Optional[str]
    last_modified: Optional[int]
    git_branch: Optional[str] = None


@dataclass(frozen=True)
class DiscoveredSession:
    """One Claude Code session discovered on the machine (the ``/sessions`` row model).

    Carries only metadata (SB3): the ``session_id`` (full; the render shows a short prefix),
    its ``cwd`` (which can be ANYWHERE on the Mac — SB2 governs *attaching*, not *listing*),
    a ``title`` line (custom title / first prompt / summary — operator-authored prompt text,
    truncated + escaped at render time), the ``last_active`` epoch seconds, ``git_branch``
    if known, and the composite ``running`` hint. Frozen + hashable so dedup/merge is cheap.

    **``liveness_degraded`` (the safe-default-on-doubt signal).** ``running`` answers "did any
    signal say live?" but a clean ``running=False`` is ambiguous between *confidently idle*
    (every signal executed and none fired) and *couldn't tell* (a probe sub-step — the ``ps``
    scan, the registry read, or a transcript stat — RAISED and was swallowed to "no signal").
    ``liveness_degraded`` distinguishes them: it is ``True`` ONLY when an exception occurred
    while gathering this session's liveness (never just because the session is quiet). The
    READ-ONLY ``/sessions`` listing ignores it (a degraded session still shows the idle glyph —
    display-only). But a WRITE/adopt action (``attach_session``, P11 T2) reads it to choose the
    SAFE default: confidently-idle → continue the same id; **uncertain → FORK** (never co-drive
    a possibly-live ``(id, cwd)`` transcript on the strength of a probe that errored).
    """

    session_id: str
    cwd: Optional[str]
    title: Optional[str]
    last_active: Optional[int]
    running: bool = False
    git_branch: Optional[str] = None
    liveness_degraded: bool = False


@dataclass(frozen=True)
class _ProcInfo:
    """A live ``claude`` process seen by ``ps`` (pid, start time, full command).

    ``started`` is the ``ps`` ``lstart`` string (e.g. ``"Wed Jun 24 02:33:31 2026"``);
    ``started_epoch`` is that string parsed to epoch seconds (``None`` if unparseable). The
    epoch is what we validate a process-registry pid against recycling: the registry's
    ``startedAt`` is an epoch-ms field, so an epoch-vs-epoch compare is timezone-robust —
    crucially, the registry's ``procStart`` *string* is in a DIFFERENT timezone than ``ps``
    ``lstart`` (observed +4h skew on this machine), so a naive string compare wrongly fails.
    ``command`` is the full argv line; we scan it for ``--resume <id>`` / ``--session-id
    <id>`` / ``stream-json``.
    """

    pid: int
    started: str
    command: str
    started_epoch: Optional[float] = None


# ---------------------------------------------------------------------------
# Liveness tuning knobs
# ---------------------------------------------------------------------------

#: A transcript whose ``mtime`` is within this many seconds of "now" is treated as a live
#: signal (the session was just writing). A few minutes is the design's "recent" window: a
#: turn can pause between writes, but an idle session goes quiet for far longer. This is one
#: of three signals — being stale here does NOT mean idle (the proc/registry signals may
#: still fire), and being fresh here is enough on its own (a HINT, not a lock).
LIVE_MTIME_WINDOW_SECONDS: float = 180.0

#: ``ps`` argv tokens that mark a process as a Claude *session* runner (as opposed to a
#: daemon / pty-host helper). A process whose command contains ``--resume <id>`` /
#: ``--session-id <id>`` matching the session, OR the ``stream-json`` streaming marker, is a
#: live-session signal. (The live orchestrator may show as a bare ``claude --continue`` with
#: NO id in argv — that case is caught by the process REGISTRY signal instead, not here.)
_STREAM_MARKER = "stream-json"


# ---------------------------------------------------------------------------
# 1. The SDK adapter — the ONLY code that imports the SDK's session API
# ---------------------------------------------------------------------------


def sdk_list_sessions() -> list[_RawSession]:
    """Enumerate every session via the SDK, mapped to :class:`_RawSession` (RB1, SB-pinned).

    This is the **sole** call site of the SDK's ``list_sessions`` (re-exported from
    ``claude_agent_sdk._internal.sessions``; pinned ``==0.2.105``, ADR-001). The import is
    done lazily *inside* the function so that (a) an SDK that is missing/renamed degrades to
    an empty list rather than blowing up at import time, and (b) tests can inject a fake
    lister into :func:`discover_sessions` without importing the SDK at all.

    Maps each ``SDKSessionInfo`` to our own :class:`_RawSession` so no SDK type escapes this
    module — an SDK shape change touches only this mapping. The "title" is the first
    non-empty of ``custom_title`` → ``first_prompt`` → ``summary`` (the SDK populates these
    from the transcript head). **Never raises (RB1):** any import/attribute/call error is
    logged and yields ``[]``; a single malformed entry is skipped, not fatal.
    """
    try:
        from claude_agent_sdk import list_sessions as _sdk_list_sessions
    except Exception:  # SDK missing / API moved (pin drift) → degrade to empty (RB1)
        log.warning("claude_agent_sdk.list_sessions unavailable; no sessions discovered", exc_info=True)
        return []

    try:
        infos = _sdk_list_sessions()
    except Exception:  # a bad ~/.claude, an OSError mid-scan, etc. → empty (RB1)
        log.warning("SDK list_sessions() failed; no sessions discovered", exc_info=True)
        return []

    out: list[_RawSession] = []
    for info in infos or []:
        try:
            session_id = getattr(info, "session_id", None)
            if not session_id:
                continue  # an entry with no id is unusable
            title = (
                getattr(info, "custom_title", None)
                or getattr(info, "first_prompt", None)
                or getattr(info, "summary", None)
            )
            last_modified = _epoch_seconds(getattr(info, "last_modified", None))
            out.append(
                _RawSession(
                    session_id=str(session_id),
                    cwd=_opt_str(getattr(info, "cwd", None)),
                    title=_opt_str(title),
                    last_modified=last_modified,  # already normalized to epoch SECONDS
                    git_branch=_opt_str(getattr(info, "git_branch", None)),
                )
            )
        except Exception:  # one odd record must not sink the whole listing (RB1)
            log.debug("skipping a malformed SDK session record", exc_info=True)
            continue
    return out


def _opt_str(value: object) -> Optional[str]:
    """``str(value)`` for a non-empty value, else ``None`` (defensive normalization)."""
    if value is None:
        return None
    text = str(value)
    return text if text else None


#: Threshold (epoch seconds) above which a timestamp is clearly in MILLISECONDS, not seconds.
#: The SDK's ``SDKSessionInfo.last_modified`` is epoch **milliseconds** (observed live: values
#: ~1.78e12 for 2026) — but ``relative_age`` / the recency sort want epoch **seconds**, so a
#: raw ms value made every row read "just now" and broke the age column. ~1e11 s is the year
#: ~5138, so any real seconds timestamp is far below it and any real ms timestamp far above —
#: a safe, unit-agnostic discriminator that also survives an SDK that ever switches to seconds.
_MILLIS_THRESHOLD = 1e11


def _epoch_seconds(value: object) -> Optional[int]:
    """Normalize an SDK timestamp to epoch **seconds** (the unit the render layer expects).

    The SDK reports ``last_modified`` in epoch **milliseconds**; this divides a millisecond-
    magnitude value (≥ :data:`_MILLIS_THRESHOLD`) by 1000 and leaves an already-seconds value
    untouched, so both a current ms SDK and a hypothetical future seconds SDK normalize
    correctly (the live bug was the render layer treating ms as seconds → permanent "just
    now"). Returns ``None`` for a missing / non-numeric / boolean value (defensive, RB1 — a
    bad timestamp must not crash discovery or mis-sort). Truncates to ``int`` seconds.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = value / 1000.0 if value >= _MILLIS_THRESHOLD else float(value)
    return int(seconds)


# ---------------------------------------------------------------------------
# Transcript path (matches the SDK / CLI sanitization so we stat the right file)
# ---------------------------------------------------------------------------

#: Same rule the CLI/SDK use to turn a cwd into a project-dir name: every non-alphanumeric
#: char → ``-`` (``claude_agent_sdk._internal.sessions._SANITIZE_RE``). We re-derive it here
#: (rather than import the private helper) so this module's ONE SDK coupling stays the
#: ``list_sessions`` call; the transcript path is a stable on-disk convention.
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")
#: The SDK truncates a sanitized name longer than this and appends a hash; bot-discovered
#: cwds are short enough that we never hit it, and the mtime signal is one of three (a miss
#: here just means we lean on the proc/registry signals) — so we keep the simple form.
_MAX_SANITIZED_LENGTH = 200


def claude_home() -> Path:
    """The Claude config dir (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``), like the SDK.

    Used to locate transcripts (``<home>/projects/...``) and the process registry
    (``<home>/sessions/<pid>.json``). Pure path construction; never touches the filesystem.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def transcript_mtime(
    session_id: str,
    cwd: Optional[str],
    *,
    home: Optional[Path] = None,
    degraded: Optional[list[bool]] = None,
) -> Optional[float]:
    """The ``mtime`` (epoch seconds) of ``session_id``'s transcript under ``cwd``, or ``None``.

    The transcript lives at ``<home>/projects/<sanitized-cwd>/<session-id>.jsonl`` (the CLI's
    convention; ``<sanitized-cwd>`` replaces every non-alphanumeric char with ``-``). Returns
    the file's mtime if it exists and we can stat it, else ``None`` (no cwd, missing file, or
    an OSError). Never raises (RB1) — a ``None`` simply means "no mtime signal" for liveness.

    **``degraded`` (P11 T2 — fork-on-doubt).** When a mutable ``degraded`` list is passed, a
    stat error that is **not** a plain not-found (``FileNotFoundError`` / ``NotADirectoryError``)
    — e.g. a ``PermissionError`` or a genuine I/O failure — appends ``True`` to it: the mtime
    signal could not be CONFIDENTLY gathered. A MISSING transcript does NOT degrade (it is a
    confident "no mtime signal" — an idle/gone session), so a clean negative stays a confident
    continue. Callers that don't care about the distinction pass nothing (default ``None``) and
    behavior is unchanged.
    """
    if not session_id or not cwd:
        return None
    base = home or claude_home()
    sanitized = _SANITIZE_RE.sub("-", cwd)[:_MAX_SANITIZED_LENGTH]
    path = base / "projects" / sanitized / f"{session_id}.jsonl"
    try:
        return path.stat().st_mtime
    except (FileNotFoundError, NotADirectoryError):
        # A missing transcript is a CONFIDENT "no mtime signal" (idle/gone) — never degraded.
        return None
    except OSError:
        # A non-not-found stat error (permissions / I/O) — we could not tell. Flag it so a
        # write/adopt action forks on doubt rather than trusting a possibly-stale negative.
        if degraded is not None:
            degraded.append(True)
        return None


# ---------------------------------------------------------------------------
# 2. Composite liveness — process scan + process registry (+ transcript mtime)
# ---------------------------------------------------------------------------

#: Match an explicit session id on a ``claude`` argv line: ``--resume <id>`` or
#: ``--session-id <id>`` (the two flags that bind a process to a specific session). Captures
#: the id so we only count a process that is driving THIS session, not any ``claude`` proc.
_PROC_ID_RE = re.compile(r"--(?:resume|session-id)[=\s]+(\S+)")


def scan_claude_processes(
    *, runner: Optional[Callable[[], str]] = None
) -> list[_ProcInfo]:
    """Return the live ``claude`` processes seen by ``ps`` (RB1 — ``[]`` on any failure).

    Runs ``ps -axww -o pid=,lstart=,command=`` (via ``runner`` if injected — tests pass a
    canned string) and parses each line into a :class:`_ProcInfo`. Only lines whose command
    mentions ``claude`` are kept, and obvious non-session helpers (the bot itself, ``.app``
    bundles, pty-host / daemon helpers) are filtered so we don't false-positive on unrelated
    Claude tooling. The ``lstart`` field is a fixed 5-token date (``Wed Jun 24 06:33:31
    2026``); we split it off the front, the pid off the very front, and treat the remainder
    as the command. **Never raises:** a missing ``ps``, a non-zero exit, or a timeout → ``[]``.
    """
    try:
        raw = runner() if runner is not None else _default_ps()
    except Exception:  # ps missing / timeout / OSError → no proc signal (RB1)
        log.debug("ps scan for claude processes failed", exc_info=True)
        return []

    procs: list[_ProcInfo] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "claude" not in line.lower():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        rest = parts[1]
        # lstart is exactly 5 whitespace-separated tokens: Dow Mon DD HH:MM:SS YYYY.
        lstart_tokens = rest.split(None, 5)
        if len(lstart_tokens) < 6:
            continue
        started = " ".join(lstart_tokens[:5])
        command = lstart_tokens[5]
        if _is_ignorable_proc(command):
            continue
        procs.append(
            _ProcInfo(
                pid=pid,
                started=started,
                command=command,
                started_epoch=_parse_lstart_epoch(started),
            )
        )
    return procs


#: ``ps`` ``lstart`` format: ``Dow Mon DD HH:MM:SS YYYY`` (e.g. ``Wed Jun 24 02:33:31 2026``).
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def _parse_lstart_epoch(started: str) -> Optional[float]:
    """Parse a ``ps`` ``lstart`` string to LOCAL epoch seconds, or ``None`` if it won't parse.

    ``ps`` prints ``lstart`` in the machine's local timezone, so ``strptime`` + ``timestamp``
    (which interprets a naive datetime as local) yields the same epoch the kernel recorded —
    matching the registry's ``startedAt`` epoch-ms field. Defensive: any parse failure → None
    (the registry validation then falls back to the ``procStart`` epoch or a plain pid check).
    """
    try:
        return datetime.strptime(started, _LSTART_FORMAT).timestamp()
    except (ValueError, OverflowError):
        return None


def _default_ps() -> str:
    """Run the real ``ps`` and return stdout (the injectable boundary's default)."""
    result = subprocess.run(
        ["ps", "-axww", "-o", "pid=,lstart=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.stdout or ""


#: Command substrings that mark a ``claude``-mentioning process as NOT a user session we
#: should attribute liveness to: the bot's own repo/process, packaged ``.app`` bundles and
#: VS Code helpers, and the daemon / pty-host / spare background plumbing. Filtering these
#: keeps the proc signal specific (a stray "Claude" helper never marks a session running).
_IGNORABLE_PROC_MARKERS = (
    "claude-telegram",
    "claude_tg",
    ".app/",
    "daemon run",
    "--bg-pty-host",
    "--bg-spare",
    "Claude Helper",
    "grep",
)


def _is_ignorable_proc(command: str) -> bool:
    return any(marker in command for marker in _IGNORABLE_PROC_MARKERS)


def read_process_registry(*, home: Optional[Path] = None) -> list[dict]:
    """Read every ``~/.claude/sessions/<pid>.json`` process-registry entry (RB1 → ``[]``).

    The CLI records a JSON file per live session process: ``{pid, sessionId, cwd, procStart,
    status, ...}``. We return the parsed dicts (only well-formed objects carrying a
    ``sessionId`` and a ``pid``). This is the signal that catches the live orchestrator even
    when its argv has no ``--resume <id>`` (e.g. ``claude --continue``). **Never raises:** a
    missing dir, an unreadable / corrupt file → that entry is skipped; the worst case is
    ``[]``. (Reads metadata only — pid/cwd/sessionId — never transcript content, SB3.)
    """
    base = (home or claude_home()) / "sessions"
    out: list[dict] = []
    try:
        entries = list(base.glob("*.json"))
    except OSError:
        return []
    import json

    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:  # corrupt / unreadable single file → skip, keep going (RB1)
            continue
        if not isinstance(data, dict):
            continue
        if data.get("sessionId") and data.get("pid") is not None:
            out.append(data)
    return out


#: Tolerance (seconds) when matching a registry start time to a ``ps`` start time. ``ps``
#: ``lstart`` is whole-second; the registry's ``startedAt`` is epoch-ms — so the same process
#: differs by sub-second rounding (observed ~0.65 s here). A few seconds absorbs that without
#: ever accepting a genuinely different (recycled) process, whose start time differs by far
#: more.
_START_MATCH_TOLERANCE_SECONDS: float = 5.0


def _pid_alive_as(pid: int, record: dict, procs: Sequence[_ProcInfo]) -> bool:
    """Is ``pid`` alive AND actually the process the registry recorded (anti-recycling)?

    Pids recycle, so a live ``pid`` is **not** proof the registry entry is current — the pid
    might now belong to an unrelated program. We require BOTH the pid be present in the ``ps``
    snapshot AND its start time match what the registry recorded, validated **by epoch** (the
    timezone-robust comparison):

    * **Primary:** the registry's ``startedAt`` (epoch **milliseconds**) vs the matched
      process's ``ps`` ``lstart`` parsed to epoch seconds, within
      :data:`_START_MATCH_TOLERANCE_SECONDS`. This is robust because both are absolute
      instants — unlike the registry's ``procStart`` *string*, which is rendered in a
      DIFFERENT timezone than ``ps`` ``lstart`` (a +4 h skew observed on this machine) and so
      would spuriously fail a naive string compare (the bug that made the live orchestrator
      read idle in the first smoke test).
    * **Fallback:** if ``startedAt`` is absent/odd, parse the ``procStart`` *string* to epoch
      and compare with the same tolerance.
    * **Last resort:** if neither start time is usable, accept the bare live-pid match (best
      effort — the transcript-mtime / argv-id signals still gate the overall verdict).

    This is why we never use a bare ``os.kill(pid, 0)``: it would mark a recycled pid as a
    live Claude session. Defensive (RB1): any parse hiccup degrades toward the looser check,
    never raises.
    """
    match = next((p for p in procs if p.pid == pid), None)
    if match is None:
        return False
    if match.started_epoch is None:
        # We can't place the live process in time → accept the pid match (best effort).
        return True

    # Primary: epoch-vs-epoch against the registry's startedAt (ms).
    started_at = record.get("startedAt")
    if isinstance(started_at, (int, float)):
        return abs(match.started_epoch - float(started_at) / 1000.0) <= _START_MATCH_TOLERANCE_SECONDS

    # Fallback: parse the procStart string to epoch (still epoch-vs-epoch, TZ-robust).
    proc_start = record.get("procStart")
    if isinstance(proc_start, str) and proc_start.strip():
        parsed = _parse_lstart_epoch(proc_start.strip())
        if parsed is not None:
            return abs(match.started_epoch - parsed) <= _START_MATCH_TOLERANCE_SECONDS
        # An unparseable recorded string → fall through to the looser pid match.

    # No usable recorded start time → accept the live-pid match (best effort).
    return True


def probe_liveness(
    session: _RawSession,
    *,
    procs: Sequence[_ProcInfo],
    registry: Sequence[dict],
    now: float,
    home: Optional[Path] = None,
    degraded: Optional[list[bool]] = None,
) -> bool:
    """Composite "is this session running?" — ``True`` if ANY signal fires (a HINT).

    No single signal is reliable, so we OR three independent ones (the design's composite):

    1. **Transcript mtime** — the session's ``.jsonl`` was modified within
       :data:`LIVE_MTIME_WINDOW_SECONDS` of ``now`` (it was just writing).
    2. **Process argv** — a ``claude`` process in ``procs`` has ``--resume`` /
       ``--session-id`` matching this ``session_id``, or carries the ``stream-json`` marker
       (a streaming-session runner).
    3. **Process registry** — a ``~/.claude/sessions/<pid>.json`` entry names this
       ``sessionId`` AND its ``pid`` is alive *and validated* against recycling
       (:func:`_pid_alive_as` — pid in ``ps`` with a matching ``procStart``). This is what
       catches the live orchestrator whose argv shows no id (``claude --continue``).

    All inputs are injected (``procs`` / ``registry`` / ``now`` / ``home``) so this is a pure
    function over fakes in tests — no real ``ps``, no real ``~/.claude``. Returns a HINT;
    discovery never treats "running" as a lock (this is a read-only listing). Defensive: a
    malformed proc/registry entry is skipped, never fatal (RB1).

    **``degraded`` (P11 T2 — fork-on-doubt).** When a mutable ``degraded`` list is passed, a
    sub-step that could not be CONFIDENTLY evaluated (today: a non-not-found transcript stat
    error — see :func:`transcript_mtime`) appends ``True`` to it. The scan-level signal
    failures (``proc_scan`` / ``registry`` raising) are recorded by :meth:`SessionDiscovery.discover`
    BEFORE this call (they degrade ALL sessions' signals 2+3 for the call) and OR-ed with this.
    A clean negative (every signal ran, none fired) leaves it untouched — confidently idle.
    """
    sid = session.session_id
    if not sid:
        return False

    # 1. Transcript mtime within the recent window (a non-not-found stat error → degraded).
    mtime = transcript_mtime(sid, session.cwd, home=home, degraded=degraded)
    if mtime is not None and (now - mtime) <= LIVE_MTIME_WINDOW_SECONDS:
        return True

    # 2. A claude process explicitly bound to this id (or any streaming-json runner).
    for proc in procs:
        cmd = proc.command
        if _STREAM_MARKER in cmd:
            # A stream-json runner that also names this id is a strong match; a bare
            # stream-json proc with a DIFFERENT id is not ours. Prefer an id match.
            ids = _PROC_ID_RE.findall(cmd)
            if not ids or sid in ids:
                if not ids:
                    # No explicit id on a stream-json runner → can't attribute it to a
                    # specific session; skip (avoid a false positive across sessions).
                    continue
                return True
        ids = _PROC_ID_RE.findall(cmd)
        if sid in ids:
            return True

    # 3. The process registry, pid validated against recycling.
    for record in registry:
        try:
            if record.get("sessionId") != sid:
                continue
            pid = record.get("pid")
            if not isinstance(pid, int):
                continue
            if _pid_alive_as(pid, record, procs):
                return True
        except Exception:  # one odd registry record must not break the probe (RB1)
            continue

    return False


# ---------------------------------------------------------------------------
# 3. discover_sessions — glue the adapter + the composite probe
# ---------------------------------------------------------------------------


@dataclass
class SessionDiscovery:
    """Discover all machine sessions, with every external dependency injected (testable).

    The three seams default to the real implementations but are overridable so a test can
    feed a fixed session list + a fixed liveness verdict with NO dependency on the real SDK,
    ``ps``, or ``~/.claude``:

    * ``lister``     — returns the raw sessions (default: :func:`sdk_list_sessions`).
    * ``proc_scan``  — returns the live ``claude`` processes (default:
      :func:`scan_claude_processes`).
    * ``registry``   — returns the process-registry dicts (default:
      :func:`read_process_registry`).
    * ``clock``      — monotonic-independent wall seconds for the mtime window (default:
      :func:`time.time`).

    :meth:`discover` does the work; the module-level :func:`discover_sessions` is the
    zero-config entry point the bot calls (all real deps).
    """

    lister: Callable[[], list[_RawSession]] = sdk_list_sessions
    proc_scan: Callable[[], list[_ProcInfo]] = scan_claude_processes
    registry: Callable[[], list[dict]] = read_process_registry
    clock: Callable[[], float] = time.time
    home: Optional[Path] = field(default=None)

    def discover(self) -> list[DiscoveredSession]:
        """Return every discovered session with its composite liveness (RB1 — never raises).

        Pulls the raw sessions from ``lister`` and probes each for liveness against ONE
        ``ps`` snapshot + ONE registry read (taken once, not per session). Any failure in a
        seam degrades to "what we can" (the SDK lister already returns ``[]`` on failure; the
        proc/registry scans likewise) so a broken ``ps`` just means everything reads idle, a
        broken SDK means an empty list — discovery is best-effort and total.
        """
        try:
            raw = self.lister() or []
        except Exception:  # a custom lister that misbehaves must not crash /sessions (RB1)
            log.warning("session lister failed; returning no sessions", exc_info=True)
            return []
        # P11 T2 (fork-on-doubt): a SCAN failure degrades liveness for EVERY session this call
        # (signals 2+3 are gathered once, here). Record it so attach forks on doubt rather than
        # trusting a negative computed with no proc/registry data. /sessions display ignores it.
        scan_degraded = False
        try:
            procs = self.proc_scan() or []
        except Exception:
            procs = []
            scan_degraded = True
        try:
            registry = self.registry() or []
        except Exception:
            registry = []
            scan_degraded = True
        now = _safe_now(self.clock)

        out: list[DiscoveredSession] = []
        for rs in raw:
            # Per-session degradation sink: the mtime sub-step appends to it on a non-not-found
            # stat error (transcript_mtime), and a whole-probe exception below sets it too. OR
            # in the call-wide scan failure so a broken ps/registry marks every session uncertain.
            sink: list[bool] = []
            try:
                running = probe_liveness(
                    rs, procs=procs, registry=registry, now=now, home=self.home, degraded=sink
                )
            except Exception:  # a liveness hiccup → assume idle (RB1), but flag it UNCERTAIN so
                # a write/adopt action forks on doubt; the read-only listing still shows it idle.
                running = False
                sink.append(True)
            out.append(
                DiscoveredSession(
                    session_id=rs.session_id,
                    cwd=rs.cwd,
                    title=rs.title,
                    last_active=rs.last_modified,
                    running=running,
                    git_branch=rs.git_branch,
                    liveness_degraded=scan_degraded or bool(sink),
                )
            )
        return out


def _safe_now(clock: Callable[[], float]) -> float:
    """``clock()`` as a float, falling back to ``time.time()`` if it misbehaves (RB1)."""
    try:
        return float(clock())
    except Exception:
        return time.time()


def discover_sessions() -> list[DiscoveredSession]:
    """Discover all Claude Code sessions on the machine (zero-config, all real deps; RB1).

    The bot's ``/sessions`` entry point. Equivalent to ``SessionDiscovery().discover()`` —
    uses the real SDK lister, the real ``ps`` scan, the real process registry, and wall
    time. Never raises: every dependency degrades cleanly to "list what we can" (an empty
    list is a fine answer when ``~/.claude`` is absent or the SDK is unavailable).
    """
    return SessionDiscovery().discover()
