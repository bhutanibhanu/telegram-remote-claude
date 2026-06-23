# P6 — Security Audit & Threat Model (GATE 3)

_Delta "feature" on the shipped P0–P5 codebase (`main` @ 572936c). This is an AUDIT phase, not a feature build: the deliverable is a threat model + a findings report + fixes for blocking findings + a **verdict & recommendation surfaced to the owner** (hard gate — never silently declare "secure for release"). Framing: defensive correctness review of an authorization/confinement boundary (it's our own code, our own bot)._

## Problem
The bot lets a remote Telegram operator drive **Claude Code with real tool access on the host** (file I/O, shell via the SDK). That is inherently high-privilege: the security of the whole system rests on (a) only the operator can talk to it, (b) Claude's actions stay confined, (c) nothing leaks secrets, (d) it fails closed and never wedges. P0–P5 were built against explicit SB/RB invariants; P6 verifies they are **actually enforced everywhere** in the merged tree and rates the residual risk before the eventual OSS release (P9).

## Success
- A written threat model (assets, trust boundaries, attackers, attack surface).
- A findings table: each SB/RB invariant audited across the real code → enforced / gap, with severity (Critical/High/Med/Low) + file:line evidence.
- Blocking findings (Critical/High) fixed (supervised, red-green) + re-verified; the rest recorded with a recommendation (fix-now / fix-before-P9 / accept).
- A clear **verdict + recommendation** for the owner: is it safe (1) for the owner's own use today, (2) for public OSS release at P9 (with what conditions)?

## Anti-goals
- NOT a pentest of Telegram or Anthropic infrastructure (trust their TLS/auth). NOT auditing the Claude model's alignment. NOT adding features. NOT a perf audit (except where a DoS/resource-exhaustion is a security concern).
- We assume a **local attacker on the host has already won** (they can read `.env`); file perms (0600) only limit casual exposure, not a determined local attacker. In scope is the REMOTE + the Claude-confinement boundary.

## Threat model

### Assets
1. **Bot token** (Telegram credential) — `.env`, gitignored. Compromise = attacker impersonates the bot.
2. **The host** — the bot runs Claude with tool access; an escape = arbitrary code exec / file access on the operator's machine outside intended roots.
3. **Session state** (`session_store`: per-project cwd + `claude_session_id`) — atomic + 0600.
4. **Conversation content** — operator↔Claude messages, tool I/O.

### Trust boundaries & the controls that defend them
- **Telegram → bot (inbound updates + callback taps).** Control: **SB1** — every inbound is authenticated against `TELEGRAM_ALLOWED_CHAT_IDS`; non-allowlisted is dropped. The single most important boundary.
- **Bot → Claude/host (tool actions).** Controls: **SB2** path confinement (`paths.resolve_within_roots` / `ALLOWED_ROOTS`) on every cwd-setting + path-taking entrypoint; **permission gating** (operator approves tool use); **SB5** the only bypass (yolo/skip-permissions) must be off-by-default + loud.
- **Bot → disk/logs.** Controls: **SB3** body-free, no-secret logging; **SB6** fail-closed; **RB6** persistence integrity (atomic, 0600).

### Attackers
- **A1 — Unauthorized Telegram user.** Messages/taps the bot without being allowlisted. MUST be fully rejected on EVERY handler (commands, free-text, callbacks). [SB1]
- **A2 — Misaligned / prompt-injected Claude.** Tries to write/read outside `ALLOWED_ROOTS`, exfiltrate secrets, or take an unapproved high-impact action. Bounded by SB2 + permission gating + SB5. _Key question: does the bot actually constrain the SDK's filesystem reach, or only its own path commands?_
- **A3 — Repo/log reader.** Finds a secret in git history, logs, or evidence files. [SB3 + secret_scan]
- **A4 — Resource exhaustion / DoS.** Floods messages/turns to exhaust host resources or wedge the bot. [RB5 rate-limit, MAX_CONCURRENT_RUNS cap, RB1 never-crash]

## Audit scope — the invariant checklist (verify ACTUAL enforcement in the merged P0–P5 tree)
- **SB1 authn-everywhere:** enumerate EVERY PTB handler (each command, message, callback) and confirm each gates on `_authorized` before any side effect. Any handler reachable without the check = finding. (P5 added the routed-callback path — re-confirm.)
- **SB2 path-confinement-everywhere:** every entrypoint that sets/uses a cwd or path — `/new`, `/cd`, resume-path re-validation, project registry load — applies `resolve_within_roots`. **Critically:** does Claude's own tool execution (the SDK) stay within `ALLOWED_ROOTS`, or can a tool call escape? Document the actual confinement mechanism + its limits.
- **SB3 no-secret/body-free logging & notifications:** scan logging + error + notification paths for token / file-content / message-body / `claude_session_id` leakage (the live-verify memory: a summary file once leaked a raw session id). Confirm the body-free notification routing added in P5.
- **SB4 name validation:** project-name (`^[A-Za-z0-9_-]{1,32}$`) + any other user-supplied identifier validated before use (no path traversal via names).
- **SB5 bypass off-default + loud:** the startup log shows `skip_permissions: True` — **AUDIT THIS**: is the permission bypass on by default? When/how is it enabled, is it loud + transient + reset-on-restart, and does the operator-facing surface make an allow-all state obvious? (Background-yolo loudness gap was already flagged in P5.)
- **SB6 fail-closed:** ambiguous/erroring auth or path checks deny rather than allow.
- **RB1 never-crash / RB2 clean-failure:** unhandled exceptions in any handler; a turn error must fail clean, not crash the bot or wedge the chat.
- **RB3 restart/resume correctness · RB6 persistence integrity:** atomic writes, 0600, schema-migration safety, no in-flight runs persisted.
- **RB5 rate-limit safety:** the per-chat send gate + cap actually bound outbound + resource use.

## ⭐ Lead remediation task (carried from P5 live-verify; PRE-EXISTING)
A permission/ask hold open **>120s** trips the streaming per-message `_send_timeout` (`engine.py`/`adapter_sdk` `asyncio.wait_for(120)`) → `driver_error`; a verified-session `driver_error` is never recovered (engine not stopped/rebuilt; `_is_resume_failure_event` excludes "timed out") → **the project wedges until process restart**. High-frequency (humans take >2 min to approve). This is an RB1/RB2 reliability defect.
- **Fix:** an open permission/ask hold must NOT trip the per-message liveness timeout — human-wait is governed by the answer-hold backstop (the 120s bounds a stuck *Claude*, not a waiting *human*); + defensively tear down + rebuild a project's engine on a verified-session `driver_error` so it recovers instead of wedging. Streaming-only (oneshot/`claude_runner` unaffected). Red-green + live re-verify the >120s-hold scenario.

## Expected build order (input to /plan)
1. **Threat model doc** (this design's threat model, formalized) + **the audit findings report** (dispatch audit subagents per invariant area; cross-model Codex security pass) → findings table with severity.
2. **Fix the lead task** (timeout-wedge) — supervised, red-green, + targeted live re-verify.
3. **Fix any other Critical/High findings** the audit surfaces (supervised each).
4. **Verdict & recommendation** doc → **SURFACE TO OWNER** (GATE 3 hard stop for the security verdict).

## Inherited facts / constraints
- `ENGINE_MODE` defaults to **oneshot**; streaming/multi-project/concurrency is opt-in. The live bot today runs whatever the owner set.
- Gates (worktree `.venv`): `pytest`, `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`. Regression floor: 754 tests.
- ADRs 001/004/005 are accepted; findings must cite them where relevant. SB/RB definitions live in ADR-001 + the prior phase docs.
