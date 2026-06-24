# Progress: p12-supervise

_From design.md · roadmap-v2 P12 "supervise" · supervised build. Baseline 1248 tests. Both features SDK-spike-PROVEN (plan-mode interception + thinking_delta streaming)._

## Task list
- [x] T-PLAN — Plan-mode approval (`/plan`) (`9f96099` + marker-lifecycle fix `e76db0b`)
- [x] T-THINK — Live thinking (`/thinking on|off`, default off) (`724e001`)
- [x] T-VERIFY — live phone-verify both + 4 gates + merge (+ hold-sequence regression tests `8951a97`)

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (sha) · `[!]` blocked

## Tasks

### T-PLAN — Plan-mode approval (`/plan`)
- **Files:** `engine/adapter_sdk.py` + `engine/engine.py` + `engine/substrate.py` (per-turn `permission_mode` threading — mechanism (a): session-creation param like `model`, OR (b) `set_permission_mode`; pick simplest), `stream_session.py` (`_ProjectRuntime` one-shot plan marker, RB3 transient; turn driver reads+clears it), `bot.py` (`cmd_plan` SB1 + COMMAND_MENU + HELP lock-step).
- **Accept:** `/plan` (SB1) arms a one-shot per-project marker → that one turn runs in plan mode → Claude calls ExitPlanMode → the existing PlanEvent + `[✅ Approve]/[✋ Reject+feedback]` keyboard shows; **Approve → execution resumes but every subsequent risky tool STILL hits the permission gate (ADR-001 C4 — the security-critical line, NOT auto-allowed)**; Reject → deny-with-feedback → Claude revises; a backstop/timeout or `/cancel` on a plan hold ends the turn clean (RB2/RB4), never auto-allows; a normal (non-`/plan`) turn is byte-for-byte unchanged; marker cleared after one turn + never survives restart (RB3).
- **Tests:** `_build_options`/set_permission_mode issued only when armed; `cmd_plan` calls `_ok` + arms exactly one turn + menu/handler lock-step; **C4 — approve→next-tool-still-gated** (mutation-probe: if approval auto-allowed later tools, the test fails); reject carries feedback; backstop on a plan hold = deny; `/cancel` aborts clean.

### T-THINK — Live thinking stream (`/thinking on|off`)
- **Files:** `engine/types.py` (`ThinkingEvent`), `engine/adapter_sdk.py` (two normalize branches: `thinking_delta`→incremental ThinkingEvent, `ThinkingBlock`→full; **drop `signature`; `redacted_thinking`/RedactedThinkingBlock → opaque fixed line, NEVER raw**; enable `include_partial_messages` + `thinking={"type":"adaptive","display":"summarized"}` ONLY when the project has thinking on), `render.py` (capped/collapsed 🧠 status line, cleared at turn end), `stream_session.py` (per-project thinking flag; twin-render dedup must hold with partials on), `bot.py` (`cmd_thinking` SB1 + menu + HELP).
- **Accept:** `/thinking on` (SB1, per-project, default OFF) → the next turns surface reasoning as a coalesced/capped `🧠` line via the existing send-gate, cleared at turn end; `/thinking off` → no thinking, normal turn (partials NOT enabled → no wire cost); `redacted_thinking` never renders raw (opaque indicator only); thinking is capped (no flood); the assistant's final answer is unchanged + not duplicated (twin-render dedup holds with partials on); RB1 — a malformed thinking block never crashes.
- **Tests:** normalize emits ThinkingEvent for `thinking_delta`/`ThinkingBlock`, drops `signature`, redacted→opaque; partials/`display` enabled ONLY when on; render caps + clears; toggle per-project; **dedup — final answer rendered once with thinking on** (mutation-probe the dedup); SB1 + menu lock-step.

### T-VERIFY — live-verify + merge — DONE
- **Codex QA:** round 1 NO_SHIP (1 blocker — `/plan` marker could survive a failed/aborted turn into a later message) → fixed (consume early in `handle_message`, thread `plan_turn` down) → **Codex re-check SHIP** (blocker CLOSED). Independent reviewer: **C4 AGREE + SB3 AGREE** (both mutation-probed with teeth).
- **Live phone-verify (real Mac, Telegram Web) — PASS:**
  - **Plan mode:** `/plan` → planning prompt → Claude proposed (didn't execute) → plan rendered with `[✅ Approve] [✋ Reject+feedback]`. Tapped Approve → execution resumed and the subsequent **Write STILL hit the permission gate** (ADR-001 **C4 verified live**) → tapped Deny → **no file created** (deny held, clean).
  - **Thinking:** `/thinking on` → confirmed it engages the SDK (live CLI args show `--include-partial-messages` + `--permission-mode default`); reasoning prompts → final answer rendered **exactly once** with the done footer (**dedup-with-partials holds live**), no Telegram render crash. (The transient 🧠 line clears too fast to screenshot on short prompts; mechanism is the unit-tested `edit_status` path.)
- **Robustness finding (investigated → NOT a code bug):** a messy live sequence (approve plan → deny a Write → rapid colliding inputs) left a turn busy ~9 min. Deterministic repro proves `_hold_depth` is correctly balanced across sequential holds + both allow/deny (the 300s liveness timeout DOES re-arm + fire when no hold is open); mutation-probe confirms. Root cause = a **Claude/CLI-side silent stall after the deny sequence** (a known SDK edge case — `read_messages()` hangs with no exception; the liveness bound is the only backstop) and/or input-collision; the P6/H2 mechanism is correct + P12-reachable, not P12-broken. Regression tests added (`8951a97`). **Follow-up (future phase):** tighten the post-deny liveness / kill the stalled subprocess on timeout (the lingering `--permission-mode plan` CLI didn't get reaped).
- 1288 tests; ruff/mypy/secret_scan clean.
