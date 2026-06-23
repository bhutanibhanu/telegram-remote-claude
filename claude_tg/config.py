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


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_chat_ids: frozenset[int]
    workdir: Path
    claude_bin: str = "claude"
    model: str | None = None
    timeout_seconds: int = 600
    skip_permissions: bool = True
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
    # SB2 /cd path confinement (decision-log: confinement ON by default). The canonical
    # roots a `/cd` target must sit inside; the default is `(workdir,)` (set by
    # from_env), so an unset ALLOWED_ROOTS confines /cd to the workdir (which itself
    # defaults to $HOME). The owner widens via ALLOWED_ROOTS. An empty tuple combined
    # with allow_any_path=False rejects every /cd (fail-closed, SB6).
    allowed_roots: tuple[Path, ...] = ()
    # SB2 explicit opt-out: ALLOW_ANY_PATH=true disables /cd containment entirely (the
    # owner takes the wheel). Default False — confinement is the safe default (SB6).
    allow_any_path: bool = False

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

        raw_timeout = (os.environ.get("CLAUDE_TIMEOUT_SECONDS") or "600").strip()
        try:
            timeout = int(raw_timeout)
        except ValueError as exc:
            raise ValueError(f"CLAUDE_TIMEOUT_SECONDS must be an integer, got {raw_timeout!r}") from exc
        if timeout <= 0:
            raise ValueError("CLAUDE_TIMEOUT_SECONDS must be positive")

        skip = _env_bool("CLAUDE_SKIP_PERMISSIONS", True)

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
            timeout_seconds=timeout,
            skip_permissions=skip,
            state_file=state_file,
            engine_mode=engine_mode,
            answer_backstop_seconds=answer_backstop,
            max_concurrent_runs=max_concurrent_runs,
            render_chat_send_interval_seconds=render_chat_send_interval_seconds,
            allowed_roots=allowed_roots,
            allow_any_path=allow_any_path,
        )
