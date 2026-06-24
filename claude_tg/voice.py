"""P10 T2 — pluggable, graceful-off voice transcription.

A voice note is an operator-first input on a phone; transcribing it is **operator-provided
infrastructure**, not a hard dependency of the bot. This module is the pluggable seam: the
operator sets ``TRANSCRIBE_CMD`` (a shell-command TEMPLATE) and the bot runs it over a voice
note it downloaded; with nothing configured, voice is **gracefully off** (the handler sends a
clean "set it up" message — never a crash). No transcriber is bundled.

The ``TRANSCRIBE_CMD`` contract
-------------------------------
``TRANSCRIBE_CMD`` is a command *template* with two placeholders:

* ``{audio}`` — the path to the input audio file (a temp file the bot controls). Substituted
  as a SINGLE argv token, so a path with spaces/metacharacters is one argument, never re-split.
* ``{out}``   — an output **basename** (no extension). Optional. If the template mentions
  ``{out}``, the transcript is read from the produced ``<out>.txt`` (the whisper.cpp
  ``-otxt -of {out}`` convention); if it does NOT, the transcript is read from the command's
  **stdout**.

Examples::

    # whisper.cpp (writes <out>.txt):
    TRANSCRIBE_CMD=whisper-cli -m /models/ggml-base.en.bin -f {audio} -otxt -of {out}

    # an API/CLI that prints the transcript to stdout:
    TRANSCRIBE_CMD=my-stt --file {audio}

Injection-safety
----------------
The template is parsed ONCE with :func:`shlex.split` (so the operator's OWN tokens/quoting are
honored) and then run via :func:`asyncio.create_subprocess_exec` — **never** ``shell=True`` and
**never** a string interpolated into a shell. The ``{audio}`` / ``{out}`` placeholders are
substituted **after** the split, each as a complete argv token, so even if the temp path
contained shell metacharacters (it never does — the bot names the temp file) they could not be
re-tokenized or interpreted by a shell. There is no shell anywhere in this path.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path

log = logging.getLogger(__name__)

#: The placeholder for the input audio path in a ``TRANSCRIBE_CMD`` template.
AUDIO_PLACEHOLDER = "{audio}"
#: The placeholder for the output basename in a ``TRANSCRIBE_CMD`` template (optional —
#: present → read ``<out>.txt``; absent → read stdout).
OUT_PLACEHOLDER = "{out}"


class TranscriptionUnavailable(Exception):
    """``TRANSCRIBE_CMD`` is unset/empty → voice is gracefully off (not an error).

    The handler maps this to the one-time "set up a transcriber" message — NOT a failure
    banner. Distinct from :class:`TranscriptionError` so graceful-off and a real failure get
    different operator-facing copy.
    """


class TranscriptionError(Exception):
    """The configured transcriber ran but produced no usable transcript (RB1/RB2).

    Covers: a malformed/empty template, the transcriber binary missing, a non-zero exit, a
    timeout, or an empty result. The message is bot-authored + body-free (it never embeds the
    transcriber's raw stderr, which could carry a path/secret) — the raw detail goes only to
    the local debug log.
    """


def build_transcribe_argv(template: str, *, audio_path: str, out_base: str) -> list[str]:
    """Turn a ``TRANSCRIBE_CMD`` template into a safe argv list (pure; no I/O).

    Splits ``template`` with :func:`shlex.split` (honoring the operator's quoting), then
    replaces the ``{audio}`` / ``{out}`` placeholders in each resulting token with
    ``audio_path`` / ``out_base``. Because substitution happens AFTER the split — and each
    placeholder occupies a whole token — the substituted path is always a single argv element,
    never re-tokenized (the injection-safety guarantee; see the module docstring).

    Raises :class:`TranscriptionError` if the template is empty/whitespace, fails to parse
    (unbalanced quotes), or splits to nothing — a misconfiguration is a clean error, never a
    crash. A template with no ``{audio}`` is allowed (some CLIs read a fixed path), but is
    almost always a mistake; the caller passes a temp ``audio_path`` regardless.
    """
    try:
        tokens = shlex.split(template)
    except ValueError as exc:  # unbalanced quotes etc.
        raise TranscriptionError(f"TRANSCRIBE_CMD is not a valid command: {exc}") from exc
    if not tokens:
        raise TranscriptionError("TRANSCRIBE_CMD is empty")
    argv = [
        tok.replace(AUDIO_PLACEHOLDER, audio_path).replace(OUT_PLACEHOLDER, out_base)
        for tok in tokens
    ]
    return argv


def template_uses_out(template: str) -> bool:
    """True if ``template`` references ``{out}`` (→ read ``<out>.txt``; else read stdout)."""
    return OUT_PLACEHOLDER in template


async def _run(argv: list[str], *, timeout: float) -> tuple[int, bytes, bytes]:
    """Run ``argv`` (no shell), bounded by ``timeout``; return (rc, stdout, stderr).

    Patched in tests. On timeout the process is killed and a :class:`TranscriptionError` is
    raised (the caller maps it to a clean message). ``stdin`` is closed (the transcriber reads
    a file, not stdin) so it can never hang waiting for input.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        raise TranscriptionError("the transcriber timed out") from exc
    return proc.returncode or 0, out or b"", err or b""


async def transcribe(
    *, template: str, audio_path: str, work_dir: str, timeout: float
) -> str:
    """Transcribe ``audio_path`` via the configured ``TRANSCRIBE_CMD`` template.

    Returns the transcript text (stripped, guaranteed non-empty). Raises:

    * :class:`TranscriptionUnavailable` when ``template`` is empty/whitespace — voice is OFF.
    * :class:`TranscriptionError` for every real failure — bad template, binary not found,
      non-zero exit, timeout, or an empty/missing result.

    Reads the transcript from the produced ``<out>.txt`` when the template references
    ``{out}`` (the whisper.cpp convention), else from the command's stdout. The ``{out}``
    basename is placed inside ``work_dir`` (the bot's per-turn temp dir), so any ``.txt`` the
    transcriber writes is cleaned with the audio (RB1). SB3: the raw stderr is logged at DEBUG
    only (never echoed to the chat) and the audio/transcript bytes are never logged here.
    """
    if not template or not template.strip():
        raise TranscriptionUnavailable()

    out_base = str(Path(work_dir) / "transcript")
    argv = build_transcribe_argv(template, audio_path=audio_path, out_base=out_base)

    try:
        rc, stdout, stderr = await _run(argv, timeout=timeout)
    except FileNotFoundError as exc:
        # The transcriber binary is not on PATH (a misconfiguration) — clean error (RB2).
        raise TranscriptionError(
            f"the transcriber command was not found: {argv[0]!r}"
        ) from exc
    except OSError as exc:  # permission denied / not executable / etc.
        raise TranscriptionError(f"couldn't run the transcriber: {exc}") from exc

    if rc != 0:
        # SB3: the raw stderr can carry file paths — log it body-free at DEBUG, never echo it.
        log.debug(
            "transcriber exited %d; stderr=%r",
            rc,
            stderr.decode("utf-8", "replace")[:500],
        )
        raise TranscriptionError(f"the transcriber failed (exit {rc})")

    if template_uses_out(template):
        out_txt = Path(out_base + ".txt")
        try:
            text = out_txt.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise TranscriptionError(
                "the transcriber produced no output file"
            ) from exc
    else:
        text = stdout.decode("utf-8", "replace")

    text = text.strip()
    if not text:
        raise TranscriptionError("the transcriber returned an empty transcript")
    return text
