# Feature Handoff: p5-concurrency

## Goal
Background concurrency + notifications for multi-project sessions: multiple projects can run turns concurrently (per-project lock, capped + FIFO-queued), and the ADR-001 **session/run correlation envelope** routes every button tap / answer to the project that actually asked — so a mid-run `/switch` never strands a held turn and a tap never resolves the wrong project. Adds foreground/background notification routing and an RB5 per-chat rate-gated sender.

## Files changed
```
 claude_tg/bot.py                            |  282 +++-
 claude_tg/config.py                         |   92 ++
 claude_tg/render.py                         |  323 +++-
 claude_tg/session_store.py                  |   26 +
 claude_tg/stream_session.py                 | 2056 ++++++++++++++++++++-----
 docs/adr/ADR-005-concurrency-correlation.md |  268 ++++
 docs/features/p5-concurrency/{design,progress}.md + state.json
 tests/test_bot_streaming.py                 |  552 ++++++-
 tests/test_concurrency_matrix.py            | 1036 +++++++++++++  (new)
 tests/test_config.py                        |  123 +-
 tests/test_multi_project.py                 |  111 +-
 tests/test_render.py                        |  244 +++
 tests/test_security_reliability.py          |    9 +-
 tests/test_session_store.py                 |   56 +
 tests/test_skill_launch.py                  |    6 +-
 tests/test_stream_session.py                | 3474 ++++++++++++++++++++++++++-
 19 files changed, 8457 insertions(+), 1071 deletions(-)  (merge-base b589527)
```
_Includes the cross-model-QA fix round (`35285e2`): B1 background distinct-`tool_use_id` prompts no longer throttle-suppressed; B2 a project can't queue behind itself; NB1–3 queued-turn cancel/position/reset correctness._

## How to run
```bash
cd /Users/ray/dev/claude-telegram-bot-p5 && source .venv/bin/activate
# streaming-only feature; one-shot remains the safe default (unchanged)
export ENGINE_MODE=streaming
export CLAUDE_STATE_FILE=/tmp/p5verify/state.json
export CLAUDE_WORKDIR=/tmp/p5verify ALLOWED_ROOTS=/tmp/p5verify
export MAX_CONCURRENT_RUNS=3          # cap; (N+1)th turn queues FIFO
python main.py
```
Exercise: `/new a /tmp/p5verify/a`, `/new b /tmp/p5verify/b`; start a long turn in `a`, `/switch b`, start a turn in `b` (both run); tap `a`'s permission/ask prompt while `b` is foreground → resolves `a`. `/projects` shows per-project status (idle/running/awaiting_*/queued). `/to <name> <msg>`, reply-to-route, `/cancel <name|all>`.

## Expected behavior (locked decisions D1–D9, ADR-005)
- **D1/D2** Per-project turn lock; `/switch` & `/new` are free during runs (no stop-other). Concurrent turns across projects.
- **D2/⭐ correlation envelope (T2):** relay-layer pending-request index `{tool_use_id → (project, kind)}` on `_ChatState`; `resolve_callback`/cancel route **by id**, not by foreground `_active_engine` (retired from the resolve path). A tap for A's id resolves A even when B is foreground+running; forged/foreign/stale `callback_data` resolves nothing. `callback_data` wire format unchanged (SB1 authn + decode preserved).
- **D4** Foreground turn renders inline; background turn is silent inline and only **pings** (`🔔` attention / `✅`/`⚠️` terminal) — body-free (SB3). A turn that flips foreground→background mid-stream switches to pings.
- **D6** `MAX_CONCURRENT_RUNS` cap (default 3); (N+1)th turn → `queued` + one-time `⏳` notice; dequeues FIFO on slot free; slot accounting leak-safe (finally-release; transfer to waiter).
- **D7** Per-turn state lifted to `_ProjectRuntime` + a `ProjectStatus` enum.
- **D8** Per-chat `ChatSendGate`: rate-bounded sends, **verbatim-priority** (verbatim never starved/dropped behind status backlog), per-project status coalescer.
- **D5/D9** Free-text routing: reply-to / `/to <name>` / newest-wins, never silent misroute; `/cancel name|all`; `/rm` of a running project refused (or drains queued waiter — no zombie run).
- **RB3** On restart: no in-flight runs; queue/run-status/index are in-memory only; lazy per-project resume on next message. **One-shot mode behaves exactly as pre-P5.**

## Test plan
- **737 automated** (712 baseline + 17 matrix + 8 QA-fix regressions), `ruff`/`mypy`/`secret_scan` clean. Per-task independent reviewer on T1–T10 (each mutation-probed); `tests/test_concurrency_matrix.py` pins the cross-cutting acceptance end-to-end through the real `on_callback` path with mock engines. Phase-level Verifier subagent + cross-model Codex (Codex caught B1/B2; both fixed + re-reviewed AGREE).
- **Needs live check (T11):** real Telegram→PTB→bot callback path + real-Claude **two-project-concurrent** holds — ADR-001's "SDK tolerates multiple concurrent open permission holds" assumption is only authoritatively provable live (engine.resolve probes bypass PTB; unit tests use mock engines). This is the remaining task.

## Known risks
- **Concurrent open holds vs the live SDK** — the headline assumption P5 leans on; mitigated by id-routing but must be phone-verified (T11). Look here first if a live tap mis-resolves or a held turn wedges.
- **Rate-gate timing** — `ChatSendGate` verbatim-priority logic (the T8 follow-up fix) is timing-sensitive; covered by a 2000-trial fuzz + mutation tests, but watch real-world send ordering under burst.
- **Slot accounting** — leak would strand the queue; covered by the no-leak/FIFO matrix test, but it's the highest-consequence concurrency invariant.

## Open questions
- Live SDK exception text for a torn/aged resume under concurrency (the `_is_resume_failure` heuristic is text-based) — deferred from P4, revisit in P6 security/hardening.
- None blocking ship of the code; T11 live verify is the gate before merge.
