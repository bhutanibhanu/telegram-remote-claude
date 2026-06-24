# P11 (T1+T2) QA — cross-model Codex + independent reviewer

## Round 1 — Codex: NO_SHIP (3 blockers, all on the never-co-drive guarantee)

### Blockers
1. **Degradation not detected on swallowed scan failures** — `sessions_discovery.py`: `scan_claude_processes()` / `read_process_registry()` swallow real `ps`/registry failures as `[]` *internally*; `discover()` only sets `liveness_degraded=True` when those callables *raise*. So a real `ps` timeout/nonzero or unreadable/corrupt registry → `running=False, liveness_degraded=False` → `attach_session()` continues in place instead of forking on doubt. (The fork-on-doubt hardening is bypassed because the failure never propagates.)
2. **In-memory fork intent lost across restart while the base id is persisted** — `stream_session.py`: live/uncertain attach persists the live base id, but the fork decision lives only in `rt.attach_fork` (in-memory, lost on restart per the runtime comment). A bot restart before the first turn → `_ensure_engine()` reads the persisted base id and resumes `fork=False` → **co-drives the live session.**
3. **Fork decided at attach time, not at first write (TOCTOU)** — an idle session that goes live elsewhere between `/attach` and the first message is co-driven (`_ensure_engine` uses the stale `attach_fork`). Design-accepted in the handoff, but closable by re-probing liveness at first resume.

### Non-blocking
- Handoff overstated "base id never seeded/written" — it IS persisted to the project registry until the first forked turn overwrites it (the SDK adapter avoids seeding it internally; the store does persist it transiently).
- `render.py` does not dedupe duplicate `session_id`s if the SDK itself returns any (merge-with-bot-projects dedupes by id, but not SDK-internal dups).

### Verdict: NO_SHIP
Happy-path live attach threads `fork=True` correctly and the SDK adapter avoids seeding the base id on fork — but the guarantee fails under real failure / restart / ordering conditions.

## Live-verify finding (concurrent with QA)
- `/sessions` on the real Mac (≈334 sessions) crashed with `BadRequest: Message is too long` — the listing rendered every session into one >4096-char message. Fix: sort (active+bot-known pinned, then most-recent) + cap with honest "showing X of Y" footer + `split_message` safety net.

## Round 2 — fixes (committed `dc58c2a`, + /sessions overflow fix `9d0d0d1`)
- **B1:** `scan_claude_processes`/`read_process_registry` now take a `degraded` sink and flag it on a *swallowed* real failure (`_default_ps` `check=True` → nonzero/timeout degrades; corrupt/unreadable registry degrades; clean-empty / missing dir does NOT). `discover()` ORs it into `liveness_degraded` → fork-on-doubt trips on real failures.
- **B2+B3:** the binding fork decision is made at the **first write**, not attach time. `attach_session` persists a `fork_pending` marker (`session_store.set/get_fork_pending`); `_ensure_engine` at the first resume of a `fork_pending` project re-probes the base id's current liveness (`SessionDiscovery.probe_one(id,cwd) → (running, degraded)`, RB1-total) and **forks on `running or degraded`, continues only on confident idle**. `fork_pending` is cleared (persisted) only after the first *clean* turn → a restart re-derives from a fresh probe (closes B2), and a newly-live session is caught at the write (closes B3).
- Folded in: dedupe discovered sessions by `session_id`; handoff corrected re: the transient base-id persist.
- `/sessions` overflow (live-verify): relevance sort (active+bot-known pinned, then recent) + cap 15 with honest "X of Y" footer + `split_message` net; SDK epoch ms→s normalization (recency was always "just now").

## Round 2 verdicts — BOTH agree: SHIP
- **Codex (cross-model) re-QA: SHIP.** B1/B2/B3 all **CLOSED**, 0 blockers. "The never-co-drive path now fails closed for real liveness uncertainty: `probe_one()` is RB1-total, `_reprobe_liveness()` converts crashes to degraded, and fork is selected before any adopted resume write."
- **Independent reviewer: AGREE** (never-co-drive holds under failure+restart+race), 0 blockers; all 3 mutation-probes fire — (a) clear-at-connect → restart co-drive test fails; (b) probe_one swallows degraded → fork-on-doubt test fails; (c) skip re-probe → 5 B2/B3 tests fail.
- **Convergent non-blocking (both flagged):** `_recover_failed_resume` cleared the failed `session_id` but not `fork_pending` → a stale marker could cause an unnecessary re-probe/fork after restart (self-healing, not a co-drive). **Fixed in the hardening pass** + a durable regression test pinning the clear-at-clean-turn boundary (the independent reviewer's probe (a) broke zero existing tests, so the suite now locks it).

**Final verdict: SHIP** (after the hardening pass + live-verify). 1191→ tests, gates green.
