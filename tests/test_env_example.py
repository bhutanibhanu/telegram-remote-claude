"""Drift guard: `.env.example` must document every env var `config.py` reads (P7/T2).

`Config.from_env` is the single place that consumes process env. If someone adds a new
`os.environ.get("NEW_VAR")` / `_env_bool("NEW_VAR", ...)` there but forgets to document it
in `.env.example`, an operator copying the template silently misses a knob. This test
parses the *keys* out of `config.py`'s source (no execution, no env mutation) and asserts
each appears in `.env.example` — so the example can never drift behind the config reader.

It also pins the two safety-critical facts the example must state correctly:
  * `CLAUDE_SKIP_PERMISSIONS` is documented as defaulting to **false** (gate ON) — a
    template that shipped `=true` would quietly opt every fresh install into allow-all.
  * the required vars (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`) are present and
    *active* (uncommented) so `cp .env.example .env` yields a file you only have to fill in.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import claude_tg.config as config_mod

# config.py lives at <repo>/claude_tg/config.py; .env.example sits at the repo root.
_CONFIG_PATH = Path(config_mod.__file__)
_REPO_ROOT = _CONFIG_PATH.parent.parent
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# Env-reading call targets in config.py whose FIRST string arg is an env-var name.
_ENV_READERS = {"_env_bool", "getenv"}  # _env_bool(name, ...) and os.environ.get(name, ...)


def _env_keys_read_by_config() -> set[str]:
    """Statically extract every env-var name `config.py` reads.

    Walks the AST for ``os.environ.get("KEY"...)``, ``os.getenv("KEY"...)`` and
    ``_env_bool("KEY"...)`` and collects the literal first argument. Static parsing (not
    import-time interception) keeps the guard independent of which branches run.
    """
    tree = ast.parse(_CONFIG_PATH.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Attribute):
            # os.environ.get(...) / os.getenv(...)
            if func.attr in {"get", "getenv"}:
                name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name not in ({"get", "getenv"} | _ENV_READERS):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            # Filter out dict .get() calls that aren't on os.environ by requiring the
            # key to look like an ENV_VAR (upper snake case). config.py only ever
            # .get()s from os.environ, but this keeps the heuristic honest.
            if re.fullmatch(r"[A-Z][A-Z0-9_]+", first.value):
                keys.add(first.value)
    return keys


def _env_example_text() -> str:
    assert _ENV_EXAMPLE.is_file(), f".env.example not found at {_ENV_EXAMPLE}"
    return _ENV_EXAMPLE.read_text(encoding="utf-8")


def test_env_example_documents_every_config_var():
    keys = _env_keys_read_by_config()
    # Sanity: we actually found the known keys (guards against the parser silently
    # extracting nothing and the test passing vacuously).
    assert {"TELEGRAM_BOT_TOKEN", "ENGINE_MODE", "CLAUDE_SKIP_PERMISSIONS"} <= keys
    text = _env_example_text()
    missing = sorted(k for k in keys if not re.search(rf"\b{re.escape(k)}\b", text))
    assert not missing, f".env.example is missing config var(s): {missing}"


def test_required_vars_are_active_not_commented():
    text = _env_example_text()
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS"):
        # An active assignment: a line beginning with KEY= (no leading '#').
        assert re.search(rf"(?m)^{re.escape(key)}=", text), f"{key} should be active (uncommented)"


def test_skip_permissions_documented_safe_default():
    text = _env_example_text()
    # The template must not ship an ACTIVE allow-all opt-in, and must state false default.
    assert not re.search(r"(?m)^CLAUDE_SKIP_PERMISSIONS=true", text)
    assert re.search(r"CLAUDE_SKIP_PERMISSIONS=false", text)
