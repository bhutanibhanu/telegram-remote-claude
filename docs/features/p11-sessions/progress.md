# Progress: p11-sessions

_From design.md · Universal Session Control Plane · supervised build. Baseline 1098 tests. Spikes PROVEN (discovery+resume+mirror)._

## Task list
- [x] T1 — Session discovery adapter + `/sessions` read-only listing (see ALL Mac sessions, incl. the live one; merged+deduped with bot projects; running/idle) (ccbddbc)
- [x] T2 — Attach/switch to any session (idle→continue, live-OR-uncertain→FORK; SB2 on out-of-root cwd; fork-on-doubt safe default) (f620a84)
- [~] T3 — (stretch) Live-mirror `/watch <id>` — DEFERRED to its own clean cycle after T1+T2 merge (phase ran long; design says "may defer")
- [>] T4 — verify + Codex QA + live-verify + merge (T1+T2)

Legend: `[ ]` todo · `[x]` done (sha) · `[!]` blocked

## Tasks
### T1 — discovery + `/sessions` (read-only)
- **Files:** new `claude_tg/sessions_discovery.py` (wrap SDK `list_sessions`/`get_session_info` [`_internal`, SDK-pinned] + composite liveness: transcript mtime + `ps` --resume + `~/.claude/sessions/<pid>.json` validated by pid+procStart), `claude_tg/bot.py` (`/sessions` cmd, COMMAND_MENU+HELP), `claude_tg/render.py` (the listing render).
- **Accept:** `/sessions` (SB1) lists all discoverable Mac sessions — short id, cwd `<code>`, title, last-active, running/idle — merged + deduped (by session_id) with the bot's own projects (bot-known/active marked); the live orchestrator appears; body-free; read-only (no attach). Discovery isolates the `_internal` SDK use in ONE adapter (pinned). RB1: a missing/odd `~/.claude` never crashes (empty list).
- **Tests:** discovery merges/dedups by id; composite liveness (running vs idle, stale-pid handled); `/sessions` SB1-gated + body-free + paths `<code>`; empty/missing store → clean.

### T2 — attach/switch any session
- **Files:** `claude_tg/bot.py` (`/attach <id>` + tap on a `/sessions` entry → reuse the P9 switch-callback infra), `claude_tg/stream_session.py` (adopt an external `(session_id, cwd)` as a project; fork-if-live), `claude_tg/sessions_discovery.py` (liveness for the fork decision).
- **Accept:** attach an IDLE session → continue (same id); attach a LIVE-elsewhere session → **FORK** (new id) + tell the operator (never co-drive — the hard rule); SB2: an out-of-`ALLOWED_ROOTS` cwd is refused or requires explicit confirm; SB1; the adopted session drives through the normal turn + permission path; persists in the registry.
- **Tests:** attach-idle continues (same id); attach-live forks (mock liveness→live → fork_session=True, new id); out-of-root cwd refused/confirmed; SB1; no co-drive of a live id.

### T3 — live-mirror (stretch, may defer)
- **Accept:** `/watch <id>` tails the transcript → dict→Event normalizer → `render_event` → per-chat send gate (read-only follow); SB3 body-free (raw tool bodies scrubbed); a send-queue bounds Telegram flood; `/unwatch` stops. Defer if the phase runs long (record as a follow-up).

### T4 — verify + merge
Codex QA → live-verify (`/sessions` shows the live orchestrator; attach a session + fork-on-live) → merge to `main`.
