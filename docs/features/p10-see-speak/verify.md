# P10 Live Phone-Verify Checklist — see & speak

_Drive the real bot (logged-in Telegram Web) once Codex QA is SHIP. Fresh `/tmp/p10verify` sandbox, streaming, gate-on. Screenshots + files are fully verifiable; voice verifies only the graceful-off path (no transcriber installed)._

## Setup
`mkdir -p /tmp/p10verify` then `cd /Users/ray/dev/claude-telegram-bot-p10 && CLAUDE_STATE_FILE=/tmp/p10verify/state.json CLAUDE_WORKDIR=/tmp/p10verify ALLOWED_ROOTS=/tmp/p10verify ENGINE_MODE=streaming .venv/bin/python -m claude_tg > /tmp/p10verify/bot.log 2>&1 &` — clean startup, no Conflict. One instance per token; stop after.

## Scenarios (screenshot each to `verify-p10-<tag>.png`)
- [ ] **T1 ⭐ screenshot → Claude SEES it:** create a small image with a known word (e.g. a PNG containing "MANGO" — or send any screenshot with readable text), send it to the bot with caption "what word is in this image?". **PASS:** Claude's reply correctly reads the word → proves native multimodal (Claude saw the pixels). Also send a bare photo (no caption) → it still gets described. An oversized image (>5MB) → clean "too large" refusal.
- [ ] **T3 file IN:** send a small `.txt`/`.py` document (caption "summarize this"). **PASS:** the bot saves it into the project cwd + runs a turn where Claude reads/summarizes it; the file is on disk inside `/tmp/p10verify`.
- [ ] **T3 file OUT (`/get`):** after Claude (or you) creates a file in the project, `/get <that file>` → the bot uploads it to the chat as a document. `/get /etc/hosts` (out-of-root) → refused ("outside the permitted roots"); `/get nope.txt` (missing) → clean refusal.
- [ ] **T2 voice (graceful-off):** send a voice note → bot replies the clean setup message ("🎙️ Voice transcription isn't set up… `brew install whisper-cpp` … set TRANSCRIBE_CMD…") — NO crash, no turn. (Full transcription deferred — needs a transcriber installed.)
- [ ] **Regression:** a plain text message still runs a normal turn (the new photo/voice/document handlers don't break text).

## Result — live-verify 2026-06-24 (real bot, browser)
- **T1 ⭐ screenshot → Claude SEES it: PASS** — `mango.png` + "what word is in this image?" → "The word in the image is MANGO." (`· 1 turn · $0.03`); bare image also described. Native multimodal confirmed (pixels, not a path). `verify-p10-t1-mango.png`, `verify-p10-t1-bare-image.png`.
- **T3 file IN: PASS** — `note.txt` ("what is the secret fruit?") → saved into the project + Claude read it → "DRAGONFRUIT" (`· 2 turns`). `verify-p10-t3-file-in.png`.
- **T3 file OUT (`/get`): PASS** — `/get note.txt` uploaded the doc; `/get /etc/hosts` → "Path not allowed (outside the permitted roots)"; `/get nope.txt` → "No such file". `verify-p10-t3-get.png`.
- **Regression: PASS** — plain text "5+5?" → "10".
- **T2 voice graceful-off: was BROKEN → FIXED.** The live verify (real voice note via the browser mic) hit a `BadRequest` crash — `VOICE_SETUP_MESSAGE` sent as Markdown, `TRANSCRIBE_CMD` underscore unterminated → user got nothing. **Fixed:** now plain text (+ code-spanned token) + a balanced-Markdown guard test. Deterministic (plain text can't parse-error); unit-verified; live re-check deferred (was `verify-p10-t2-voice-bug.png`). Full transcription still needs a transcriber installed (owner enables).

### Findings from QA+verify (all fixed in the fix round)
The live-verify + cross-model Codex together caught 4 real issues — all closed: (A) the voice-setup Markdown crash; (B) inbound `.part` symlink-escape (SB2) → `mkstemp`+`os.replace`; (C) raw transcriber stderr logged (SB3) → body-free; (D) voice/audio missing size-cap → pre+post cap; + `/get` fd context-manager. The C2 path-confinement gate also correctly caught a model attempting an out-of-root write (gate doing real work).

**Bottom line:** the headline (Claude SEES screenshots) + files in/out + confinement + text regression all PASS live; voice graceful-off fixed (deterministic). Live-verify earned its keep — it found the Markdown crash the 1090-green unit suite missed (fake bot ≠ Telegram's entity parser).
