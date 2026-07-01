# ADR-010 — Observability: live activity line + rolling-limit signal

**Status:** Accepted · **Date:** 2026-06-30 · Supersedes/extends: [ADR-009 (statusline)](ADR-009-statusline.md)

## Context

The owner drives Claude Code from their phone. During a turn — especially subagent-heavy
`/pipeline` / `/grill` runs — the bot showed only a generic ⚙️ "working" marker, so there was no
way to tell *what* was running (which subagent, which tool) from a hang. Separately, the owner
repeatedly hit the Claude **rolling session limit** mid-turn ("session limit · resets 8pm" +
`turn_error`) with no advance warning and no live sense of token burn.

Two SDK capabilities (verified on `claude-agent-sdk==0.2.105`, the pinned version) made this
addressable without guessing — the two build spikes (T1/T2) confirmed:

- **`RateLimitInfo.utilization`** — a fraction `0.0–1.0` of the rolling limit consumed (first-class
  field, confirmed in the SDK parser), alongside `status` (`allowed`/`allowed_warning`/`rejected`).
  So a **precise %** of the limit is exposed, not just a status.
- **`Task*` lifecycle messages** — `TaskStartedMessage` carries the subagent classifier as a
  first-class **`task_type`** field (e.g. `general-purpose`, `Explore`) — NOT buried in a tool
  input — with `TaskUpdatedMessage.status` for the lifecycle; `tool_use` + `parent_tool_use_id`
  is the fallback when `Task*` is absent.

## Decision

Add two **observers** — both body-free, both best-effort off the turn's critical path:

1. **Live activity line** (`ActivityMixin`, `stream_session/activity.py`). A *transient* message
   posted on first activity in a FOREGROUND turn, **edited in place** as the current tool /
   active subagents change, and **removed at turn end**. NOT pinned, NOT a per-turn "done" footer
   (the owner disliked that — the pinned statusline is the persistent summary). Format:
   `⚙️ <subagent type-names | N agents> · <tool>` — **names only**.
2. **🪙 rolling-limit field** on the pinned statusline (extends ADR-009). `🪙 <pct>%` when the
   precise `utilization` is available; else the `🟢/🟡/🔴` health badge from the normalized status;
   **omitted entirely** when no signal has been seen (mirrors the `ctx —` never-fabricate rule).
3. **Proactive one-time warning.** When the limit signal first crosses 🟡 `approaching` / 🔴
   `limited` during a foreground turn, post EXACTLY ONE wrap-up heads-up, de-duped per limit-window.

### The rules these MUST obey (the load-bearing decisions)

- **SB3 — body-free, names only.** The activity line shows tool NAMES and subagent TYPE-classifiers
  ONLY — never tool arguments, the `Task` prompt/description, file paths, command strings, or any
  output. The telemetry layer reads only `task_type`/`status`/tool-`name`; the SINGLE place it ever
  touches a tool input (`_subagent_type_from_task_tool_use`, the `Task*`-absent fallback) reads
  EXACTLY the one `subagent_type` classifier key and nothing else. The limit field is numbers/badge.
- **RB1 — observers off the critical path.** Every capture (`_capture_limit`/`_capture_activity` in
  the receive loop) and every render/send/edit/delete is wrapped and swallowed; a raising read, a
  send error, or odd state degrades silently (no field / no line) and NEVER breaks or wedges a turn.
- **Foreground-only + the B2 `/switch` re-check.** Both surfaces describe "what you're looking at":
  a BACKGROUND project's turn never writes them, and a SYNC `_is_foreground` re-check fires
  immediately before every raw send/edit (no `await` between) so a `/switch` during the gate wait
  drops a stale write — the exact discipline ADR-009 established (and B2-regression-locked here).
- **Anti-spam throttle.** The activity line is ONE message, edited with **skip-identical** + a
  **≲1 edit/sec time-throttle** (a change inside the interval is coalesced WITHOUT advancing the
  shown text, so the next change past the interval still shows the latest state); it rides the
  existing per-chat send gate.
- **Warning de-dup / re-arm.** One warning per non-`ok` window: set the de-dup flag
  (`_ChatState.limit_warned`) only AFTER a SUCCESSFUL send (a failed send re-warns next turn rather
  than silently swallowing the operator's only heads-up); re-arm (clear) when the status returns to
  `ok`. An unknown status is a non-event (neither warns nor mutates the flag). In-memory (RB3).
- **No precise reset time / no per-subagent attribution / no new commands in v0.** The warning does
  not invent a reset time (the limit accessor stays `(status, pct)` — not widened); per-subagent
  `TaskUsage` attribution and `/tokens`·`/agents` detail views are deferred.

## Consequences

- The phone now shows, live and without spam: the running tool + active subagents, the % of the
  rolling limit, and a one-time heads-up before a mid-turn cutoff.
- New telemetry seams on the substrate (`limit_status()`, `last_activity()` → `ActivitySnapshot`)
  + `Engine` delegates, mirroring `context_percentage()`/`last_model()` (additive, defensive getattr).
- The activity line + the limit field are **foreground-only** and best-effort — a background turn
  or any failure leaves the chat exactly as before (no regression to the 1670 floor; **1740** now).
- **Live confirmation** that the bot's streaming session actually emits `Task*` is the one thing the
  offline spikes could not prove — covered by the T6 phone-verify; the `tool_use`+`parent` fallback
  means the line works regardless.

## Alternatives considered

- *Activity in the pinned statusline* (one line, no new message) — rejected: too little room for
  subagent + tool, and the statusline is foreground-pinned state, not a live ticker.
- *On-demand `/agents` / `/tokens` commands only* — rejected for v0 as the primary surface (the
  owner wanted live visibility), but kept as a natural future addition.
- *A message per tool/subagent event* — rejected: spam + Telegram edit-rate violations.
- *Raw cumulative token count on the bar* — rejected in favour of the **% of the rolling limit**,
  which targets the actual pain (the cap), since `utilization` exposes it precisely.
