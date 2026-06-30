# Observability — Feature Design

_Slug: `observability` · DELTA on the shipped streaming bot (P0–P14 + statusline, main @ 8e7a915)._
_Two phone-facing capabilities: (1) see agents working, (2) token/limit awareness._

## Section 1 — The basics

**Elevator pitch.** During a turn — especially subagent-heavy `/pipeline`/`/grill` runs — show on
the phone *what Claude is doing right now* and *how close the session is to its usage limit*, so the
owner isn't staring at a blank ⚙️ and isn't blindsided by a mid-turn limit cutoff.

**The problem (not the solution).** Two pains observed in live use:
1. A long turn shows only a generic ⚙️ "working" marker — no view of which subagent/tool is running,
   so the owner can't tell progress from a hang.
2. The owner repeatedly hits the Claude **rolling session limit** mid-turn (saw "session limit ·
   resets 8pm" + `turn_error` during today's verify) with no advance warning and no live sense of burn.

**The user.** The single operator (owner) driving Claude Code from their phone via Telegram; a power
user running subagent-heavy workflows that consume tokens fast.

**Definition of success.**
- During a turn, the owner sees the current tool + active subagent(s) updating live, **without spam**.
- The statusline shows how close the session is to its limit; the owner gets a **one-time** heads-up
  *before* hitting it.
- **Zero regressions** (1670-test floor green); **SB3/SB1/RB1 hold**; no added latency on the turn's
  critical path.

**Anti-goals (explicitly OUT).**
- NOT full transcript / streaming-output mirroring of subagents (that is `/watch`).
- NOT displaying tool **arguments** or any body/output (SB3 — tool/subagent **names only**).
- NOT historical token graphs, analytics, or a usage dashboard.
- NOT a message-per-tool event stream (edit-in-place only — no spam).
- NOT touching the permission-gate or turn-execution flow (pure observers; RB1).

**Constraints.** Delta on the shipped bot; PTB + `claude-agent-sdk==0.2.105` (pinned). Single
operator. Per-worktree `.venv` gates: `pytest` / `ruff` / `mypy` / `secret_scan`. Clean single-line
commits, **NO Co-Authored-By**.

## Section 2 — Requirements

**Functional (ranked).**
1. **Live activity line (agents).** A transient message posted at turn start, **edited in place** as
   the turn runs, showing the current tool name + active-subagent count/names. **Throttled** (coalesce
   to ≲1 edit/sec or on tool/subagent change). Collapsed to a one-line summary (or removed) at turn
   end. **Foreground chat only** (a background project's turn never writes it).
2. **Statusline limit field (🪙).** The pinned statusline gains a `🪙` field = **% of the rolling
   session limit** used (precise % if the SDK exposes it; else a `🟢/🟡/🔴` health badge). Rides the
   existing statusline lifecycle (turn start/end + refresh; foreground-only).
3. **Proactive limit warning.** A **one-time** heads-up message when the limit signal crosses
   "approaching" (🟡), per limit-window, suggesting wrap-up. De-duped — never repeats within a window.
4. **(Supporting) Telemetry tap.** Normalize SDK subagent/usage messages
   (`TaskStartedMessage`/`TaskUpdatedMessage`/`TaskUsage`, `RateLimitInfo`/`RateLimitStatus`) into new
   events the relay can render.

**Non-functional.**
- **SB3 (body-free):** activity line = tool/subagent **names only** — never args, file contents, or
  output. Limit field = numbers/badge only.
- **SB1:** the warning + any new command honor the existing authn allowlist.
- **RB1 (never-crash):** every new render path is a **best-effort observer off the turn's critical
  path** — any failure degrades silently (no line / no field), never breaks the turn.
- **Anti-spam / rate:** the activity line edits **one** message, throttled, within Telegram's edit
  limits and the existing per-chat send-gate. No new message per tool.
- **Latency:** observers add no blocking work to the turn loop.

**Future (6–12 mo, not v0).** Per-subagent token attribution (`TaskUsage`); `/tokens` + `/agents`
detail commands; limit forecasting. Today's design (event normalization + mixins) accommodates these
without rework.

## Section 3 — Architecture (deltas only)

**Data flow.**
```
SDK msgs ──► adapter_sdk.normalize()
  TaskStarted/Updated/Usage ─► ActivityEvent(tool/subagent name, status)   ─┐
  RateLimitEvent/RateLimitInfo ─► LimitEvent(status, pct?)                  ─┤
                                                                            ▼
  core turn loop ─► ActivityMixin (transient line, throttled, collapse@end) ─► edit-in-place msg
                 ─► statusline 🪙 field (format_statusline) + one-time LimitWarning ping
```

**New / changed modules (expected).**
- `claude_tg/engine/adapter_sdk.py` — normalize `Task*` + `RateLimit*` into `ActivityEvent`/`LimitEvent`;
  capture limit status/% (parallel to the existing `_capture_usage`); expose `limit_status()` /
  `last_activity()` accessors.
- `claude_tg/engine/engine.py` — delegate accessors (mirror `context_percentage()` / `last_model()`).
- `claude_tg/stream_session/activity.py` — **NEW** `ActivityMixin`: the transient activity-line
  lifecycle (post-at-start / edit-throttled / collapse-at-end), foreground-only, SB3 names-only, RB1
  best-effort — modelled on `StatuslineMixin`'s gated-edit + foreground-recheck discipline.
- `claude_tg/stream_session/statusline.py` + `render.py` — add the `🪙` limit field to
  `format_statusline` (+ the badge fallback).
- `claude_tg/stream_session/core.py` — wire the activity + limit observers into the turn lifecycle;
  fire the one-time warning.

**No new external deps. No persistence** (in-memory observers, RB3). **No new auth surface** beyond
SB1 on any command.

**Top open architecture question (spike first).** Does the bot's streaming SDK session actually emit
`Task*` messages for the subagents Claude spawns, or only `tool_use` blocks with `parent_tool_use_id`?
If `Task*` aren't emitted in this SDK/mode → fall back to inferring subagent activity from
`tool_use` + `parent_tool_use_id`. **This is the #1 technical risk → de-risk in T1.**

## Section 5 — Risks & open questions

**Top risks.**
1. **(technical) `Task*` not emitted** for the bot's subagents in streaming → can't name subagents.
   _Mitigation:_ spike in T1; fall back to `tool_use` + `parent_tool_use_id` inference.
2. **(technical) precise limit % not exposed** (only a status enum). _Mitigation:_ `🟢/🟡/🔴` badge
   fallback (owner already accepted this).
3. **(operational) edit-rate / spam** — a fast tool sequence could exceed Telegram edit limits.
   _Mitigation:_ throttle (coalesce ≲1/sec or on-change); reuse the send-gate; RB1 swallow on failure.
4. **(UX) two transient surfaces** (activity line + pinned statusline) clutter the chat.
   _Mitigation:_ activity line collapses/removes at turn end; the pinned statusline persists.

**Open questions (resolve in build).**
- Does the SDK emit `Task*` for the bot's sessions? (T1 spike — decides the agents data source.)
- Is a precise limit % available (`RateLimitInfo.raw`? a usage API?) or only a status badge?
- Warning trigger: map the SDK's "warning" status, or pick a % threshold (e.g. ≥80%).

**ADR to write.** **ADR-010 — observability:** the live activity line + the limit signal; the SB3
names-only rule; the throttle/edit-in-place discipline; the observer-off-the-critical-path RB1 stance;
the precise-%-or-badge fallback; the one-time-per-window warning de-dup.

## Section 6 — Roadmap

**v0 (MVP) — both capabilities. Suggested build order (de-risk first):**
- **T1 — telemetry spike + plumbing:** confirm `Task*` / `RateLimit*` availability in the bot's
  streaming session; normalize them into `ActivityEvent`/`LimitEvent`; expose accessors. _(De-risks the
  whole feature — decides the agents data source + the limit-% source.)_
- **T2 — statusline 🪙 limit field:** precise % or `🟢/🟡/🔴` badge + `format_statusline` + render tests.
  _(Smallest, reuses the statusline.)_
- **T3 — proactive limit warning:** one-time, de-duped per limit-window; SB1; RB1.
- **T4 — live activity line:** the `ActivityMixin` transient message (throttled, collapse-at-end,
  foreground-only, SB3, RB1) + tests.
- **T5 — ADR-010 + docs + live phone-verify** (drive Telegram Web; confirm the line + the 🪙 field
  render and the warning fires).

**Out of v0:** per-subagent token attribution, `/tokens` & `/agents` detail commands, historical /
analytics views.
