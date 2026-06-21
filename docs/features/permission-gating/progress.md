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
- [x] T1 — ADR-003: permission-gating model · doc (a211432)
- [x] T2 — Risk classifier + per-session policy state (`permissions.py`) · unit (81185f2)
- [x] T3 — Engine approval gate: classify + hold-for-approval + verdict mapping · unit (mock) (aeb29ac)
- [x] T4 — Render permission prompt + `permission` callback kind + `/yolo` indicator · unit (c4779e5)
- [x] T5 — Bot/session wiring: route taps (SB1) + grants + `/yolo`/`/unyolo` + reset-clear · unit (bf70dc7)
- [x] T6 — SB5 posture: no streaming bypass + blast-radius docs + fail-closed config · unit + docs (e6bc40a)
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
- **Status:** done (a211432) — `docs/adr/ADR-003-permission-gating.md` written, status **Proposed**.
  Records the model: fail-closed safe-allowlist (D1/D2 — Read/Glob/Grep/LS/TodoWrite/WebSearch auto; all
  else incl. WebFetch/`mcp__*`/unknown gate), `[Allow once]`/`[Allow for session]`/`[Deny]` verdicts
  (allow-session = engine-side per-tool-NAME grant over the ADR-001 per-request primitive), canned-deny
  (D5), `/yolo` allow-all off-by-default + loud + in-memory (D6/D7), and the **reuse** of P1's
  `PendingRegistry` hold/backstop/cancel for permission prompts (RB4). Binds the ADR-001 caveat
  (post-approval / plan-approval never greenlights arbitrary execution — every risky tool gates
  independently). Consistent with ADR-001/002.

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
- **Status:** done (81185f2) — `claude_tg/permissions.py` (**pure** — no telegram/engine/SDK/IO/async):
  `is_risky()` fail-closed safe-allowlist (`SAFE_TOOLS`={Read,Glob,Grep,LS,TodoWrite,WebSearch}; everything
  else incl. **WebFetch**/`mcp__*`/unknown/empty/non-str → RISKY; an **allowlist** check, never a deny-list)
  + `PermissionPolicy` (in-memory per-session: `needs_approval`=False iff yolo|safe|per-name grant;
  `grant_session` by NAME only [D4]; `set_yolo` [D6, off by default]; `clear()` drops grants+yolo [D7]).
  **Verified by me on 0.2.105:** pytest **284 passed** (+37), ruff/mypy/secret-scan clean. **One fresh
  independent reviewer AGREES done** — mutation-probed (a fail-OPEN flip fails 31/37 tests; the
  anti-widening guard bites), purity + scope confirmed (only `permissions.py` + its test; engine/bot/render
  untouched). Wiring is T3+.

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
- **Status:** done (aeb29ac) — engine gate replaces P1 auto-allow (ADR-003). New types: `PermissionEvent`
  (body-free summary, SB3), `PermissionDecision` (allow_once/allow_session/deny), `DENIED_MESSAGE`,
  `safe_input_summary` (SDK-free). `on_tool_request`: ask/plan → answer-hold **unchanged**; ordinary →
  `policy.needs_approval` False (safe/grant/yolo) → allow no prompt; risky + no `tool_use_id` →
  **fail-closed deny** (SB6); else `_permission_hold` (inject `PermissionEvent` + hold on the **shared**
  `PendingRegistry` + map verdict). `_verdict_for`: allow_session → `policy.grant_session(name)` (in the
  engine, on resolve, per-NAME — ADR-001 caveat) + allow; allow_once → allow; deny → deny(`DENIED_MESSAGE`).
  `Engine.__init__` `permission_policy` defaults to a fresh `PermissionPolicy()` → **fail-closed default**.
  **RB4 for free:** backstop → `PermissionVerdict(deny)` and `/cancel` → `Cancel` both fall through
  `decision_to_substrate` → substrate **DENY** (never auto-allow, never hang); `decision_to_substrate` stays
  **pure**. **Verified by me on 0.2.105:** pytest **295 passed** (+11), ruff/mypy/secret-scan clean. **One
  fresh independent reviewer AGREES done** — traced RB4 backstop/cancel→deny (mutation: flip backstop→allow
  fails a test), false-pass mutations caught (drop grant → suppression fails; revert to auto-allow → 9 tests
  fail with correct blast radius), P1 tests re-pointed not weakened. Scope: `engine.py`/`types.py`/`__init__`
  + 2 engine tests; permissions/adapter/render/bot/stream_session/config/main untouched. Render/bot = T4/T5.

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
- **Status:** done (c4779e5) — `render.py`: `PermissionEvent` → **verbatim** `RenderAction` (body-free
  summary, SB3) + `permission_keyboard` [✅ Allow once / ☑️ Allow for session / ⛔ Deny]. Codec extended:
  `KIND_PERMISSION="m"` (single char) + 1-char action codes o/s/d → `permission_action` once/session/deny;
  `"m|<id>|<action>"` = **53 B** worst-case (≤64; `ValueError` past 64). `decode_callback` defensive
  (unknown action / wrong arity / oversize / empty → `None`, never raises). `yolo_banner()`/`yolo_indicator()`
  loud ⚠️ helpers (D6; placement is T5). **Verified by me on 0.2.105:** pytest **315 passed** (+20),
  ruff/mypy/secret-scan clean. **One fresh independent reviewer AGREES done** — byte budget proven (53 B
  worst, `ValueError` at 61-char id), defensive decode mutation-probed (fail-open → 4 subtests fail), no
  regression to ask/plan codec, SB3 body-free render confirmed. Scope: `render.py` + `test_render.py` only.
  Bot wiring (taps→verdict, `/yolo` command, indicator placement) = T5.

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
- **Status:** done (bf70dc7) — `bot.py` + `stream_session.py` wire the gate. A single per-chat
  `PermissionPolicy` (fresh, fail-closed) is **shared** engine↔session: `_ChatState.policy` is threaded
  through the `EngineFactory` → `Engine(permission_policy=…)`, so `/yolo` + the engine's allow-session grant
  + `/reset`-clear all act on ONE object. `resolve_callback` `permission` branch → `_resolve_permission`
  maps the tap (once/session/deny) → `PermissionDecision` → `engine.resolve` (grant recorded **engine-side**,
  T3 — not here). `/yolo` + `/unyolo` commands (allowlisted via `_ok`; streaming-only; loud `yolo_banner`
  reply) registered with the `allowed` filter; **`on_callback`/`_authorized` UNCHANGED** (permission taps
  ride the existing SB1-checked handler). `_drive_turn` leads each turn with a standalone ⚠️
  `yolo_indicator()` while yolo on (D6, uncoalescable). `reset` → `policy.clear()` (D7). **Verified by me on
  0.2.105:** pytest **331 passed** (+16), ruff/mypy/secret-scan clean. **One fresh independent reviewer
  AGREES done** — load-bearing probes confirmed (drop `reset` `clear()` → /reset test fails; drop
  `_authorized` → unauthorized permission-tap test fails), shared-policy + verdict mapping correct. Scope:
  `bot.py`/`stream_session.py` + 3 test files; engine/permissions/render/config/main untouched.

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
- **Status:** done (e6bc40a) — SB5 posture confirmed + documented. Streaming path = `permission_mode="default"`
  + the `can_use_tool` gate + the shared `PermissionPolicy`, **no** `--dangerously-skip-permissions` /
  allow-all flag → fail-closed by default; `/yolo` is the only bypass (off by default, loud). Guard tests
  (`test_security_reliability.py`): the factory wires default-mode + gate + shared policy and no bypass;
  `PermissionPolicy().yolo` is False + `yolo_banner` is loud (⚠️). `design.md` gains a **"Security posture &
  blast radius (SB5/SB6)"** section: the trust-model shift (approval gates + path policy, not bypass), the
  one-shot legacy exception (D3 — no `ENGINE_MODE` flip), and the blast radius (single operator/chat;
  classifier fail-closed; `cwd` not an OS sandbox; plan-approval greenlights nothing — ADR-001 caveat; full
  SB consolidation = P6). **Verified by me on 0.2.105:** pytest **333 passed** (+2), ruff/mypy/secret-scan
  clean. Done directly (docs + structural guards, like T1's ADR); behavioral SB5 (`/yolo` via the bot) is in
  the T7 matrix. Scope: `design.md` + `test_security_reliability.py`.

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
