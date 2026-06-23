# Progress: p7-packaging

_From design.md · packaging & keep-alive, no bot-behavior change · supervised build. Baseline 854 tests._

## Task list
- [ ] T1 — Fix `pyproject` packaging (deterministic discovery) + `claude-telegram-bot` console entry (`cli.py`) → `pip install .` and `pip install -e .` both work
- [ ] T2 — `.env.example` completeness (every config from `config.py from_env`, secure defaults, comments)
- [ ] T3 — macOS launchd keep-alive (LaunchAgent plist template/generator + docs; one-instance; logs; load/unload)
- [ ] T4 — `run.sh` reconcile (idempotent; uses the console entry / `python -m`) + optional `__version__`/`--version`
- [ ] T5 — Verify: clean-venv `pip install .` smoke + live launchd load/start/restart-on-kill (bot survives a kill, no Conflict) → merge

Legend: `[ ]` todo · `[x]` done (sha) · `[!]` blocked

## Tasks
### T1 — packaging + console entry
- **Files:** `pyproject.toml`, new `claude_tg/cli.py` (thin wrapper over the existing `main()`), maybe `main.py`.
- **Acceptance:** in a CLEAN venv, `pip install .` succeeds AND `pip install -e .` succeeds (the current editable build error is gone); a `claude-telegram-bot` command is installed and starts the bot identically (loads `.env` from CWD); imports unaffected (854 tests green). Runtime deps declared in one source of truth (no drift between `pyproject` + `requirements.txt`).
- **Tests:** an install smoke (clean-venv `pip install .` then `claude-telegram-bot --help`/import) if feasible in CI; the existing suite stays green.

### T2 — .env.example
- **Acceptance:** lists every env var `config.py from_env` reads, with secure defaults (`CLAUDE_SKIP_PERMISSIONS=false`, etc.) + a one-line comment each; cross-checked against `from_env` so none are missed; no real token.
- **Tests:** a test asserting `.env.example` covers each `os.environ.get(...)` key in `config.py` (drift guard) if cheap.

### T3 — launchd keep-alive
- **Acceptance:** a documented LaunchAgent plist (template or generator under `deploy/`) with `RunAtLoad`+`KeepAlive`, pointing at the installed command + the `.env` working dir; ONE instance (no duplicate poller → no Telegram `Conflict`); logs to `StandardOutPath`/`StandardErrorPath`; token NOT in the plist; `launchctl load/unload` documented. (systemd unit = optional bonus.)
- **Tests:** none (deploy artifact) — validated live in T5.

### T4 — run.sh + version
- **Acceptance:** `run.sh` is idempotent (re-runnable), starts via the console entry / `python -m claude_tg` consistently; optional `__version__` + `--version`.
- **Tests:** none / minimal.

### T5 — verify + merge
- **Acceptance:** clean-venv `pip install .` smoke passes; live launchd load → bot starts → `kill` the process → launchd restarts it (KeepAlive) → still no `Conflict`; unload cleanly. Then merge to `main`.
