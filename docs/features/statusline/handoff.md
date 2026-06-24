# Feature Handoff: statusline

## Goal
Bring Claude Code's terminal statusline to the phone — one message **pinned at the top of the chat, edited in place**, showing `📁 worktree · 🤖 model·effort · 🧠 ctx X% · 🔒 mode` — replacing the per-turn `✅ done · $0.20` footer and removing dollars from routine output.

## Files changed
```
 claude_tg/bot.py                | 187 +   (/effort cmd, pin/unpin closures, _refresh_statusline)
 claude_tg/engine/adapter_sdk.py | 186 +   (effort kwarg, context_percentage() + usage fallback)
 claude_tg/engine/engine.py      |  24 +   (context_percentage delegate)
 claude_tg/render.py             | 155 +   (format_statusline + model_short_label; done-footer $ removed)
 claude_tg/session_store.py      |  65 +   (set/get_effort)
 claude_tg/stream_session.py     | 384 +   (effort runtime, _update_statusline lifecycle, trigger wiring)
 + tests across 6 files                    (1577 → 1649)
```

## How to run
- Gates (worktree `.venv`): `pytest -q` (1649), `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`.
- Live (streaming): `ENGINE_MODE=streaming CLAUDE_STATE_FILE=… .venv/bin/python main.py` → a status message pins at the top and updates as you work; `/effort max` sets reasoning effort.

## Expected behavior
- A single **pinned** message at the top, edited in place (pin once silently → silent edits; identical text → no I/O; if you unpin/delete it, the next change re-sends + re-pins). Fields: worktree (active project), `model·effort`, `ctx %` (live `get_context_usage().percentage`, `ctx —` if unavailable — never fabricated), `mode` (gate/yolo/plan), `⚙️` while a turn runs.
- Updates on: turn start (⚙️ on) / turn end (⚙️ off + ctx refresh), `/switch`, `/yolo`·`/unyolo`, `/plan`, `/effort`, `/fast`·`/deep`·`/auto`. **Foreground-only** — a background concurrent turn never rewrites the line.
- **`/effort low|medium|high|xhigh|max`** — per-project reasoning effort (persisted; rebuilds the session next turn; default unset = SDK default).
- The `✅ done` result no longer shows `· N turns · $X`; **cost stays on `/status`**.
- **RB1:** every pin/edit/send is best-effort — a Telegram failure never breaks or wedges a turn. **SB1:** statusline only to the allowlisted chat. **SB3:** the line is structurally body-free (name/model/effort/ctx-int/mode; HTML-escaped; path-shaped name → `<code>`; no tool body/path/secret/dollar can appear).

## Test plan
- **Automated (1649):** effort store round-trip/validate/RB6; `_build_options` effort kwarg only-when-set + warm-rebuild; `cmd_effort` SB1/menu-lock-step; pure `format_statusline` (incl. SB3 escape/path cases) + `ctx —` fallback; `context_percentage` (SDK %/raises→None/usage-fallback); `_update_statusline` pin/edit/skip-identical/orphan-recovery + **RB1 all-closures-raise→returns-normally**; trigger wiring at each trigger; **concurrency: a background turn does NOT rewrite the foreground line** (mutation-probed); `_render_result` no-`$` + `/status` still has cost.
- **Manual (live phone-verify, T-VERIFY):** pin appears once (no re-ping on edits), updates on turn/switch/effort/yolo, `ctx %` moves as context grows, unpin → reappears, a failure never wedges a turn.

## Known risks
- Pin spam / rate limits — mitigated by identical-text skip + the per-chat send-gate (non-verbatim) + state-change-only updates. Telegram shows one pinned message in the bar.
- ctx %: `get_context_usage()` is on the live `ClaudeSDKClient`; if a future SDK changes it, `context_percentage()` degrades to the usage-fallback then to `ctx —` (never fabricated). SDK pinned.
- Effort `xhigh` is Opus-4.7-only — on other models it may no-op (the SDK handles it; the bot just passes the level).

## Open questions
- Proactive/scheduler turns also pin/update the line (the deferred `⏰` marker from design §3.4 is not added — basic line only). Acceptable for v1.
