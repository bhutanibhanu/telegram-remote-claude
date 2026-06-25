# Progress: statusline

_From design.md · live mobile statusline (pinned, edited-in-place) replacing the done/$ footer · supervised build. Baseline 1577 tests. Both spikes proven (`get_context_usage().percentage` + `ClaudeAgentOptions.effort`). Owner-confirmed: ⚙️ working marker, all 5 effort levels, ctx refresh at turn-end._

## Task list
- [ ] T-EFFORT — the `/effort` knob (design T1+T2+T3): per-project effort persistence + thread into `ClaudeAgentOptions` (+ warm-engine rebuild key) + `/effort low|medium|high|xhigh|max` command (SB1, streaming-only, menu+HELP lock-step). Parallel to `/fast·/deep·/auto` + `/thinking`.
- [ ] T-SL-CORE — the statusline machinery (design T4+T5+T6): pure `format_statusline(...)` formatter + `model_short_label` (body-free, HTML-escaped, `ctx —` fallback) + best-effort `context_percentage()` (`get_context_usage().percentage`, usage-derived fallback, `None` never fabricated) + the pin/edit-in-place lifecycle (`_update_statusline`: pin-once-silent → edit-in-place → skip-identical → orphan-recovery on edit-fail; all via the send-gate non-verbatim; **RB1 best-effort — never breaks a turn**).
- [ ] T-SL-WIRE — wire triggers + remove the footer (design T7+T8): call `_update_statusline` at turn start/end (working on/off + ctx refresh at end), `/switch`, and after `set_model`/`set_effort`/`set_yolo`/`unyolo`/`arm_plan`; **foreground-only** (a background turn doesn't rewrite the line); remove the done-footer `$`/turn-count (cost stays on `/status`).
- [x] T-VERIFY — docs (ADR-009 + README + index) + Codex QA (NO_SHIP → 3 blockers + B2-residual fixed → **re-check SHIP**) + Verifier SHIP + **live phone-verify PASS** + UX polish (`🤖 default` fallback) + merge. **DONE:**
  - **QA:** same-model Verifier SHIP, but cross-model **Codex caught 3 real blockers it missed** — ctx % silently broken (un-awaited async `get_context_usage`, hidden by a sync test fake), foreground-switch race, `/plan` mode never shown — + a pin-retry gap; the async fix *reopened* the race at a new await point (Codex re-flagged) → closed with a post-await foreground re-check. **Codex re-check SHIP, no regressions.**
  - **Live phone-verify PASS:** pinned bar at top (pin once, edited in place, no errors); `🤖 opus·max` after `/deep`+`/effort max`; **real `🧠 ctx 1%`** (awaited SDK %); mode flips `gate→yolo→plan` (and `🔒 plan` shown during a live plan turn); **no `$` footer**.
  - **UX polish:** `🤖 default` when no model configured (was a blank `🤖`). 1663 tests; ruff/mypy/secret_scan clean.

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (sha) · `[!]` blocked

## Tasks

### T-EFFORT — `/effort` knob
- **Files:** `session_store.py` (`set_effort`/`get_effort` — parallel to `set_model`, atomic 0600, validate `{low,medium,high,xhigh,max}`, unknown→None, RB6), `engine/adapter_sdk.py` + `engine/engine.py` (`effort` param → `_build_options` sets `kwargs["effort"]` when not None; + warm-engine match-key like `engine_thinking`), `stream_session.py` (`_ProjectRuntime.effort` + `_resolve_project_effort` + `set_effort`), `bot.py` (`cmd_effort` + register + COMMAND_MENU + HELP).
- **Accept:** `/effort max` (SB1) persists + confirms; bare `/effort` shows usage/clears; bad level → clean error; one-shot → notice; unauth → no-op; a project with effort builds `ClaudeAgentOptions(effort=…)` (default → no kwarg); a change rebuilds next turn (not mid-turn). RB6 persists across restart.
- **Tests:** store round-trip/clear/unknown→None (mirror model-override); `_build_options` sets the kwarg only when set; warm-engine rebuild on change; `cmd_effort` (mirror `cmd_thinking`) SB1 + menu/HELP lock-step.

### T-SL-CORE — statusline machinery
- **Files:** `render.py` (`format_statusline(*, worktree, model_label, effort, ctx_pct, mode, working) -> str` pure + `model_short_label`), `engine` (`context_percentage()` best-effort via `get_context_usage()` + usage fallback), `stream_session.py` (`_ChatState.statusline_message_id`/`_text`; `_update_statusline(...)` pin/edit lifecycle with injected pin/unpin closures).
- **Accept:** formatter emits the exact body-free format (`📁 … · 🤖 opus·max · 🧠 ctx 6% · 🔒 gate`), `ctx —` when None, working marker present/absent, odd model→raw id (RB1), HTML-escaped (SB3 — a `<`/`&` name escaped; a path-shaped name sanitized/`<code>`, no fake-link); `context_percentage()` returns the SDK % or None (never fabricated); `_update_statusline` sends+pins (silent) first, edits in place after, skips identical, re-sends+re-pins on edit-fail (orphan recovery), all best-effort (a pin/edit raise never breaks a turn), only one id held.
- **Tests:** pure formatter + label (incl. SB3 escape/path cases); `context_percentage` with a fake client (returns %/raises→None/usage-fallback math); `_update_statusline` call-sequence + identical-skip + failure-recovery with fake send/edit/pin closures.

### T-SL-WIRE — triggers + footer removal
- **Files:** `stream_session.py` (call `_update_statusline` at turn start/end in the drive loop, in `/switch`'s core, after the knob setters; foreground-only), `bot.py` (inject pin/unpin closures), `render.py` (`_render_result` drops the `$`/turn-count footer suffix).
- **Accept:** a foreground turn pins then refreshes (working on→off + ctx at end); `/switch` rewrites the line; `/yolo` flips `🔒 gate`→`🔒 yolo`; a background turn leaves the foreground line intact (concurrency); the result render contains no `$` (cost still on `/status`).
- **Tests:** trigger integration (assert statusline text at each trigger, with fakes); concurrency (background turn doesn't rewrite); `_render_result` no-`$` + `/status` still has cost.

### T-VERIFY — docs + QA + live-verify + merge
ADR-009 (pinned-statusline lifecycle, effort knob, ctx-% source, footer removal) + README (the line format, `/effort`, "cost moved to /status") + docs index → cross-model Codex QA (iterate to SHIP) → independent reviewer → **live phone-verify** (pin once/no re-ping, updates on turn/switch/effort/yolo, ctx % moves, unpin→reappears, failure never wedges; scrub evidence) → 4 gates → merge to `main`.
