# P10 — See & Speak (multimodal + voice + files)

_Roadmap-v2 phase 2 (see `docs/roadmap-v2.md`). The mobile-native inputs the owner asked for. Delta on P0–P9 (`main`). Spike PROVEN: native image input works through `claude-agent-sdk==0.2.105` (a streamed user dict with `[text, image(base64)]` content blocks — Claude saw "BANANA" from a PNG, no Read tool)._

## Problem
Today the bot is text-only in/out. A phone is camera- and voice-first; the highest-friction tasks ("here's the error on my screen", dictating a long prompt, pulling a generated patch out) are exactly what the bot can't do. Add the three I/O primitives.

## Success
- 📸 Send a **photo/screenshot** → Claude SEES it (multimodal) and acts; the caption is the prompt.
- 🎙️ Send a **voice note** → transcribed → run as a turn (transcript echoed so you see what it heard).
- 📎 Send a **file** → saved (path-confined) into the active project + offered to Claude; pull a file OUT with `/get <path>`.
- Gates green (1003 floor); SB1/SB2/SB3 intact; each verifiable feature live-verified on the real bot.

## Anti-goals
No engine/permission/concurrency changes beyond threading attachments into `send`. No new HARD dependency (voice transcription is pluggable + graceful-off). Don't break the text path. Streaming-mode focus (oneshot multimodal is a bigger lift — fall back to the Read-tool path / document streaming-only for images).

## Features

### T1 — 📸 Screenshots/photos → Claude (native multimodal)
- Telegram `PHOTO` (+ image-`Document`) handler: download the image, base64-encode, thread it into the turn as a multimodal content block. Per the spike: extend `SdkSubstrate.send` (and the `Substrate`/`Engine.send` signature) to accept optional `images`; when present, `client.query(<async-iterable>)` yields a `user` dict whose `content` is `[{"type":"text",...caption/prompt...}, {"type":"image","source":{"type":"base64","media_type":...,"data":...}}]`. Receive loop unchanged.
- The photo's **caption** is the prompt (default a sensible "look at this image and …" if no caption). Size-cap the image (reject > a few MB); set `media_type` from the file. Operator-supplied pixels (SB3: pixels can't be "body-free"-scrubbed but they're operator-provided — acceptable; never log the bytes).
- **Oneshot:** images need `--input-format stream-json` (bigger change) — for oneshot, fall back to saving the image in cwd + a "Read this image at <path>" prompt (Claude's Read renders images), or document images as streaming-only. Pick the cleaner; don't block T1 on oneshot.
- **Accept:** WHEN an allowlisted chat sends a photo (streaming), Claude receives it as an image block + responds to it + the caption; SB1-gated; size-capped; the text path is unchanged.
- **Tests:** the send path builds the `[text, image]` content-block dict for a photo; SB1-gated; size-cap rejects an oversized image; no image bytes logged. (Live-verify proves Claude actually sees it.)

### T2 — 🎙️ Voice notes → transcribe → turn (pluggable)
- Telegram `VOICE`/`AUDIO` handler: download the `.ogg`/opus, convert via `ffmpeg` (present) to a transcriber-friendly format if needed, transcribe via a **pluggable backend**, echo the transcript back (quoted, so the operator sees what was heard), then fire it as a normal turn.
- **Transcription backend (pluggable, no hard dep):** a `TRANSCRIBE_CMD` config (a shell command template, e.g. `whisper-cli -m <model> -f {audio} -otxt`) OR an API option; if NONE configured → a clean "voice transcription isn't set up — install a transcriber (e.g. `brew install whisper-cpp`) and set TRANSCRIBE_CMD, or type your message" message (graceful, RB2). During build: if a local transcriber can be cleanly installed (whisper.cpp + a small model), wire it as the default + live-verify; else ship pluggable + document.
- **Accept:** WHEN a voice note arrives + a transcriber is configured, it SHALL transcribe → echo → run as a turn; WHEN none configured, a clean setup message (no crash). SB1-gated; the audio file is temp + cleaned; transcript echoed before/with the run.
- **Tests:** with a MOCK transcriber, voice → echo + turn; no-transcriber → graceful message; SB1-gated; temp audio cleaned. (Live-verify only if a real transcriber is wired.)

### T3 — 📎 File send/receive
- **In:** `Document` (non-image) handler: save into the active project's cwd (path-confined via `paths.resolve_within_roots`, size-capped) + a prompt "I've added <file> — …" (the caption or a default), so Claude can Read it. SB2-confined write; reject oversized; never auto-execute.
- **Out:** `/get <path>` — resolve `<path>` within `ALLOWED_ROOTS` (SB2), size-cap, and `send_document` it to the chat. Refuse out-of-root / oversized / missing (RB2). SB1-gated.
- **Accept:** an inbound document saves inside roots (out-of-root/oversized refused) + is offered to Claude; `/get <in-root file>` uploads it; `/get <out-of-root>` refused. SB1/SB2.
- **Tests:** inbound save path-confined + size-capped; `/get` in-root uploads, out-of-root/missing/oversized refused; SB1-gated.

## Build order
T1 (screenshots — headline, proven mechanism) → T3 (files — Telegram-side, surest) → T2 (voice — pluggable; wire+verify a transcriber if feasible) → verify + Codex QA + live-verify + merge. (T4 rich tool-output rendering from the roadmap is DEFERRED to keep this phase tractable — noted as a follow-up.)

## Inherited facts / constraints
- Gates (worktree `.venv`): `pytest`, `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`. Floor: 1003 tests.
- SB1 on every new handler/command; SB2 path-confine every inbound save + `/get`; SB3 never log image/file bytes; size-cap all attachments; temp files cleaned (RB1). Streaming is the multimodal path; one bot per token; don't rotate the token.
- Multimodal plumb point: `claude_tg/engine/adapter_sdk.py` `SdkSubstrate.send` + the `Substrate`/`Engine.send` signature + `stream_session` call sites. Telegram handlers: `claude_tg/bot.py`.
