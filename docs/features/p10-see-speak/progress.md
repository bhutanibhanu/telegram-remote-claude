# Progress: p10-see-speak

_From design.md · 3 mobile-native I/O features · supervised build. Baseline 1003 tests. Multimodal image input PROVEN by spike._

## Task list
- [x] T1 — 📸 Screenshots/photos → Claude (native multimodal: image content-block threaded into `send`) (97a191d)
- [x] T2 — 🎙️ Voice notes → transcribe (pluggable `TRANSCRIBE_CMD`, injection-safe, graceful-off) → echo → turn (39da452→) (commit after T3)
- [x] T3 — 📎 File send/receive (in: doc→cwd SB2-confined; out: `/get <path>`) (39da452)

_Built; 1090 tests. T1 send-path threading mirrors P9 model-kwarg (text path byte-identical). T3 confinement mutation-probed. T2 subprocess injection-probed + graceful-off (no transcriber installed → live-verify of voice deferred to owner enabling one)._

Legend: `[ ]` todo · `[x]` done (sha) · `[!]` blocked · (T4 rich tool-output rendering DEFERRED)

## Tasks
### T1 — screenshots/photos (multimodal)
- **Files:** `claude_tg/engine/adapter_sdk.py` (`SdkSubstrate.send` + images), `claude_tg/engine/substrate.py` + `engine.py` (`send` signature), `claude_tg/stream_session.py` (thread images through the turn), `claude_tg/bot.py` (PHOTO + image-Document handler).
- **Accept:** allowlisted photo (streaming) → Claude gets `[text(caption), image(base64)]` content block + responds; SB1-gated; size-capped; bytes never logged (SB3); text path unchanged; oneshot fallback (Read-tool or documented streaming-only).
- **Tests:** send builds the content-block dict; SB1; size-cap rejects oversized; no-bytes-logged. Live-verify: Claude actually reads an image.

### T2 — voice (pluggable transcription)
- **Files:** `claude_tg/bot.py` (VOICE/AUDIO handler), a transcription module (pluggable: `TRANSCRIBE_CMD`/API; ffmpeg convert), `claude_tg/config.py`.
- **Accept:** voice + transcriber configured → transcribe → echo transcript → run as a turn; none configured → clean setup message (RB2, no crash); SB1; temp audio cleaned. Wire a local whisper.cpp + live-verify IF a clean install is feasible, else ship pluggable + document.
- **Tests:** mock transcriber → echo + turn; no-transcriber → graceful; SB1; temp cleaned.

### T3 — file send/receive
- **Files:** `claude_tg/bot.py` (Document handler + `/get`), `claude_tg/paths.py` (reuse confinement).
- **Accept:** inbound doc → saved in cwd (SB2 path-confined, size-capped; out-of-root/oversized refused) + offered to Claude; `/get <in-root>` uploads; `/get <out-of-root|missing|oversized>` refused (RB2); SB1.
- **Tests:** inbound save confined + capped; `/get` in-root uploads, out-of-root/missing refused; SB1.

### T4 — verify + merge
Codex QA → live-verify (screenshots: Claude reads an image; files: in/out + confinement; voice: if a transcriber is wired) → merge to `main`.
