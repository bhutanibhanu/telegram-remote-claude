# P7 — Packaging & Keep-Alive

_Delta on the shipped P0–P6 codebase (`main` @ d5596e4). Goal: make the bot **easy to install** and **stay running** on the owner's Mac, without changing bot behavior. Not a feature phase._

## Problem
Today the bot runs via an ad-hoc `run.sh` (creates a venv, installs, starts `main.py`). Two gaps: (1) it isn't cleanly installable — `pip install .` / `pip install -e .` **fails** (setuptools can't discover the package: "Multiple top-level packages discovered in a flat-layout" / editable build error, observed when building the P6/P7 venvs), so the `pyproject.toml` packaging metadata is broken; and (2) there's no supervised keep-alive — if the Mac reboots or the process crashes, the bot stays down until the owner manually restarts it. The README even has a "Keeping it running" section that needs real content.

## Success
- `pip install .` (and `pipx install .`) works from a clean checkout and installs a `claude-telegram-bot` console command that starts the bot.
- A documented, copy-pasteable **macOS launchd keep-alive** that (re)starts the bot on login/boot and on crash — respecting **one-instance-per-token** (no duplicate pollers → no Telegram `Conflict`).
- `.env.example` lists **every** supported config with secure defaults + one-line docs (incl. the P5/P6 additions).
- `run.sh` still works for the quick-start path (or cleanly delegates to the console entry).
- Gates stay green (854 tests); no behavior change to the bot itself.

## Anti-goals
- No bot behavior/feature changes. No Docker/cloud deploy (owner runs on their Mac — launchd is the target; a systemd unit can be a documented bonus, not a requirement). No PyPI publish (that's adjacent to the P9 OSS release; P7 just makes the package *installable from source*). No rewrite of `main.py`'s startup logic beyond exposing an entry point.

## Decisions / scope (input to /plan)
1. **Fix `pyproject.toml` packaging.** Declare the package explicitly so discovery is deterministic — `[tool.setuptools.packages.find]` scoped to `claude_tg` (or list `packages = ["claude_tg", "claude_tg.engine"]`), exclude `tests`/`docs`/`scripts`. Confirm `pip install .` AND `pip install -e .` both succeed in a clean venv. Keep the existing `requirements.txt`/`requirements-dev.txt` working (or fold runtime deps into `[project.dependencies]` and have requirements.txt `-r` it — pick one source of truth, don't duplicate-drift).
2. **Console entry point.** `[project.scripts] claude-telegram-bot = "claude_tg.cli:main"` (add a thin `claude_tg/cli.py` that calls the existing `main()` — or point at `main:main` if kept at root). The entry must load `.env` from the CWD (current behavior) and start the bot identically.
3. **macOS launchd keep-alive.** A `LaunchAgent` plist template (`deploy/com.<user>.claude-telegram-bot.plist` or a generator) with `KeepAlive` (restart on crash) + `RunAtLoad`, pointing at the installed console command + the working dir holding `.env`. Document `launchctl load/unload`, where logs go (`StandardOutPath`/`StandardErrorPath`), and the **one-instance** guarantee (a single agent; unload before running manually). Token never embedded in the plist (it lives in `.env`).
4. **`.env.example` completeness.** Regenerate it to list all configs with secure defaults + comments: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `CLAUDE_WORKDIR`, `CLAUDE_BIN`, `CLAUDE_MODEL`, `CLAUDE_TIMEOUT_SECONDS`, `CLAUDE_SKIP_PERMISSIONS` (=false, loud opt-in), `ENGINE_MODE` (oneshot|streaming), `ALLOWED_ROOTS`, `ALLOW_ANY_PATH`, `MAX_CONCURRENT_RUNS`, `RENDER_CHAT_SEND_INTERVAL_SECONDS`, `STREAM_MESSAGE_TIMEOUT_SECONDS`, `ANSWER_BACKSTOP_SECONDS`, `CLAUDE_STATE_FILE`. (Cross-check against `config.py from_env` so none are missed.)
5. **`run.sh` reconcile.** Keep the quick-start (venv + install + run) but make it idempotent + have it use the console entry or `python -m`/`main.py` consistently. A `python -m claude_tg` module entry is a nice-to-have if cheap.
6. **(Optional) `claude-telegram-bot --version` / a `__version__`** so the install is identifiable; only if cheap.

## Build order (input to /plan)
1. Fix `pyproject` packaging + console entry (`cli.py`) → `pip install .`/`-e .` both work; `claude-telegram-bot` starts the bot. (RB: a clean-venv install test in CI if feasible.)
2. `.env.example` completeness (cross-checked against `config.py`).
3. launchd keep-alive plist + template/generator + docs (one-instance, logs, load/unload).
4. `run.sh` reconcile + optional `python -m`/`--version`.
5. Verify: clean-venv `pip install .` smoke + a real launchd load/start/restart-on-kill check (live, on the Mac) → the bot comes up and survives a kill; no `Conflict`.

## Inherited facts / constraints
- One bot instance per token (duplicate pollers → Telegram `Conflict`). The keep-alive must guarantee a single instance.
- Gates (worktree `.venv`): `pytest`, `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`. Regression floor: 854 tests. Don't let packaging changes break imports.
- `.env` is gitignored + holds the token (never commit; never embed in the plist). `cp` `.env` between worktrees (reading it is permission-denied).
- This feeds P8 (docs: the README "Setup"/"Keeping it running" sections will document the P7 install + launchd) and P9 (the OSS release — P7 makes it installable-from-source; actual PyPI/publish is P9-adjacent and a hard-stop).
