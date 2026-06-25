# Statusline — Codex re-check #2 (after the B2-residual fix)

## B2 status
- CLOSED — `_statusline_text` now returns `(text, built_for)`; both gated write helpers do a final SYNCHRONOUS `_is_foreground(chat_id, built_for)` check after ALL awaits (gate wait + ctx await), with NO await between the check and the edit/send. A `/switch` during any await makes `built_for` non-foreground → the stale write returns before any send/edit; the `/switch`'s own trigger writes the correct line (no loop, no stale write).

## Regressions
- none (B1 ctx-await, B3 /plan, pin-retry, RB1, foreground-only concurrency, SB3 all still hold; focused statusline suite 29 passed).

## Verdict: SHIP
