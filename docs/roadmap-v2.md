# Roadmap v2 — "Entirety Remote": the universal Claude control plane

_Next-gen roadmap after P0–P8 (shipped on `main`). Vision: make the **entire** Claude-Code workflow drivable from your phone — see and control **every** session on your Mac (the bot's own + external terminal/IDE sessions, even the one orchestrating this), with rich mobile-native input (voice, photos, files) and trustworthy supervision. Public OSS release (the original P9) is **deferred to the end** — these feature phases come first._

_Execution model: same per-phase pipeline as P0–P8 — `/grill` scope → `/plan` → supervised subagent build → gates → cross-model Codex QA → live phone-verify → merge. Spikes de-risk uncertain SDK capabilities BEFORE the dependent phase is scoped._

---

## ⭐ The headline new requirement (owner): Universal Session Control Plane
> "I should be able to see ALL active sessions on my phone — even this current one — and switch to any active session on my Mac or Telegram. The entirety of the process becomes remote if needed."

**What this means:** today the bot only knows about sessions *it* created (its named projects). The new capability: the bot becomes the **control plane for every Claude Code session on the machine** — discover them all (bot projects + external `claude`/IDE sessions + the live orchestration session), show them in one phone view (running vs idle, cwd, last activity), and **attach/switch to any of them from the phone** to drive it remotely. This is the centerpiece (P11).

**Feasibility (needs a spike):** Claude Code persists sessions under `~/.claude/projects/<cwd>/<session-id>.jsonl`; the bot already resumes by `(session_id, cwd)` (ADR-004). So "list all + attach any (resume)" is plausible by scanning that store + the existing resume path. **Open questions the spike must answer:** (1) reliable discovery of *running* vs idle sessions (process/lock detection); (2) can we resume a session that's *actively driven elsewhere* — or only idle ones (live takeover vs resume/fork)? (3) real-time *mirroring* of a live session's output to the phone (a shared event tail) vs. simple resume. v1 target: **see all + attach/resume any**; live-mirror of an actively-driven session is a stretch goal.

---

## Phases (proposed order — review/reorder freely)

### P9 — Quick wins & foundations  ·  effort S  ·  risk low
Fast, satisfying base that also sets up discoverability for everything after.
- **Command menu** (`setMyCommands`) + first-run onboarding — tappable, self-documenting
- **`/status`** health command (uptime, mode, per-project state, concurrency)
- **Cost/usage display** — `total_cost_usd`/`num_turns` are already captured & discarded today
- **Model routing** `/fast` (Haiku) / `/deep` (Opus) — instant for quick asks, full power for hard ones
- **Macros / saved prompts** (`/save qa "…"` → `/run qa`) + smart-reply chips
- **Notification polish** (no link previews, tappable, queue counter)

### P10 — See & Speak: multimodal + voice + files  ·  effort M  ·  spike: multimodal `query()` input
The mobile-native inputs you called out.
- **📸 Screenshots/photos → Claude** (path-confined, multimodal content block) — "here's the error/mockup, act on it"
- **🎙️ Voice notes → transcribe → turn** — dictate a task hands-free
- **📎 File send/receive** — drop a file/log/screenshot in; `/get` a patch/log/artifact out (no more 12-message dumps)
- **Rich tool-output rendering** — pytest/jest summaries, collapsible tracebacks, long logs auto-offered as a file
- _Spike first: confirm `claude-agent-sdk==0.2.105` `query()` accepts image content blocks._

### P11 — ⭐ Universal Session Control Plane  ·  effort L  ·  spike: discovery + resume-any + live semantics
The headline. **The entirety-remote capability.**
- **Discover all sessions** on the Mac — the bot's projects + external terminal/IDE `claude` sessions + the live orchestration session — running vs idle, cwd, last activity
- **`/sessions`** unified phone view across Mac + Telegram
- **Attach/switch to ANY session** from the phone (resume `(session_id, cwd)` as a controllable project) — drive any of them remotely
- Handle the live-session case: attach-resumes (other driver yields) for v1; **real-time mirror** of a live session = stretch goal
- _Spike first: session discovery (scan `~/.claude` + process detection), resume-any-session, and the live-takeover/mirror semantics — this is the riskiest unknown; spike before scoping._

### P12 — Supervise: plan-mode + live thinking/progress  ·  effort M  ·  spike: thinking-delta + mode-switch
Trustworthy, legible autonomy from the phone (much of the relay is already built).
- **Plan-mode review** — start a turn in `permission_mode="plan"`; approve/edit/reject the plan from the phone before execution
- **Live thinking stream + progress** — elapsed timer, last action, one-tap Cancel; collapsible "🧠 thinking" (the adapter already *sees* these deltas and discards them)
- **Show-diff-before-approve** — expand the actual change on a permission prompt before tapping Allow
- _Spike: renderable thinking-delta content + mid-session plan→default mode switch on one client._

### P13 — Trust layer for power  ·  effort M  ·  risk: security-core
So granting all this remote power stays safe (pairs with P10–P12).
- **📜 Audit trail** — durable, body-free log of every tool Claude ran (the single chokepoint `on_tool_request` already exists); phone-reviewable `/audit`
- **Bash destructive-command policy** — re-confirm `rm -rf`/force-push/`curl|sh` even when granted (closes the documented C2 residual)
- **Proactive owner-alerts** — DM on self-heal/wedge-rebuild/restart-loop (today they're silent)
- **Cost budget + alerts**, resource limits (wall-clock cap), **state backup/restore**

### P14 — Proactive & Integrated  ·  effort M–L
Pull→push + the dev loop.
- **git/PR surface** — `/diff` `/commit` `/pr`, CI status, `/ci`
- **Scheduled / triggered runs** — cron ("run tests at 9am, ping if red"), CI-fail hooks
- **MCP servers** — web/GitHub/DB/Sentry as gated tools (the gate already fail-closes every `mcp__*`)

### P15 — Public OSS release  ·  🛑 HARD STOP (the deferred original P9)
LICENSE finalize, final scrub, token rotation, make-public — all owner-gated, irreversible.

---

## Notes
- **Spikes first (de-risk before scoping):** P10 (multimodal input), P11 (session discovery/resume/mirror — the biggest unknown), P12 (thinking deltas + mode switch). Each is a short throwaway probe in the spirit of `spikes/session-substrate/`.
- **Reorder freely.** Suggested order balances value × risk × dependency: quick wins first (warm up the surface), the inputs you love next (P10), then the headline control plane (P11), then supervision + trust, then integrations, then release.
- Every phase keeps the safety posture (gate-on-default, path confinement, body-free) and ships only after cross-model QA + live phone-verify, same as P0–P8.
