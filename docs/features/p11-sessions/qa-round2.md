# P11 QA — Codex round-2 verdict (blocker fixes)

Full synthesized QA record (both rounds, both reviewers) is in `qa.md`. Raw Codex round-2 verdict:

## Blockers
- None

## Prior-blocker status
- **B1: CLOSED** — `ps` failures now use `check=True` and set the degraded sink on swallowed failures; corrupt/unreadable registry entries also set degraded, while clean empty/missing registry paths do not.
- **B2: CLOSED** — attach persists base `session_id` plus `fork_pending`, and restart reconstruction re-enters the first-write re-probe before resume.
- **B3: CLOSED** — `_ensure_engine()` re-probes at first resume and forks on `running or degraded`; persisted `fork_pending` is cleared only after a successful resumed turn.

## Non-blocking
- `stream_session.py` `_recover_failed_resume()` clears the failed `session_id` but not `fork_pending` → stale marker could cause an unnecessary re-probe/fork after restart. (Fixed in the hardening pass.)

## Verdict
SHIP

## Reasoning
The never-co-drive path now fails closed for real liveness uncertainty: `probe_one()` is RB1-total, `_reprobe_liveness()` converts crashes to degraded, and fork is selected before any adopted resume write.
