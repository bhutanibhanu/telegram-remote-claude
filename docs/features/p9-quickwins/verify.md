# P9 Live Phone-Verify Checklist — quick wins

_Drive the real bot (@your_test_bot, logged-in Telegram Web) once Codex re-QA is SHIP. Bot from this worktree, streaming, gate-on. Each new P9 feature exercised end-to-end._

## Setup
`cd /Users/ray/dev/claude-telegram-bot-p9 && CLAUDE_STATE_FILE=/tmp/p9verify/state.json CLAUDE_WORKDIR=/tmp/p9verify ALLOWED_ROOTS=/tmp/p9verify ENGINE_MODE=streaming .venv/bin/python -m claude_tg` (mkdir the sandbox first; confirm clean startup, no Conflict). One instance per token; stop the real `…/Python` PID after.

## Scenarios
- [ ] **T1 command menu + onboarding:** the Telegram `/` menu lists the commands with descriptions (tap to see). A first-ever message to a fresh chat → one-time welcome (mode + cwd + hint); a 2nd message → no re-welcome.
- [ ] **T2 `/status`:** `/status` → uptime, ENGINE_MODE, gate posture, active-vs-cap, per-project line (name `<b>`, cwd `<code>`, status, model if set, cumulative cost, `⚠️ yolo` if allow-all). Body-free, paths monospace.
- [ ] **T3 cost:** a turn's done message shows `· N turn(s) · $X.XX` (when SDK provides); `/status` shows the per-project cumulative.
- [ ] **T4 model routing:** `/fast` → next turn runs Haiku; `/deep` → Opus; `/auto` → back to default; the active model shows in `/status`. (Verify via `/status` + the turn behaving.)
- [ ] **T5 macros:** `/save greet "say hello to $1"`; `/run greet World` → fires "say hello to World" as a turn; `/macros` lists it; case-insensitive (`/save Greet …` overwrites); `/unsave greet`; an unknown `/run x` → clean error. **⭐ regression check:** while a free-text prompt is open (reject-feedback "Other"), `/run greet` starts a FRESH turn (does NOT become the rejection text).
- [ ] **T6 notifications + chips + [Open]:** a background project's done/attention ping has NO link preview + an `[Open <project>]` button that switches to it on tap (and a non-allowlisted tap switches nothing); a queued project shows "(N more waiting)"; a free-text prompt offers quick-reply chips (one-tap), removed after use.

## Result — ALL PASS (live, 2026-06-24)
Driven on the real bot (browser, fresh `/tmp/p9verify`, streaming, gate-on, cap=2); clean startup, no Conflict, stopped cleanly. Evidence: `verify-p9-*.png` (13 shots).
- **/help renders (the Codex-blocker fix): PASS** — sends clean + fully formatted; the `$*` placeholder code-span keeps the bold markers balanced. `/` command menu lists all commands (T1).
- **T2 `/status`: PASS** — uptime, mode, gate, runs-vs-cap, per-project status + cwd (monospace) + cost; body-free.
- **T3 cost: PASS** — done-footer `· 1 turn · $0.03` (singular correct), `/status` cumulative.
- **T4 model routing: PASS** — `/fast`→`claude-haiku-4-5`, `/deep`→`claude-opus-4-8`, `/auto`→default; shown in `/status`.
- **T5 macros: PASS** — save/run(`$1`)/list/`/unsave`/unknown-clean; **case-insensitive overwrite (P0 fix) confirmed** (`/save Greet` overwrites `greet`, one entry); **⭐ `/run` during an open free-text prompt starts a fresh turn (NOT swallowed) — the Codex-blocker fix confirmed live.**
- **T6: PASS** — background ping has no link-preview + `[Open b]` button; tapping it switches active to `b` (verified via `/status`); quick-reply chips shown on a free-text prompt.
- **`/cancel all`: PASS** — "Cancelled all running/queued projects." (project-scoped wording).

### Observations recorded (not P9 regressions; for follow-up polish)
1. **Background-ping timing race (the un-verified P5 scenario (f)):** starting a turn then *immediately* `/switch`-ing away can have the hold/done event processed while the project is still foreground → it renders inline (no `🔔`/`[Open]`) instead of pinging. The fg/bg decision is evaluated at event-processing time; a switch landing exactly at a hold boundary misses the ping. Works correctly once the switch has committed. **→ recorded for a polish fix.**
2. The C2 path-confinement gate correctly intercepted a turn where Claude proposed writing `/Users/ray/ping.txt` (outside the project root) — nothing written. Gate doing real work (not a bug).
3. Not exercised: the queued "(N more waiting)" counter (needs 3 concurrent vs cap=2) + the fresh-chat one-time welcome (chat had prior history).
