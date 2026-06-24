# ADR-006 — Known limitations accepted as-is (P8)

> Records two small, deliberately-deferred items surfaced during the P5–P7 work and the P8 doc pass.
> Neither is a bug; each is a low-value-to-fix or hard-to-verify trade-off documented here so a future
> maintainer (or a P9 OSS reader) knows it is **intentional**, where it lives, and why fixing it was not
> worth the risk. Sibling of [ADR-001](ADR-001-session-substrate.md) … [ADR-005](ADR-005-concurrency-correlation.md).

- **Status:** Accepted — documented limitations, no code change.
- **Date:** 2026-06-24
- **Deciders:** repo owner
- **Related:** [ADR-005](ADR-005-concurrency-correlation.md) (the P5 notification throttle the first item
  lives inside); [ADR-001](ADR-001-session-substrate.md) (the resume coupling the second item guards).

---

## Context

P8 is the final polish/doc phase before the P9 public release. Two items were flagged across P5–P7 as
"document, don't necessarily fix." Recording them keeps the codebase honest for a public reader rather
than leaving silent rough edges.

---

## Decisions

### 1. `notify_last` is not pruned — accepted (bounded, transient)

The per-chat notification throttle keeps `notify_last: dict[tuple[str, ...], float]`
(`claude_tg/stream_session.py`, `_ChatState`), mapping a key to the monotonic time the last ping of that
key was sent. For an **actionable** hold the key is `(project, ping_kind, tool_use_id)` — distinct per held
request, so each distinct hold keeps its own answerable keyboard (cross-model-QA BLOCKER 1) — and for a
terminal/non-actionable ping it is `(project, ping_kind)`. Entries are written in
`StreamingSession._should_send_chat_notification` and **never deleted**, so the dict can accrue one entry
per distinct held request over a long-lived process.

**Why we leave it as-is.** The growth is bounded and transient:
- It is **in-memory only** — cleared on every restart (RB3 abandon-and-lazy-resume; nothing about the
  throttle is persisted).
- It is bounded by **one operator's** concurrent holds (single-operator model, SB1) — the actionable keys
  are gated by `MAX_CONCURRENT_RUNS` worth of live projects, each with a small number of open holds.
- The terminal keys are `(project, kind)` — a fixed small set.

Pruning would mean adding deletes on the resolve / turn-end path, which is the **P5 concurrency-sensitive**
notification/throttle code (D8). The correctness bar there (never drop a verbatim ping, never let one
project's burst reset another's throttle) is delicate, and the upside of pruning a transient,
operator-bounded dict is marginal. So the throttle stays a pure record-and-decide structure; the dict is
left to be reclaimed at restart. *(If a future change makes holds long-lived or multi-operator, revisit —
prune on resolve/turn-end alongside the existing `reply_to_index` prune, which already does this for the
same lifecycle reasons.)*

### 2. `_is_resume_failure` is a text heuristic — accepted (documented, not a guarantee)

When a `--resume` turn fails because the session is gone, the bot recognizes it by **matching the
SDK/CLI error text**: `ClaudeRunner._is_resume_failure` (`claude_tg/claude_runner.py`) keys on
session-gone phrasing — `"no conversation found"`, `"no parseable output"`, or `"session"` together with
`"not found" / "invalid" / "expired"` — and explicitly excludes `"timed out"` / `"binary not found"`. The
streaming path reuses the **exact same** function via `_is_resume_failure_event`
(`claude_tg/stream_session.py`) so both runners stay consistent.

This is necessarily a **heuristic**, not a guarantee: the normalized event/result shape carries no
dedicated "resume failed" discriminator, so classification rides the error string. Confirming it against
the **real** SDK requires a torn or aged resume (an expired/evicted session id), which is hard to force
deterministically in a test, so it is a documented heuristic rather than a verified contract.

**Why this is safe to leave as a heuristic.** A misclassification does not wedge the bot — the engine
recovers either way:
- A **false positive** (an ordinary error misread as a resume failure) clears the persisted session id
  and asks the operator to resend; the next turn simply starts fresh
  (`StreamingSession._recover_failed_resume`). The cost is one lost session, no wedge.
- A **false negative** (a real resume failure not matched by the text) is caught by the **independent**
  transport/liveness `driver_error` path, which tears down and rebuilds a dead/wedged engine so the next
  turn starts a fresh client — recovery does not depend on the resume-failure text matching.

So the heuristic is an **optimization** (recover on the *first* failed turn with a clear message); the
fail-safe fallback is the rebuild path, which does not rely on the string match.

---

## Consequences

No behavior change — this ADR only records intent. Both items have a clear revisit trigger (long-lived /
multi-operator holds for the first; a deterministic torn-resume fixture for the second) should the
trade-off stop holding. Neither affects the P6 threat model: `notify_last` holds no bodies (SB3 — keys are
project name + kind + id only), and the resume heuristic reads already-body-free error text.
