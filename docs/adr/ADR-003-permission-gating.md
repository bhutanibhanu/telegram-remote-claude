# ADR-003 — Per-tool permission gating model

> Derived from the P2 design ([`docs/features/permission-gating/design.md`](../features/permission-gating/design.md))
> and the G-Scope owner decisions **D1–D7**. Builds on **ADR-001** (the C2 per-tool permission primitive:
> allow / deny + the "allow-session vs allow-once is engine-side state" + "post-approval execution stays
> gated" findings) and **ADR-002** (the async answer-hold this model **reuses** for permission prompts).
> Sibling of [ADR-001](ADR-001-session-substrate.md) / [ADR-002](ADR-002-async-answer-hold.md).

- **Status:** **Proposed** — chosen model for P2; owner reviews with the P2 branch.
- **Date:** 2026-06-21
- **Deciders:** repo owner
- **Related:** ADR-001 (substrate + C2 primitive); ADR-002 (async hold/backstop/cancel);
  `docs/features/permission-gating/design.md`; `docs/cross-cutting-requirements.md` (SB5/RB4/SB1/SB6/SB3).

---

## Context

P1 shipped the streaming engine with an **interim permission posture** (design S3): ordinary tools
(Write/Edit/Bash/…) are **auto-allowed** inside the single allowlisted chat at
`engine.on_tool_request`, and the legacy one-shot runner uses `--dangerously-skip-permissions`. That is
not safe for real risky work. P2 turns the **C2 per-tool permission primitive** — proven programmatically
in P0 (ADR-001), held open across an async human delay in P1/T5, and verified live end-to-end in P1/T9 —
into an **actual approval gate**, and removes the bypass from the go-forward (streaming) path.

The question this ADR settles: **what is the gating model** — which tools gate, what the approval verdicts
mean, how the escape hatch (`/yolo`) behaves, how denial is conveyed, and how the gate **reuses** P1's hold
machinery rather than rebuilding it — grounded in the owner's G-Scope decisions.

## Decision drivers

- **Cross-cutting (first satisfied at P2):** **SB5** (bypass explicit + visible), **RB4** (cancel + 60-min
  backstop don't wedge), **SB1** (button-callback authn on the new approval taps), **SB6** (fail closed),
  **SB3** (no sensitive tool bodies in the prompt/logs).
- **ADR-001:** allow/deny is the **per-request** primitive (`PermissionResultAllow(updated_input=…)` /
  `PermissionResultDeny(message=…)`); **allow-session vs allow-once is engine-side state** over that
  primitive; **approving a plan does NOT greenlight arbitrary execution** — post-approval tool calls stay
  permission-gated (a requirement P2 owns).
- **ADR-002:** the async answer-hold = a `PendingDecision` Future per request + a per-request backstop timer
  + `/cancel`. P2 **reuses** this for permission prompts (the same `PendingRegistry`), it does not rebuild it.
- **Owner G-Scope decisions D1–D7** (design.md).

## Decision

**Implement permission gating as a fail-closed risk classifier + an engine-side approval gate that reuses
P1's async hold. Risky tools surface an `[Allow once] / [Allow for session] / [Deny]` prompt held open via
the existing `PendingRegistry`; safe tools run free; `/yolo` is the one loud, per-session, off-by-default
bypass.**

1. **Risk taxonomy — fail-closed safe-allowlist (D1/D2).** A pure `classify(tool_name, tool_input)` returns
   SAFE only for an explicit allowlist — **Read, Glob, Grep, LS, TodoWrite, WebSearch** (local reads +
   search) — and **RISKY for everything else**: Write, Edit, MultiEdit, NotebookEdit, Bash, **WebFetch**
   (arbitrary-URL egress), **any `mcp__*`**, and **any unknown/new tool**. Unknown gates by default
   (fail-closed, SB6). `AskUserQuestion` / `ExitPlanMode` are **not** classified here — they remain on the
   P1 answer-hold path (they are answered, not permission-gated).
2. **Approval verdicts.** The operator taps one of three:
   - **[Allow once]** → `PermissionResultAllow` for this request only (the next use re-asks).
   - **[Allow for session]** → record an **engine-side per-tool-NAME grant** (D4) + allow; subsequent uses of
     that tool name this session auto-allow with no prompt.
   - **[Deny]** → `PermissionResultDeny(message=<canned>)` — a fixed "operator denied this tool call"
     message the model adapts to (**D5**; no free-text reason in P2).
3. **`/yolo` (D6).** A per-session **allow-all** flag, **off by default**, toggled by `/yolo` (`/unyolo`
   reverts). When on, every gated tool auto-allows; it is **loud** — a `⚠️` banner on enable **and** a
   persistent on-indicator on each auto-allowed action so it is never silently on.
4. **Hold / backstop / cancel — reuse P1 (RB4).** A permission prompt is registered as a held pending
   request in the **existing** `PendingRegistry`: the operator's tap resolves it; the per-request **60-min
   backstop** auto-resolves to **DENY + notify** and leaves the session usable; **`/cancel`** clean-aborts.
   No new hold mechanism is built.
5. **State lifetime (D7).** Allow-session grants + the `/yolo` flag are **in-memory per session**, cleared on
   `/reset`, a fresh session, or a bot restart — a restart never silently resumes an allow-all / yolo posture.
6. **Bypass removal (SB5 / D3).** The streaming engine runs `permission_mode="default"` + the gating
   callback and introduces **no** `--dangerously-skip-permissions`; `/yolo` is the **only** (loud,
   per-session) bypass. The one-shot runner's `--dangerously-skip-permissions` is **unchanged** — it has no
   approval channel — and is documented as the **retiring legacy exception**; **no `ENGINE_MODE` default
   flip** happens in P2 (that is a later owner-gated cleanup).

## Consequences

**What P2 builds.** `claude_tg/permissions.py` (the pure classifier + per-session grant/yolo state); the
engine gate at `on_tool_request` (a `PermissionEvent` injected + held; allow-once/session/deny mapped through
the existing `decision_to_substrate(PermissionVerdict)`); the permission render + a `permission` callback
kind; bot/session wiring (`/yolo`/`/unyolo`, SB1-checked taps, grant clearing on reset); the SB/RB test
matrix; and a live verify probe + owner checklist.

**The ADR-001 caveat is binding.** Approving a plan (P1) or one tool grants **nothing** about other tools —
**every** risky tool call hits the gate independently. P2 carries an explicit test for this (a second risky
tool after one approval still prompts; a post-plan-approval risky tool still prompts).

**What P2 reuses (does not rebuild).** P1's `PendingRegistry` (hold + 60-min backstop + cancel),
`decision_to_substrate(PermissionVerdict)`, the SB1-hardened `bot.py` `on_callback`, and the `render.py`
callback codec. The interactive answer-hold for `AskUserQuestion`/`ExitPlanMode` is untouched.

**Deferred / not in P2.** `[Deny + reason]` free-text denial; per-input / per-resource allow-session
granularity (P2 is per tool name); Bash command-aware classification (whole-Bash gates); persisted grants +
multi-project (P4); concurrency (P5); the security-audit consolidation (P6); the actual
`ENGINE_MODE=streaming` default flip + one-shot retirement.

**Residual risk.** A gate that is reflexively approved, or a session left in `/yolo`, is security theater.
Mitigated by the **small** safe-allowlist (reads/search run free, so prompts are rare and meaningful),
per-tool-name allow-session (cuts repeat asks), and a **loud, restart-cleared** `/yolo` (D6/D7) so allow-all
cannot quietly become the norm. The classifier's wrong-way error is **fail-closed** (an unknown tool gates,
not runs), so the safe error is friction, not exposure.
