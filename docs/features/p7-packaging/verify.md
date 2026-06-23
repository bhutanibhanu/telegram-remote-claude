# P7 Verify — packaging & keep-alive

_T5 verification on the real Mac, 2026-06-23. No bot-behavior change; the bot ran live against the real token for ~40s total during the launchd test, then was fully stopped + uninstalled (system left as found)._

## Part A — clean-venv install (re-confirms T1 from the built artifacts) — PASS
- `python3 -m venv /tmp/… && pip install /…/claude-telegram-bot-p7` → SUCCEEDS (builds `claude_telegram_bot-0.1.0-py3-none-any.whl`, installs runtime deps: python-telegram-bot 21.11.1, claude-agent-sdk 0.2.105, …). The pre-P7 editable-build failure is gone.
- `claude-telegram-bot --version` → `claude-telegram-bot 0.1.0` (exits, no boot). `python -m claude_tg --version` → same. `claude_tg/__main__.py` is packaged into the wheel.
- (Transient: `python -m claude_tg` from the worktree root shadows the install with the source tree — a cwd artifact, not a defect; works from any neutral cwd.)

## Part B — live macOS launchd keep-alive — PASS
- `deploy/install-launchd.sh` resolved command `…/.venv/bin/python -m claude_tg` (the documented fallback when the console script isn't on PATH), WorkingDirectory the worktree (holds `.env`), logs `~/Library/Logs/com.claude-telegram-bot.{out,err}.log`.
- Loaded → bot started: **PID1**, exactly one process, log `… engine_mode: streaming … Application started`, **no Telegram `Conflict`**.
- **KeepAlive:** `kill PID1` → clean shutdown in log → ~10s later launchd respawned **PID2 ≠ PID1** (PPID=1, launchd-managed), second clean startup, still no `Conflict`. One instance throughout.
- **Teardown:** `install-launchd.sh --uninstall` → `launchctl list` empty, plist removed from `~/Library/LaunchAgents/`, no bot process. Clean.

## Result
All P7 acceptance met: pip-installable (both paths), `claude-telegram-bot` console command + `python -m claude_tg` + `--version`, `.env.example` complete (drift-guarded), and a working one-instance launchd keep-alive (restart-on-crash verified live). 861 tests green; ruff/mypy/secret_scan clean. → merging to `main`.
