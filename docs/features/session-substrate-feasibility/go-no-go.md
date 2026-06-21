# Go / No-Go — P0 Session Substrate Feasibility (GATE 1)

> **Owner-facing recommendation.** Synthesis of the recorded C1–C6 evidence matrix
> (`spikes/session-substrate/evidence/matrix.{md,json}`) and the per-criterion evidence. Companion to
> [ADR-001](../../adr/ADR-001-session-substrate.md) (status **Proposed** — you decide at **G-ADR**).
> Decided from the evidence; no substrate was pre-selected.

---

## Recommendation: **GO**

GATE 1 is **GREEN**. The product's headline assumption — that code can drive an *interactive* Claude
Code session and answer its interactive prompts with **no TTY** — is **proven**, on **both**
candidate substrates. The "constrained-mode / re-plan" risk (the design's top risk) is **retired**.

**Both make-or-break criteria PASS on both substrates:**
- **C3 — AskUserQuestion:** answered programmatically (native `answers` map) on A **and** B.
- **C4 — ExitPlanMode:** approve / reject-with-feedback honored on A **and** B.

Full matrix — **no FAIL, no PARTIAL**:

| | C1 stream | C2 permission | C3 ask ⭐ | C4 plan ⭐ | C5 skill | C6 resume |
|---|---|---|---|---|---|---|
| **A — Agent SDK** | PASS | PASS | PASS (native) | PASS | PASS | PASS |
| **B — raw CLI stream-json** | N/A¹ | PASS | PASS (native) | PASS | N/A¹ | N/A¹ |

¹ Per design S1, B is exercised on the make-or-break/backbone C2/C3/C4; on C1/C5/C6 (A passed,
non-make-or-break) B is intentionally not run.

Environment of record: `claude-agent-sdk==0.2.105`; `claude` CLI 2.1.183/2.1.185; Python 3.14.5;
host CLI auth, **no API key**.

---

## Substrate recommendation

- **Primary: Substrate A — `claude-agent-sdk==0.2.105`.** Proven on all of C1–C6; the SDK manages
  transport, the `can_use_tool` callback, handshake, and session bookkeeping (least to maintain).
- **Fallback: Substrate B — raw `claude` CLI `stream-json` control protocol.** Proven independently
  (no SDK import) on the make-or-break C2/C3/C4; a real escape hatch if the SDK is unavailable or
  regresses.
- **No hybrid (Option C).** A covers the make-or-break C3/C4 natively with no help from B or hooks,
  so the hybrid trigger never fires.

C2/C3/C4 are **substrate-neutral** (identical mechanism on both), so the A-over-B call is **not** a
capability call. The **true differentiator is maintainability / managed transport** (the SDK owns
transport, handshake, framing, timeouts vs. B's hand-rolled equivalents + its undocumented flag);
broader coverage is **supporting** — B's C1/C5/C6 are untested-**by-design** (per S1, because A
passed), not failed, so the breadth gap is demonstrated-vs-expected, not a capability gap. A
**B-primary choice would be defensible** on this evidence; A primary stands on the maintenance
argument.

---

## Top risks + required workarounds (carry into P1)

1. **B couples to an undocumented flag.** B's permission channel only activates with
   `--permission-prompt-tool stdio`, which is **absent from `claude --help`**. *Workaround:* if B is
   ever used, pin + monitor the CLI version; treat a missing/changed flag as fail-clean.
2. **No native plan-feedback field (C4).** Reject feedback rides the **deny `message`** channel;
   whether it lands in the structured plan is model-variant, and honoring it is
   model-interpretation-dependent on **both** substrates. *Workaround:* assert on the model's
   revision reply, not a structured field. Post-approval execution was contained/untested, so P1
   **MUST** ensure post-approval tool calls remain gated by the C2 permission path (a P1 requirement,
   not a proven behavior).
3. **Resume is cwd/project-scoped (C6).** A session id is **not** a global handle — resume fails from
   a different cwd. *Workaround:* persist `(session_id, cwd)` and resume only from the original cwd;
   **guard double-attach** at the engine level (the substrate does not).
4. **Allow must carry input (C2).** On B an ALLOW must include `updatedInput` (ZodError otherwise).
   *Workaround:* default it to the original tool input on allow (the SDK does this for you).
5. **Containment was policy-level, not an OS sandbox.** `cwd` is never a security boundary.
   *Workaround:* P1 implements real path confinement (SB2).

---

## Not-tested — P1 MUST validate before relying on it

- AskUserQuestion **free-text "Other"** (`response` field), `annotations`, and **multi-question**
  asks (only single-question 2–3-option probes were run).
- **Post-approval arbitrary / out-of-fixture execution** after a plan is approved. (In the C4
  approve trial one ALLOWED in-fixture Write — `greet.py` — did execute post-approval; what is
  untested is post-approval *arbitrary / out-of-fixture* execution, which was otherwise contained.)
  P1 **MUST** keep post-approval tool calls gated by the C2 permission path.
- Resume edge cases: **crash-mid-turn** (torn transcript → must fail clean, RB3), **concurrent /
  double-attach**, **aged** sessions, **CLI/SDK-upgrade** resume.
- Substrate **B on C1/C5/C6** (untested per S1).
- Operator **mid-turn cancel** (RB4 — cancel is proven only as full-disconnect via `stop()`, not as
  an operator-initiated mid-turn cancel), dedicated **rate-limit handling** (RB5 — a
  `rate_limit_event` frame *was* observed once in T13, so the `status` carrier exists, but handling on
  it is unproven), **allow-session** scope.
- Streaming is **coarse-grained (≈3–4 deltas/turn), not token-level** — design any live rendering for
  chunked updates, not per-token.

---

## Recommended P1 starting point

1. **Accept ADR-001 at G-ADR** (it is **Proposed**; nothing below P0 is scoped until you accept).
2. **Build P1's streaming engine on Substrate A** behind the **normalized engine interface**
   (`spikes/session-substrate/normalized_interface.md`): lifecycle `start` / `resume` / `send` /
   `stop`; events out (`text`, `tool_use`, `ask`, `plan`, `error`, `result`, `status`); decisions in
   (permission verdict, native question answer, plan verdict, free-text reply, cancel). Keep B's wire
   contract documented as the fallback.
3. **Bake in the workarounds from day one:** native `answers`-map for C3 (deny-message fallback),
   deny-message feedback for C4 with post-approval still permission-gated, `(session_id, cwd)`
   persistence + double-attach guard for C6, `updatedInput`-on-allow for C2.
4. **Stand up the cross-cutting baseline (P1 owns it):** SB1 authenticated decision-in (every
   permission verdict / question answer / plan verdict / cancel / free-text reply is
   operator-authenticated before it reaches the engine; button-callback taps are allowlist-checked —
   a new attack surface; an unauthenticated callback must never approve a plan or allow a tool),
   SB2 path confinement, SB3 secret hygiene, SB5 bypass OFF-by-default + LOUD-when-on
   (`--dangerously-skip-permissions` removed from the default path — the spike used no bypass),
   SB6 fail-CLOSED defaults (spike confirmed default-mode auto-DENY, incl. substrate B without
   `--permission-prompt-tool stdio`; a missing/changed B flag must fail closed, never auto-allow) +
   documented blast radius, RB1/RB2 clean-failure, RB5 rate-limit safety, and GitHub Actions CI
   (tests + lint + type-check + security scan) — each RB item gets a dedicated test (RB7).
5. **Migrate off the one-shot runner:** replace `claude_tg/bot.py → claude_tg/claude_runner.py`
   one-shot calls with the persistent streaming engine. **Harvest, don't rebuild:** the existing
   `claude_runner.py` already has `(session_id, cwd)` persistence, `--resume`, a resume-failure
   recovery heuristic, and a per-chat in-flight lock. The real shift is introducing a stateful,
   long-lived bidirectional connection (a new idle/wedge/reconnect/double-attach failure class), not
   just swapping a runner. Rewrite/replace obsolete one-shot tests with streaming + interaction +
   SB/RB coverage (per the Test policy — count is not a gate).
6. **Treat `spikes/session-substrate/` as reference only** — never imported by production; removable
   only via a separately reviewed task once equivalent integration evidence exists (design S3).
