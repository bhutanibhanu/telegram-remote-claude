# Statusline QA — Verifier + cross-model Codex

## Verifier (same-model, independent): SHIP — 0 blockers
RB1 (defense-in-depth swallow), foreground-only concurrency, SB3 body-free, SB1, effort warm-rebuild, footer-$ removed — all mutation-probed. Non-blocking: cosmetic empty-model double-space; coverage gaps (proactive-statusline test, empty-model test, create_default=False side-effect test).

## Codex (cross-model): NO_SHIP — 3 blockers
1. **ctx % not awaited** — `adapter_sdk.py:834`: `ClaudeSDKClient.get_context_usage()` is ASYNC in the SDK but `context_percentage()` calls it WITHOUT await → the real SDK % is never read (silently falls to the usage-fallback). The sync test fake hid it. The headline ctx feature doesn't use the real API.
2. **Foreground-switch race** — `stream_session.py:4164`: foreground gated before dispatch, but `_update_statusline` snapshots state then awaits gated I/O; a `/switch` between snapshot and send/edit can write a stale previous-project line.
3. **/plan mode never displayed** — `stream_session.py:3245`/`4211`: `rt.plan_next` is consumed before `_drive_turn`, but `_statusline_text` reads plan-mode from `rt.plan_next` → during the plan turn the line shows `🔒 gate` not `🔒 plan`.

Non-blocking: send-succeeds-but-pin-fails stores the id+text → identical updates skip → stays unpinned until a later edit fails (a transient pin failure leaves it unpinned).

### Verdict: NO_SHIP (Codex) — 3 real blockers; the cross-model reviewer caught the async/race/lifecycle bugs the same-model Verifier's SHIP missed.
