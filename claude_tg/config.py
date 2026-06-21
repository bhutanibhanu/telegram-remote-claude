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
        )
