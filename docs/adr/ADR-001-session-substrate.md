# ADR-001 — Session Substrate for the Interactive Claude Code Remote

> **SKELETON — to be filled by the P0 feasibility spike. Do not treat any field below as decided.**
> Until this ADR is marked Accepted, no later pipeline (P1+) is scoped or built.

- **Status:** Proposed — **blocked on P0 spike** (not yet decided)
- **Date:** TBD (set when accepted)
- **Deciders:** repo owner
- **Related:** [`docs/interactive-remote-design.md`](../interactive-remote-design.md) ·
  [`docs/cross-cutting-requirements.md`](../cross-cutting-requirements.md)

---

## Context

The remote must drive an **interactive** Claude Code session programmatically from Python:
stream Claude's activity out (to relay to Telegram), feed operator replies back in, answer
**permission requests**, and answer **interactive tools** (AskUserQuestion, ExitPlanMode) that
skills such as `/grill` and `/pipeline` emit. The current one-shot `claude -p --output-format json`
runner cannot do any of this.

A prior doc-only research pass was **unreliable** (it wrongly concluded the Claude Agent SDK does
not exist, and it never empirically tested whether interactive tools can be answered
programmatically). Therefore this decision is settled by a **throwaway spike that actually runs the
session**, not by documentation alone.

**This ADR is a genuine gate (GATE 1).** If neither substrate can answer interactive prompts
programmatically, the product's headline capability changes shape ("constrained mode") and the
roadmap is re-planned before P1.

---

## Decision drivers / success criteria

The spike must produce a documented **yes / no / partial (+ how)** for each, with evidence:

- [ ] **C1 — Bidirectional streaming.** A persistent, multi-turn session where new operator
      messages can be sent in and a continuous event stream comes out.
- [ ] **C2 — Per-tool permission decision.** Code is asked before a risky tool runs and its
      allow/deny (with optional reason / modified input) is honored.
- [ ] **C3 — AskUserQuestion.** A multiple-choice question raised mid-session can be intercepted
      and answered programmatically (no TTY).
- [ ] **C4 — Plan approval (ExitPlanMode).** A proposed plan can be surfaced and approved/rejected
      (with feedback) programmatically.
- [ ] **C5 — Skill invocation.** A custom slash-command skill can be invoked in-session and its
      interactive prompts flow through the same channels as C2–C4.
- [ ] **C6 — Session resume.** A session can be resumed by id across separate process runs with
      history intact.

C3 and C4 are the make-or-break criteria.

---

## Options considered

### Option A — Claude Agent SDK (Python)
- **Summary:** persistent client (e.g. streaming session) with a permission callback, hooks, MCP,
  skill loading, and session resume; drives Claude Code under the hood.
- **Evidence (fill from spike):** _TBD — package name + version; C1–C6 results._
- **Pros / Cons (fill from spike):** _TBD._

### Option B — `claude` CLI `stream-json` protocol (fallback)
- **Summary:** drive `claude -p --input-format stream-json --output-format stream-json`; parse the
  event stream; handle permissions via the CLI's permission mechanism / hooks.
- **Evidence (fill from spike):** _TBD — observed event types; whether C2–C4 are answerable over
  the wire on CLI v2.1.183 (note: `--permission-prompt-tool` was NOT present in this version's
  `--help`)._
- **Pros / Cons (fill from spike):** _TBD._

### Option C — Hybrid (only if needed)
- **Summary:** SDK for session/streaming/permissions, with hooks or workflow constraints filling
  any gap the SDK leaves on C3/C4.
- **Evidence / when this is required (fill from spike):** _TBD._

---

## Decision

_TBD — chosen substrate + one-paragraph justification, set when the spike is complete and the owner
accepts (G-ADR)._

## Normalized engine interface (output of P0)

_TBD — define the "events in / decisions out" contract P1 builds on:_
- _Event types emitted to the bot (text, tool_use, ask, plan, error, result, status)._
- _Decision/answer types accepted from the bot (permission verdict, question answer, plan verdict,
  free-text reply, cancel)._
- _Session lifecycle calls (start, resume, send, stop)._

## Consequences

_TBD — what P1 inherits; any capability that needs a workaround (e.g. if C3/C4 are partial); migration
notes from the current one-shot runner._

## Evidence log

_TBD — link/paste the spike prototype results per criterion C1–C6 (what was run, what was observed)._
