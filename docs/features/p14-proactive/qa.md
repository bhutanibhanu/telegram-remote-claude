# P14 (proactive scheduler) QA — cross-model Codex + independent reviewer

## Round 1

### Both reviewers AGREE on the make-or-break FORCE-GATE (the unattended-action security core), mutation-probed:
- A proactive (scheduled, unattended) turn treats `/yolo` + session-grants as OFF (risk-only criterion) via a per-turn `Engine._force_gate` (set at `send(proactive=True)` start, reset in `finally`; the persistent policy is NEVER mutated). No auto-allow path precedes the force-gate check; a NORMAL turn under `/yolo` still auto-allows (interactive bypass unchanged — regression-guarded); per-project Engine+lock → no flag race. Probe: invert the `_needs_approval` force-gate branch → 3 proactive-gating tests fail; restore → green.
- **Unattended fail-safe (RB4):** a proactive risky tool with nobody to approve HOLDS → the 60-min backstop auto-DENIES → clean. **RB1:** the driver loop survives a raising fire / bad schedule / store error (per-fire + per-tick guards); shutdown cancels cleanly. **No stacking:** fire-into-busy → skip+audit. **Persistence:** rearm-from-now on start (no missed-fire replay).

### Codex: NO_SHIP — 2 blockers
1. **`/schedules` rendered the schedule PROMPT text** (`render.py` `schedule_listing`/`preview_html`) — not body-free. (Independent reviewer rated this non-blocking — consistent with the `/macros` posture, operator's own text — but the orchestrator sided with the stricter call given the trust-layer theme.) **Fix:** `/schedules` body-free — name/interval/next-run/paused/project only; drop the prompt preview (prompt stays persisted-to-fire, not displayed).
2. **SB1 not re-checked at FIRE time** — the driver fires persisted schedules without re-checking `chat_id` is still allowlisted, so a chat REMOVED from `TELEGRAM_ALLOWED_CHAT_IDS` would still get proactive turns. **Fix:** re-check the allowlist at fire time; a de-authorized chat's schedule is skipped (audited `unauthorized`), never fired.

### Codex non-blocking
- `Engine._force_gate`/`_out_queue` single-flight relies on `StreamingSession`'s per-project lock, not a guard local to `Engine` (production-safe; optional defensive assertion). The "scheduled" header is sent before the busy check (a busy-skip shows both a start header + a skip notice — fix ordering).

## Round 2 — fix
`/schedules` made body-free (no prompt preview); fire-time SB1 re-check (de-authorized chat → skip+audit); header sent only when the turn will run; + a defensive single-flight assert in `Engine.send`.

## Final: SHIP
- **Codex re-check: SHIP** — B1 (/schedules prompt leak) CLOSED (listing renders no prompt), B2 (fire-time SB1) CLOSED (`fire_schedule` allowlist-checks before header/audit/turn for both the driver + `/runnow`). No new issues.
- **Live phone-verify PASS:** `/runnow schedtest` fired a proactive turn → Claude replied `SCHEDULEFIRED`; `proactive_fire (schedtest)` audited body-free (name only); `/schedules` body-free. **Force-gate confirmed:** with `/yolo` ON, a proactive risky Bash command STILL hit the permission gate (Allow/Deny) → denied → no file written (an unattended fire never inherits allow-all).
- ADR-008 + README + docs index; stale code comments corrected. 1577 tests; ruff/mypy/secret_scan clean.
