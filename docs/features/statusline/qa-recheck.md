# Statusline — Codex re-check #1 (after the 3-blocker fix)

## Blocker status
- B1 (ctx await): CLOSED — `get_context_usage()` is now awaited; the live SDK % wins over the usage fallback; still best-effort (raise/no-client → fallback → None, never fabricated).
- B2 (foreground race): **STILL-OPEN** — the B1 async-ctx fix added a NEW `/switch` window: `_statusline_text()` captures the foreground, then awaits ctx; a `/switch` during that await still writes a stale previous-project line (the gate-wait rebuild's own ctx-await straddles the switch).
- B3 (/plan mode): CLOSED — `in_plan_turn` set before the turn-start refresh, cleared before the end refresh → `🔒 plan` shows only during the plan turn.
- NB (pin retry): CLOSED — a failed pin leaves `statusline_pinned=False`; the next update retries the pin before the identical-text skip.

## Verdict: NO_SHIP — B2 residual (the async-ctx await-straddle) must be closed.
