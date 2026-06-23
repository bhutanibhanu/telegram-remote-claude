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

- [ ] **(a) Concurrent runs:** `/new a /tmp/p5verify/a`, `/new b /tmp/p5verify/b`. Start a long-ish turn in `a` (e.g. "count slowly to 20, one line each"), `/switch b`, start a turn in `b`. **PASS:** both make progress concurrently (cap=2); `/projects` shows both `running`.

- [ ] **(b) ⭐ Cross-project routing on the LIVE callback path (the headline):** in `a` ask for something needing approval (e.g. "create /tmp/p5verify/a/x.txt"), so `a` parks at a real permission prompt. `/switch b`; start a turn in `b` (now foreground+running). Tap **`a`'s** Allow button. **PASS:** the tap resolves **`a`** (the file write proceeds in `a`), `b` is untouched, no wedge. _Proves id-routing on the real PTB path + the SDK tolerates a held `a` while `b` runs._

- [ ] **(c) ⭐ Two concurrent OPEN permission holds (ADR-001's assumption):** drive both `a` and `b` to a real permission prompt at the same time (start `a`'s approve-needing turn, `/switch b`, start `b`'s approve-needing turn). Both prompts open simultaneously. Tap `b`'s Allow, then `a`'s Allow. **PASS:** each resolves its own project, both turns complete, the SDK did not error on the second concurrent open hold. _If this fails live, it's the ADR-001 caveat — capture the SDK error text._

- [ ] **(d) Background ping + the B1 fix:** with `a` foreground, start a turn in `b` then `/switch a` (so `b` is background). Drive `b` to a permission prompt. **PASS:** `b`'s prompt arrives as a `🔔` ping (not inline) with a WORKING keyboard; tapping it resolves `b`. **B1:** if `b` hits two distinct approvals in quick succession, **both** pings send an answerable keyboard (neither suppressed).

- [ ] **(e) Cap + queue + B2:** with cap=2 and `a`,`b` both running, `/new c …` and send `c` a turn. **PASS:** `c` shows `queued` (one-time `⏳`); when `a` or `b` finishes, `c` dequeues and runs (FIFO). **B2:** while `c` is queued, send `c` a 2nd message → **busy** refusal, not a 2nd queue entry.

- [ ] **(f) Foreground→background flip mid-stream:** start a turn in `a` (foreground), `/switch b` mid-stream. **PASS:** `a` stops rendering inline and pings (`✅`/`⚠️`) on completion instead.

- [ ] **(g) Free-text routing (D5/D9):** reply-to one of `b`'s messages while `a` is active → routes to `b`; `/to a <msg>` routes to `a`; with no reply/`/to`, newest-active wins. **PASS:** never silently misrouted.

- [ ] **(h) Concurrency-aware lifecycle:** `/cancel b` cancels `b`'s turn (and reports it even if `b` was only queued — NB1); `/cancel all`; `/rm` of a running project is refused. **PASS** each.

- [ ] **(i) RB5 rate-gate:** a turn that emits a burst of status/tool lines doesn't flood the chat, and Claude's actual answer text (verbatim) always lands (never starved/dropped).

- [ ] **(j) RB3 restart mid-concurrent-state:** with `a` running and `c` queued, full process kill + restart. **PASS:** on restart no in-flight runs — every project comes back `idle`, the queue is empty; next message to each lazily resumes its own session.

## Result
_(fill on completion: PASS/FAIL per scenario, the live SDK behavior for (c), any evidence screenshots in `/Users/ray/dev/claude-telegram-bot-interactive-prompts/verify-*.png` — disposable. Cosmetic `/segment` auto-linkify is a known P8 polish item, not a P5 bug.)_
