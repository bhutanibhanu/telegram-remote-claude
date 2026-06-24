# P12 (supervise) QA — cross-model Codex + independent reviewer

## Round 1

### Codex (cross-model): NO_SHIP — 1 blocker
- **`/plan` one-shot marker lifecycle:** `rt.plan_next` is read+cleared too late (inside `_ensure_engine` after the SB2 `resolve_within_roots` check that can raise `PathNotAllowed`), and the pre-engine abort paths (`stream_session.py:2733`/`:2750`) `return` before `_ensure_engine` is called — so a failed/aborted/cancelled plan turn can leave the marker ARMED, and a later unrelated message unexpectedly runs in plan mode. Violates the "exactly one turn, even failed/cancelled" contract.
- Non-blocking: none.

### Independent reviewer: 0 blockers (rated the same lifecycle item non-blocking/fail-safe)
- Flagged the `_ensure_engine` SB2-before-clear ordering as near-unreachable + fail-safe (plan mode = more supervision, never auto-exec). Codex found it broader (the pre-engine aborts are more reachable), so it's treated as a blocker and fixed.

### Both validators AGREE on the two make-or-break contracts (mutation-probed):
- **C4 (security-critical) — AGREE:** approving a plan grants NOTHING about tools. The engine core is UNCHANGED by P12; `permission_mode` is purely an SDK session-creation param, never read by the gate. Plan-approve resolves only the held ExitPlanMode via `PlanVerdict(approve=True)` → a bare allow (no grant, no policy mutation); session grants come only from an explicit `PermissionDecision(allow_session)`. Probe: making plan-approve set `yolo=True` / auto-allow → both C4 tests FAIL; restore → pass.
- **SB3 (data-leak) — AGREE:** `ThinkingEvent` has no signature field; the normalizer reads only `.thinking`; `signature_delta` dropped; `redacted_thinking` → fixed opaque indicator, never raw; thinking text capped (280) + status-line coalesced (no flood); thinking-OFF turn byte-for-byte unchanged. Probe: making the normalizer emit the signature / redacted body → 3 tests FAIL; restore → pass.
- Dedup with partials ON: final answer renders exactly once (probe: break the footer-swap → once-only test fails).

### Verdict: NO_SHIP (round 1) — fix the `/plan` marker lifecycle, then re-QA.

## Round 2 — fix
Consume the `/plan` marker EARLY (at the point the prompt-turn commits to driving, before the pre-engine aborts + the SB2 check) and thread the captured `plan_turn` flag down to `_ensure_engine` — so once a prompt message is taken as the turn, the marker is consumed regardless of any later abort/SB2-raise/cancel (next message always normal); kept prompt-turn-scoped (a following COMMAND does not consume it). Tests pin every abort path Codex cited + a mutation-probe.

**Re-QA: Codex re-check = SHIP** (blocker CLOSED; commands don't consume; C4/SB3 surfaces untouched; normal turn unchanged). See `qa-recheck.md`.

## Final: SHIP
- Codex re-check SHIP + independent reviewer C4 AGREE + SB3 AGREE (both mutation-probed).
- **Live phone-verify PASS:** plan mode (plan shown + Approve/Reject) → **C4 verified live** (post-approve Write still hit the gate; deny held → no file); thinking (`/thinking on` engages `--include-partial-messages`; final answer renders once = dedup-with-partials holds; no render crash).
- **Robustness finding (investigated → not a code bug):** a messy approve-plan→deny-Write→colliding-inputs sequence left a turn busy ~9 min; deterministic repro proves `_hold_depth` is balanced + the 300s liveness re-arms (mutation-probed). Root cause = an SDK-side silent-stall-after-deny (read_messages hangs, no exception — the liveness bound is the only backstop) and/or input collision; P6/H2 mechanism correct, P12-reachable not P12-broken. Regression tests `8951a97`. Follow-up: tighten post-deny liveness / reap the stalled subprocess on timeout.
- 1288 tests; ruff/mypy/secret_scan clean.
