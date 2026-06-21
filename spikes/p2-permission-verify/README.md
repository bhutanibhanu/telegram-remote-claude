# spikes/p2-permission-verify — T8 live end-to-end verify (permission gate)

Throwaway, contained harness that drives the **real** `claude_tg.engine.Engine` end-to-end
with **code-injected** permission verdicts (no Telegram tap needed), proving the headline P2
**per-tool permission gate** against real Claude. Pairs with the owner phone-verify checklist
at `docs/features/permission-gating/verify.md`.

## What it does

`verify_permissions.py` runs six trials, each exercising the **permission-hold drive-loop**
(`async for ev in engine.send(prompt)` → on a `PermissionEvent` call
`engine.resolve(ev.tool_use_id, PermissionDecision(verdict=…))` inline → continue to
`ResultEvent`) — exactly the path the bot's SB1-checked callback handler
(`StreamingSession._resolve_permission`) drives at runtime:

- **V1** risky **ALLOW** — a risky Write is HELD; code injects `allow_once`; the action RUNS
  (the marker file appears in the temp cwd).
- **V2** risky **DENY** — a risky Write is HELD; code injects `deny`; the action does NOT run
  (file absent) and the session continues (the model adapts to the canned denial).
- **V3** **allow-session SUPPRESSES** — two uses of the same tool in one session emit
  **exactly one** `PermissionEvent` (the first `allow_session` records the per-NAME grant; the
  second is auto-allowed, no second prompt).
- **V4** **safe FREE** — a safe Read runs with **no** `PermissionEvent` (unprompted).
- **V5** **/yolo** — under `set_yolo(True)` a risky Write runs with **no** `PermissionEvent`;
  `set_yolo(False)` re-gates (a fresh risky write HOLDS again).
- **V6** **/cancel** (RB4, optional-but-preferred) — a held permission is `engine.cancel()`'d
  instead of resolved → clean unwind (no hang), the action did not run, and a follow-up turn
  still completes (session usable).

The harness **owns the `PermissionPolicy`** (a fresh one per trial — fail-closed isolation),
the SAME object the production wiring threads from the chat
(`_ChatState.policy` → `_default_engine_factory` → `Engine(permission_policy=…)`). It stands
in for the bot/session: it `set_yolo`s for V5 and observes the allow-session grant land on
resolve in V3.

Every predicate is **code-driven** (a UNIQUE marker filename is chosen by the harness and the
model is asked to create exactly that file; we count `PermissionEvent`s) and tolerant of model
nondeterminism (it asserts on the **gate mechanics** + the real filesystem — permission
emitted / not emitted; file present / absent; exactly one prompt for two uses — not exact
prose). A model that ignores the instruction, or a gate that fails to hold, yields FAIL/PARTIAL,
never a false PASS. (Mutation-probed in `--mock`: forcing deny on V1, allow on V2,
risky-classifying the V4 read, dropping the V3 grant, no-op-ing `set_yolo` for V5, and
cancel→allow for V6 each flip the trial OFF PASS.)

## Modes

```sh
# self-test against a scripted fake substrate (NO live Claude) — proves the loop + predicates:
cd <repo> && .venv/bin/python spikes/p2-permission-verify/verify_permissions.py --mock

# LIVE against real Claude (the ORCHESTRATOR runs this; produces the committed evidence):
cd <repo> && .venv/bin/python spikes/p2-permission-verify/verify_permissions.py --live
```

- `--mock` wires the engine over a `MockSubstrate` that calls the engine's decision callback
  for each scripted safe/risky tool (just like `SdkSubstrate`), so the gate + resolve path is
  exercised deterministically. For a risky tool the mock parks until the harness resolves the
  injected `PermissionEvent`, then performs the tool's **real effect** (write / don't-write the
  marker file) keyed on the returned `SubstrateDecision.allow` — so the "did it run?" predicate
  inspects the real filesystem just like live.
- `--live` (default) mirrors `claude_tg.stream_session._default_engine_factory`
  (`SdkSubstrate(cwd=…, permission_mode="default", decision_callback=engine.on_tool_request)`
  then `Engine(…, permission_policy=<harness-owned>)`), with the substrate `cwd` a fresh
  `tempfile.mkdtemp()` **outside the repo**. Host CLI auth, **no API key** (asserted unset at
  start; **aborts** if set). No `--dangerously-skip-permissions` anywhere (SB5).

## Containment / hygiene

The gate means a risky tool only runs when the harness ALLOWS it — so the temp cwd (outside the
repo) only ever sees the marker files we explicitly allow, and is `rmtree`'d regardless. Repo
`git status --porcelain` is asserted **unchanged** before/after; `~/.claude/projects/<temp-cwd>`
transcript dirs are cleaned; `descendant_claude_pids()` is asserted empty after stop; **every
recorded string is scrubbed** (SB3) via the P0 `record_criterion` recorder. The
`PermissionEvent` summary is already body-free (lengths, not contents). Evidence →
`evidence/<name>.{json,transcript.txt}` + overall `p2_permission_verify.{json,transcript.txt}`.

Reuses `spikes/p1-async-latency/_common.py` (scrub / record_criterion / descendant_claude_pids
/ clean_project_transcript_dir / git_porcelain) via `sys.path` — no production file is modified;
the harness only **imports** `claude_tg` (`engine`, `permissions`).
