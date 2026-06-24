"""Configuration loaded from environment / .env (no secrets hardcoded)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | os.PathLike[str]) -> None:
    """Minimal .env loader: ``KEY=VALUE`` lines. Does NOT override existing env vars."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def parse_chat_ids(raw: str) -> set[int]:
    """Parse a comma/semicolon-separated list of numeric chat ids."""
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ValueError(
                f"Invalid chat id {part!r} in TELEGRAM_ALLOWED_CHAT_IDS (must be an integer)"
            ) from exc
    return ids


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def parse_allowed_roots(raw: str, *, default: Path) -> tuple[Path, ...]:
    """Parse the ``/cd`` confinement allow-list (SB2), tolerant of separators.

    Splits ``raw`` on BOTH ``os.pathsep`` (``:`` on POSIX, ``;`` on Windows) AND comma
    — mirroring :func:`parse_chat_ids`'s tolerance so the owner can use whichever feels
    natural — and ``expanduser().resolve()`` each entry to a canonical absolute path.

    **If none are given, returns ``(default,)``** — this is the locked design decision
    (T8 / progress.md SB2): confinement is ON by default and the single default root is
    the workdir (which itself defaults to ``$HOME``). The owner widens by listing roots
    here, or disables containment entirely via ``ALLOW_ANY_PATH=true``. Returning the
    default (never an empty tuple) means "unset" is safe-but-usable, while an explicitly
    empty allow-list combined with ``allow_any=False`` would fail closed at resolve time.
    """
    roots: list[Path] = []
    for part in raw.replace(os.pathsep, ",").split(","):
        part = part.strip()
        if not part:
            continue
        roots.append(Path(part).expanduser().resolve())
    if not roots:
        return (default,)
    return tuple(roots)


#: T4 (P9) — the default fast/deep model ids for ``/fast`` · ``/deep`` per-project
#: routing. ``/fast`` selects :data:`DEFAULT_FAST_MODEL` (Haiku — cheap + quick),
#: ``/deep`` selects :data:`DEFAULT_DEEP_MODEL` (Opus — the most capable). Both are
#: env-overridable (``FAST_MODEL`` / ``DEEP_MODEL``) so the operator can re-pin them
#: without a code change as new model ids ship — kept in ONE place rather than scattered
#: literals (the ids below are the current sensible defaults; verified against the
#: claude-api skill). ``/auto`` clears the per-project override → the configured
#: ``CLAUDE_MODEL`` (or the SDK default if unset).
DEFAULT_FAST_MODEL = "claude-haiku-4-5"
DEFAULT_DEEP_MODEL = "claude-opus-4-8"

#: Valid values for ENGINE_MODE (S4 flag). ``oneshot`` keeps today's behavior; the
#: live bot is never broken until the owner flips to ``streaming``.
ENGINE_MODES = ("oneshot", "streaming")


def parse_engine_mode(raw: str | None) -> str:
    """Parse + validate ENGINE_MODE (default ``oneshot``).

    The S4 migration flag: ``oneshot`` (default) routes to the existing one-shot
    runner unchanged; ``streaming`` selects the new engine. Empty/unset -> default;
    case-insensitive; anything else is a configuration error (fail loud at startup,
    not silently fall back, so a typo can't quietly disable streaming).
    """
    if raw is None or not raw.strip():
        return "oneshot"
    mode = raw.strip().lower()
    if mode not in ENGINE_MODES:
        raise ValueError(
            f"ENGINE_MODE must be one of {ENGINE_MODES}, got {raw!r}"
        )
    return mode


#: Default answer-hold backstop: 60 minutes (decision-log #4 / ADR-002). The engine's
#: per-request timer auto-resolves a pending interactive decision to DENY + notify when
#: this elapses, leaving the session usable. Configurable via ``ANSWER_BACKSTOP_SECONDS``.
DEFAULT_ANSWER_BACKSTOP_SECONDS = 3600

#: Default concurrency cap (P5 / ADR-005 D6): at most 3 turns RUN at once across the
#: whole process; a turn started while at the cap is QUEUED (FIFO, per chat) and starts
#: when a slot frees — never refused, never dropped (SB6 fail-closed → queue). The cap
#: protects host CPU + the shared CLI/SDK + the Telegram send budget. Configurable via
#: ``MAX_CONCURRENT_RUNS``.
DEFAULT_MAX_CONCURRENT_RUNS = 3

#: Default per-chat send-rate budget (P5 / ADR-005 D8): the minimum seconds between any
#: two outbound sends/edits/notifications for ONE chat. Under concurrency N projects
#: flushing at once (plus their proactive pings) would burst past Telegram's ~1 msg/s/chat
#: ceiling, so ALL outbound for a chat funnels through a per-chat ``ChatSendGate`` spaced
#: at this interval (verbatim prioritized over status churn — never dropped, D8). 1 s is
#: the conservative budget. Configurable via ``RENDER_CHAT_SEND_INTERVAL_SECONDS``; only
#: consulted in streaming mode.
DEFAULT_CHAT_SEND_INTERVAL_SECONDS = 1.0

#: Default per-message liveness bound (P6/H2/RB2): the max seconds the streaming substrate
#: waits for the NEXT SDK message before declaring Claude wedged and surfacing a clean
#: ``driver_error`` (RB2). It bounds BOTH a genuinely-silent Claude AND the execution time
#: of an APPROVED long-running tool (a build / test-run / install) that legitimately emits
#: no intermediate message for minutes — so it must be GENEROUS or such a tool trips it and
#: a completed-but-slow turn is reported as a spurious ``driver_error`` + needless engine
#: rebuild. The earlier hardcoded 120 s was too tight for a coding agent; 300 s lets normal
#: multi-minute tools finish while a truly-hung Claude still eventually times out. The bound
#: is SUSPENDED entirely while a decision hold is open (the human-approval wait is bounded by
#: the ~60-min answer-backstop instead — see ``adapter_sdk._next_message``). Configurable via
#: ``STREAM_MESSAGE_TIMEOUT_SECONDS``; only consulted in streaming mode.
DEFAULT_STREAM_MESSAGE_TIMEOUT_SECONDS = 300.0

#: Default max inbound image size (P10 T1, multimodal): 5 MB. A photo/screenshot the
#: operator sends is downloaded, base64-encoded, and threaded into a turn as an image
#: content block; an image larger than this is REJECTED with a clean message at the
#: handler (never downloaded into a turn). The cap protects host memory + the SDK/model
#: request size; 5 MB comfortably covers a phone screenshot while bounding abuse.
#: Configurable via ``IMAGE_MAX_BYTES``. Only consulted in streaming mode.
DEFAULT_IMAGE_MAX_BYTES = 5 * 1024 * 1024

#: Default max file size (P10 T3, file send/receive): 20 MB — both directions. An inbound
#: non-image ``Document`` is saved (path-confined) into the active project's cwd, and
#: ``/get <path>`` uploads an in-root file back to the chat; a file larger than this is
#: REFUSED with a clean message (never written to disk inbound, never uploaded outbound).
#: The cap bounds host disk/memory + the Telegram upload budget (Telegram's own bot upload
#: ceiling is ~50 MB, so 20 MB is a comfortable, conservative default that covers ordinary
#: code/log/patch attachments). Configurable via ``FILE_MAX_BYTES``. Only consulted in
#: streaming mode (the inbound save + ``/get`` are streaming-mode surfaces — see bot.py).
DEFAULT_FILE_MAX_BYTES = 20 * 1024 * 1024


def parse_answer_backstop_seconds(raw: str | None) -> int:
    """Parse + validate ANSWER_BACKSTOP_SECONDS (default 3600 = 60 min).

    The streaming engine holds a pending ``ask``/``plan`` request open while the
    operator decides; this is the harness-side backstop (ADR-002) that auto-denies +
    notifies if no answer arrives. Empty/unset -> default; must be a positive integer
    (fail loud on a bad value rather than silently using a surprising hold length).
    Per ADR-002 the figure should sit BELOW any later-observed CLI/model ceiling — the
    5-60 min band is untested — so it is deliberately configurable down.
    """
    if raw is None or not raw.strip():
        return DEFAULT_ANSWER_BACKSTOP_SECONDS
    try:
        seconds = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"ANSWER_BACKSTOP_SECONDS must be an integer, got {raw!r}"
        ) from exc
    if seconds <= 0:
        raise ValueError("ANSWER_BACKSTOP_SECONDS must be positive")
    return seconds


def parse_max_concurrent_runs(raw: str | None) -> int:
    """Parse + validate MAX_CONCURRENT_RUNS (default 3; P5 / ADR-005 D6).

    Bounds simultaneously-*executing* runs across the whole process: a turn started
    while at the cap is QUEUED (FIFO, per chat) and starts when a slot frees, never
    refused (D6/SB6). Parsing mirrors :func:`parse_answer_backstop_seconds` with one
    deliberate difference (the locked D6 rule): **empty/unset/``0`` → the default 3**
    (``0`` reads as "unset" — a cap of zero would deadlock every turn, so it is treated
    as the default rather than accepted), while a **negative or non-integer** value is a
    configuration error and fails loud at startup (a typo must not silently change the
    cap). So ``""``/unset/``"0"`` → 3; ``"5"`` → 5; ``"-1"``/``"x"`` → raise.
    """
    if raw is None or not raw.strip():
        return DEFAULT_MAX_CONCURRENT_RUNS
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"MAX_CONCURRENT_RUNS must be an integer, got {raw!r}"
        ) from exc
    if value < 0:
        raise ValueError("MAX_CONCURRENT_RUNS must not be negative")
    if value == 0:
        # 0 == "unset" (a zero cap would queue every turn forever — deadlock). Treat it
        # as the default rather than accept an unusable cap (D6).
        return DEFAULT_MAX_CONCURRENT_RUNS
    return value


def parse_chat_send_interval_seconds(raw: str | None) -> float:
    """Parse + validate RENDER_CHAT_SEND_INTERVAL_SECONDS (default ~1 s; P5 / ADR-005 D8).

    The minimum seconds between any two outbound sends for one chat — the per-chat send
    budget the :class:`~claude_tg.render.ChatSendGate` enforces so N concurrent projects'
    status edits + notifications never burst past Telegram's ~1 msg/s/chat ceiling.
    Empty/unset → the default; must be a **non-negative** number (``0`` disables the
    spacing — every send goes immediately — which is a valid choice for a low-traffic
    deployment, unlike the concurrency cap where ``0`` would deadlock). A negative or
    non-numeric value is a configuration error and fails loud at startup (a typo must not
    silently change the budget). So ``""``/unset → 1.0; ``"0"`` → 0.0; ``"2.5"`` → 2.5;
    ``"-1"``/``"x"`` → raise.
    """
    if raw is None or not raw.strip():
        return DEFAULT_CHAT_SEND_INTERVAL_SECONDS
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"RENDER_CHAT_SEND_INTERVAL_SECONDS must be a number, got {raw!r}"
        ) from exc
    if value < 0:
        raise ValueError("RENDER_CHAT_SEND_INTERVAL_SECONDS must not be negative")
    return value


def parse_stream_message_timeout_seconds(raw: str | None) -> float:
    """Parse + validate STREAM_MESSAGE_TIMEOUT_SECONDS (default 300 s; P6/H2/RB2).

    The per-message liveness bound the streaming substrate applies while waiting for the
    next SDK message (suspended while a decision hold is open). It must be GENEROUS: it
    also governs how long an APPROVED long-running tool (build/test/install) may run with
    no intermediate message before the turn is declared wedged, so too small a value turns
    a slow-but-fine tool into a spurious ``driver_error``. Parsing mirrors
    :func:`parse_answer_backstop_seconds`: empty/unset → the default; must be a **positive**
    number (a ``0``/negative bound would time out every message instantly — nothing could
    complete — so, unlike the send interval where ``0`` validly disables spacing, it is a
    configuration error here and fails loud at startup rather than silently wedging every
    turn). So ``""``/unset → 300.0; ``"600"`` → 600.0; ``"0"``/``"-1"``/``"x"`` → raise.
    """
    if raw is None or not raw.strip():
        return DEFAULT_STREAM_MESSAGE_TIMEOUT_SECONDS
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"STREAM_MESSAGE_TIMEOUT_SECONDS must be a number, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError("STREAM_MESSAGE_TIMEOUT_SECONDS must be positive")
    return value


def parse_image_max_bytes(raw: str | None) -> int:
    """Parse + validate IMAGE_MAX_BYTES (default 5 MB; P10 T1, multimodal).

    The max size (in BYTES) of an inbound photo/screenshot the bot will accept and thread
    into a turn as an image content block; a larger image is refused with a clean message
    at the handler (never downloaded into a turn). Parsing mirrors
    :func:`parse_stream_message_timeout_seconds`: empty/unset → the default; must be a
    **positive** integer (a ``0``/negative cap would reject every image — so it is a
    configuration error and fails loud at startup rather than silently disabling images).
    So ``""``/unset → 5 MB; ``"1048576"`` → 1 MB; ``"0"``/``"-1"``/``"x"`` → raise.
    """
    if raw is None or not raw.strip():
        return DEFAULT_IMAGE_MAX_BYTES
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"IMAGE_MAX_BYTES must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError("IMAGE_MAX_BYTES must be positive")
    return value


#: Default voice-transcription subprocess timeout (P10 T2): 120 s. A configured
#: ``TRANSCRIBE_CMD`` (a local whisper.cpp run, an API CLI, …) is run under this wall-clock
#: bound; if it does not finish in time it is killed and the operator gets a clean timeout
#: error (RB2) rather than the handler hanging forever. 120 s comfortably covers a short
#: voice note through a small local model on a Mac while bounding a wedged transcriber.
#: Configurable via ``TRANSCRIBE_TIMEOUT_SECONDS``.
DEFAULT_TRANSCRIBE_TIMEOUT_SECONDS = 120.0


def parse_transcribe_timeout_seconds(raw: str | None) -> float:
    """Parse + validate TRANSCRIBE_TIMEOUT_SECONDS (default 120 s; P10 T2, voice notes).

    The wall-clock bound the configured ``TRANSCRIBE_CMD`` subprocess runs under; on
    timeout it is killed and the operator gets a clean error (RB2). Parsing mirrors
    :func:`parse_stream_message_timeout_seconds`: empty/unset → the default; must be a
    **positive** number (a ``0``/negative bound would kill every transcribe instantly —
    nothing could transcribe — so it is a configuration error and fails loud at startup
    rather than silently breaking voice). So ``""``/unset → 120.0; ``"300"`` → 300.0;
    ``"0"``/``"-1"``/``"x"`` → raise.
    """
    if raw is None or not raw.strip():
        return DEFAULT_TRANSCRIBE_TIMEOUT_SECONDS
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"TRANSCRIBE_TIMEOUT_SECONDS must be a number, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError("TRANSCRIBE_TIMEOUT_SECONDS must be positive")
    return value


def parse_file_max_bytes(raw: str | None) -> int:
    """Parse + validate FILE_MAX_BYTES (default 20 MB; P10 T3, file send/receive).

    The max size (in BYTES) of a file the bot will accept inbound (a non-image ``Document``
    saved into the active project's cwd) OR upload outbound (``/get <path>``); a larger file
    is refused with a clean message (inbound: never written to disk; outbound: never
    uploaded). Parsing mirrors :func:`parse_image_max_bytes`: empty/unset → the default;
    must be a **positive** integer (a ``0``/negative cap would reject every file — so it is a
    configuration error and fails loud at startup rather than silently disabling file
    transfer). So ``""``/unset → 20 MB; ``"1048576"`` → 1 MB; ``"0"``/``"-1"``/``"x"`` →
    raise.
    """
    if raw is None or not raw.strip():
        return DEFAULT_FILE_MAX_BYTES
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"FILE_MAX_BYTES must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError("FILE_MAX_BYTES must be positive")
    return value


#: Default audit-log rotation bound (P13 T-AUDIT): 5 MB before a 1-keep rotation, so the
#: durable audit JSONL is bounded to ~2x this on disk (the live file + a single ``.1``
#: keep). The audit log is body-free (SB3) — only tool names, ``safe_input_summary``
#: outputs, decisions, redacted ids + timestamps — written atomically with ``0600`` perms
#: next to ``CLAUDE_STATE_FILE``. Configurable via ``AUDIT_LOG_MAX_BYTES``.
DEFAULT_AUDIT_LOG_MAX_BYTES = 5 * 1024 * 1024

#: The suffix appended to ``CLAUDE_STATE_FILE`` to derive the default audit-log path when
#: ``AUDIT_LOG_FILE`` is unset (e.g. ``<state>.audit.jsonl``). It lives next to the session
#: store so it inherits the same dir + ``0600`` posture (P13 T-AUDIT design §1.2).
AUDIT_LOG_SUFFIX = ".audit.jsonl"

#: Values that EXPLICITLY disable the audit log when set as ``AUDIT_LOG_FILE`` (case-
#: insensitive). An empty string also disables it. Otherwise the value is a path.
_AUDIT_DISABLE_VALUES = frozenset({"", "off", "none", "disabled", "0", "false"})


def parse_audit_log_max_bytes(raw: str | None) -> int:
    """Parse + validate AUDIT_LOG_MAX_BYTES (default 5 MB; P13 T-AUDIT).

    The size (in BYTES) the durable audit JSONL may reach before a 1-keep rotation
    (``<file>.1``). Parsing mirrors :func:`parse_file_max_bytes`: empty/unset → the
    default; must be a **positive** integer (a ``0``/negative bound would rotate on every
    write or never — so it is a configuration error and fails loud at startup rather than
    silently producing a degenerate log). So ``""``/unset → 5 MB; ``"1048576"`` → 1 MB;
    ``"0"``/``"-1"``/``"x"`` → raise.
    """
    if raw is None or not raw.strip():
        return DEFAULT_AUDIT_LOG_MAX_BYTES
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"AUDIT_LOG_MAX_BYTES must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError("AUDIT_LOG_MAX_BYTES must be positive")
    return value


#: Valid values for BASH_POLICY_MODE (P13 T-BASH). ``flag`` (default) ESCALATES the existing
#: approval prompt for a matched dangerous Bash command (⚠️ + the matched pattern, no
#: [Allow for session], and a re-prompt even under an active session-grant/yolo — the
#: deliberate C2-residual closure); ``deny`` AUTO-DENIES a matched command (a hard wall,
#: overriding grant/yolo); ``off`` disables the policy entirely → today's exact gate.
BASH_POLICY_MODES = ("flag", "deny", "off")

#: The default Bash policy mode. ``flag`` is the design recommendation (P13 T-BASH §2.1):
#: NON-BREAKING (it only ADDS friction to already-RISKY commands that already prompt — it can
#: never make anything auto-run that doesn't today) and never blocks a genuine need (a false
#: positive costs one extra tap, not a wedge). Set ``BASH_POLICY_MODE=off`` for byte-for-byte
#: pre-P13 behavior, or ``deny`` for a hard wall.
DEFAULT_BASH_POLICY_MODE = "flag"


def parse_bash_policy_mode(raw: str | None) -> str:
    """Parse + validate BASH_POLICY_MODE (default ``flag``; P13 T-BASH).

    The Bash command-policy mode layered ADDITIVELY on the approval gate: ``flag`` (default)
    escalates a matched dangerous command's prompt (and overrides grant/yolo for it),
    ``deny`` auto-denies it, ``off`` disables the policy (today's exact behavior). Parsing
    mirrors :func:`parse_engine_mode`: empty/unset → the default; case-insensitive; anything
    not in :data:`BASH_POLICY_MODES` is a configuration error and **fails loud at startup**
    (a typo must not silently disable the guardrail — fail-safe). So ``""``/unset → ``flag``;
    ``"DENY"`` → ``deny``; ``"strict"``/``"x"`` → raise.
    """
    if raw is None or not raw.strip():
        return DEFAULT_BASH_POLICY_MODE
    mode = raw.strip().lower()
    if mode not in BASH_POLICY_MODES:
        raise ValueError(
            f"BASH_POLICY_MODE must be one of {BASH_POLICY_MODES}, got {raw!r}"
        )
    return mode


def parse_bash_policy_extra_patterns(raw: str | None) -> tuple[str, ...]:
    """Parse BASH_POLICY_EXTRA_PATTERNS into a tuple of extra denylist regexes (P13 T-BASH).

    Owner-supplied patterns ADDITIVE to the conservative built-in denylist (the built-ins
    can NOT be removed via config — dropping a safety pattern must be a code change,
    fail-safe). The value is split on **newlines** and **semicolons** (a regex legitimately
    contains commas, so — unlike :func:`parse_chat_ids` — comma is NOT a separator), each
    entry stripped, blanks dropped. Empty/unset → an empty tuple (built-ins only).

    **Fail-LOUD on a malformed regex (Codex BLOCKER 2).** Each pattern is COMPILED here, at
    config load, via :func:`~claude_tg.bash_policy.validate_extra_patterns`; an invalid regex
    raises :class:`~claude_tg.bash_policy.InvalidBashPattern` so the owner learns at STARTUP
    (consistent with the other fail-loud knobs like :func:`parse_bash_policy_mode`). Silently
    dropping it would be fail-OPEN — the owner's rule meant to catch a dangerous command would
    be lost, and that command would then auto-allow under a grant/``/yolo``.
    """
    if raw is None or not raw.strip():
        return ()
    parts: list[str] = []
    for chunk in raw.replace(";", "\n").split("\n"):
        entry = chunk.strip()
        if entry:
            parts.append(entry)
    patterns = tuple(parts)
    # Fail loud on a malformed regex (the policy module owns compilation). Imported here
    # (not at module top) to keep config.py import-light and the import local to this knob.
    from .bash_policy import validate_extra_patterns

    validate_extra_patterns(patterns)
    return patterns


def resolve_audit_log_file(raw: str | None, *, state_file: Path | None) -> Path | None:
    """Resolve the audit-log path (P13 T-AUDIT design §1.2) — default-on but non-breaking.

    Precedence:

    * ``AUDIT_LOG_FILE`` set to a real path → that path (``expanduser()``), the explicit
      override.
    * ``AUDIT_LOG_FILE`` set to an explicit disable token (``""``/``off``/``none``/
      ``disabled``/``0``/``false``, case-insensitive) → ``None`` (audit OFF), the
      documented full-disable.
    * ``AUDIT_LOG_FILE`` UNSET + a ``state_file`` configured → ``<state_file><suffix>``
      (next to the session store, inheriting its dir + ``0600`` posture). This is the
      **default-on** behavior: a stateful deploy gets an audit log out-of-the-box.
    * ``AUDIT_LOG_FILE`` UNSET + NO ``state_file`` (a stateless oneshot deploy) → ``None``
      (audit OFF unless ``AUDIT_LOG_FILE`` is set explicitly). Non-breaking: a deploy with
      no durable state stays exactly as before (no surprise file).

    Returns the resolved :class:`~pathlib.Path`, or ``None`` when audit is disabled. Pure
    (no I/O — the caller/``AuditLog`` creates the file lazily on first append).
    """
    if raw is not None and raw.strip().casefold() in _AUDIT_DISABLE_VALUES:
        # An explicit disable token (incl. an explicit empty string) → audit OFF.
        return None
    value = (raw or "").strip()
    if value:
        return Path(value).expanduser()
    # Unset/blank → default next to the state file (default-on), or off if no state file.
    if state_file is not None:
        return state_file.with_name(state_file.name + AUDIT_LOG_SUFFIX)
    return None


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_chat_ids: frozenset[int]
    workdir: Path
    claude_bin: str = "claude"
    model: str | None = None
    # T4 (P9): the fast/deep model ids for the per-project ``/fast`` · ``/deep`` override.
    # Env-overridable (FAST_MODEL / DEEP_MODEL); default to the sensible current ids
    # (DEFAULT_FAST_MODEL / DEFAULT_DEEP_MODEL). The per-project CHOICE lives in the session
    # store; these are just the id each choice resolves to. ``/auto`` clears the override
    # back to ``model`` (CLAUDE_MODEL) / the SDK default.
    fast_model: str = DEFAULT_FAST_MODEL
    deep_model: str = DEFAULT_DEEP_MODEL
    timeout_seconds: int = 600
    # SB5 / C1: the operator approval-gate bypass. Default **False** = the permission
    # gate is ON, so a fresh install (and any bare ``Config(...)``) runs Claude's tools
    # behind the CLI's approval prompt. Setting this True opts INTO
    # ``--dangerously-skip-permissions`` (allow-all) on the oneshot path — a loud,
    # explicit choice surfaced as a WARNING at startup (see main.py). The safe state is
    # the default; the bypass is reachable only by an explicit opt-in.
    skip_permissions: bool = False
    state_file: Path | None = None
    # S4 migration flag: "oneshot" (default, existing behavior) | "streaming" (P1 engine).
    # bot.py reads this to select the runner; T4 only parses/validates it (T7 wires the switch).
    engine_mode: str = "oneshot"
    # Streaming engine answer-hold backstop (ADR-002): seconds to hold a pending
    # ask/plan request before auto-denying + notifying. Default 60 min; T7 passes it
    # to the Engine. Only consulted in streaming mode.
    answer_backstop_seconds: int = DEFAULT_ANSWER_BACKSTOP_SECONDS
    # P5 / ADR-005 D6 concurrency cap: the max number of turns that RUN at once across the
    # whole process (streaming mode). A turn started while at the cap is QUEUED (FIFO, per
    # chat) and runs when a slot frees — never refused (SB6 → queue). Default 3; 0/unset →
    # default; negative/non-integer → fail loud (parse_max_concurrent_runs). Only consulted
    # in streaming mode.
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS
    # P5 / ADR-005 D8 per-chat send budget: the minimum seconds between any two outbound
    # sends/edits/notifications for ONE chat (streaming mode). The per-chat ChatSendGate
    # spaces ALL outbound at this interval so N concurrent projects' status edits + pings
    # never burst past Telegram's ~1 msg/s/chat ceiling (verbatim prioritized; never
    # dropped — D8). Default ~1 s; unset → default; negative/non-numeric → fail loud
    # (parse_chat_send_interval_seconds). Only consulted in streaming mode.
    render_chat_send_interval_seconds: float = DEFAULT_CHAT_SEND_INTERVAL_SECONDS
    # P6/H2/RB2 per-message liveness bound: the max seconds the streaming substrate waits
    # for the next SDK message before declaring Claude wedged and yielding a clean
    # driver_error (suspended while a decision hold is open — the human-approval wait is
    # bounded by answer_backstop_seconds instead). GENEROUS because it ALSO bounds how long
    # an approved long-running tool (build/test/install) may run with no intermediate
    # message — too tight and a slow-but-fine tool trips a spurious driver_error. Threaded
    # to the Engine's send_timeout by stream_session's factory. Default 300 s; unset →
    # default; 0/negative/non-numeric → fail loud (parse_stream_message_timeout_seconds).
    # Only consulted in streaming mode.
    stream_message_timeout_seconds: float = DEFAULT_STREAM_MESSAGE_TIMEOUT_SECONDS
    # SB2 /cd path confinement (decision-log: confinement ON by default). The canonical
    # roots a `/cd` target must sit inside; the default is `(workdir,)` (set by
    # from_env), so an unset ALLOWED_ROOTS confines /cd to the workdir (which itself
    # defaults to $HOME). The owner widens via ALLOWED_ROOTS. An empty tuple combined
    # with allow_any_path=False rejects every /cd (fail-closed, SB6).
    allowed_roots: tuple[Path, ...] = ()
    # SB2 explicit opt-out: ALLOW_ANY_PATH=true disables /cd containment entirely (the
    # owner takes the wheel). Default False — confinement is the safe default (SB6).
    allow_any_path: bool = False
    # P10 T1 (multimodal): the max size in BYTES of an inbound photo/screenshot the bot
    # accepts and threads into a turn as an image content block. An image larger than this
    # is refused at the handler with a clean message (never downloaded into a turn) — it
    # bounds host memory + the SDK/model request size. Default 5 MB; unset → default;
    # 0/negative/non-integer → fail loud (parse_image_max_bytes). Only consulted in
    # streaming mode (the multimodal turn path is streaming-only — see bot.py).
    image_max_bytes: int = DEFAULT_IMAGE_MAX_BYTES
    # P10 T3 (file send/receive): the max size in BYTES of a file the bot accepts inbound
    # (a non-image Document saved, path-confined, into the active project's cwd) OR uploads
    # outbound (/get <path>). A file larger than this is refused with a clean message —
    # inbound it is never written to disk, outbound it is never uploaded. It bounds host
    # disk/memory + the Telegram upload budget. Default 20 MB; unset → default;
    # 0/negative/non-integer → fail loud (parse_file_max_bytes). Only consulted in streaming
    # mode (the inbound save + /get are streaming-mode surfaces — see bot.py).
    file_max_bytes: int = DEFAULT_FILE_MAX_BYTES
    # P10 T2 (voice notes, PLUGGABLE backend): the shell-command TEMPLATE the bot runs to
    # transcribe a downloaded voice note. EMPTY/unset (the default) = voice is GRACEFULLY OFF
    # — a voice note gets a clean "transcription isn't set up" setup message (never a crash).
    # When set it is a template with placeholders the bot substitutes safely (split into argv
    # — NEVER shell=True with interpolated data): ``{audio}`` = the input audio path (a temp
    # file the bot controls) and ``{out}`` = an output basename. The transcript is read from
    # the command's STDOUT, or — if the template contains ``{out}`` — from the produced
    # ``{out}.txt`` (whisper.cpp's ``-otxt -of {out}`` convention). See claude_tg/voice.py
    # for the full contract. Env: ``TRANSCRIBE_CMD``. Only consulted in streaming mode.
    transcribe_cmd: str = ""
    # P10 T2: the wall-clock bound the TRANSCRIBE_CMD subprocess runs under (seconds); on
    # timeout it is killed and the operator gets a clean error (RB2). Default 120 s; unset →
    # default; 0/negative/non-numeric → fail loud (parse_transcribe_timeout_seconds). Only
    # consulted in streaming mode (and only when TRANSCRIBE_CMD is set).
    transcribe_timeout_seconds: float = DEFAULT_TRANSCRIBE_TIMEOUT_SECONDS
    # P13 T-AUDIT: the durable BODY-FREE audit-log path (append-only JSONL, atomic + 0600,
    # size-bounded 1-keep rotation). Default-on but NON-BREAKING: when CLAUDE_STATE_FILE is
    # set it defaults to ``<state_file>.audit.jsonl`` (next to the store, same dir/perms);
    # with no state file (a stateless oneshot deploy) it is None (off) unless AUDIT_LOG_FILE
    # is set explicitly. AUDIT_LOG_FILE overrides the path; ""/off/none/disabled/0/false
    # disable it (resolve_audit_log_file). ``None`` here → no audit log is constructed, so
    # every Engine is built with audit_sink=None (a no-op) and behavior is IDENTICAL.
    audit_log_file: Path | None = None
    # P13 T-AUDIT: the audit log's rotate-once size bound in BYTES (the live file + one
    # ``.1`` keep ≈ 2x this on disk). Default 5 MB; unset → default; 0/negative/non-integer
    # → fail loud (parse_audit_log_max_bytes). Only consulted when audit_log_file is set.
    audit_log_max_bytes: int = DEFAULT_AUDIT_LOG_MAX_BYTES
    # P13 T-BASH: the Bash command-policy mode, layered ADDITIVELY on the approval gate. The
    # one documented-UNCONFINED tool (Bash, C2) gets a conservative denylist guardrail:
    # ``flag`` (default) ESCALATES a matched dangerous command's prompt — ⚠️ + the matched
    # pattern, NO [Allow for session], and a re-prompt even under an active session-grant or
    # /yolo (the deliberate C2-residual closure: the guardrail beats the bypass for matched
    # commands only); ``deny`` AUTO-DENIES it (a hard wall, overriding grant/yolo); ``off`` is
    # byte-for-byte the pre-P13 gate. NON-BREAKING default: ``flag`` only adds friction to
    # already-RISKY commands that already prompt — it can never auto-run something that doesn't
    # today. The policy ESCALATES only (prompt→deny or auto-allow→prompt); it NEVER converts a
    # would-prompt/would-deny into an auto-allow. Fail-closed: a classifier error on a Bash
    # command escalates/denies, never silent-allows. Only consulted in streaming mode.
    bash_policy_mode: str = DEFAULT_BASH_POLICY_MODE
    # P13 T-BASH: owner-supplied extra denylist regex patterns, ADDITIVE to the built-ins
    # (the built-ins can NOT be removed via config — fail-safe). Newline/semicolon-separated
    # in BASH_POLICY_EXTRA_PATTERNS (comma is NOT a separator — a regex may contain commas).
    # Empty by default. A pattern that fails to compile is dropped at match time (fail-safe).
    bash_policy_extra_patterns: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, dotenv_path: str | os.PathLike[str] | None = ".env") -> "Config":
        if dotenv_path is not None:
            load_dotenv(dotenv_path)

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required (set it in .env)")

        allowed = parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
        if not allowed:
            raise ValueError(
                "TELEGRAM_ALLOWED_CHAT_IDS is required and must list at least one "
                "numeric chat id (comma-separated). Get yours from @userinfobot."
            )

        workdir = Path(os.environ.get("CLAUDE_WORKDIR") or str(Path.home())).expanduser()
        claude_bin = (os.environ.get("CLAUDE_BIN") or "claude").strip() or "claude"
        model = (os.environ.get("CLAUDE_MODEL") or "").strip() or None
        # T4 (P9): fast/deep model ids — env-overridable, default to the current sensible
        # ids. An empty/whitespace override falls back to the default (never an empty id).
        fast_model = (os.environ.get("FAST_MODEL") or "").strip() or DEFAULT_FAST_MODEL
        deep_model = (os.environ.get("DEEP_MODEL") or "").strip() or DEFAULT_DEEP_MODEL

        raw_timeout = (os.environ.get("CLAUDE_TIMEOUT_SECONDS") or "600").strip()
        try:
            timeout = int(raw_timeout)
        except ValueError as exc:
            raise ValueError(f"CLAUDE_TIMEOUT_SECONDS must be an integer, got {raw_timeout!r}") from exc
        if timeout <= 0:
            raise ValueError("CLAUDE_TIMEOUT_SECONDS must be positive")

        # SB5 / C1: default OFF (gate). An unset/empty CLAUDE_SKIP_PERMISSIONS keeps the
        # operator approval gate ON; only an explicit truthy value opts into the allow-all
        # bypass. (Was `_env_bool(..., True)` pre-C1, which made a fresh install fail-open.)
        skip = _env_bool("CLAUDE_SKIP_PERMISSIONS", False)

        state_raw = (os.environ.get("CLAUDE_STATE_FILE") or "").strip()
        state_file = Path(state_raw).expanduser() if state_raw else None

        engine_mode = parse_engine_mode(os.environ.get("ENGINE_MODE"))
        answer_backstop = parse_answer_backstop_seconds(
            os.environ.get("ANSWER_BACKSTOP_SECONDS")
        )
        max_concurrent_runs = parse_max_concurrent_runs(
            os.environ.get("MAX_CONCURRENT_RUNS")
        )
        render_chat_send_interval_seconds = parse_chat_send_interval_seconds(
            os.environ.get("RENDER_CHAT_SEND_INTERVAL_SECONDS")
        )
        stream_message_timeout_seconds = parse_stream_message_timeout_seconds(
            os.environ.get("STREAM_MESSAGE_TIMEOUT_SECONDS")
        )
        image_max_bytes = parse_image_max_bytes(os.environ.get("IMAGE_MAX_BYTES"))
        file_max_bytes = parse_file_max_bytes(os.environ.get("FILE_MAX_BYTES"))
        # P10 T2 (voice, pluggable): the transcriber command template — empty/unset =
        # graceful-off (a voice note gets a clean setup message). Whitespace-only normalizes
        # to "" (off). The timeout bounds the subprocess (positive; else fail loud).
        transcribe_cmd = (os.environ.get("TRANSCRIBE_CMD") or "").strip()
        transcribe_timeout_seconds = parse_transcribe_timeout_seconds(
            os.environ.get("TRANSCRIBE_TIMEOUT_SECONDS")
        )

        # P13 T-AUDIT: resolve the durable body-free audit-log path (default next to
        # CLAUDE_STATE_FILE; off when no state file unless AUDIT_LOG_FILE is set; an
        # explicit disable token turns it off) + its rotate-once size bound. Default-on
        # but non-breaking — a deploy with no state file is unchanged.
        audit_log_file = resolve_audit_log_file(
            os.environ.get("AUDIT_LOG_FILE"), state_file=state_file
        )
        audit_log_max_bytes = parse_audit_log_max_bytes(
            os.environ.get("AUDIT_LOG_MAX_BYTES")
        )

        # P13 T-BASH: the Bash command-policy mode (flag/deny/off; default flag, fail loud on
        # a bad value) + owner extra denylist patterns (additive to the built-ins). Default
        # flag is non-breaking — it only adds friction to already-RISKY commands.
        bash_policy_mode = parse_bash_policy_mode(os.environ.get("BASH_POLICY_MODE"))
        bash_policy_extra_patterns = parse_bash_policy_extra_patterns(
            os.environ.get("BASH_POLICY_EXTRA_PATTERNS")
        )

        # SB2 /cd confinement. Default the allow-list to the workdir so an unset
        # ALLOWED_ROOTS still confines /cd (ON by default); ALLOW_ANY_PATH=true is the
        # explicit owner opt-out. workdir is already expanduser()'d above; resolve it so
        # the default root is canonical and compares cleanly against canonical targets.
        allowed_roots = parse_allowed_roots(
            os.environ.get("ALLOWED_ROOTS", ""), default=workdir.resolve()
        )
        allow_any_path = _env_bool("ALLOW_ANY_PATH", False)

        return cls(
            bot_token=token,
            allowed_chat_ids=frozenset(allowed),
            workdir=workdir,
            claude_bin=claude_bin,
            model=model,
            fast_model=fast_model,
            deep_model=deep_model,
            timeout_seconds=timeout,
            skip_permissions=skip,
            state_file=state_file,
            engine_mode=engine_mode,
            answer_backstop_seconds=answer_backstop,
            max_concurrent_runs=max_concurrent_runs,
            render_chat_send_interval_seconds=render_chat_send_interval_seconds,
            stream_message_timeout_seconds=stream_message_timeout_seconds,
            allowed_roots=allowed_roots,
            allow_any_path=allow_any_path,
            image_max_bytes=image_max_bytes,
            file_max_bytes=file_max_bytes,
            transcribe_cmd=transcribe_cmd,
            transcribe_timeout_seconds=transcribe_timeout_seconds,
            audit_log_file=audit_log_file,
            audit_log_max_bytes=audit_log_max_bytes,
            bash_policy_mode=bash_policy_mode,
            bash_policy_extra_patterns=bash_policy_extra_patterns,
        )
