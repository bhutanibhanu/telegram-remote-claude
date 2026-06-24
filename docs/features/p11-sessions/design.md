# P11 — Universal Session Control Plane (the "entirety remote" headline)

_Roadmap-v2 phase 3 (see `docs/roadmap-v2.md`). Delta on P0–P10 (`main`). **Both spikes PROVEN** — discovery (`list_sessions`/`get_session_info`), resume/fork any `(session_id, cwd)`, AND live-mirror (append-only tail). This phase wires those into the bot._

## The vision (owner)
"See ALL active Claude Code sessions on my phone — even the one orchestrating right now — and switch to / drive any session, started on my Mac OR via Telegram. The entirety of the process becomes remote." Today the bot only knows the sessions IT created (its named projects). P11 makes it the control plane for **every** Claude session on the machine.

## Proven mechanisms (from the spikes — see `[[autonomous-p5-p9-run]]`)
- **Discovery:** `claude_agent_sdk._internal.sessions.list_sessions()` / `get_session_info(id, directory=cwd)` enumerate every session under `~/.claude/projects/<sanitized-cwd>/<id>.jsonl` (id, cwd, title, last_modified, git_branch, msg count). 334 found live. **`_internal` → PIN the SDK + wrap in ONE adapter.**
- **"Even this one":** the live orchestrator is discoverable via the process registry `~/.claude/sessions/<pid>.json` + the SDK lookup (proven).
- **Liveness (composite — none alone is reliable):** transcript `mtime` advancing + `ps` for `claude … (--resume|--session-id) <id>`/`stream-json` + `~/.claude/sessions/<pid>.json` validated by pid **and** `procStart` (pids recycle). "running" is a HINT, never a lock.
- **Attach:** resume any `(session_id, cwd)` — `fork_session=False` continues the SAME id; `=True` forks (new id, transcript copied). **cwd-scoped** (wrong cwd → `ProcessError`). **NO lock guards double-attach** → two writers SILENTLY fork-corrupt the transcript.
- **Live-mirror (v1.5):** the `.jsonl` is append-only; tail by (mtime, byte-offset), consume only up to the last `\n` (a reader can catch a half-written line); ~0.1–0.3 s flush lag. Transcript line `type`s map to the bot's `Event`s (assistant text → TextEvent, tool_use → ToolUseEvent, …).

## ⭐ HARD safety rules (non-negotiable)
1. **Never resume a session that is live elsewhere — FORK it.** Two writers on one `(id, cwd)` silently corrupt the conversation tree. The composite liveness check gates fork-vs-continue.
2. **SB1** on every new command/callback (`/sessions`, attach, watch).
3. **SB2 on discovered cwds:** a discovered session's cwd can be ANYWHERE on the Mac (outside `ALLOWED_ROOTS`). Attaching/driving one whose cwd is out-of-roots must be **refused or require explicit confirmation** (don't silently drive a session in an arbitrary dir) — consistent with the gate-on/confinement posture. (Tool use stays path-confined by P6 regardless.)
4. **SB3 on mirror:** a raw transcript line carries FULL tool bodies (file contents, command output). The mirror MUST run them through the body-free scrub (`safe_input_summary` / the render discipline) before sending — never dump raw bodies to Telegram.
5. **Privacy:** `/sessions` shows the owner their own machine's sessions (fine, SB1). Mirroring CONTENT is the operator's explicit choice per session.

## Tasks (build order)
- **T1 — Session discovery + `/sessions` (read-only listing).** A discovery adapter module wrapping the SDK `list_sessions`/`get_session_info` + the composite liveness signal (SDK-pinned, `_internal` isolated in one place). `/sessions` (SB1): list all Mac sessions — short id, cwd (`<code>`), title, last-active, running/idle marker — **merged + deduped with the bot's own projects** (mark which are bot-known/active). Read-only (no attach yet). This is the "see all, even this one" half — self-contained, lower-risk, high-value. Tests: discovery merges/dedups by id; liveness composite; SB1; body-free; the live orchestrator appears.
- **T2 — Attach / switch to any session.** Tap a `/sessions` entry or `/attach <id>` → adopt that `(session_id, cwd)` as a controllable project: idle → continue (same id); **live-elsewhere → FORK** (new id) + tell the operator why. SB2: refuse/confirm an out-of-`ALLOWED_ROOTS` cwd. Reuse the engine resume path + the P9 `[Open]`/switch callback infra. Tests: attach-idle continues; attach-live forks (never co-drives); out-of-root cwd refused/confirmed; SB1; the resumed session drives through the normal turn+gate path.
- **T3 — Live-mirror (v1.5, stretch).** `/watch <id>` tails a session's transcript → a dict→Event normalizer → `render_event` → the per-chat send gate, read-only follow; SB3-scrubbed (no raw tool bodies); a send-queue for Telegram flood. Higher effort — may defer if the phase runs long.
- **T4 — verify + Codex QA + live-verify (`/sessions` shows the live orchestrator; attach-fork on a live session) + merge.**

## Inherited facts / constraints
- Gates (worktree `.venv`): `pytest`, `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`. Floor: 1098 tests.
- Pin `claude-agent-sdk` (the discovery/resume APIs are `_internal`, re-exported — could break on upgrade; wrap in one adapter with a clear contract). CLI/SDK version coupling (ADR-001).
- SB1/SB2/SB3 per the hard rules above; one bot per token; don't rotate the token; live-verify via the logged-in Telegram Web.
- Mechanism + risk detail: the spike reports (discovery/resume + live-mirror) recorded in `[[autonomous-p5-p9-run]]`.
