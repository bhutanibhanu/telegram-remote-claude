# Design: P1 — Interactive Streaming Session Engine

> **Feature slug:** `streaming-engine` · **Pipeline:** P1 (first build pipeline after the P0 gate).
> Builds on **ADR-001** (accepted-in-principle at G-ADR; status *Proposed* in-file pending the
> owner's formal merge): **Substrate A = `claude-agent-sdk==0.2.105` primary**, CLI `stream-json`
> a documented fallback, no hybrid. Parents:
> [`docs/interactive-remote-design.md`](../../interactive-remote-design.md) ·
> [`docs/cross-cutting-requirements.md`](../../cross-cutting-requirements.md) ·
> [`docs/adr/ADR-001-session-substrate.md`](../../adr/ADR-001-session-substrate.md) ·
> inherited contract: [`spikes/session-substrate/normalized_interface.md`](../../../spikes/session-substrate/normalized_interface.md).
>
> **The point of P1:** replace the one-shot `claude -p` runner with a **persistent, bidirectional
> streaming session** the operator can drive live from Telegram — stream Claude's activity out,
> send messages back in, and **answer Claude's interactive tools (AskUserQuestion, ExitPlanMode)**
> so `/grill` and `/pipeline` can be run end-to-end from the phone. **Per-tool permission approval
> gating (allow/deny, bypass-removal, backstop) is deliberately P2** (per the parent roadmap).

---

## Scoping decisions (confirmed at G-Scope)

These shape P1's effort and deliverables. Confirmed with the owner before planning.

| # | Decision | Value |
|---|---|---|
| S1 | **Substrate** | **A only** (`claude-agent-sdk`). Build against the inherited normalized interface (which already abstracts A/B); implement only the **A adapter**. B is a **documented adapter slot**, not built (YAGNI — B is proven as fallback; add if A regresses). |
| S2 | **De-risk first** | **Task 1 is a throwaway spike** proving the SDK `can_use_tool` callback (and the interactive-tool answer round-trip) **survives a multi-minute human delay** (button tap) up to the ~60-min backstop without timing out or wedging the session. **PASS → build the engine on it. FAIL/PARTIAL → redesign the answer flow before any engine code.** |
| S3 | **P1 ↔ P2 boundary** | **Follow the parent roadmap.** P1 = engine + live rendering + **answering interactive TOOLS** (AskUserQuestion / ExitPlanMode) + CI + SB/RB baseline + minimal `/cd` path policy. **Per-tool PERMISSION approval gating (allow/deny/allow-session before risky tools, removing the default bypass, risk classification, button-callback authenticity, 60-min backstop) is P2.** Interim P1 tool posture: risky tools run in the substrate's **default permission mode** inside the single allowlisted chat (no new bypass introduced); gating hardens in P2. |
| S4 | **Migration** | **Parallel behind a flag.** New engine ships alongside `claude_tg/claude_runner.py`, gated by **`ENGINE_MODE` (`oneshot` default → `streaming`)**. The running one-shot bot — the owner's **only remote access** — keeps working until streaming is proven; flip the flag to cut over; retire one-shot in a later cleanup. **Harvest** the existing `(session_id, cwd)` persistence, `--resume`, resume-failure heuristic, and per-chat in-flight lock — do not rebuild. |
| S5 | **Single session** | One active session per chat (the current model). **Multi-project (P4), background concurrency (P5), and crash-mid-turn restart recovery (P4)** are explicitly OUT of P1. |

---

## The basics

**Elevator pitch.** Turn the Telegram bot from a one-shot `claude -p` relay into a **live,
interactive Claude Code session you drive from your phone** — watch the work stream in, reply
mid-run, and answer the questions and plan-approvals that `/grill` and `/pipeline` raise.

**The actual problem.** The current `claude_tg/claude_runner.py` is **stateless per turn**
(`claude -p --output-format json` per message): it cannot stream activity, cannot be messaged
mid-run, and cannot answer Claude's interactive tools (AskUserQuestion / ExitPlanMode). So the
headline workflows (`/grill`, `/pipeline`) — which *depend* on asking the operator questions and
getting plan approvals — cannot run from the phone at all. P0 proved (ADR-001, GATE 1 GREEN) that
a persistent session **can** stream and **can** answer those tools programmatically with no TTY.
P1 builds the production engine that does it.

**Who it's for.** The **repo owner** — a single allowlisted operator running this bot as their
remote control for Claude Code on their dev host. Not multi-user (one operator per deployment).

**Definition of success (concrete).**
- From Telegram, the operator can start a session, **see Claude's output stream live** (coalesced,
  not flooded), and **send a follow-up message mid-session** that the same session receives.
- Running **`/grill` (or any AskUserQuestion-emitting flow) from the phone works end-to-end**: the
  question renders as option buttons, the operator taps one (or replies "Other"), and the session
  **continues on that answer**.
- Running a **plan-producing flow works**: an ExitPlanMode plan renders with **[Approve] /
  [Reject + feedback]**; the chosen verdict is honored (approve → proceeds; reject → revises on the
  typed feedback).
- The above survives a realistic **multi-minute delay** between prompt and the operator's tap
  (de-risk spike PASS), with a **60-min backstop** that fails clean (auto-resolves + notifies) and
  leaves the session usable (RB4 shape; full backstop UX lands with P2 gating, but the answer-hold
  mechanism is proven here).
- **`ENGINE_MODE=oneshot` remains the default and the existing behavior is unchanged**; flipping to
  `streaming` exercises the new engine. Existing user-facing guarantees (allowlist enforcement,
  message chunking, never-crash-on-bad-input, current commands) hold in both modes.
- **CI is green** on every push (tests + lint + type-check + secret scan), gating merges.

**Anti-goals (explicit non-features).**
- **No per-tool permission approval system** (allow/deny/allow-session buttons before risky tools,
  default-bypass removal, risk classification, button-callback authenticity, the full 60-min
  backstop UX) — that is **P2**. P1 introduces **no new bypass** and runs tools in the substrate's
  default mode within the allowlisted chat.
- **No multi-project / multiple named sessions** (P4). One active session per chat.
- **No background concurrency / parallel runs** (P5).
- **No crash-mid-turn restart recovery** (P4) — a clean restart reload may be partial; an in-flight
  turn that dies fails clean rather than silently continuing, but full RB3 recovery is P4.
- **Not the Substrate-B adapter** — slot only (S1).
- **Not token-level streaming** — P0 showed substrate streaming is coarse-grained (≈3–4 deltas/turn).

**Constraints.**
- Builds on **`claude-agent-sdk==0.2.105`**, host `claude` CLI ≥ 2.1.185, **host CLI auth — no API
  key** (ADR-001 environment of record). Python supports up to 3.14.
- The **running bot is the owner's only remote access** — the migration must never break the
  working one-shot path (hence S4's flag).
- macOS dev host of record; avoid macOS-only assumptions in anything that informs later pipelines
  (e.g. the cwd→project-dir path sanitization for resume).
- Cross-cutting **SB (security) + RB (reliability) baselines apply from P1** (see Requirements).

---

## Requirements

### Functional — ranked (top capabilities P1 must deliver)

1. **Persistent streaming session engine.** Long-lived `start` / `resume` / `send` / `stop`
   lifecycle over Substrate A; multi-turn; emits the normalized **events out** stream
   (`text`, `tool_use`, `ask`, `plan`, `error`, `result`, `status`) and accepts **decisions in**
   (question answer, plan verdict, free-text reply, cancel — *permission verdict wired but gating
   deferred to P2*). Implements the inherited `normalized_interface.md` contract for real.
2. **Interactive-tool relay.** `ask` (AskUserQuestion) → message + **one inline button per option**
   (+ "Other" → free-text reply); `plan` (ExitPlanMode) → plan text (chunked) +
   **[Approve] / [Reject + feedback]**. Answered via the **native `answers`-map on the allow
   channel** (C3) / allow-or-deny+message (C4) proven in P0. **This is the headline — `/grill` &
   `/pipeline` must run from the phone.**
3. **Async answer-hold (the de-risked core).** Hold the session's pending interactive request open
   while the operator decides asynchronously (button tap), resolving the answer back into the live
   session; a **60-min backstop** auto-resolves + notifies and leaves the session usable; `/cancel`
   aborts a waiting run cleanly (RB4).
4. **Live workflow rendering.** Normalized events → Telegram: **verbatim** for meaningful output
   (questions, plans, errors, final results), **one-liner / status** for noise; **coalesce +
   throttle** updates (prefer editing a status message over flooding) to respect Telegram's
   ~1 msg/s/chat send limit (RB5).
5. **Flagged migration + lifecycle plumbing.** `ENGINE_MODE` switch; harvest `(session_id, cwd)`
   persistence + `--resume` + the per-chat in-flight lock from `claude_runner.py`; existing commands
   (`/reset` `/cd` `/pwd` `/help`) keep working; add `/cancel`.

### Non-functional

- **Security (SB — applies from P1).**
  - **SB1** — allowlist authn on every inbound, **including button-callback taps** (new surface
    introduced by interactive buttons): a non-allowlisted chat/callback is silently ignored and can
    **never** answer a question or approve a plan.
  - **SB2** — `/cd` path confinement: canonicalize with symlinks resolved, enforce containment in
    `ALLOWED_ROOTS`; `ALLOW_ANY_PATH=true` is the explicit opt-out. (Extends to `/new` in P4.)
  - **SB3** — secret hygiene: bot token never logged, sensitive tool output kept out of logs,
    `.env` git-ignored, state files `0600`. (P0's scrubber discipline carries into prod logging.)
  - **SB4** — no injection: never build shell/args from message text.
  - **SB6** — safe defaults / fail-closed; document the trust model + blast radius. (Note: P1
    introduces **no new bypass**; SB5 default-bypass-removal is P2.)
- **Reliability (RB — applies from P1).**
  - **RB1** never crash on bad input (preserve the invariant). **RB2** clean failure on
    engine/substrate errors + timeouts — a clear message, never a silent hang or stack trace (the
    `error` event contract; both harnesses already fail clean). **RB5** rate-limit safety —
    throttle/coalesce holds under streaming bursts. **RB7** each of RB1/RB2/RB5 has a dedicated test.
  - *Deferred:* **RB3** restart/resume-after-crash and **RB4** full cancel+backstop semantics are
    P2/P4; P1 proves the answer-hold + `/cancel` + 60-min backstop *mechanism* but not crash recovery.
- **Scale / latency.** Single operator, single active session; no RPS/availability targets.
  Streaming is coarse-grained; rendering is the latency-sensitive path (throttling, RB5).
- **Explicitly N/A:** multi-tenant scale, i18n, accessibility, persistence-integrity-under-concurrency
  (P4/RB6), availability SLAs.

### Future (6–12 mo — does today's design survive it?)

The engine sits **behind the normalized interface**, so P2 (permission gating) layers a decision
filter on the existing `permission verdict` decision-in; P4 (multi-project) extends the session
registry + events/decisions with a **session/run correlation envelope** (flagged in ADR-001 as the
P0 contract's known gap); P5 (concurrency) reuses the same envelope. **Design the engine
single-session but make the event/decision types carry a session id from day one** so P4/P5 extend
rather than rewrite.

---

## Architecture (delta from current)

**Current:** `claude_tg/bot.py` → `claude_tg/claude_runner.py` (one-shot `claude -p`,
per-chat `--resume`, per-chat `asyncio.Lock`, `(session_id, cwd)` in `session_store.py`).

**P1 delta** — add a streaming engine behind the normalized interface, selected by `ENGINE_MODE`:

```
Telegram ──messages / button taps──▶ claude_tg/bot.py (transport, commands, callbacks, allowlist)
   │                                        │  ENGINE_MODE: oneshot → claude_runner.py (unchanged)
   │                                        │              streaming → engine/ (new)
   ▼                                        ▼
 render layer  ◀── normalized events ──  engine/  (SDK ClaudeSDKClient: start/resume/send/stop;
 (render.py)      decisions in ──────▶            can_use_tool callback; events out; answer-hold)
```

- **`engine/`** (new) — the session engine over Substrate A. Implements `normalized_interface.md`:
  lifecycle (`start`/`resume`/`send`/`stop`), **events out** (normalize SDK
  `AssistantMessage`/`StreamEvent`/`ResultMessage`/tool blocks → `text`/`tool_use`/`ask`/`plan`/
  `error`/`result`/`status`), **decisions in** (question answer via the native `answers` map; plan
  verdict via allow / deny+message; free-text reply; cancel). A **`Substrate` adapter seam** with an
  **A adapter** built and a **B adapter slot** documented (S1). Carries a **session id on every
  event/decision** (future-proofing for P4/P5).
- **`render.py`** (new or extracted) — normalized event → Telegram message(s): verbatim vs
  one-liner; interactive prompt → inline keyboard (`ask` → option buttons + "Other"; `plan` →
  [Approve]/[Reject+feedback]); **coalescing + throttling** (edit-in-place status message; respect
  ~1 msg/s/chat).
- **`bot.py`** (delta) — `ENGINE_MODE` switch; **inline-keyboard callback handler** (SB1-checked);
  route a button tap / "Other" reply / plan verdict back into the engine's pending request; `/cancel`.
- **Harvested from `claude_runner.py`** — `(session_id, cwd)` persistence (`session_store.py`),
  `--resume` semantics, the resume-failure heuristic, and the **per-chat in-flight lock** (the
  single-active-session invariant). One-shot path stays intact under `ENGINE_MODE=oneshot`.

**Data model (delta).** Session record gains the streaming fields the engine needs:
`{chat_id, session_id, cwd, status (idle|running|awaiting_answer), pending_request (tool_use_id +
kind + options + timeout_at) | null, last_active}` — persisted `0600`, forward-compatible toward
the P4 multi-project registry.

**Auth model (unchanged + extended).** Secret token + chat-id allowlist is the trust boundary;
**button-callback taps are now allowlist-checked** (SB1). No new bypass; tools run default-mode.

---

## SDLC plan (delta — P1 stands up the project's CI + test baseline)

- **Repo layout.** New `engine/` package + `render.py`; `bot.py`/`session_store.py` evolve;
  `claude_runner.py` retained behind the flag. (Note: code currently lives under `claude_tg/`; the
  parent design's `engine/`/`render.py`/`permissions.py` names are adopted within that package.)
- **Branch/commit.** `feat/streaming-engine` (this worktree); supervised per-task commits via the
  pipeline build loop; nothing lands without a reviewed diff.
- **Testing strategy** (cross-cutting Test policy — the 53-test snapshot is a baseline, not a floor):
  - **Preserve** existing user-facing behavior with **equivalent-or-stronger** coverage (allowlist
    ignore, chunking, never-crash, command behavior).
  - **Add** coverage in: **Streaming** (event normalization, verbatim vs one-liner, throttle/
    coalesce), **Interaction** (AskUserQuestion answer routing, plan approve/reject+feedback,
    free-text reply, the async answer-hold + backstop + `/cancel`), **Security (SB1/SB2/SB3/SB4)**,
    **Reliability (RB1/RB2/RB5)**.
  - **Substrate mocked** in unit tests (no live Claude / network); the engine's substrate adapter
    is the mock seam. Live end-to-end is a manual/`/verify` check, not a CI gate.
- **CI/CD.** **GitHub Actions, stood up in P1** (decision-log #6): tests + lint + type-check +
  secret scan on every push/PR; branch protection requires green CI to merge. (Docs-baseline commit
  only after full-diff review.)
- **Observability.** Structured logs (secret-scrubbed, SB3); clear error surfacing (RB2). Metrics/
  tracing deferred.
- **Release.** Feature-flagged (`ENGINE_MODE`); cut over by flipping the flag once streaming is
  verified; retire one-shot in a later cleanup task.

---

## Risks & open questions

**Top risks**
1. *(Technical — #1, de-risked FIRST)* **Async human-in-the-loop answer latency.** P0 answered every
   interactive prompt **synchronously in-process**. Production must hold the session's pending
   `ask`/`plan` (and, in P2, permission) request open while the operator decides asynchronously —
   potentially minutes, up to the 60-min backstop. **Unproven that the SDK `can_use_tool` callback /
   the session tolerates a multi-minute await without timing out or wedging.** *Mitigation:* **Task 1
   is a throwaway spike** (hold a callback ~2 min and toward ~60 min, resolve allow/deny + a
   structured answer, confirm the session continues). PASS → engine. FAIL/PARTIAL → redesign the
   answer flow (e.g. a queued/re-prompt pattern) before engine code.
2. *(Technical — inherited from ADR-001)* **Shared CLI/SDK version coupling.** A and B share the
   same CLI control protocol; a `claude` CLI upgrade can break the engine. *Mitigation:* pin +
   monitor versions; the substrate adapter isolates the blast radius; fail clean on protocol drift.
3. *(Operational)* **Breaking the live bot during migration.** It is the owner's only remote access.
   *Mitigation:* S4's `ENGINE_MODE` flag (one-shot default); the streaming path is opt-in until
   verified; CI gates merges.
4. *(Product)* **Streaming is coarse-grained** (≈3–4 deltas/turn) and there's **no substrate
   throttling contract** — naive rendering floods Telegram or feels laggy. *Mitigation:* coalesce +
   edit-in-place status message; RB5 tests under burst.
5. *(Technical)* **C4 reject-feedback is model-interpretation-dependent** (no structured plan-feedback
   field; rides the deny message). *Mitigation:* surface the typed feedback verbatim; treat
   incorporation as best-effort; re-present the revised plan for another verdict.

**Open questions (resolve during plan/build, not before)**
- Exact rendering throttle params (edit interval, coalesce window) — tune against Telegram limits.
- Inline-keyboard layout details (option-button wrapping, "Other" affordance, plan chunking +
  feedback capture) — settle in build.
- Interim P1 tool posture specifics: confirm "default permission mode in the allowlisted chat" is
  acceptable until P2, and exactly which (if any) tools to hard-disallow in P1.
- `/cancel` + backstop interaction with the answer-hold (clean abort semantics).

**ADRs to write before code**
- **ADR-002 — Async answer-hold mechanism** (write *after* the Task-1 de-risk spike, *before* the
  engine): how a pending interactive request is held, resolved, timed out (60-min backstop), and
  cancelled — grounded in the spike's evidence.

---

## Roadmap

**In scope (P1 / this pipeline):** the async-latency de-risk spike → streaming engine (Substrate A,
normalized interface, session-id-carrying events/decisions) → interactive-tool relay
(AskUserQuestion buttons + "Other"; ExitPlanMode Approve/Reject+feedback) with the async answer-hold
+ 60-min backstop + `/cancel` → live coalesced rendering → `ENGINE_MODE` flag + harvested
lifecycle/persistence → CI (GitHub Actions) → SB1/SB2/SB3/SB4/SB6 + RB1/RB2/RB5 baseline + tests.

**Out of scope (deferred):** per-tool permission approval gating + default-bypass removal +
button-callback-authenticity hardening + risk classification (**P2**); Substrate-B adapter (later,
only if A regresses); multi-project sessions + restart recovery (**P4**); concurrency (**P5**);
packaging/keep-alive (**P7**).

**Expected build order (input to `/plan`):**
1. **De-risk spike (throwaway):** async answer-hold latency on `can_use_tool` + interactive-tool
   answer round-trip (2-min and ~60-min holds). Record PASS/PARTIAL/FAIL. *Gate: PASS before engine.*
2. **ADR-002** (async answer-hold mechanism) from the spike evidence.
3. **CI + project test harness** (GitHub Actions; lint/type-check/secret-scan; preserve the existing
   behavioral snapshot as regression coverage).
4. **Engine skeleton** — substrate adapter seam (A built, B slot) + lifecycle (start/resume/send/stop)
   + events-out normalization; `ENGINE_MODE=streaming` opt-in; one-shot untouched. Unit-tested
   against a mocked substrate.
5. **Decisions-in + answer-hold** — question answer (native `answers` map), plan verdict (allow /
   deny+feedback), free-text reply, cancel; 60-min backstop.
6. **Render layer** — events → Telegram (verbatim vs one-liner), inline-keyboard prompts, coalesce/
   throttle (RB5).
7. **Wire `bot.py`** — `ENGINE_MODE` switch, SB1-checked callback handler, `/cancel`, message routing.
8. **SB/RB tests + `/cd` path policy (SB2)**; preserve existing guarantees; green CI.
9. **Manual live verify** (`/verify`): run `/grill` from a real chat end-to-end under
   `ENGINE_MODE=streaming`.
