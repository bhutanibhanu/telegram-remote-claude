# session-substrate feasibility spike (P0 / GATE 1)

This is the **throwaway-quality but retained** P0 spike that empirically settles the
session-substrate decision and fills `docs/adr/ADR-001-session-substrate.md`. See
`docs/features/session-substrate-feasibility/design.md` and `progress.md` for the
authoritative scope.

The goal: run a real interactive Claude Code session from Python and prove, with captured
evidence, whether code can stream a session's activity (C1), answer per-tool permission
decisions (C2), answer interactive tools — AskUserQuestion (C3 ⭐) and ExitPlanMode (C4 ⭐) —
programmatically with no TTY, invoke a custom skill through those same channels (C5), and
resume a session by id across processes (C6). Each criterion gets a runnable check that prints
`PASS / FAIL / PARTIAL` plus a scrubbed transcript saved under `evidence/`. A `FAIL`/`PARTIAL`
is a valid outcome — fail-clean, never a silent hang.

## Isolation rule (hard)

The spike **imports nothing from production**, and **production imports nothing from the
spike**. Nothing under `engine/`, `bot.py`, `claude_runner.py`, `session_manager.py`,
`permissions.py`, or `render.py` is created or modified by this spike. The tree is retained
after P0 as a non-production reference (design S3); P1 may remove it only via a separately
reviewed task after equivalent integration evidence exists.

## Directory layout

The full tree is built incrementally across tasks T1–T19. T1 (this task) stands up the
skeleton, the isolated venv, and the recorded lockfile. The remaining files arrive in later
tasks and are **planned**, not yet present:

```
spikes/session-substrate/
  README.md             # this file (T1)
  requirements.lock     # exact pinned spike deps, committed (T1; re-verified at T4)
  .venv/                # isolated, git-ignored virtualenv (T1; NOT committed)
  scrub.py              # planned (T2) — secret scrubber; the spike's one tested unit
  test_scrub.py         # planned (T2) — the only required automated test in the spike
  evidence_recorder.py  # planned (T3) — records {criterion, verdict, reason} + scrubbed transcript
  preflight.py          # planned (T4) — Python / claude CLI versions + Agent SDK probe
  harness_sdk.py        # planned (T5) — Option A: Agent SDK persistent session lifecycle
  harness_cli.py        # planned (T13) — Option B: claude CLI stream-json driver
  test_skill/           # planned (T10) — custom skill that emits a permission + interactive prompt
  checks/               # per-criterion runnable checks (c1..c6, plus B-path *_cli variants)
  run_all.py            # planned (T17) — prints the C1–C6 × {A,B} PASS/FAIL/PARTIAL matrix
  evidence/             # captured, token-scrubbed transcripts + per-criterion result notes
  normalized_interface.md  # planned (T18) — drafted "events in / decisions out" engine contract
```

`checks/` and `evidence/` exist now as empty placeholders (`.gitkeep`); later tasks populate
them. `evidence/secret-scan.txt` (T18) records the consolidated clean secret-scan result that
gates committing any evidence (X3).

## Setup — isolated, git-ignored venv (X2)

Third-party packages live in an isolated venv scoped to this spike, **never** in the repo's
production dependency files. The repo-root `.gitignore` already ignores `.venv/` at any depth,
so `spikes/session-substrate/.venv/` is git-ignored automatically.

```sh
# from the repo root
python3 -m venv spikes/session-substrate/.venv
spikes/session-substrate/.venv/bin/python -m pip install pytest
spikes/session-substrate/.venv/bin/python -m pip freeze > spikes/session-substrate/requirements.lock
```

To reproduce exactly from the committed lock:

```sh
python3 -m venv spikes/session-substrate/.venv
spikes/session-substrate/.venv/bin/python -m pip install -r spikes/session-substrate/requirements.lock
```

`requirements.txt` and `requirements-dev.txt` (production) stay byte-for-byte unchanged.

## Running things

Always use the spike venv's interpreter so the isolated, pinned deps are used:

```sh
SPIKE_PY=spikes/session-substrate/.venv/bin/python

# the one unit test (arrives in T2)
$SPIKE_PY -m pytest spikes/session-substrate/test_scrub.py

# preflight / checks / matrix (arrive in later tasks)
$SPIKE_PY spikes/session-substrate/preflight.py
$SPIKE_PY spikes/session-substrate/run_all.py
```

The spike depends on the **Claude Code CLI installed and logged in** on the host (`claude` on
PATH) and uses the host's existing auth — **no API key**, no paid API path. No secrets are ever
written to committed evidence; transcripts are scrubbed (T2) before they are persisted (T3) and
secret-scanned before commit (X3).
