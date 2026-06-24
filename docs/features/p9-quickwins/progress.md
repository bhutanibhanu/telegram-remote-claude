# Progress: p9-quickwins

_From design.md · 6 quick-win features · supervised build. Baseline 875 tests._

## Task list
- [x] T1 — Command menu (`setMyCommands`) + first-run onboarding (b09cc8a)
- [x] T2 — `/status` health command (b09cc8a; per-project yolo added in fix round)
- [x] T3 — Cost/usage surfacing (done-footer + per-project total + `/status`) (b09cc8a)
- [x] T4 — Model routing `/fast`·`/deep`·`/auto` (per-project override) (f30d649)
- [x] T5 — Macros `/save`·`/run`·`/macros`·`/unsave` (persisted prompt templates) (f30d649; case-collision + /run-prompt-consume bugs fixed in QA round)
- [x] T6 — Notification polish (no link preview, [Open] switch button SB1+cwd-revalidated, queue counter) + smart-reply chips (4e7816c)

_QA: per-batch reviewers (AGREE) + whole-bot UX/bug audit (1 P0 + 4 P1, all fixed) + cross-model Codex (NO_SHIP → fixed /run-consumes-prompt blocker → re-QA). 1001 tests. Fix round: see below._

Legend: `[ ]` todo · `[x]` done (sha) · `[!]` blocked

## Tasks

### T1 — Command menu + onboarding
- **Files:** `claude_tg/bot.py` (`build_application`/`post_init`), `claude_tg/app.py`, maybe `claude_tg/session_store.py` (seen-flag).
- **Acceptance:** startup calls `set_my_commands` with every registered command + a 1-line description (no documented-but-unregistered cmd); a chat's FIRST-ever message gets a one-time welcome (mode + cwd + hint), subsequent don't; onboarding authn-gated (SB1).
- **Tests:** command list passed to `set_my_commands` matches the registered handlers; first-message welcome fires once.

### T2 — `/status` health
- **Files:** `claude_tg/bot.py` (new `cmd_status`), `claude_tg/render.py` (status render).
- **Acceptance:** allowlisted `/status` → uptime, ENGINE_MODE, gate/yolo, active-vs-cap, per-project `ProjectStatus`, last-error-time; body-free, paths `<code>`; non-allowlisted ignored (SB1).
- **Tests:** `/status` reply contains the fields; SB1 unauth ignored.

### T3 — Cost/usage surfacing
- **Files:** `claude_tg/render.py` (done-footer), `claude_tg/stream_session.py` (accumulate), `claude_tg/session_store.py` (persist total).
- **Acceptance:** done message includes `· N turns · $X.XX` when the SDK provided them (omit gracefully if absent); per-project cumulative cost persists + shows in `/status`; no secret in the cost line.
- **Tests:** footer renders turns+cost when present, omits when absent; cumulative persists across reload.

### T4 — Model routing `/fast`·`/deep`
- **Files:** `claude_tg/bot.py` (`cmd_fast`/`cmd_deep`), `claude_tg/engine/adapter_sdk.py` (`_build_options` model), `claude_tg/claude_runner.py` (`--model`), `claude_tg/session_store.py` (per-project model), `claude_tg/config.py` (model ids).
- **Acceptance:** `/fast`→Haiku, `/deep`→Opus set per active project; applies on the NEXT turn/session (not mid-turn); persists for the project; shown in `/status`/`/projects`; unknown/invalid → safe default.
- **Tests:** `/fast` sets the project model; the next turn's options carry it; default fallback on bad value.

### T5 — Macros `/save`·`/run`
- **Files:** `claude_tg/bot.py` (cmd_save/run/macros/unsave), `claude_tg/session_store.py` (macro store).
- **Acceptance:** `/save <name> <prompt>` stores per-chat (atomic+0600, SB4 name); `/run <name> [args]` expands (`$1`/`$*`) + fires as a turn to the active project; `/macros` lists, `/unsave` removes; persists (RB6); unknown `/run` → clean reply (RB2), no crash (RB1).
- **Tests:** save→run round-trip + arg substitution; SB4 name validation; persist across reload; unknown-name clean.

### T6 — Notification polish + chips
- **Files:** `claude_tg/render.py` (notification builders, keyboards), `claude_tg/stream_session.py` (notify paths).
- **Acceptance:** background notifications: `disable_web_page_preview`, `[Open <project>]` switch button on attention/done, "(N more waiting)" when queued>0; free-text prompts get scoped quick-reply chips, removed after; body-free (SB3) preserved.
- **Tests:** notification has no-preview + switch button + queue counter; chips scoped + dismissed; SB3 body-free intact.
