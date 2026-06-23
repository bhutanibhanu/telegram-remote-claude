# Progress: p6-security-audit

_Audit done (`findings.md`); owner directive 2026-06-23: **fix all four (C1,C2,H1,H2) now, don't stop.** C2 posture decision (owner deferred to my recommendation): **prompt-on-out-of-root** — out-of-root tool targets become an approval prompt; in-root stays frictionless; `ALLOW_ANY_PATH` opts out. Supervised build: Implementer → independent reviewer → commit on green+AGREE._

## Task list
- [ ] R1 — C1/SB5: operator permission gate ON by default (bypass opt-in + loud)
- [ ] R2 — C2/SB2: confine the SDK's tool execution to ALLOWED_ROOTS (prompt-on-out-of-root)
- [ ] R3 — H1/SB3: scrub session ids in logs + body-free error rendering by default
- [ ] R4 — H2/RB2: open hold doesn't trip the per-message liveness timeout + rebuild engine on verified-session driver_error
- [ ] R5 — Re-audit (Codex) to SHIP + live re-verify (>120s hold recovers; default-gated prompts; out-of-root prompts) → verdict update → merge

Legend: `[ ]` todo · `[x]` done (sha) · `[!]` blocked

## Tasks

### R1 — C1/SB5: permission gate ON by default
- **Files:** `config.py` (defaults), `claude_runner.py` (flag), `bot.py` (loud startup).
- **Acceptance:** default `Config.skip_permissions=False`; `from_env` with `CLAUDE_SKIP_PERMISSIONS` unset → False; default oneshot does NOT pass `--dangerously-skip-permissions`; explicit `CLAUDE_SKIP_PERMISSIONS=true` still works AND logs a loud WARNING ("permission gate DISABLED"); streaming gating unaffected.
- **Tests:** default-gated; opt-in loud; oneshot flag presence keyed to opt-in. Mutation-probe.

### R2 — C2/SB2: SDK tool path confinement (prompt-on-out-of-root)
- **Files:** `permissions.py` (path-aware policy), engine permission decision (`engine/engine.py`/`types.py`), `paths.py` (resolve helper).
- **Acceptance:** a tool call whose resolved target sits OUTSIDE `ALLOWED_ROOTS` requires approval (even safe tools Read/Glob/Grep/LS); in-root calls keep current behavior (safe→auto, risky→prompt); paths resolved canonically (symlink/`..` traversal can't escape); `ALLOW_ANY_PATH=true` disables the path policy (operator takes the wheel); Bash (no reliable static target) → treated as out-of-root-class (prompt) unless ALLOW_ANY_PATH. Per-tool input keys: Read/Write/Edit/MultiEdit/NotebookEdit `file_path`/`notebook_path`, Glob/Grep `path`, LS `path`. Fail-closed on unparseable input (SB6).
- **Tests:** in-root Read auto; out-of-root Read/Glob prompts; out-of-root Write/Edit prompts; `..`/symlink escape resolved + caught; ALLOW_ANY_PATH bypasses; Bash prompts by default. Mutation-probe.

### R3 — H1/SB3: log/error scrubbing
- **Files:** `engine/engine.py`, `stream_session.py`, `render.py`, `engine/adapter_sdk.py`, `claude_runner.py`.
- **Acceptance:** `claude_session_id` is hashed/truncated (never raw) in any log line; foreground + background error rendering is body-free by default (no raw `ErrorEvent.message`/tool/CLI/stderr body to Telegram) — a fixed safe summary + kind; raw detail only behind an explicit local debug artifact (scrubbed). No token ever logged (already true — keep).
- **Tests:** a session id does not appear raw in captured logs; an error event with secret-bearing text renders body-free to the chat. Mutation-probe.

### R4 — H2/RB2: open-hold timeout + driver_error recovery
- **Files:** `engine/adapter_sdk.py`, `engine/engine.py`, `stream_session.py`.
- **Acceptance:** while a permission/ask/plan hold is OPEN, the per-message liveness timeout does NOT fire (human-wait is governed by the answer-hold backstop, not the 120s send-timeout); a `driver_error` on a verified session tears down + rebuilds that project's engine so the NEXT message works (no wedge-until-restart); per-project isolation preserved; oneshot/`claude_runner` unaffected unless it shares the path.
- **Tests:** a hold held > the send-timeout does NOT produce `driver_error`; a simulated verified-session `driver_error` → engine rebuilt → next turn succeeds; slot/lock not leaked. Mutation-probe + live re-verify in R5.

### R5 — Re-audit + live re-verify + verdict update
- **Acceptance:** Codex re-audit returns no remaining Critical/High on C1/C2/H1/H2; full gates green; live re-verify (browser) confirms: default config now PROMPTS for a risky tool; an out-of-root Read/Write PROMPTS; a >120s hold completes (no wedge); a verified-session error recovers. Update `findings.md` verdict → release-readiness re-assessed; merge P6 to `main`.
