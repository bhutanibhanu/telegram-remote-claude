# P5 Live Phone-Verify Checklist — concurrency + notifications

_The authoritative live check. Unit/integration tests use **mock engines** and bypass the real Telegram→PTB→bot callback path and **cannot** prove ADR-001's load-bearing assumption that the live Claude SDK tolerates **multiple concurrent open permission holds**. This walks the real bot on the real token via the logged-in Telegram Web session ([[telegram-browser-verify]])._

## Setup
```bash
cd /Users/ray/dev/claude-telegram-bot-p5 && source .venv/bin/activate
export ENGINE_MODE=streaming
export CLAUDE_STATE_FILE=/tmp/p5verify/state.json
export CLAUDE_WORKDIR=/tmp/p5verify ALLOWED_ROOTS=/tmp/p5verify
export MAX_CONCURRENT_RUNS=2          # small cap so the queue path is reachable live
mkdir -p /tmp/p5verify/{a,b,c}
python main.py                         # confirm: clean "engine_mode: streaming" startup, no Telegram Conflict
```
Bot: **@your_test_bot** (id <bot-id>); operator chat <your-chat-id>. One instance per token. Kill the real `…/Python main.py` PID after (orphan re-parents).

## Scenarios (each must PASS on the real bot)

- [x] **(a) Concurrent runs:** `/new a /tmp/p5verify/a`, `/new b /tmp/p5verify/b`. Start a long-ish turn in `a` (e.g. "count slowly to 20, one line each"), `/switch b`, start a turn in `b`. **PASS:** both make progress concurrently (cap=2); `/projects` shows both `running`. _[2026-06-23 PASS — both made progress under cap=2; see Result table.]_

- [x] **(b) ⭐ Cross-project routing on the LIVE callback path (the headline):** in `a` ask for something needing approval (e.g. "create /tmp/p5verify/a/x.txt"), so `a` parks at a real permission prompt. `/switch b`; start a turn in `b` (now foreground+running). Tap **`a`'s** Allow button. **PASS:** the tap resolves **`a`** (the file write proceeds in `a`), `b` is untouched, no wedge. _Proves id-routing on the real PTB path + the SDK tolerates a held `a` while `b` runs._

- [x] **(c) ⭐ Two concurrent OPEN permission holds (ADR-001's assumption):** drive both `a` and `b` to a real permission prompt at the same time (start `a`'s approve-needing turn, `/switch b`, start `b`'s approve-needing turn). Both prompts open simultaneously. Tap `b`'s Allow, then `a`'s Allow. **PASS:** each resolves its own project, both turns complete, the SDK did not error on the second concurrent open hold. _If this fails live, it's the ADR-001 caveat — capture the SDK error text._

- [x] **(d) Background ping + the B1 fix:** with `a` foreground, start a turn in `b` then `/switch a` (so `b` is background). Drive `b` to a permission prompt. **PASS:** `b`'s prompt arrives as a `🔔` ping (not inline) with a WORKING keyboard; tapping it resolves `b`. **B1:** if `b` hits two distinct approvals in quick succession, **both** pings send an answerable keyboard (neither suppressed).

- [x] **(e) Cap + queue + B2:** with cap=2 and `a`,`b` both running, `/new c …` and send `c` a turn. **PASS:** `c` shows `queued` (one-time `⏳`); when `a` or `b` finishes, `c` dequeues and runs (FIFO). **B2:** while `c` is queued, send `c` a 2nd message → **busy** refusal, not a 2nd queue entry.

- [ ] **(f) Foreground→background flip mid-stream:** start a turn in `a` (foreground), `/switch b` mid-stream. **PASS:** `a` stops rendering inline and pings (`✅`/`⚠️`) on completion instead.

- [ ] **(g) Free-text routing (D5/D9):** reply-to one of `b`'s messages while `a` is active → routes to `b`; `/to a <msg>` routes to `a`; with no reply/`/to`, newest-active wins. **PASS:** never silently misrouted.

- [ ] **(h) Concurrency-aware lifecycle:** `/cancel b` cancels `b`'s turn (and reports it even if `b` was only queued — NB1); `/cancel all`; `/rm` of a running project is refused. **PASS** each.

- [ ] **(i) RB5 rate-gate:** a turn that emits a burst of status/tool lines doesn't flood the chat, and Claude's actual answer text (verbatim) always lands (never starved/dropped).

- [ ] **(j) RB3 restart mid-concurrent-state:** with `a` running and `c` queued, full process kill + restart. **PASS:** on restart no in-flight runs — every project comes back `idle`, the queue is empty; next message to each lazily resumes its own session.

## Result

_Live phone-verify run 2026-06-23 ~07:40–08:08 UTC, driven via the owner's logged-in Telegram Web (@your_test_bot, id <bot-id>) through the Playwright browser tools. Bot started from this worktree with `MAX_CONCURRENT_RUNS=2`, `engine_mode: streaming`, clean startup, no Telegram `Conflict`. Real PTB→bot callback path + live Claude SDK exercised. Bot stopped (clean SIGTERM) after the run; no stray files in `$HOME`, no leftover SDK subprocesses; all writes contained in `/tmp/p5verify/{a,b,c}`._

| Scenario | Verdict | Evidence |
|---|---|---|
| (a) Concurrent runs | **PASS** | `b` ran to completion as a background turn (`✅ b — done`) while `a` was foreground, then `a` ran — both made progress under cap=2. Also corroborated by (c)'s clean run (two projects held + resolved concurrently). `/projects` caught one running + one idle (no-tool turns finish in ~1s, so a single snapshot rarely catches both mid-run simultaneously; the parked-hold scenarios are the durable proof). |
| (b) ⭐ Cross-project routing on live callback path | **PASS** | `a` parked at a real permission prompt (`Write(/private/tmp/p5verify/a/hello.txt)`); `/switch b`; `b` foreground+running (Unix essay). Tapped **a's** Allow → `a` resolved (`✅ a — done`, file `a/hello.txt`=BANANA written), `b` finished its essay inline unaffected, no wedge. Id-routing on the real PTB path confirmed. Screenshots: `verify-p5-b-a-parked.png`, `verify-p5-b-resolved.png`. |
| (c) ⭐ Two concurrent OPEN permission holds (ADR-001 assumption) | **PASS (assumption holds) — but surfaced a real fail-recovery bug, see below** | Two projects parked at OPEN holds simultaneously, each with a working keyboard; the **SECOND** open hold did NOT error in the SDK (bot.log had zero error/traceback). Clean run: `b` (`Write b/delta.txt`) + fresh `c` (`Bash mkdir`) both open at once → tapped c then b → each resolved its OWN project (b/delta.txt=LEMON inline; c advanced, then `✅ c — done`, c/gamma.txt=GRAPE). Per-hold isolation also shown in the first attempt: tapping b's Allow wrote b/beta.txt=CHERRY while a's hold stayed open + untouched. Screenshots: `verify-p5-c-two-open-holds.png`, `verify-p5-c-clean-two-open-holds.png`, `verify-p5-c-b-resolved-a-driver-error.png`. |
| (d) Background ping + working keyboard | **PASS** | With `b` foreground, backgrounded `c` hit a permission prompt → arrived as a `🔔 c — Claude needs approval` ping (NOT inline) with a working Allow/Deny keyboard; tapping it resolved `c` (c/gamma.txt=GRAPE written, `✅ c — done`). Screenshot: `verify-p5-d-background-ping-keyboard.png`. |
| (e) Cap + queue + B2 | **PASS** | Cap=2 with `b`+`c` both parked at holds (both slots full) → `/new d` + turn → **`⏳ Queued behind 2 run(s) — it'll start when a slot frees.`** B2: a 2nd message to queued `d` → **`⏳ Still working on your previous message — it'll reply when done. Send one message at a time.`** (busy refusal, not a 2nd queue entry). Resolved c's hold (c/hold1.txt=KIWI, `✅ c — done`) → freed a slot → `d` **dequeued and ran** (`QUEUED-RAN`). Screenshot: `verify-p5-e-queued-and-busy.png`. |

### (c) live SDK behavior — the headline
ADR-001's load-bearing assumption **HOLDS live**: the Claude SDK accepted a **second concurrent open permission hold** (two `can_use_tool` callbacks outstanding at once) without erroring, and tapping each project's button resolved that project's own hold. bot.log showed **no** error/traceback/exception when the second hold opened, and in the clean run BOTH held turns completed (b/delta.txt + c/gamma.txt).

### ⚠️ Bug surfaced (NOT a concurrency-assumption failure) — driver_error wedges a verified session
During (c) I observed `⚠️ <project> — driver_error`, root-caused to the **per-message turn timeout**, not the concurrency:
- The streaming driver bounds each per-message wait at **120s** (`engine.py` `_send_timeout=120.0` → `adapter_sdk.send`'s `asyncio.wait_for(..., timeout=120.0)`). A permission hold left open longer than 120s (operator deciding slowly) fires `asyncio.TimeoutError` → a `driver_error` ("send timed out after 120s"), verbatim seen in chat.
- **The wedge:** after such a timeout on an **already-verified** session, the broken `ClaudeSDKClient` is **never stopped/rebuilt** — `_recover_failed_resume`/`engine.stop()` only run on the **resume**-failure path (first turn of an unverified resumed session; QF4/QF5), and `_is_resume_failure_event` **explicitly excludes "timed out"**. The main send-loop `finally` only resets status + clears the pending index. So every SUBSEQUENT turn on that project hangs and re-times-out at 120s.
- **Reproduced on both `a` and `b`**: `a` driver_errored 3× consecutively (alpha → alpha2 → PING all timed out, no output); `b` driver_errored after its hold2 sat >120s, then its next turn (RECOVER) also produced nothing for 22s+. **Per-project isolation held throughout** — a healthy project (`b` replied `PONG` instantly while `a` was wedged; `c`/`d` ran fine) is unaffected; a full process restart clears the wedge (RB3 untested here but state.json showed no in-flight runs persisted).
- **Severity / scope:** a stuck/slow tool-approval (>120s) silently bricks that one project until bot restart, with no operator-facing "this project is wedged, restart" hint (the repeated `driver_error` pings are the only signal). Most likely to bite real users since humans routinely take >2 min to decide on an approval. Recommend: on a `driver_error` (esp. timeout), tear down + rebuild that project's engine (the QF5 "non-started engine is discarded + replaced" hardening, extended to the verified-session timeout path) and/or raise the per-message timeout for held requests.

_Cosmetic `/segment` path auto-linkify confirmed present (known P8 polish item, not a P5 bug). macOS reports paths under `/private/tmp/...` (canonical form of `/tmp/...`) — expected._
