# P10 QA — see & speak

_Cross-model Codex + a fully self-driven live phone-verify, iterated to SHIP. **1098 tests**, ruff/mypy/secret_scan clean. (Per-feature, the engine-send threading + SB2 confinement + subprocess injection were mutation-probed during build.)_

## Live phone-verify — found a real bug the unit suite missed
Drove the real bot end-to-end (screenshots, files, voice via the browser mic). **T1 screenshots ⭐, T3 file in/out, text regression all PASS** (Claude read "MANGO" from real pixels — native multimodal confirmed; "DRAGONFRUIT" from a saved file; `/get` upload + out-of-root + missing refusals). **It caught the voice graceful-off CRASH** (`VOICE_SETUP_MESSAGE` Markdown + `TRANSCRIBE_CMD` underscore → `BadRequest` → user got nothing) — invisible to the 1090-green unit suite because the test fake doesn't run Telegram's entity parser. Exactly why live-verify is mandatory.

## Cross-model Codex — NO_SHIP → 3 blockers (all fixed) → re-QA
1. **SB2 — inbound `.part` symlink escape:** the temp write `dest.name+".part"` could follow a pre-placed in-root symlink out of `ALLOWED_ROOTS` → fixed with `tempfile.mkstemp(dir=dest.parent)` + `os.replace` (random name can't be a pre-placed symlink; replace doesn't write through a dest symlink). Red-green + mutation-probed.
2. **SB3 — raw transcriber stderr logged at DEBUG** → body-free summary only (exit code + length).
3. **Size-cap — voice/audio had none** → pre+post-download cap (reuse `file_max_bytes`).
- NB: `/get` `InputFile` fd leak → context manager.

## Fix round
All of the above + the voice Markdown crash (→ plain text + code-spanned `TRANSCRIBE_CMD` + a balanced-Markdown guard test). 2 bugs red-green + mutation-probed. 1090 → 1098 tests.

## Decision
3 Codex blockers + the live Markdown crash closed → **merge P10 to `main`** (pending the re-Codex SHIP confirm). Voice end-to-end (real transcription) is deferred — it's operator infra (`brew install whisper-cpp` + `TRANSCRIBE_CMD`); the bot ships voice working-when-configured + graceful-off (now correct). Recorded follow-up (from P9, still open): background-ping timing race (P5 scenario f). DEFERRED from P10: rich tool-output rendering (roadmap T4).
