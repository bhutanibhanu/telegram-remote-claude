# Progress: permission-gating (P2)

_Plan generated 2026-06-21 from design.md · 8 tasks · autonomous supervised build_

> **P2 — per-tool permission gating.** Replaces P1's interim "ordinary tool → auto-allow" posture with a
> real approval gate: risky tools pause for **[Allow once] / [Allow for session] / [Deny]**, reads/search
> run free, an unanswered prompt auto-denies at the 60-min backstop, `/cancel` aborts a waiting prompt, and
> the streaming default introduces **no bypass** (SB5). `/yolo` is the one loud, per-session escape hatch.
> Builds on P1 (`feat/streaming-engine`, tip `316b5ec`): **reuses** the engine's async hold + backstop +
> cancel (`engine/pending.py`), `decision_to_substrate(PermissionVerdict)`, the SB1-hardened `bot.py`
> `on_callback`, and `render.py`'s callback codec — it does not rebuild them.
>
> Decisions (G-Scope, design.md): **D1** fail-closed safe-allowlist · **D2** WebSearch auto / WebFetch+MCP
> gate · **D3** gate streaming = safe default, one-shot legacy flag untouched (no `ENGINE_MODE` flip) ·
> **D4** allow-session per tool NAME · **D5** canned deny · **D6** `/yolo` allow-all, off by default, loud
> throughout · **D7** grants + yolo in-memory, cleared on `/reset`/new-session/restart.

## Cross-cutting acceptance (applies where relevant)
- **SB5** (first satisfied here) — no default bypass on the streaming path; `/yolo` off by default + loud
  when on; one-shot's `--dangerously-skip-permissions` is the documented retiring legacy exception — T6.
- **SB1** — permission-approval taps are authn'd: a non-allowlisted / forged tap can **never** allow a tool
  (reuses P1's `on_callback` recheck + defensive `decode_callback`) — T5.
- **SB6** — fail-closed: unknown/new tools gate; defaults safe (yolo off) — T2/T6.
- **SB3** — the permission prompt renders a **body-free** tool-input summary (lengths, not contents); no
  secrets logged — T3/T4.
- **RB4** (first satisfied here) — `/cancel` + the 60-min backstop on a *permission* prompt don't wedge the
  session (auto-deny + notify, session usable after) — primary T3, **dedicated tests T7 (RB7)**.
- **RB1/RB2** — bad input never crashes (defensive decode, stray/late taps no-op); engine/substrate errors
  fail clean — threaded through T3/T4/T5.
- **ADR-001 caveat** — post-approval / plan-approval does **NOT** greenlight arbitrary execution; **every**
  risky tool gates independently — T3 (behavior) + T7 (explicit test).

## Task list
- [ ] T1 — ADR-003: permission-gating model · doc
- [ ] T2 — Risk classifier + per-session policy state (`permissions.py`) · unit
- [ ] T3 — Engine approval gate: classify + hold-for-approval + verdict mapping · unit (mock)
- [ ] T4 — Render permission prompt + `permission` callback kind + `/yolo` indicator · unit
- [ ] T5 — Bot/session wiring: route taps (SB1) + grants + `/yolo`/`/unyolo` + reset-clear · unit
- [ ] T6 — SB5 posture: no streaming bypass + blast-radius docs + fail-closed config · unit + docs
- [ ] T7 — SB/RB test matrix (SB5/SB1/RB4 + classifier + allow-once-vs-session + post-approval-gated) · unit
- [ ] T8 — Live end-to-end verify (real Claude) + owner phone-verify checklist · live probe

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (short sha) · `[!]` blocked

## Tasks

### T1 — ADR-003: permission-gating model
- **Goal:** Record the permission-gating model — risk taxonomy, allow-once vs allow-session, `/yolo`,
  canned-deny, and the reuse of P1's hold/backstop/cancel — grounded in D1–D7 + ADR-001/002.
- **Depends on:** none
- **Files (expected):** `docs/adr/ADR-003-permission-gating.md`.
- **Acceptance:**
  - ADR-003 SHALL document: the **fail-closed safe-allowlist** taxonomy (D1/D2 — WebSearch auto; Write/Edit/
    Bash/WebFetch/`mcp__*`/unknown gate); **allow-once vs allow-session** as engine-side state over the
    per-request `PermissionResultAllow` primitive (per ADR-001); **`/yolo`** (allow-all, off by default,
    loud, in-memory, `/unyolo`); **canned-deny** (D5); and the **reuse** of P1's `PendingRegistry`
    hold/backstop/cancel for permission prompts. Status **Proposed**; consistent with ADR-001/002.
  - ADR-003 SHALL state the **ADR-001 caveat**: approving a plan or one tool does **not** greenlight
    arbitrary execution — every risky tool gates independently.
- **Tests:** none — documentation deliverable.
- **Status:** todo

### T2 — Risk classifier + per-session policy state (`permissions.py`)
- **Goal:** A pure risk classifier (fail-closed safe-allowlist) + the per-session grant/`yolo` state the
  engine consults.
- **Depends on:** T1
- **Files (expected):** `claude_tg/permissions.py`; `tests/test_permissions.py`.
- **Acceptance:**
  - WHEN `classify(tool_name, tool_input)` is called for **Read/Glob/Grep/LS/TodoWrite/WebSearch**, it SHALL
    return SAFE; for **Write/Edit/MultiEdit/NotebookEdit/Bash/WebFetch/ any `mcp__*` / any unknown name**, it
    SHALL return RISKY (**fail-closed**, D1/D2). **(unit)**
  - The policy SHALL record an **allow-session grant keyed by tool NAME** (D4) and report a granted tool as
    allowed without re-asking; `clear()` SHALL drop all grants + `yolo` (D7).
  - WHEN `yolo` is on the policy SHALL report every tool as allowed; `yolo` is **off by default** (D6).
  - `AskUserQuestion` / `ExitPlanMode` SHALL NOT be treated as gated tools here (they stay on P1's
    answer-hold path).
- **Tests:** unit — classifier matrix (safe / risky / unknown→gate / WebFetch+MCP→gate / WebSearch→auto),
  grant record + per-name suppression, `clear()`, `yolo` on/off.
- **Status:** todo

### T3 — Engine approval gate: classify + hold-for-approval + verdict mapping
- **Goal:** Gate risky ordinary tools at `engine.on_tool_request` via the policy + a held approval; ask/plan
  answer-hold unchanged.
- **Depends on:** T2
- **Files (expected):** `claude_tg/engine/engine.py`, `claude_tg/engine/types.py`.
- **Acceptance:**
  - WHEN `on_tool_request` receives an ordinary tool the policy reports **allowed** (safe / granted / yolo),
    the engine SHALL allow it with **no prompt** (maps to `PermissionVerdict(allow)`). **(unit, mock substrate)**
  - WHEN it receives a **RISKY, not-granted** tool, the engine SHALL inject a **`PermissionEvent`** (carrying
    `tool_name`, a **body-free** input summary, `tool_use_id`, `session_id`) and **HOLD** the request via the
    existing `PendingRegistry` until a verdict arrives; **allow-once / allow-session → allow**; **deny →
    `PermissionVerdict(deny, message=<canned>)`** (D5).
  - **allow-session** SHALL record the per-tool-name grant so a second use of that tool in the session is
    auto-allowed (no second prompt) (D4).
  - WHEN the backstop elapses or `/cancel` fires on a pending permission request, the engine SHALL auto-deny
    and leave the session usable (**RB4**, inherited from `pending.py`).
  - `AskUserQuestion` / `ExitPlanMode` SHALL still route to the answer-hold **unchanged** (no permission gate).
  - **ADR-001 caveat:** approving one tool SHALL NOT auto-allow a different risky tool (each gates
    independently).
- **Tests:** unit (mock substrate) — allow path (safe/granted/yolo, no prompt), hold+allow-once, hold+
  allow-session (2nd use auto-allowed), deny→canned message, backstop+cancel auto-deny (no wedge), ask/plan
  unaffected, post-approval still gated.
- **Status:** todo

### T4 — Render permission prompt + `permission` callback kind + `/yolo` indicator
- **Goal:** Map `PermissionEvent` → Telegram message + the 3-button keyboard; extend the callback codec;
  render the loud `/yolo` indicator.
- **Depends on:** T3
- **Files (expected):** `claude_tg/render.py`; `tests/test_render.py` (additions).
- **Acceptance:**
  - WHEN a `PermissionEvent` renders, the system SHALL produce a message (body-free summary, SB3) + an inline
    keyboard with **[Allow once] / [Allow for session] / [Deny]**. **(unit)**
  - The `callback_data` codec SHALL encode/decode a **`permission`** kind (payload once/session/deny +
    `tool_use_id` correlation), assert **≤64 bytes** at build, and `decode_callback` SHALL reject malformed /
    foreign / oversize / wrong-arity → `None` (defensive — feeds SB1/RB1).
  - WHEN `/yolo` is on, the renderer SHALL produce a persistent loud **⚠️** indicator for outbound /
    auto-allowed actions (D6).
- **Tests:** unit — permission render + keyboard, codec round-trip + ≤64B + defensive decode of a permission
  payload, yolo indicator.
- **Status:** todo

### T5 — Bot/session wiring: route taps (SB1) + grants + `/yolo`/`/unyolo` + reset-clear
- **Goal:** Wire the permission taps, grant recording, `/yolo`/`/unyolo`, the loud indicator, and grant
  clearing into the bot + streaming session.
- **Depends on:** T3, T4
- **Files (expected):** `claude_tg/bot.py`, `claude_tg/stream_session.py`; `tests/` additions.
- **Acceptance:**
  - WHEN an **allowlisted** operator taps [Allow once]/[Allow for session]/[Deny], the system SHALL resolve
    the held permission request with that verdict; WHEN the tap is from a **non-allowlisted** chat (or
    decodes to None), it SHALL resolve nothing (**SB1**, reusing `on_callback`'s recheck). **(unit)**
  - **[Allow for session]** SHALL record the per-tool-name grant; a subsequent use of that tool SHALL NOT
    re-prompt (D4).
  - WHEN `/yolo` is sent the system SHALL enable allow-all for the session + surface the loud indicator;
    `/unyolo` SHALL revert; off by default (D6).
  - WHEN `/reset` (or a fresh session) occurs, the system SHALL clear all grants + `yolo` (D7).
  - No bypass flag SHALL be introduced on the streaming path (SB5/SB6).
- **Tests:** unit (mock engine) — tap→verdict routing, SB1 forged/non-allowlisted tap ignored, allow-session
  suppression, `/yolo`+`/unyolo`, `/reset` clears grants+yolo.
- **Status:** todo

### T6 — SB5 posture: no streaming bypass + blast-radius docs + fail-closed config
- **Goal:** Ensure the streaming default introduces no bypass, document the trust-model shift + the one-shot
  legacy exception, and keep config defaults fail-closed.
- **Depends on:** T3, T5
- **Files (expected):** `claude_tg/config.py`; `docs/features/permission-gating/design.md` (or a SECURITY
  note); possibly a `bot.py`/`stream_session.py` guard.
- **Acceptance:**
  - The streaming engine SHALL run with `permission_mode="default"` + the gating callback and SHALL NOT pass
    `--dangerously-skip-permissions`; **`/yolo` SHALL be the only (loud, per-session) bypass** (SB5). **(unit/inspection)**
  - The one-shot runner's `--dangerously-skip-permissions` SHALL be **unchanged** and documented as the
    retiring legacy exception (D3); **no `ENGINE_MODE` default flip** in P2.
  - Config defaults SHALL be fail-closed (unknown tools gate; `yolo` off) and the trust model + blast radius
    SHALL be documented (SB6).
- **Tests:** unit — assert the streaming command/path builds **no** skip-permissions flag; `yolo` off by
  default. (Docs otherwise.)
- **Status:** todo

### T7 — SB/RB test matrix (SB5/SB1/RB4 + classifier + allow-once-vs-session + post-approval-gated)
- **Goal:** The dedicated P2 security + reliability tests the cross-cutting baseline requires (RB7).
- **Depends on:** T2, T3, T4, T5, T6
- **Files (expected):** `tests/test_permission_sb_rb.py` (or extend `tests/test_security_reliability.py`).
- **Acceptance:** Dedicated, clearly-named tests SHALL exist for:
  - **SB5** — the streaming path introduces no default bypass; `/yolo` off by default + loud when on.
  - **SB1** — a non-allowlisted / forged permission tap can **never** allow a tool.
  - **RB4** — `/cancel` and the backstop on a *permission* prompt don't wedge; the session is usable after.
  - **classifier matrix** — safe auto-run; risky gate; unknown→gate; WebFetch+`mcp__*`→gate; WebSearch→auto.
  - **allow-once vs allow-session** — allow-once re-asks next time; allow-session suppresses (per tool name).
  - **canned-deny** — deny is relayed to the model as the canned denial.
  - **post-approval still gated** — a second risky tool after one approval still prompts; approving a plan
    does NOT free subsequent risky tools (the ADR-001 caveat).
- **Tests:** unit — the matrix above; substrate/engine mocked; no live Claude/network.
- **Status:** todo

### T8 — Live end-to-end verify (real Claude) + owner phone-verify checklist
- **Goal:** Prove the gate works **live** end-to-end against real Claude with code-injected verdicts, and
  hand the owner a phone-verify checklist for P2 acceptance.
- **Depends on:** T3, T4, T5, T6, T7
- **Files (expected):** `spikes/p2-permission-verify/` (throwaway, contained); `docs/features/permission-gating/verify.md`.
- **Acceptance:**
  - WHEN the engine runs live and a **risky** tool (e.g. Write/Bash) is requested, it SHALL pause; a
    code-injected **allow** SHALL run it; **deny** SHALL block it + relay the denial; **allow-session** SHALL
    suppress the next prompt for that tool; a **safe** tool (Read) SHALL run unprompted; **`/yolo`** SHALL run
    all unprompted (loud); **`/cancel`** + **backstop** SHALL clean-abort — verdict + scrubbed transcript.
    **(live probe; not in CI)**
  - The owner checklist SHALL give exact phone steps to confirm each of the above under `ENGINE_MODE=streaming`.
  - No API key; effects contained; evidence scrubbed (SB3).
- **Tests:** none — live verdict + transcript + owner checklist are the artifacts.
- **Status:** todo

## Rules
- **Flag-gated, branch-only.** All work on `feat/permission-gating`; **never merge to main**; the live
  one-shot bot keeps running unchanged (D3 — its legacy flag untouched; no `ENGINE_MODE` default flip in P2).
- **Reuse, don't rebuild.** P1's async hold + 60-min backstop + cancel (`engine/pending.py`),
  `decision_to_substrate(PermissionVerdict)`, the SB1-hardened `on_callback`, and the `render.py` callback
  codec are inherited — P2 adds a permission path on top.
- **Anti-goals:** `[Deny + reason]`; per-input / per-resource allow-session granularity; Bash command-aware
  classification; multi-project (P4) / concurrency (P5) / persisted grants + restart recovery (P4); the
  actual `ENGINE_MODE=streaming` default flip + one-shot retirement.
- **Substrate mocked in unit tests;** the live probe (T8) is isolated, contained, scrubbed, no API key.
- **`progress.md` is the single source of task truth;** `state.json` tracks the phase.
