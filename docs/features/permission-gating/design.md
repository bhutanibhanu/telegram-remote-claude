# Design: P2 — Per-Tool Permission Gating

> **Feature slug:** `permission-gating` · **Pipeline:** P2 (second build pipeline; depends on P1).
> Worktree `feat/permission-gating`, branched off P1's `feat/streaming-engine` (tip `316b5ec`) — it
> carries the full P1 streaming engine. Parents:
> [`docs/interactive-remote-design.md`](../../interactive-remote-design.md) §P2 ·
> [`docs/cross-cutting-requirements.md`](../../cross-cutting-requirements.md) (**SB5**, **RB4**, SB1, SB6) ·
> [`docs/adr/ADR-001-session-substrate.md`](../../adr/ADR-001-session-substrate.md) (the C2 permission
> primitive; "allow-session vs allow-once is engine-side state over the per-request primitive";
> post-approval execution stays permission-gated) ·
> [`docs/adr/ADR-002-async-answer-hold.md`](../../adr/ADR-002-async-answer-hold.md) (the hold/backstop/cancel
> mechanism P2 **reuses** for permission prompts).
>
> **The point of P2:** stop running risky tools unprompted. Replace P1's interim "ordinary tool →
> auto-allow" posture with a real per-tool approval gate — risky actions pause for
> **[Allow once] / [Allow for session] / [Deny]**, reads/search run free, an unanswered prompt
> auto-denies at the 60-min backstop, `/cancel` aborts a waiting prompt, and the default streaming
> path no longer bypasses permissions. `/yolo` is the one deliberate, loud, per-session escape hatch.

---

## Scoping decisions (confirmed at G-Scope, 2026-06-21)

Owner-confirmed via grill. These shape the build; the rest of the doc elaborates them.

| # | Decision | Value |
|---|---|---|
| **D1** | **Risk classification** | **Safe-allowlist, gate the rest (fail-closed, SB6).** An explicit allowlist of known read-only/safe tools auto-runs; **everything else — Write/Edit/Bash/MCP/unknown — pauses** for approval. A new/unknown tool gates by default. |
| **D2** | **Network + MCP** | **WebSearch auto-runs** (a "read"); **WebFetch (arbitrary-URL egress) and all `mcp__*` tools GATE.** |
| **D3** | **Bypass removal (SB5)** | **Gate streaming = the safe default.** P2 fully gates the streaming engine and makes it the intended default; the one-shot runner keeps `--dangerously-skip-permissions` as a **non-default legacy path** (it has no approval channel), retired later. The actual `ENGINE_MODE` flip is the owner's call after P1 phone-verify. |
| **D4** | **allow-session scope** | **Per tool NAME.** [Allow for session] stops re-asking for all future uses of that tool this session. (Granularity can refine later; `/yolo` is the broad hatch.) |
| **D5** | **Deny UX** | **Canned deny, no typing.** [Deny] denies immediately with a standard message the model adapts to. (No `[Deny + reason]` in P2 — deferred.) |
| **D6** | **`/yolo`** | **Allow-all for the session, loud throughout.** `/yolo` auto-allows every gated tool for the rest of the session; `/unyolo` reverts; **off by default**; a `⚠️` banner on enable **and a persistent on-indicator on every auto-allowed action** so it is never silently on. |
| **D7** | **Grant lifetime** | **In-memory; cleared on `/reset`, a fresh session, or a bot restart.** A restart never silently resumes an allow-all / `/yolo` posture. |

---

## The basics

**Elevator pitch.** Make the remote **safe for risky work**: Claude can no longer write files or run
shell commands unprompted — each risky action pauses on your phone with **[Allow once] /
[Allow for session] / [Deny]**, while reads and searches run free. `/yolo` is a loud, per-session
speed escape hatch.

**The actual problem.** P1 shipped the streaming engine with an **interim posture**: ordinary tools
(Write/Edit/Bash/…) are **auto-allowed** inside the single allowlisted chat (`engine.on_tool_request`),
and the legacy one-shot runner uses `--dangerously-skip-permissions`. That is fine for a trusted demo
but **not safe for real risky work** — a wrong or hijacked instruction edits files / runs shell with no
chance to intervene. P2 turns the engine's existing **C2 permission primitive** (proven in P0/ADR-001,
held async in P1/T5) into an actual approval gate, and removes the bypass from the go-forward path.

**Who it's for.** The **single allowlisted repo owner** driving the bot as their remote control. Not
multi-user; one operator, one active session (the P1 model).

**Definition of success (concrete).** Under `ENGINE_MODE=streaming`, from the phone:
- A **Write/Edit/Bash** pauses with **[Allow once] / [Allow for session] / [Deny]**.
- **[Allow once]** runs it and **re-asks** the next time that tool is used.
- **[Allow for session]** runs it and **stops re-asking that tool** for the rest of the session (D4).
- **[Deny]** runs nothing and relays a denial the model **adapts to** (D5).
- A **Read/Grep/Glob/WebSearch** runs with **no prompt**; a **WebFetch / `mcp__*`** **pauses** (D2).
- An **unknown/new tool** pauses (fail-closed, D1).
- **`/yolo`** runs everything with a **persistent loud `⚠️` indicator**; **`/unyolo`** restores gating (D6).
- A prompt left unanswered **auto-denies at the 60-min backstop + notifies**, session stays usable (RB4).
- **`/cancel`** aborts a waiting prompt cleanly (RB4).
- The **default streaming path introduces no bypass** (SB5); a **restart clears** all grants + `/yolo` (D7).
- **One-shot behavior is unchanged** (legacy path; still its own flag) and **CI is green**.

**Anti-goals (explicit non-features).**
- **No `[Deny + reason]` free-text denial** (D5 — canned only; deferred).
- **No per-input / per-resource allow-session granularity** (D4 — per tool name; deferred).
- **No Bash command-pattern classification** (whole-Bash gates; command-aware auto-run is a deferred
  refinement).
- **No multi-project / named sessions** (P4); **no concurrency** (P5); **no crash-recovery / persisted
  grants** (D7 keeps grants in-memory by design; RB3/RB6 are P4).
- **Not changing the one-shot runner's posture** (D3 — its `--dangerously-skip-permissions` stays until
  one-shot is retired in a later cleanup).
- **Not re-opening P1's interactive answer-hold** — `AskUserQuestion` / `ExitPlanMode` stay on the
  answer-hold path unchanged; P2 only gates *ordinary* tools.

**Constraints.** Builds on P1 (engine, `pending.py` hold+backstop+cancel, render/callback codec, SB1
callback handler). `claude-agent-sdk==0.2.105`, host CLI auth, **no API key**. Single operator, single
active session. SB/RB cross-cutting baselines apply; **SB5 + RB4 are first satisfied here**.

---

## Requirements

### Functional — ranked

1. **Risk classifier (`permissions.py`).** A pure function `classify(tool_name, tool_input) →
   SAFE | RISKY` implementing the **fail-closed safe-allowlist** (D1/D2): `SAFE_AUTO_RUN` = {Read, Glob,
   Grep, LS, TodoWrite, WebSearch, NotebookRead-shaped reads}; **everything else gates**, including Write,
   Edit, MultiEdit, NotebookEdit, Bash, **WebFetch**, **`mcp__*`**, and any **unknown** tool. (`AskUserQuestion`
   / `ExitPlanMode` are NOT classified here — they remain on P1's answer-hold path.)
2. **Approval gate at the engine seam.** Replace `engine.on_tool_request`'s ordinary-tool auto-allow with:
   classify → **SAFE** (or granted / `/yolo`) ⇒ allow; **RISKY** ⇒ surface a **permission prompt** and
   **hold** the request (reusing P1's `PendingRegistry` — inject a `PermissionEvent`, await the operator's
   verdict). Map the verdict via the existing `decision_to_substrate(PermissionVerdict(...))`.
3. **Approval UX + grant state.** Render the permission prompt as a message + **[Allow once] /
   [Allow for session] / [Deny]** inline keyboard (new callback `kind`); a tap routes (SB1-checked) to the
   held request. **[Allow for session]** records an in-memory per-tool-name grant (D4/D7); **[Deny]** sends
   the canned denial (D5). Subsequent SAFE/granted/yolo tools auto-allow with no prompt.
4. **`/yolo` + `/unyolo` (SB5).** Per-session, off by default; `/yolo` auto-allows all gated tools; loud
   `⚠️` banner on enable + a persistent on-indicator on each auto-allowed action; `/unyolo` reverts (D6).
5. **Backstop + cancel for permission prompts (RB4).** A permission prompt is a pending request, so it
   inherits P1's per-request **60-min backstop** (auto-deny + notify, session usable) and **`/cancel`**
   (clean abort) **for free** — P2 wires the new `kind` through, it does not rebuild the hold.
6. **SB5 default-bypass removal.** The streaming engine introduces **no bypass** (it already uses
   `permission_mode="default"` + the callback; P2 makes that callback actually gate). `/yolo` is the only,
   loud, per-session bypass. One-shot's legacy flag is untouched + documented (D3).

### Non-functional

- **SB5 — bypass explicit + visible** (first satisfied here): no default bypass on the streaming path;
  `/yolo` off by default, per-session, loud when on; one-shot's `--dangerously-skip-permissions` is the
  documented legacy exception, retired later.
- **RB4 — cancel + backstop don't wedge** (first satisfied here): `/cancel` aborts a waiting permission
  prompt; the 60-min backstop auto-denies + notifies and leaves the session usable. **Dedicated tests (RB7).**
- **SB1 — button-callback authn**: the new permission-approval buttons are new decision surface; an
  unauthorized/forged tap can **never** allow a tool. Reuses P1's `on_callback` allowlist recheck +
  defensive `decode_callback`; extended with the `permission` kind. **Dedicated test.**
- **SB6 — fail closed**: unknown tools gate (D1); a malformed/again-stale permission callback resolves
  nothing (RB1).
- **SB3 — secret hygiene**: the permission prompt renders a **body-free tool-input summary** (lengths,
  not contents); nothing sensitive logged.
- **RB1/RB2** preserved (bad input never crashes; engine/substrate errors fail clean).
- **Scale/latency:** single operator, single session; the gate adds a human-in-the-loop pause (bounded by
  the backstop). No RPS/availability targets.

### Future (P4/P5 fit — does today's design survive it?)

The classifier + grant state live behind the engine seam, keyed by `tool_use_id` (+ the session id every
event already carries from P1). **P4** (multi-project) moves grant/yolo state into the per-project session
record (the in-memory map becomes per-session — already keyed that way); **P5** (concurrency) reuses the
same per-request correlation. **P6** (security audit) consolidates SB5. No rewrite required — P2 builds the
**reusable pending-prompt/button layer** the roadmap calls for.

---

## Architecture (delta from P1)

**P1 today:** `engine.on_tool_request` routes `AskUserQuestion`/`ExitPlanMode` → the async answer-hold
(`pending.py`), and **ordinary tools → auto-allow**. `decision_to_substrate(PermissionVerdict(behavior=
"allow"|"deny", …))` already maps a verdict to the substrate. The bot's `on_callback` SB1-checks taps and
routes them via `stream_session.resolve_callback`; `render.py` encodes ask/plan callbacks.

**P2 delta:**

```
ordinary tool → on_tool_request
                   │  classify(tool_name, tool_input)         ← NEW permissions.py (pure, fail-closed)
                   ├─ SAFE  ───────────────────────────────▶ allow (no prompt)
                   ├─ granted-this-session (per tool name) ─▶ allow (no prompt)        ← grant state (D4/D7)
                   ├─ /yolo on ────────────────────────────▶ allow + ⚠️ loud indicator  ← D6
                   └─ RISKY ─▶ inject PermissionEvent + HOLD (reuse pending.py) ─▶ await verdict
                                   operator taps (SB1) → [Allow once]/[Allow session]/[Deny]
                                   │ allow once     → PermissionVerdict(allow)
                                   │ allow session  → record grant(tool_name) + PermissionVerdict(allow)
                                   │ deny            → PermissionVerdict(deny, message="…denied…")
                                   backstop(60m) → auto-deny + notify ;  /cancel → clean deny   (RB4, P1)
```

- **`claude_tg/permissions.py`** (new) — the **risk classifier** (pure `classify()`; the `SAFE_AUTO_RUN`
  set + fail-closed default, D1/D2) and the **per-session permission policy/state**: in-memory
  allow-session grants (set of tool names, D4) + `/yolo` flag (D6), with `clear()` for `/reset`/new-session
  (D7). Pure-ish + unit-testable in isolation.
- **`claude_tg/engine/engine.py`** — `on_tool_request` ordinary-tool branch: consult the policy → allow /
  hold-for-approval. New **`PermissionEvent`** injected like ask/plan; the held verdict is one of
  {allow_once, allow_session, deny} → mapped to `PermissionVerdict` (allow-session also records the grant).
  The ask/plan branch is **unchanged**.
- **`claude_tg/engine/types.py`** — add `PermissionEvent` (events-out: `tool_name`, `tool_input_summary`,
  `tool_use_id`, `session_id`, `risk_reason`) and the operator verdict shape (allow_once / allow_session /
  deny — engine-side state over the per-request `PermissionVerdict` allow primitive, per ADR-001).
- **`claude_tg/render.py`** — render `PermissionEvent` → message + **[Allow once] / [Allow for session] /
  [Deny]** keyboard; extend the `callback_data` codec with a **`permission`** kind (payload: once/session/
  deny; index/`tool_use_id` correlation as today, ≤64 B, defensive decode). The `/yolo` loud indicator
  rendering.
- **`claude_tg/stream_session.py`** — route permission callback taps (after the bot's SB1 recheck):
  resolve the held verdict, record an allow-session grant on [Allow for session]; hold `/yolo` state +
  the loud indicator; **clear grants + yolo on `/reset` / fresh session** (D7). Permission prompts reuse the
  existing hold so `/cancel` + backstop already apply.
- **`claude_tg/bot.py`** — `/yolo` + `/unyolo` commands (allowlisted); the persistent `⚠️` on-indicator on
  outbound messages while yolo is on; the permission callback rides the **existing SB1-checked**
  `on_callback` path (no new bypass of the auth gate).
- **`claude_tg/config.py`** — document/representation of the safe-allowlist default; the SB5 posture
  (streaming = no bypass; one-shot legacy flag unchanged). Optional `ALLOWED_TOOLS`/classification override
  is a deferred nicety, not P2-required.

**Data model (delta).** Per-chat streaming state (in `stream_session`/`permissions`) gains:
`{ allow_session_tools: set[str], yolo: bool }` — **in-memory only**, cleared on `/reset` / new session /
restart (D7). Forward-compatible toward the P4 per-project session record.

**Auth model (unchanged + extended).** Secret token + chat-id allowlist; **permission-approval taps are
allowlist-checked** on the same hardened P1 `on_callback` path (SB1). The trust model shifts from
"bypass + trust" to **"approval gates + path policy"** (the design's intent). `/yolo` is the one
deliberate, loud, per-session risk re-introduction.

---

## Risks & open questions

**Top risks**
1. *(Security — the whole point)* **A gate that's easy to bypass-by-habit.** If every action prompts,
   the operator reflexively taps [Allow] / lives in `/yolo`, and the gate is theater. *Mitigation:* the
   safe-allowlist (D1/D2) keeps reads/search frictionless so prompts are rare and meaningful;
   allow-session (D4) cuts repeat asks; `/yolo` is loud + per-session + cleared on restart (D6/D7) so it
   can't silently become the norm.
2. *(Technical)* **Post-approval / plan-approval does NOT greenlight arbitrary execution** (ADR-001
   caveat). Approving a plan (P1) or one tool must not let later risky tools run unprompted. *Mitigation:*
   **every** risky tool call hits the gate independently; approving a plan changes nothing about per-tool
   gating. Explicit test.
3. *(Product)* **Classifier wrong-way error.** A mis-classified risky tool auto-running (fail-open) is a
   security hole; a mis-classified safe tool gating is mere friction. *Mitigation:* **fail closed** — the
   allowlist is small + explicit, unknown gates (D1); friction is the safe error.
4. *(Operational)* **Don't break the live one-shot bot.** D3 leaves one-shot untouched; all gating is on
   the streaming path. The `ENGINE_MODE` default flip is the owner's call after phone-verify.

**Open questions (resolve during build, not before)**
- Exact membership of `SAFE_AUTO_RUN` at the margins (e.g. `NotebookRead`, `BashOutput`, `TodoWrite`) —
  settle in build against the live tool list; default any uncertain tool to **gate** (fail-closed).
- The persistent `/yolo` indicator's exact placement (per-message prefix vs status line) — tune in build.
- Whether `/yolo` should also auto-answer P1 ask/plan prompts, or only the new permission gate — default:
  **only permission gates** (ask/plan still need a real answer; yolo is about *risk*, not *questions*).

**ADRs to write before code**
- **ADR-003 — Permission-gating model**: the risk taxonomy (safe-allowlist + fail-closed), allow-once vs
  allow-session (engine-side state over the per-request allow primitive), `/yolo` semantics + loudness,
  canned-deny, and the reuse of P1's hold/backstop/cancel for permission prompts. Grounded in D1–D7 +
  ADR-001/002.

---

## SDLC plan (delta)

No SDLC change from P1 — same GitHub Actions CI (tests + lint + type-check + secret-scan), same lenient
ruff/mypy baseline, same **substrate-mocked** unit tests + a **live verify** probe (not in CI). The 247
P1 tests are the regression floor; P2 adds Security (SB5/SB1) + Reliability (RB4) + classifier/UX coverage.
Branch `feat/permission-gating`; supervised-autonomous per-task build (isolated Implementer + independent
reviewer, auto-commit on green + AGREE), same as P1.

---

## Roadmap

**In scope (P2 / this pipeline):** ADR-003 → risk classifier (`permissions.py`, fail-closed safe-allowlist,
D1/D2) → engine approval gate (hold risky tools, reuse `pending.py`; `PermissionEvent` + allow-once/session/
deny verdict) → render + `permission` callback kind ([Allow once]/[Allow session]/[Deny]) → bot wiring
(`/yolo`+`/unyolo`, loud indicator, grant state cleared on reset, SB1 on taps) → SB5 default-bypass removal
on the streaming path (one-shot legacy untouched) → SB/RB test matrix (SB5, SB1-callback, RB4, classifier,
allow-once-vs-session, deny-relay, post-approval-still-gated; RB7) → live verify + owner phone-verify checklist.

**Out of scope (deferred):** `[Deny + reason]` (later); per-input/per-resource allow-session granularity
(later); Bash command-aware classification (later); multi-project + persisted grants + restart recovery
(**P4**); concurrency (**P5**); the full security audit/threat-model consolidation (**P6**); the actual
`ENGINE_MODE=streaming` default flip + one-shot retirement (owner's call / later cleanup).

**Expected build order (input to `/plan`):**
1. **ADR-003** — permission-gating model (from D1–D7 + ADR-001/002).
2. **Risk classifier** `permissions.py` — pure `classify()` (safe-allowlist, fail-closed, WebFetch/MCP gate,
   WebSearch allow) + per-session grant/yolo state + `clear()`. Unit-tested (incl. unknown → gate).
3. **Engine approval gate** — `on_tool_request` ordinary branch → policy (allow / hold-for-approval);
   `PermissionEvent` + allow-once/session/deny verdict mapping; reuse `pending.py` (backstop/cancel free).
   Ask/plan unchanged. Unit (mock substrate).
4. **Render + callback codec** — `PermissionEvent` → message + 3-button keyboard; `permission` callback
   kind (≤64 B, defensive decode); `/yolo` loud indicator. Unit.
5. **Bot + session wiring** — route permission taps (SB1), record allow-session grant, `/yolo`+`/unyolo`,
   persistent indicator, clear grants on `/reset`/new session. Unit.
6. **SB5 posture** — streaming introduces no bypass; `/yolo` the only loud per-session bypass; one-shot
   legacy flag documented/untouched; (optional) config representation. Unit + docs.
7. **SB/RB test matrix (RB7 for P2)** — SB5 (no default bypass + loud yolo), SB1 (forged permission tap
   can't allow), RB4 (cancel + backstop on a permission prompt don't wedge), classifier matrix (safe/risky/
   unknown/WebFetch/MCP/WebSearch), allow-once-vs-session, canned-deny relay, **post-approval still gated**.
8. **Live verify + owner checklist** — programmatic (real Claude: a risky tool pauses → allow/deny honored,
   allow-session suppresses, `/yolo`, `/cancel`, backstop), contained + scrubbed; `verify.md` phone-checklist.
