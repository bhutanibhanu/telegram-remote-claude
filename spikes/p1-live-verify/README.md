# spikes/p1-live-verify — T9 live end-to-end verify

Throwaway, contained harness that drives the **real** `claude_tg.engine.Engine` end-to-end
with **code-injected** operator decisions (no Telegram tap needed), proving the headline P1
streaming workflow against real Claude. Pairs with the owner phone-verify checklist at
`docs/features/streaming-engine/verify.md`.

## What it does

`verify_streaming.py` runs five trials, each exercising the **answer-hold drive-loop**
(`async for ev in engine.send(prompt)` → on an `AskEvent`/`PlanEvent` call
`engine.resolve(ev.tool_use_id, <decision>)` inline → continue to `ResultEvent`) — exactly
the path the bot's SB1-checked callback handler drives at runtime:

- **V1** AskUserQuestion answered via the native answers-map (code-chosen label echoed back).
- **V2** ExitPlanMode **approve** → proceeds.
- **V3** ExitPlanMode **reject + feedback** → revises on the deny-message channel.
- **V4** context retention across 2 turns in one session + clean `stop()` (no pid leak).
- **V5** `/cancel` (RB4): cancel a held ask, then a trivial turn still completes.

Every predicate is **code-driven** (the discriminating marker is chosen by the harness; the
model can only surface it via the injected answer/verdict — a model that ignores the
instruction yields FAIL, never a false PASS) and tolerant of model nondeterminism (it
asserts on the answer-hold mechanics + the code-chosen marker, not exact phrasing).

## Modes

```sh
# self-test against a scripted fake substrate (NO live Claude) — proves the loop + predicates:
cd <repo> && .venv/bin/python spikes/p1-live-verify/verify_streaming.py --mock

# LIVE against real Claude (the ORCHESTRATOR runs this; produces the committed evidence):
cd <repo> && .venv/bin/python spikes/p1-live-verify/verify_streaming.py --live
```

- `--mock` wires the engine over a `MockSubstrate` that calls the engine's decision callback
  for each scripted interactive request (just like `SdkSubstrate`), so the resolve path is
  exercised deterministically.
- `--live` (default) mirrors `claude_tg.stream_session._default_engine_factory`
  (`SdkSubstrate(cwd=…, permission_mode="default", decision_callback=engine.on_tool_request)`
  then `Engine(…)`), with the substrate `cwd` a fresh `tempfile.mkdtemp()` **outside the
  repo**. Host CLI auth, **no API key** (asserted unset at start; aborts if set).

## Containment / hygiene

Repo `git status --porcelain` is asserted **unchanged** before/after; the temp cwd lives
outside the repo and is `rmtree`'d; `~/.claude/projects/<temp-cwd>` transcript dirs are
cleaned; `descendant_claude_pids()` is asserted empty after stop; **every recorded string is
scrubbed** (SB3) via the P0 `record_criterion` recorder. Evidence → `evidence/<name>.{json,
transcript.txt}` + overall `p1_live_verify.{json,transcript.txt}`.

Reuses `spikes/p1-async-latency/_common.py` (scrub / record_criterion / descendant_claude_pids
/ clean_project_transcript_dir / git_porcelain) via `sys.path` — no production file is
modified; the harness only **imports** `claude_tg`.
