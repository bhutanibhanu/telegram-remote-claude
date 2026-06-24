#!/usr/bin/env bash
# Voice-transcription helper for the bot's TRANSCRIBE_CMD (P10 "see & speak").
#
# Usage (as set in TRANSCRIBE_CMD):
#   TRANSCRIBE_CMD=bash /ABS/PATH/deploy/transcribe-whisper.sh {audio} {out}
#
# The bot substitutes {audio} (the downloaded voice note, e.g. Telegram .ogg/opus) and
# {out} (an output basename in a temp dir). This script:
#   1. converts {audio} to 16 kHz mono WAV with ffmpeg (whisper.cpp requires that format),
#   2. runs whisper.cpp (`whisper-cli`) to produce {out}.txt (the bot reads that),
#   3. removes the temp WAV.
#
# Requirements (one-time):
#   brew install ffmpeg whisper-cpp
#   mkdir -p ~/.cache/whisper-models
#   curl -fsSL -o ~/.cache/whisper-models/ggml-base.en.bin \
#     https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
#
# Model is configurable via WHISPER_MODEL (default below). Everything runs LOCALLY —
# the audio never leaves the machine.
set -euo pipefail

audio="$1"
out="$2"
model="${WHISPER_MODEL:-$HOME/.cache/whisper-models/ggml-base.en.bin}"
wav="${out}.wav"

if [ ! -f "$model" ]; then
  echo "transcribe-whisper: model not found at $model (set WHISPER_MODEL or download a ggml model)" >&2
  exit 3
fi

# Convert to the format whisper.cpp needs (16 kHz mono WAV). -nostdin so it never blocks.
ffmpeg -nostdin -y -loglevel error -i "$audio" -ar 16000 -ac 1 -f wav "$wav"

# Transcribe → writes "${out}.txt". -nt = no timestamps, -l en = English.
whisper-cli -m "$model" -f "$wav" -otxt -of "$out" -nt -l en >/dev/null 2>&1

rm -f "$wav"
