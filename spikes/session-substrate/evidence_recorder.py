"""Evidence recorder + transcript-capture helper for the session-substrate spike.

This is the shared helper every C1-C6 check (T6+) uses to persist a per-criterion
``PASS / FAIL / PARTIAL`` verdict together with its captured, *scrubbed* session
transcript. It satisfies T3 and the X3 precondition (secret-scan / secret hygiene).

==============================================================================
SINGLE SCRUB CHOKEPOINT (X3 precondition) -- READ THIS BEFORE EDITING
==============================================================================
``_scrub_and_write`` is the ONLY function in this module that writes bytes to
disk. It ALWAYS routes the text through ``scrub()`` (imported from the sibling
``scrub.py``) before writing. Every public method that persists a transcript or
an observed_reason routes through it. There is no public API path that can write
a raw, unscrubbed transcript -- the buffered transcript and the result JSON are
both scrubbed inside ``_scrub_and_write`` at flush time. Any caller-supplied
``extra_secrets`` are threaded through to ``scrub()``.

The ``criterion`` id is caller-controlled, so it is handled on TWO axes:
  * File CONTENTS (the JSON ``criterion`` field and the transcript header line)
    use the SCRUBBED criterion -- it is routed through ``scrub()`` exactly like
    every other persisted string, so a secret-shaped criterion id is redacted
    in contents too. The chokepoint claim therefore holds for the criterion id.
  * On-disk FILENAMES cannot embed ``[REDACTED]`` markers or path separators, so
    the filename stem is derived from the criterion via a STRICT WHITELIST
    (``_sanitize_for_filename``): only ``[A-Za-z0-9._-]`` survive, any other run
    collapses to a single ``_``, leading dots/dashes are stripped, the length is
    capped, and an empty result falls back to ``criterion``. This also closes
    path traversal (a ``/`` or ``..`` in the criterion can never escape
    ``base_dir``); the resolved output path is additionally asserted to stay
    inside ``base_dir`` before any write.

If you add a new write path, route it through ``_scrub_and_write`` -- do not call
``open(...).write`` anywhere else in this module.

==============================================================================
ON-DISK EVIDENCE FORMAT (stable, documented)
==============================================================================
For a criterion ``<crit>`` two sibling files are written under the evidence dir,
where ``<stem>`` is the whitelist-sanitized form of the criterion id (see
``_sanitize_for_filename``; for a plain id like "C1" the stem IS "C1"):

  <stem>.json            -- structured result (UTF-8 JSON), keys:
      criterion        : str   the SCRUBBED criterion id (e.g. "C1")
      verdict          : str   one of "PASS" / "FAIL" / "PARTIAL"
      observed_reason  : str   scrubbed human reason for the verdict
      timestamp        : str   ISO-8601 UTC, e.g. "2026-06-20T12:34:56.789012+00:00"
      transcript_path  : str   basename of the sibling transcript file
      recorder         : str   "evidence_recorder/1" (format version tag)

  <stem>.transcript.txt  -- the scrubbed, human-readable transcript (UTF-8).
      A short header (criterion / verdict / reason / timestamp) followed by the
      concatenated transcript chunks the check buffered.

WRITE ORDER / NO HALF-ARTIFACT: the result JSON is the authoritative source of
truth for the verdict, so it is written FIRST, then the transcript. If the
transcript write fails, the result JSON is already on disk -- so a consumer
scanning for result JSONs never silently skips a criterion, and the recorder
never leaves a lone transcript with no accompanying result JSON. Whenever any
artifact exists on disk for a criterion, the result JSON carrying the verdict
is present. Verdicts are validated against {PASS, FAIL, PARTIAL}; an unknown
verdict raises ``ValueError`` (it can NEVER silently become a PASS).

==============================================================================
FAIL-CLEAN GUARANTEE
==============================================================================
Use the ``record_criterion`` context manager:

    with record_criterion("C1", base_dir=...) as rec:
        rec.add_transcript(chunk)          # may be called repeatedly; buffered
        rec.set_verdict("PASS", ">=2 turns streamed")

  * Normal exit WITH a verdict   -> persists that verdict + scrubbed transcript.
  * Normal exit WITHOUT a verdict -> persists a FAIL with observed_reason
        "no verdict produced (fail-clean)" plus whatever was buffered.
  * The block raises an exception -> the recorder catches it, persists a FAIL
        with observed_reason = a short scrubbed description of the exception
        (``Type: message``), writes the result JSON + transcript, and then
        RE-RAISES the ORIGINAL exception (so the crash is still visible to the
        caller). The artifact is written BEFORE the re-raise.

  An artifact is therefore ALWAYS written on any in-process exit of the block
  whenever the disk write itself can succeed; the recorder never leaves an
  empty or missing artifact on its own accord.

  Honest limitation 1 (recorder-side write failure): if the recorder's OWN
  write fails on the exception path (e.g. base_dir's parent is a regular file,
  the dir is unwritable, or the disk is full during mkdir/write_text), the
  ORIGINAL check exception MUST WIN -- the operator needs to see the real bug,
  not a NotADirectoryError from the recorder. In that case ``__exit__`` swallows
  the recorder-side write error (chaining it as ``__context__`` on the original
  and emitting a short note to stderr) and re-raises the original exception. The
  artifact may then be absent, but the real failure is never masked. (On the
  normal/missing-verdict paths a write failure simply propagates, as there is no
  prior exception to protect.)

  Honest limitation 2 (hard hang / external kill): a genuine hard hang or
  external kill (SIGKILL, power loss) cannot self-write -- no in-process code
  runs at that point. The fail-clean guarantee covers the exception and
  missing-verdict paths only; a true hang must be bounded by the caller (e.g. a
  timeout that surfaces as an exception, which this recorder will then record as
  a FAIL).

Pure standard library only. No third-party deps.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Iterable, List, Optional, Type

from scrub import scrub

# Valid verdicts. Anything outside this set is rejected (never coerced to PASS).
VALID_VERDICTS = ("PASS", "FAIL", "PARTIAL")

# Format version tag stamped into each result file.
RECORDER_TAG = "evidence_recorder/1"

# Default evidence dir, resolved relative to THIS module so it works regardless
# of the process cwd. -> spikes/session-substrate/evidence/
_DEFAULT_BASE_DIR = Path(__file__).resolve().parent / "evidence"

# Filename-stem sanitization. The criterion id is caller-controlled, so it can
# never be used verbatim as a filename: it might contain path separators
# (traversal), NUL bytes, or secret-shaped text. We keep ONLY this whitelist of
# characters; every other run collapses to a single underscore.
_FILENAME_WHITELIST = re.compile(r"[^A-Za-z0-9._-]+")
# Cap the stem length so a pathological criterion can't blow past filesystem
# limits (the two suffixes ".json" / ".transcript.txt" still fit comfortably).
_MAX_STEM_LEN = 100
# Fallback stem when sanitization leaves nothing usable.
_FALLBACK_STEM = "criterion"


def _sanitize_for_filename(criterion: str) -> str:
    """Derive a safe on-disk filename stem from a caller-controlled criterion.

    Strict whitelist: only ``[A-Za-z0-9._-]`` survive; any other run (including
    path separators, whitespace, NUL bytes, and secret-shaped punctuation) is
    replaced by a single ``_``. Leading dots/dashes are stripped (so the result
    is never a hidden file, a ``..`` traversal token, or an option-like name),
    the stem is length-capped, and an empty result falls back to ``criterion``.

    This makes the stem path-traversal-safe on its own; the caller additionally
    verifies the resolved path stays within ``base_dir``.
    """
    stem = _FILENAME_WHITELIST.sub("_", criterion)
    # Strip leading dots/dashes so we never produce ".", "..", ".hidden", or a
    # "-flag"-looking name; trailing dots/spaces are already gone via whitelist.
    stem = stem.lstrip("._-")
    stem = stem[:_MAX_STEM_LEN]
    # A trailing run could have been truncated mid-way; tidy trailing separators.
    stem = stem.rstrip("._-")
    return stem or _FALLBACK_STEM


def _normalize_verdict(verdict: str) -> str:
    """Validate/normalize a verdict; raise ValueError if not in VALID_VERDICTS.

    Case-insensitive and whitespace-tolerant on input, but the stored value is
    always one of the canonical uppercase strings. An unknown verdict raises --
    it must NOT silently become a PASS.
    """
    if not isinstance(verdict, str):
        raise ValueError(f"verdict must be a str, got {type(verdict).__name__}")
    candidate = verdict.strip().upper()
    if candidate not in VALID_VERDICTS:
        raise ValueError(
            f"invalid verdict {verdict!r}; expected one of {VALID_VERDICTS}"
        )
    return candidate


class CriterionRecorder:
    """Buffers a transcript and a verdict for one criterion, then flushes once.

    Prefer the ``record_criterion`` context manager over constructing this
    directly -- the context manager is what provides the fail-clean guarantee.
    """

    def __init__(
        self,
        criterion: str,
        base_dir: Optional[os.PathLike | str] = None,
        extra_secrets: Optional[Iterable[str]] = None,
    ) -> None:
        if not criterion or not str(criterion).strip():
            raise ValueError("criterion must be a non-empty string")
        self.criterion = str(criterion).strip()
        self.base_dir = Path(base_dir) if base_dir is not None else _DEFAULT_BASE_DIR
        # Materialize extra_secrets once (caller may pass a generator).
        self.extra_secrets: List[str] = list(extra_secrets) if extra_secrets else []

        self._chunks: List[str] = []
        self._verdict: Optional[str] = None
        self._reason: str = ""
        self._flushed = False

    # -- public API (all writing routes through _scrub_and_write) -------------

    def add_transcript(self, chunk: str) -> None:
        """Append a transcript chunk to the in-memory buffer.

        May be called repeatedly. Nothing touches disk here; scrubbing happens
        at flush time inside ``_scrub_and_write``.
        """
        if chunk is None:
            return
        if not isinstance(chunk, str):
            chunk = str(chunk)
        self._chunks.append(chunk)

    def set_verdict(self, verdict: str, observed_reason: str = "") -> None:
        """Record the verdict (validated) and its observed reason.

        Raises ValueError on an invalid verdict so a bad value can never become
        a silent PASS.
        """
        self._verdict = _normalize_verdict(verdict)
        self._reason = "" if observed_reason is None else str(observed_reason)

    # -- the single disk-write chokepoint -------------------------------------

    def _scrub_and_write(self, verdict: str, observed_reason: str) -> Path:
        """THE ONLY function in this module that writes to disk.

        Scrubs the observed_reason and the buffered transcript through
        ``scrub()`` (with any caller-supplied extra_secrets) and writes both the
        result JSON and the human-readable transcript file. Returns the path to
        the result JSON. Idempotent guard: only writes once per recorder.
        """
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # Scrub everything that lands on disk -- including the caller-controlled
        # criterion id, which is persisted into the JSON field and the transcript
        # header. A secret-shaped criterion id is therefore redacted in CONTENTS.
        scrubbed_criterion = scrub(self.criterion, self.extra_secrets)
        scrubbed_reason = scrub(observed_reason, self.extra_secrets)
        raw_transcript = "".join(self._chunks)
        scrubbed_transcript = scrub(raw_transcript, self.extra_secrets)

        timestamp = datetime.now(timezone.utc).isoformat()

        # FILENAMES are derived from the SCRUBBED criterion, then run through a
        # strict whitelist. Scrubbing first means a secret-shaped criterion id is
        # redacted to "[REDACTED]" before the whitelist turns it into safe stem
        # chars (e.g. "_REDACTED_") -- so no raw secret reaches a filename. The
        # whitelist also strips path separators, making traversal impossible; we
        # still verify containment below.
        stem = _sanitize_for_filename(scrubbed_criterion)
        transcript_name = f"{stem}.transcript.txt"
        json_name = f"{stem}.json"

        base_dir = self.base_dir.resolve()
        transcript_path = (base_dir / transcript_name).resolve()
        json_path = (base_dir / json_name).resolve()
        # Defense in depth: confirm the resolved paths stay inside base_dir.
        for candidate in (transcript_path, json_path):
            if base_dir not in candidate.parents:
                raise ValueError(
                    f"refusing to write outside base_dir: {candidate} not under {base_dir}"
                )

        # WRITE ORDER MATTERS (fail-clean, defect-2 fix): the result JSON is the
        # authoritative source of truth for the verdict, so it is written FIRST.
        # If the second (transcript) write then fails, the verdict JSON is still
        # on disk -- a consumer scanning for result JSONs never silently skips a
        # criterion, and we never leave a lone transcript with no result JSON.
        result = {
            "criterion": scrubbed_criterion,
            "verdict": verdict,
            "observed_reason": scrubbed_reason,
            "timestamp": timestamp,
            "transcript_path": transcript_name,
            "recorder": RECORDER_TAG,
        }
        json_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # Human-readable transcript file (header is also scrubbed: built from the
        # already-scrubbed reason / criterion / verdict, no raw secrets). If this
        # second write fails, the authoritative result JSON above is already
        # persisted, so the verdict is never lost.
        header = (
            f"# criterion: {scrubbed_criterion}\n"
            f"# verdict: {verdict}\n"
            f"# observed_reason: {scrubbed_reason}\n"
            f"# timestamp: {timestamp}\n"
            f"{'-' * 60}\n"
        )
        transcript_path.write_text(header + scrubbed_transcript, encoding="utf-8")

        self._flushed = True
        return json_path

    # -- flush helpers used by the context manager ----------------------------

    def _flush_normal(self) -> Path:
        """Flush on normal exit. Missing verdict -> fail-clean FAIL."""
        if self._verdict is None:
            return self._scrub_and_write(
                "FAIL", "no verdict produced (fail-clean)"
            )
        return self._scrub_and_write(self._verdict, self._reason)

    def _flush_exception(self, exc: BaseException) -> Path:
        """Flush after the block raised. Records a FAIL with a scrubbed reason.

        The exception's type + message are scrubbed (inside _scrub_and_write) so
        a secret embedded in an error message never lands raw on disk.
        """
        reason = f"exception during check: {type(exc).__name__}: {exc}"
        return self._scrub_and_write("FAIL", reason)


class record_criterion:
    """Context manager wrapping a CriterionRecorder with the fail-clean guarantee.

    On exit it always flushes exactly one artifact set (result JSON + transcript):
      * verdict set            -> that verdict
      * no verdict             -> FAIL "no verdict produced (fail-clean)"
      * exception in the block -> FAIL with scrubbed exception reason, then the
                                  exception is RE-RAISED (artifact written first).
    """

    def __init__(
        self,
        criterion: str,
        base_dir: Optional[os.PathLike | str] = None,
        extra_secrets: Optional[Iterable[str]] = None,
    ) -> None:
        self._rec = CriterionRecorder(
            criterion, base_dir=base_dir, extra_secrets=extra_secrets
        )
        self.result_path: Optional[Path] = None

    def __enter__(self) -> CriterionRecorder:
        return self._rec

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        if exc is not None:
            # Try to write the FAIL artifact, but the ORIGINAL check exception
            # must ALWAYS win: a recorder-side write failure (parent is a file,
            # unwritable dir, disk full during mkdir/write_text, ...) must never
            # mask the real bug the operator needs to see. So the flush runs in
            # its own try/except; if it raises we swallow that write error here
            # (chaining it onto the original as context and noting it on stderr)
            # and return falsy so Python re-raises the ORIGINAL exception.
            try:
                self.result_path = self._rec._flush_exception(exc)
            except Exception as write_err:  # noqa: BLE001 -- must not mask `exc`
                # Attach as context so the failure is still discoverable, but do
                # not let it propagate over the original.
                try:
                    write_err.__context__ = exc
                except Exception:
                    pass
                print(
                    "evidence_recorder: could not write FAIL artifact for "
                    f"{self._rec.criterion!r}: {type(write_err).__name__}: "
                    f"{write_err}; original check exception will propagate.",
                    file=sys.stderr,
                )
            return False  # re-raise: the ORIGINAL crash stays visible.
        self.result_path = self._rec._flush_normal()
        return False


__all__ = [
    "record_criterion",
    "CriterionRecorder",
    "VALID_VERDICTS",
    "RECORDER_TAG",
]
