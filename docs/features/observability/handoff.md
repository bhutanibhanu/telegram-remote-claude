# Feature Handoff: observability

## Goal
Show, live and body-free on the phone, what's running during a turn (current tool + active
subagents) and how close the Claude rolling session-limit is — with a one-time heads-up before a
mid-turn cutoff.

## Files changed
```
 claude_tg/engine/adapter_sdk.py         | 319 ++  (T1 limit capture + T2 activity capture + ActivitySnapshot)
 claude_tg/engine/engine.py              |  70 +-  (Engine.limit_status() + last_activity() delegates)
 claude_tg/render.py                     |  36 +-  (T3 🪙 limit field in format_statusline + _LIMIT_BADGES)
 claude_tg/stream_session/__init__.py    |   1 +   (re-export ActivityMixin)
 claude_tg/stream_session/activity.py    | 307 +   (T5 ActivityMixin — transient activity line)
 claude_tg/stream_session/core.py        | 116 +-  (T4 _maybe_warn_limit + T5 wire-in; bases += ActivityMixin)
 claude_tg/stream_session/runtime.py     |  27 +   (_ChatState: limit_warned + activity_* fields)
 claude_tg/stream_session/statusline.py  |  16 +   (T3 foreground limit read → format_statusline)
 docs/adr/ADR-010-observability.md       |  NEW
 docs/features/observability/*           |  design/progress/state/handoff
 tests/test_engine.py                    | 405 +   (T1+T2 telemetry tests)
 tests/test_render.py                    |  67 +   (T3 render tests)
 tests/test_stream_session.py            | 866 +   (T3/T4/T5 statusline+warning+activity tests)
 14 files, +2496 / -5
```

## How to run
```
cd <worktree> && ENGINE_MODE=streaming CLAUDE_STATE_FILE=~/.claude_tg_sessions.json \
  AUDIT_LOG_FILE=~/.claude_tg_audit.jsonl .venv/bin/python main.py
```
Then from Telegram, run a turn that uses tools / spawns subagents → watch the transient `⚙️ …`
activity line edit in place and the `🪙 %` field on the pinned statusline; approach the session
limit → one warning fires.

## Expected behavior
- **Activity line:** posts on first activity in a foreground turn; `⚙️ <subagent type-names | N
  agents> · <tool>`; edited in place (skip-identical + ≲1 edit/sec throttle); removed at turn end.
  Names only (SB3). Foreground-only. A `/switch` mid-wait drops a stale write (B2).
- **🪙 limit field:** `🪙 <pct>%` (precise, from `RateLimitInfo.utilization`) else `🟢/🟡/🔴` badge;
  omitted entirely until a signal is seen (never fabricated).
- **Warning:** one message when the signal first crosses 🟡/🔴 during a foreground turn; de-duped
  per non-`ok` window; re-armed on return to `ok`; SB1 (foreground/authorized chat); body-free.
- **No regression:** every existing behavior unchanged (the statusline is byte-for-byte identical
  when no limit signal). All observers are best-effort (RB1) — none can break a turn.

## Test plan
- **Automated (1740 passed, +70 over the 1670 floor):** T1/T2 telemetry capture incl. SB3 no-leak +
  lifecycle reconcile + RB1 (test_engine.py); T3 render %/badge/omit + foreground read
  (test_render.py / test_stream_session.py); T4 warning de-dup/re-arm/unknown-non-event/send-failure
  -rewarn/foreground (15 tests); T5 post/edit/skip-identical/throttle/SB3/collapse/foreground +
  **B2 `/switch` regression lock** (22 tests).
- **Manual / live (T6):** phone-verify on Telegram Web that the activity line edits in place during
  a real subagent run, the 🪙 field renders, and — the one thing offline spikes can't prove — that
  the bot's streaming session actually emits `Task*` (the `tool_use`+`parent` fallback covers it if not).

## Known risks
- **`Task*` emission in the bot's session** is confirmed only at the type level offline; the live
  phone-verify is the proof. Fallback (`tool_use`+`parent_tool_use_id` inference) means the line
  works either way — where to look if subagent names don't appear: `_capture_activity` in
  `adapter_sdk.py`.
- **Edit-rate:** the activity line + statusline both ride the per-chat send gate; the throttle keeps
  the activity line ≲1 edit/sec. If Telegram ever rate-limits, the RB1 swallow drops the edit silently.
- **Precise %:** depends on `RateLimitInfo.utilization` being populated; if the SDK omits it the
  badge fallback engages (still useful).

## Open questions
- Reset-time in the warning, per-subagent token attribution (`TaskUsage`), and `/tokens`·`/agents`
  detail commands are deferred (noted in ADR-010 / design §6 "out of v0").
