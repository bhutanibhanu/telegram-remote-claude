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
        )
