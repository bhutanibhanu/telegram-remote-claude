# P4 multi-project — Verify + QA verdicts

Two reviewers, two different blind spots. **Codex (cross-model) returned NO_SHIP and caught two
real bugs the same-model Verifier missed.** (Raw verbose Codex transcript was 389 KB and is not
retained; the structured verdict is below, plus my verification of each claim.)

---

## Verifier subagent (isolated, same-model) — **SHIP**

- **Blockers:** none.
- **Non-blocking:** `_persist` writes to the current-active project not the turn-captured name
  (deferred defense-in-depth; busy-guard makes it safe today); `/reset`-during-a-held-turn nulls the
  engine so a later tap can't resolve until the 60-min backstop (pre-existing HEAD limitation,
  `/cancel` is the escape hatch); `paths.py` docstring still says "/cd" only (cosmetic).
- **Coverage gaps:** `/reset`/`/cancel` during a held turn (no streaming test); `/rm` non-active mid-hold.
- Gates: 576 pass, mypy/ruff clean.

## Codex (cross-model, gpt-5.5) — **NO_SHIP**

- **Blockers:**
  1. **Streaming `/reset` corrupts the active project's cwd after a `/switch`.** `cmd_reset`
     (`bot.py:91`) calls `runner.reset()` first; in streaming mode the runner shares the store, so its
     stale flat-view cwd is written into the active project via `JsonSessionStore.update()`
     (`session_store.py:191`). Violates D4/D5.
  2. **`/switch` does not re-validate the target's stored cwd before activating** (`bot.py:292`); the
     design says refuse out-of-root on switch/resume, but only the next-turn resume path checks it.
  3. **RB3 resume-failure fallback too narrow** (`stream_session.py:447`): `_ensure_engine` only falls
     back fresh if `engine.resume()` *raises*; it does not reuse `_is_resume_failure` / retry when a
     resumed turn produces a resume-related error, so stale/aged/torn session ids stay persisted.
- **Non-blocking:** v1→v2 migration isn't written back on load (lazy — `session_store.py:123`); handoff
  slightly overclaims "migration" + "resume fallback" as complete.
- **Suggested tests:** bot-level `/reset` after `/switch` (assert cwd preserved); `/switch` to an
  out-of-root cwd (assert refused, active unchanged); resume-connects-then-errors → fresh retry +
  notice + stale id cleared; migration-persisted-to-disk-as-v2.
- **Verdict reasoning:** registry + command surface solid and several tests are behavior-level, but the
  `/reset` cwd corruption and incomplete RB3 fallback break D4/D7 and can leave projects in the wrong
  dir or stuck on stale sessions.

---

## Orchestrator verification of each Codex blocker

- **B1 — CONFIRMED REAL (ship-stopper, data corruption).** `runner.reset()` → `_persist()` →
  `store.update(chat_id, None, self._cwds.get(chat_id))`. `_cwds` is seeded from the flat view at
  startup and never tracks `/switch`, so *restart → `/switch` → `/reset`* overwrites the active
  project's cwd with the runner's stale cwd (D4). **Fix:** `cmd_reset` must call `runner.reset()` only
  in one-shot mode (gate on `self.streaming`, like the other streaming-only commands); in streaming
  mode call only `streaming.reset()`.
- **B3 — CONFIRMED REAL (RB3 reliability gap).** `_ensure_engine` catches `engine.resume()` raising
  but not a resume that connects then errors on first use; the one-shot `_is_resume_failure`
  result-inspection was not ported to the streaming turn. A torn/aged session id can stay stuck. **Fix:**
  detect a resume-failure-shaped error on the first resumed turn → clear the persisted id + retry fresh
  + notice (port `_is_resume_failure` to the streaming path).
- **B2 — REAL but lower-severity (conformance/UX, not a security hole).** ADR-004 explicitly makes the
  resume path the *authoritative* SB2 gate, and it holds (no out-of-root execution ever happens — the
  next turn is refused before the engine starts). But the **design doc** says re-validate "on
  switch/resume," so an early `/switch` refusal closes a design-conformance gap and improves UX cheaply.
  **Fix (cheap):** re-validate the stored cwd in `cmd_switch` before activating; refuse + leave active
  unchanged if out-of-root.

## Status
**Phase = qa. Awaiting owner decision (fix / ship anyway / stop).** Recommendation: **fix** — B1 and B3
are genuine ship-stoppers; B2 is a cheap conformance fix. All three have clear, localized fixes + the
Codex-suggested tests. Then re-run gates + re-verify, and the owner phone-verify (`verify.md`).
