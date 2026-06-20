# Design — Interactive Claude Code Remote (Telegram)

> Umbrella design for evolving `claude-telegram-bot` from a one-shot Q&A relay into a
> full **interactive Claude Code remote** you drive from your phone. Built feature by
> feature, each shipped through `/pipeline`. This doc is the vision + canonical roadmap;
> per-pipeline feature docs live under `docs/features/<slug>/` and are created when each
> pipeline is scoped.

**Status:** scoped (grill complete 2026-06-20) · baseline reconciled · **blocked on P0** (feasibility
spike + [ADR-001](adr/ADR-001-session-substrate.md)). No later pipeline is scoped or built until P0
is run and ADR-001 is accepted.

**Companion docs:** [`docs/cross-cutting-requirements.md`](cross-cutting-requirements.md) (Security
Baseline, Reliability Baseline, CI, test policy) · [`docs/adr/ADR-001-session-substrate.md`](adr/ADR-001-session-substrate.md).

---

## Approved decisions (decision log)

| # | Decision | Value |
|---|---|---|
| 1 | Numbering | Single **P0–P9** series; **SB / RB / CI** are cross-cutting tracks, not numbered pipelines. F-series retired. |
| 2 | Path policy | **Hybrid (C)** — allowed-roots by default, explicit `ALLOW_ANY_PATH=true` opt-out. Personal root: `/Users/ray/dev`. Public deployments **must** set `ALLOWED_ROOTS`. |
| 3 | Build supervision | **Per-task diff approval** before every commit. |
| 4 | Approval timeout | **60 minutes** backstop → auto-deny + notify. |
| 5 | Concurrency | **Last** (P5), after multi-project + restart recovery (P4). |
| 6 | CI | **GitHub Actions**, stood up in **P1**, gates all later merges. Docs-baseline commit only after full-diff review. |
| 7 | ADR location | `docs/adr/ADR-001-session-substrate.md`. |
| — | Test policy | 53 passing tests = **pre-P0 snapshot only, not a permanent numeric floor.** See [Test policy](#test-policy). |

---

## The basics

**Elevator pitch.** Run Claude Code on your Mac from your phone over Telegram — not just fire
one-off prompts, but actually *use* it: run `/grill`, `/pipeline`, `/plan`; answer its questions;
approve its plans; approve risky commands before they run; watch the workflow live. A remote
terminal for Claude Code, in your pocket.

**The actual problem.** Today the bot runs `claude -p` one-shot with
`--dangerously-skip-permissions` — Claude edits files and runs shell commands with **zero
oversight and zero visibility**, and it **can't host interactive workflows** at all
(single-prompt-in, single-answer-out). The owner wants (1) to approve consequential actions
before they happen, (2) to see the real workflow as it unfolds, and (3) to drive Claude Code's
interactive skills remotely — none of which the current one-shot architecture can do.

**Who it's for.** Primary: the owner, controlling their own dev machine remotely. Secondary:
**open-source users** who clone the repo, drop in their own bot token + chat id, and run it
against their own machine. Single human operator per deployment.

**Definition of success.**
- Run `/grill` or `/pipeline` end-to-end from the phone — answer every question, approve every
  plan, hit every gate — without touching the laptop.
- No write/edit/shell/delete executes without an explicit Allow (or an active session-scoped
  allowance), with `--dangerously-skip-permissions` **gone** from the default path.
- Progress is legible: you can always tell what Claude is doing and why; never a black box,
  never a wall of noise.
- A stranger can `git clone`, set the required env vars, and have a working secure remote in <10 min.

**Anti-goals (explicit non-features).**
- **Not** a general chatbot. Pure Claude Code; no "summarize this / chat with me" mode.
- **Not** multi-user. One allowlisted operator per deployment (multiple *projects*, yes; multiple *people*, no).
- **Not** a web UI, mobile app, or hosted SaaS. Telegram is the only surface; runs on the user's own machine.
- **Not** a sandbox. It deliberately gives Claude real access to a real machine; safety comes from
  the allowlist + approval gates + path policy, not isolation.

**Constraints.**
- Solo build, incremental — one pipeline at a time; `main` stays shippable between pipelines.
- Open-source-friendly: no hardcoded paths/secrets, generic defaults, real docs.
- Depends on Claude Code CLI installed + logged in on the host (`claude` on PATH).
- Telegram limits: ~4096 chars/message (handled), ~1 msg/sec/chat send rate, inline-keyboard
  callbacks for buttons.

---

## Requirements

### Functional (ranked)

1. **Persistent interactive session engine** — long-lived two-way streaming: send messages in,
   stream events out, multi-turn. Replaces one-shot `claude -p`.
2. **Permission approvals** — risky actions (Write, Edit, Bash, deletes) pause with
   **[Allow once] [Allow for session] [Deny]**; reads/search auto-run; default path drops bypass.
3. **Interactive-prompt relay** — AskUserQuestion → option buttons (+ free-text "Other"),
   plan approval (ExitPlanMode) → plan + **[Approve] [Reject + feedback]**, free-text questions →
   reply. Makes `/grill` and `/pipeline` runnable remotely.
4. **Live workflow rendering** — verbatim for meaningful output (questions, plans, errors,
   decisions, final answers); one-liners for mechanics ("✏️ Edit main.py", "▶️ pytest"). Verbosity
   self-adjusts to the kind of work.
5. **Multi-project sessions** — several named projects (dir + own conversation), `/projects`,
   `/new <name> <path>`, `/switch <name>`; resume across restarts.
6. **Background concurrency** — a run continues after you switch away; the bot notifies and routes
   approvals/answers to the right session.
7. **Control surface** — `/cancel`, per-session `/yolo` / `/unyolo`, plus existing `/reset /cd /pwd /help`.

### Non-functional

- **Security (public-facing).** Secret token + chat-id allowlist remain the trust boundary;
  approval gates + path policy are the new primary controls. See the **Security Baseline (SB)** in
  the cross-cutting doc — applied from P1, audited at P6.
- **Reliability.** Restart/resume, clean failure on crash mid-run, timeout, cancel, rate-limit
  safety. See the **Reliability Baseline (RB)** — applied from P1.
- **Latency / rate limits.** Throttle/coalesce one-liner updates; prefer editing a status message
  over flooding.
- **Availability.** Single long-running process on the user's machine; document keep-alive
  (tmux / nohup / launchd / systemd).
- **Portability.** Python; runs wherever Claude Code does (macOS + Linux). No macOS-only defaults.

### Future (6–12 mo)

launchd/systemd service packaging; optional voice notes → prompts; richer status dashboard
message; per-project approval policies. Today's per-project session model accommodates these
without rewrite.

---

## Architecture (delta from current)

### Current

`bot.py` (python-telegram-bot) → `claude_runner.py` (`claude -p --output-format json`, one-shot
subprocess, per-chat `--resume`) → single reply. Stateless per turn; cannot stream, ask back, or
host interactive tools.

### Target

A persistent **bidirectional streaming session per project**, with an interaction layer mapping
Claude's events ↔ Telegram messages/buttons.

```
Telegram  ──messages/button taps──▶  bot.py (transport, commands, callbacks)
   ▲                                      │
   │  msgs, one-liners, prompts, buttons  ▼
   └──────────  render layer  ◀──  session engine (streaming, multi-turn)
                                          │  events: text / tool_use / ask / plan / error / result
                                          │  decisions: permission, question answer, plan verdict
                                          ▼
                                  Claude Code (per project: cwd + session id)
```

### Substrate decision → ADR-001 (P0)

We must drive an *interactive* Claude Code session programmatically: stream events, answer
permission requests, **and** answer interactive tools (AskUserQuestion, ExitPlanMode) that skills
like grill/pipeline emit. Two candidates — **(A) Claude Agent SDK** (preferred if it exposes the
interactive tools + permission callback) and **(B) CLI `stream-json` protocol** (fallback). The
prior doc-only research was unreliable on the SDK and untested on the interactive-tool question;
**P0 settles this empirically** and records it in [ADR-001](adr/ADR-001-session-substrate.md).
**P0 is a genuine gate — nothing below P0 is scoped until ADR-001 is accepted.**

### Components (new / changed)

- `engine/` — session engine over the chosen substrate; emits a normalized event stream, accepts
  decisions/answers. Replaces `claude_runner.py`.
- `session_manager.py` — multi-project registry: `{id, name, cwd, claude_session_id, status, yolo,
  session_allowed_tools, created_at, last_active}`. Extends today's `session_store.py`.
- `permissions.py` — risk classification, pending approvals, allow-once/allow-session/yolo state,
  block-until-answer + `/cancel` + 60-min backstop, path policy enforcement.
- `render.py` — normalized event → Telegram (verbatim vs one-liner); interactive prompt → inline
  keyboard; update coalescing/throttling.
- `bot.py` — transport + commands + inline-keyboard callbacks + message routing + background notifications.
- `util.py` — existing UTF-16-aware chunking (reused).

### Data model (entities)

- **Operator** — `chat_id` (allowlisted). One per deployment.
- **ProjectSession** — `{id, name, cwd, claude_session_id, status (idle|running|awaiting_input|
  awaiting_approval), yolo, session_allowed_tools[], created_at, last_active}`. Persisted; resumable.
- **PendingPrompt** — `{id, session_id, kind (permission|question|plan), payload, created_at,
  timeout_at}`. One inline keyboard each.
- **Run** — an in-flight turn within a session (one active per session; many across sessions in P5).

### Telegram interaction mapping

| Claude event | Telegram rendering |
|---|---|
| Permission needed (risky tool) | message + `[Allow once][Allow session][Deny]` |
| AskUserQuestion | message + one button per option (+ "Other" → free-text reply) |
| ExitPlanMode (plan) | plan text (chunked) + `[Approve][Reject + feedback]` |
| Free-text question | message; operator replies with a normal message |
| Routine tool_use | one-liner ("✏️ Edit `main.py`", "▶️ `pytest`") |
| Assistant text / final answer | message (chunked) |
| Error | verbatim error block |
| Background event | "🔔 `<project>` needs approval" / "✅ `<project>` done" |

### Auth / security model

Trust boundary unchanged (secret token + chat-id allowlist), strengthened by approval gates +
path policy instead of weakened by bypass. New surfaces hardened from the feature that introduces
them: button-callback authenticity (P2), path validation on `/cd` (P1) and `/new` (P4). `/yolo`
is the one deliberate risk re-introduction — per-session, off by default, loud when on. Full
checklist: cross-cutting doc, **SB**.

---

## Canonical roadmap (P0–P9)

Each P is one `/pipeline`. `main` stays runnable throughout (feature-flag half-built parts). SB/RB/CI
are cross-cutting (see below), not separate pipelines. Ruthless ordering: prove the unknown, build
the backbone, layer features, then harden/package/document/release.

| P# | Pipeline | Depends on |
|---|---|---|
| **P0** | Feasibility spike + ADR-001 *(gate)* | — |
| **P1** | Streaming engine + live updates (single session); **stand up CI + SB/RB baselines + minimal path policy (`/cd`)** | P0 |
| **P2** | Permission approvals (removes default bypass) | P1 |
| **P3** | Interactive prompts ⭐ HEADLINE (`/grill` + `/pipeline` from phone) | P2 |
| **P4** | Multi-project sessions + restart recovery (extends path policy to `/new`) | P1–P3 |
| **P5** | Background concurrency + notifications | P4 |
| **P6** | Security audit + threat model *(consolidation of SB; release gate)* | P2 (ideally after P5) |
| **P7** | Packaging + keep-alive (launchd/systemd) | P1 (best after P5) |
| **P8** | Documentation | finalize before P9 |
| **P9** | Open-source release (LICENSE, generic config, v1) *(final gate)* | P6, P8, CI |

### P0 — Feasibility spike + ADR-001  *(gate, not a user feature)*
- **Goal:** prove whether code can drive a live interactive session — stream activity, answer
  permission requests, answer AskUserQuestion + plan approval, invoke a skill, resume a session —
  and decide substrate (A vs B). Doc research already proved insufficient; this is empirical.
- **What you get:** a trustworthy go/no-go + locked architecture (ADR-001).
- **Acceptance:** documented yes/no for each criterion (streaming / permission callback /
  AskUserQuestion / plan approval / skill invocation / resume); ADR-001 written + accepted.
- **Risk / GATE 1 (biggest in the product):** if interactive prompts can't be answered
  programmatically on either substrate, the headline becomes "constrained mode" and we re-plan
  before P1.

### P1 — Streaming engine + live updates (single session)  *(backbone)*
- **Goal:** replace one-shot runner with a persistent two-way session for one project; live
  one-liner tool calls + verbatim text/final answers; messages in; `/reset`. Also **stands up CI**,
  establishes the SB/RB test baselines, and lands the **minimal path policy** — the
  `ALLOWED_ROOTS` / `ALLOW_ANY_PATH` config plus `/cd` enforcement (see [Path policy](#path-policy-decision-c)).
- **What you get:** a real-time remote (still in a permissive permission mode behind a flag —
  not yet safe for risky work; P2 fixes that).
- **Acceptance:** live one-liner progress + final answer; chunking holds; bursts don't trip rate
  limits; `/reset` works; bad input never crashes; **SB2 enforced on `/cd`** — paths canonicalized
  with symlinks resolved, then rejected unless contained in an `ALLOWED_ROOTS` entry (or
  `ALLOW_ANY_PATH=true`); RB1/RB2/RB5 satisfied with tests; CI green and gating.

### P2 — Permission approvals
- **Goal:** stop default bypass. Risky actions pause with [Allow once] [Allow session] [Deny];
  reads run free; `/cancel`; 60-min backstop; per-session `/yolo` / `/unyolo`; remove
  `--dangerously-skip-permissions` from default path. Builds the reusable button/pending-prompt layer.
- **What you get:** safe remote control with a speed escape hatch (`/yolo`).
- **Acceptance:** edit + shell each prompt; allow-session stops re-asking; deny relayed and adapted;
  60-min no-reply auto-denies + notifies; `/cancel` kills a waiting run; `/yolo` scoped to session;
  SB1 (button-callback authn), SB5; RB4 satisfied with tests.

### P3 — Interactive prompts ⭐ HEADLINE
- **Goal:** relay AskUserQuestion → buttons, ExitPlanMode → plan + approve/reject, free-text
  questions → reply; confirm full skill loops; decide how a skill is launched from Telegram.
- **What you get:** **run `/grill` and `/pipeline` entirely from the phone.**
- **Acceptance:** complete a `/grill` via buttons/replies producing the design doc; run `/pipeline`
  to a plan, approve, proceed; reject with feedback and see revision.
- **Risk:** depends on P0's interactive-tool finding.

### P4 — Multi-project sessions + restart recovery
- **Goal:** project registry; `/projects` `/new` `/switch`; per-project cwd + conversation +
  state; persist + resume across restarts; **extend the P1 path policy** to `/new` and
  project-session working directories (same config keys and the same canonicalize + symlink-resolve
  + containment checks — P4 only widens *where* they apply).
- **What you get:** multi-project remote that survives restarts.
- **Acceptance:** two projects with independent state; restart resumes both; interrupted run fails
  clean (RB3); **SB2 enforced on `/new` and project switches** — resolved targets outside every
  `ALLOWED_ROOTS` entry rejected unless `ALLOW_ANY_PATH`; RB6 (persistence) tested.

### P5 — Background concurrency + notifications  *(highest complexity, last capability)*
- **Goal:** runs continue after switching; per-project status; route approvals/questions/plans to
  the right (possibly non-active) project; proactive notifications; concurrency limits/queueing.
- **What you get:** true multitasking with pings when you're needed.
- **Acceptance:** long task in A while working B; A's approval routed + answerable without losing B;
  completion/error notifications; RB5 rate-limit safety under concurrency tested.

### P6 — Security audit + threat model  *(release gate)*
- **Goal:** consolidate and verify the cross-cutting SB across the whole surface; run the
  security-review tooling; write the threat model; document `/yolo` blast radius.
- **What you get:** a written threat model + a clean security review.
- **Acceptance / GATE 3:** non-allowlisted chat ignored (messages + buttons); traversal rejected;
  scan clean; threat model documented. No public release until this passes.

### P7 — Packaging + keep-alive
- **Goal:** one-command start; launchd (macOS) + systemd (Linux) units; graceful shutdown/restart;
  finalized `.env` with non-personal defaults; logging/health basics.
- **Acceptance:** fresh machine → one command → running; survives terminal close + reboot, resumes
  projects; defaults contain nothing machine-specific.

### P8 — Documentation
- **Goal:** README rewrite; command/button reference; usage guide (grill/pipeline from phone,
  multi-project, background); security section; troubleshooting/FAQ; contributor/architecture docs.
- **Acceptance:** a new user goes clone → token → running → first `/grill` from the README alone.

### P9 — Open-source release  *(final gate)*
- **Goal:** LICENSE; final generic-config pass; contributing guide + templates; version + tag v1 +
  release notes; final security + docs review; make public; verify clone→token→run on a clean machine.
- **Acceptance / GATE 4:** clean-machine clone → token → run works end-to-end; LICENSE present;
  CI green; security reviewed; v1 tagged.

---

## Cross-cutting tracks (SB / RB / CI)

These are **not** numbered pipelines — they are requirement sets applied to **every** pipeline,
owned from **P1**, audited at **P6**, finalized at **P9**. Full text and per-item first-applies-at
mapping live in [`docs/cross-cutting-requirements.md`](cross-cutting-requirements.md).

- **Security Baseline (SB1–SB6)** — authn on all inbound incl. button callbacks; path confinement;
  secret hygiene; no injection; explicit/visible bypass; safe defaults.
- **Reliability Baseline (RB1–RB7)** — never crash; clean failure; restart/resume correctness;
  cancel/timeout don't wedge; rate-limit safety; persistence integrity; each has a dedicated test.
- **CI** — GitHub Actions, stood up in P1 (tests + lint + type-check + security scan), gates all
  later merges; branch protection.

Each pipeline's `progress.md` acceptance criteria must explicitly reference the applicable SB/RB items.

---

## Path policy (decision C)

**Hybrid: allowed-roots by default, explicit opt-out.** The **minimal config + `/cd` enforcement
land in P1**; **P4 extends the same policy** to `/new` and project-session working directories. The
config keys and enforcement semantics are identical in both — P4 only widens *where* they apply, so
SB2 is satisfied from P1 onward (not deferred).

**Enforcement semantics (SB2).** Every operator-supplied path is **canonicalized with symlinks
fully resolved** (e.g. `realpath` / `Path.resolve()`), then checked for **containment**: the
resolved path must equal, or be a descendant of, a resolved `ALLOWED_ROOTS` entry. Traversal
(`..`), symlink escape, and out-of-root paths are rejected. Because containment is checked on the
*resolved* path, a symlink that sits inside an allowed root but points outside it is also rejected.

- `ALLOWED_ROOTS` — comma-separated base directories; resolved `/cd` targets (P1) and resolved
  `/new` / project-session targets (P4) must be contained within one of them, else rejected.
- `ALLOW_ANY_PATH=true` — deliberate opt-out to today's unrestricted behavior, for power users who
  accept the blast radius (skips the containment check; canonicalization still applies).
- **Personal deployment:** `ALLOWED_ROOTS=/Users/ray/dev`.
- **Public deployments:** **must** set `ALLOWED_ROOTS` explicitly; there is no wide-open default.
  (If unset and `ALLOW_ANY_PATH` is not true → fail closed: refuse path operations with a clear message.)

---

## Supervised checkpoint model (/pipeline-compatible)

Standard `/pipeline` phase gates plus project-specific gates. **Per-task diff approval** (decision 3)
is the core control.

- **G-Scope** — approve the pipeline's `design.md` (delta scope) before planning.
- **G-Plan** — approve `progress.md` (task graph + acceptance, incl. applicable SB/RB items) before code.
- **G-Build (per task)** — implementer writes code + tests in isolated context; tests run; **you
  review the diff + results; commit only after your OK.**
- **G-Verify** — approve verification evidence **including the SB/RB checklist for that pipeline**.
- **G-QA** — Codex QA returns SHIP / 0 blockers (or you accept a documented residual).
- **G-Ship** — approve opening the PR.
- **G-ADR (after P0)** — you explicitly accept ADR-001 before any later pipeline is scoped. *Hard gate.*
- **G-Security (P6)** and **G-Release (P9)** — public exposure blocked until both pass.

Each pipeline runs in its own git worktree/branch; `main` stays runnable; half-built capabilities
sit behind feature flags.

---

## Test policy

The repository's **53 passing tests are a pre-P0 snapshot, not a permanent numeric floor.** The
architecture changes from a one-shot runner to bidirectional streaming, so **obsolete
implementation tests may be rewritten, replaced, or removed.**

- **Preserve existing user-facing behavior** where it still applies (allowlist, ignore non-allowlisted,
  chunking, never-crash, command behavior) — with **equivalent or stronger behavioral coverage**.
- **Add new coverage** for streaming, interaction (questions/plans/approvals), security (SB), and
  reliability (RB).
- **Never use raw test count as an acceptance criterion.** Acceptance is the behavioral + SB/RB
  checklist for the pipeline, not a number going up.

---

## Shortest path to the headline + decision gates

**Shortest *safe* path to "run interactive `/grill` and `/pipeline` from Telegram": P0 → P1 → P2 → P3.**
(P2 before P3 so the button/pending-prompt plumbing is shared and you're never running unsafe.) A
demo-only path P0 → P1 → P3 with permissions permissive exists but contradicts the safety goal — use
only as a throwaway. P4–P9 extend the headline to multi-project, concurrent, hardened, packaged,
documented, and open-source.

**Decision gates:** GATE 1 (after P0 — interactive-tool feasibility; biggest risk) · GATE 2 (in P2
— risk-classification calibration) · GATE 3 (P6 — security) · GATE 4 (P9 — clean-machine + security
before public).

---

## Risks & open questions

**Top risks**
1. *(Technical)* Interactive-tool feasibility — gates the headline. *Mitigation:* P0 spike + ADR-001;
   fallback substrate identified.
2. *(Technical)* Background-concurrency complexity. *Mitigation:* ship single-active-run first
   (P1–P4); concurrency is its own pipeline (P5) on a proven base.
3. *(Operational/Security)* Public release of a remote-machine-control tool. *Mitigation:*
   approval-gated default, per-session yolo only, path policy fail-closed for public, threat-model
   docs + security review gate (P6) before the open-source tag (P9).

**Open questions (deferred, with triggers)**
- Substrate (A vs B) — **resolve in P0, before P1.**
- Restart recovery for an *in-flight* run: resume vs fail-clean — decide in P4 (lean fail-clean with
  a clear message).
- Update coalescing strategy (new messages vs editing a status message) — tune in P1 against real
  Telegram limits.

**ADRs to write**
- **ADR-001:** session substrate (output of P0) — [`docs/adr/ADR-001-session-substrate.md`](adr/ADR-001-session-substrate.md).
- **ADR-002:** permission classification model — what counts as "risky," and how allow-once /
  allow-session / yolo state is stored and scoped (P2).
