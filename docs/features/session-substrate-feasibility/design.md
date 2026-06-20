# Design: P0 — Session Substrate Feasibility Spike

> **Feature slug:** `session-substrate-feasibility` · **Pipeline:** P0 (gate, not a user feature).
> Throwaway spike that empirically settles the session substrate and fills
> [ADR-001](../../adr/ADR-001-session-substrate.md). Parent:
> [`docs/interactive-remote-design.md`](../../interactive-remote-design.md) ·
> [`docs/cross-cutting-requirements.md`](../../cross-cutting-requirements.md).
>
> **This is GATE 1 — the biggest risk in the product.** If interactive prompts (C3/C4) can't be
> answered programmatically on either substrate, the headline becomes "constrained mode" and the
> roadmap is re-planned before P1. Nothing below P0 is scoped until ADR-001 is accepted (G-ADR).

---

## Scoping decisions (this pipeline) — confirm at G-Scope

These four shape spike effort and deliverables. Defaults applied; revise here before planning.

| # | Decision | Value |
|---|---|---|
| S1 | **Substrate coverage** | **A-first.** Test Agent SDK (A) fully against C1–C6. Always test CLI `stream-json` (B) on the make-or-break **C2/C3/C4** so the fallback is credible; test B on the remaining criteria only where A fails or is partial. |
| S2 | **Evidence bar** | Each criterion gets a **runnable check** printing `PASS / FAIL / PARTIAL` **plus a captured session transcript/log** saved as evidence. Reproducible over hand-waving. |
| S3 | **Spike code location + disposition** | Lives in `spikes/session-substrate/`, committed on this branch as reference, **never imported by production**, deleted before P1. Durable deliverables are the evidence log + filled ADR-001. |
| S4 | **ADR acceptance boundary** | Spike **fills** ADR-001 (Decision, Normalized engine interface, Consequences) as status **Proposed**. Owner accepts separately at **G-ADR** — matches the design doc's hard gate. Acceptance is **not** in this pipeline's scope. |

---

## The basics

**Elevator pitch.** Run a real interactive Claude Code session from Python and prove, with captured
evidence, whether code can stream its activity, answer its permission requests, and answer its
interactive tools (AskUserQuestion, ExitPlanMode) — then lock the substrate choice in ADR-001.

**The actual problem.** The whole product (P1–P9) assumes code can *drive an interactive Claude
Code session* — not just fire one-shot prompts. That assumption is unproven. A prior **doc-only**
research pass was unreliable: it wrongly concluded the Claude Agent SDK does not exist and never
tested whether interactive tools can be answered without a TTY. We refuse to build P1 on an
untested assumption. This spike settles it **empirically, by running the session.**

**Who it's for.** Primary: the **repo owner**, who needs a trustworthy go/no-go and an accepted
ADR-001 before committing to the build. Secondary: the **P1 implementer** (likely the owner), who
inherits the *normalized engine interface* this spike drafts — the "events in / decisions out"
contract the streaming engine will be built against.

**Definition of success.**
- A documented **yes / no / partial (+ how)** for each of **C1–C6**, each backed by a runnable
  check result and a saved transcript.
- **ADR-001 filled** as *Proposed*: chosen substrate + justification, the **Normalized engine
  interface**, and **Consequences** (what P1 inherits; any capability needing a workaround).
- A clear **go / no-go** recommendation the owner can accept at G-ADR.
- Reproducible: another run on the same machine reproduces the per-criterion verdicts.

**Anti-goals (explicit non-features).**
- **No production code.** No `engine/`, no `bot.py`/`claude_runner.py` changes, no Telegram wiring,
  no `session_manager.py`/`permissions.py`/`render.py`. The spike imports nothing into prod and
  prod imports nothing from the spike.
- **No P1+ work.** No streaming engine, no live rendering, no multi-project, no real approval UI.
- **Not a polished tool.** Throwaway harnesses; readability over robustness; deleted before P1.
- **Not a substrate *implementation*.** It tests candidates and writes the decision; it does not
  build the chosen one.

**Constraints.**
- Depends on **Claude Code CLI installed + logged in** on the host (`claude` on PATH); uses the
  owner's existing auth — **no API key**, no paid API path.
- Python (repo now supports up to **3.14**, per recent baseline). Spike runs in the repo `.venv`.
- macOS dev environment (must not assume macOS-only behavior in any finding that informs P1).
- **No secrets in committed evidence** (SB3 applies even to a throwaway): transcripts are scrubbed
  of tokens before commit.
- Solo, time-boxed: prove the unknowns; do not gold-plate.

---

## Requirements

### Functional — the criteria under test (ranked; C3/C4 make-or-break)

Each criterion is proven by a runnable check (`PASS/FAIL/PARTIAL`) + a saved transcript (S2).

1. **C1 — Bidirectional streaming.** Stand up a persistent, multi-turn session; send a new operator
   message *in* mid-session and observe a continuous event stream *out*. *Proven:* ≥2 turns over one
   live session with streamed events captured.
2. **C2 — Per-tool permission decision.** Code is consulted *before* a risky tool runs; its
   allow/deny (ideally with reason / modified input) is honored. *Proven:* one risky tool (e.g. a
   `Write` or `Bash`) is **denied programmatically and does not execute**, and a second is allowed
   and does. *(make-or-break-adjacent — the safety backbone for P2.)*
3. **C3 — AskUserQuestion ⭐.** A multiple-choice question raised mid-session is intercepted and
   answered **programmatically (no TTY)**, and the session proceeds on that answer. *Make-or-break.*
4. **C4 — Plan approval / ExitPlanMode ⭐.** A proposed plan is surfaced and approved **or** rejected
   with feedback, programmatically, and the session honors the verdict. *Make-or-break.*
5. **C5 — Skill invocation.** A custom slash-command skill is invoked in-session and its interactive
   prompts flow through the **same** channels proven in C2–C4. *Proven:* a skill that deliberately
   emits a permission + an AskUserQuestion (and/or ExitPlanMode) is driven to completion in-session.
6. **C6 — Session resume.** A session is resumed **by id across separate process runs** with history
   intact. *Proven:* run A starts a session and records its id; run B (fresh process) resumes it and
   demonstrates retained context.

### Non-functional

- **Reproducibility** — runnable per-criterion checks; transcripts saved under the spike dir.
- **Secret hygiene (SB3)** — no tokens/keys in committed transcripts or logs.
- **Fail-clean observation** — a substrate that can't do a thing produces a clear `FAIL/PARTIAL`
  with the observed reason, never a silent hang the reader can't interpret.
- **Explicitly N/A for a throwaway:** scale/RPS, latency targets, availability, accessibility,
  i18n, persistence integrity, CI gating. The spike is not held to the repo's 53-test snapshot
  (per Test policy — that snapshot is pre-P0, not a floor).

### Future

The spike's lasting output is the **Normalized engine interface** (below), which P1's streaming
engine is built against. Nothing else from the spike survives.

---

## Architecture (delta from current — spike harnesses only)

Current prod: `bot.py` → `claude_runner.py` (one-shot `claude -p --output-format json`). The spike
touches **none** of it. It adds an isolated, throwaway tree:

```
spikes/session-substrate/
  preflight.py        # Step 0: detect Python/CLI versions; probe whether the Agent SDK
                      #         package actually exists + installs (prior research got this wrong);
                      #         record package name + version, or absence, as the first evidence.
  harness_sdk.py      # Option A — Claude Agent SDK: persistent client, permission callback,
                      #            interactive-tool handling, skill load, resume.
  harness_cli.py      # Option B — claude CLI stream-json: drive
                      #            `claude -p --input-format stream-json --output-format stream-json`,
                      #            parse the event stream, find how C2–C4 are answered over the wire.
  test_skill/         # minimal custom skill for C5: deliberately emits a permission + an
                      #            AskUserQuestion (and/or ExitPlanMode).
  run_all.py          # runs the criteria checks, prints the C1–C6 PASS/FAIL/PARTIAL matrix.
  evidence/           # captured transcripts + per-criterion result notes (token-scrubbed).
```

**Substrate coverage (S1).** A is exercised against all of C1–C6. B is exercised against the
make-or-break **C2/C3/C4** unconditionally (so the fallback is real, not assumed), and against the
rest only where A is FAIL/PARTIAL. Option **C (hybrid)** is recorded in the ADR *only if* the
evidence shows A needs B (or hooks/workflow constraints) to cover C3/C4.

**Known unknowns the spike must resolve (not assume):**
- Whether the Agent SDK package exists and installs, and at what name/version (prior pass erred).
- On the installed CLI, **how permission decisions are answered over the wire** — ADR-001 notes
  `--permission-prompt-tool` was *absent* from `--help` on CLI v2.1.183; the spike records the
  actual mechanism on the installed version (flag, hook, MCP permission tool, or none).
- Exact event/message shapes each substrate emits (the raw material for the normalized interface).

### Deliverable: Normalized engine interface (drafted here, inherited by P1)

The spike drafts the contract ADR-001 §"Normalized engine interface" requires:
- **Events out** (engine → bot): `text`, `tool_use`, `ask`, `plan`, `error`, `result`, `status`.
- **Decisions in** (bot → engine): permission verdict (allow-once / allow-session / deny [+reason]),
  question answer, plan verdict (approve / reject +feedback), free-text reply, cancel.
- **Lifecycle calls:** `start`, `resume`, `send`, `stop`.

This is a *draft contract for P1*, not an implementation. No engine is built in P0.

---

## SDLC plan (spike-specific)

- **Repo layout.** Spike isolated under `spikes/session-substrate/`; feature docs under
  `docs/features/session-substrate-feasibility/`; ADR at `docs/adr/ADR-001-session-substrate.md`.
- **Branch.** `feat/session-substrate-feasibility` in its own worktree; `main` untouched and
  runnable throughout.
- **Testing strategy.** No unit-test suite for throwaway harnesses; the *evidence* (runnable
  checks + transcripts) is the verification artifact. The repo's existing 53-test snapshot is **not
  touched** and not used as a gate here.
- **CI.** None for the spike (CI is stood up in P1). The merge artifact is docs + ADR, not code.
- **Docs / merge artifact.** What lands on `main` via this pipeline: this `design.md`, the evidence
  log, and the filled **ADR-001 (Proposed)**. The `spikes/` tree is reference-only and removed
  before P1.

---

## Risks & open questions

**Top risks**
1. *(Technical — GATE 1, top product risk)* **C3/C4 not answerable on either substrate** → the
   headline "run `/grill` and `/pipeline` from your phone" degrades to constrained mode.
   *Mitigation:* this spike; test B on C3/C4 unconditionally; if both fail, record the constrained
   shape and **re-plan before P1** rather than discovering it mid-build.
2. *(Technical)* **Agent SDK availability/maturity uncertain** (prior research unreliable).
   *Mitigation:* `preflight.py` probes existence/version first; B is the identified fallback.
3. *(Operational)* **Spike behaviour differs from production wiring**, giving false confidence.
   *Mitigation:* exercise the *same* interactive paths P1 will use (real skill for C5, a real risky
   tool for C2); record substrate quirks in ADR Consequences so P1 inherits the caveats.

**Open questions (resolve during the spike, not before)**
- Exact custom skill content for C5 and which risky tool for C2 — chosen during planning/build to
  best exercise the channels; not a scoping blocker.
- Whether Option C (hybrid) is needed — decided *by the evidence*, recorded in the ADR.

**ADR to write**
- **ADR-001 — Session Substrate** (the output of this spike): fill Decision, Evidence log per
  C1–C6, Normalized engine interface, and Consequences; set status **Proposed**; owner accepts at
  **G-ADR**.

---

## Roadmap

**In scope (P0 / this pipeline):** preflight + two throwaway harnesses + test skill; runnable
C1–C6 checks with captured transcripts; the C1–C6 evidence matrix; the drafted normalized engine
interface; ADR-001 filled to *Proposed*; a go/no-go writeup.

**Out of scope (deferred to P1+):** the streaming engine, live rendering, permission UI,
multi-project, restart recovery, concurrency, CI — none begin until ADR-001 is accepted.

**Expected build order (input to `/plan` — do not execute yet):**
1. **Step 0 — Preflight:** record Python + `claude` CLI versions; probe Agent SDK existence/version.
2. **A-harness:** C1 streaming → C2 permission → C3 AskUserQuestion → C4 plan → C5 skill → C6 resume.
3. **B-harness:** C2/C3/C4 unconditionally; remaining criteria only where A is FAIL/PARTIAL.
4. **Evidence:** capture token-scrubbed transcripts + per-criterion `PASS/FAIL/PARTIAL`; build the
   matrix via `run_all.py`.
5. **Draft** the normalized engine interface from observed event/decision shapes.
6. **Fill ADR-001** (Proposed): Decision + justification, Evidence log, Normalized interface,
   Consequences (incl. any C3/C4 workaround or hybrid).
7. **Present go/no-go;** owner reviews for G-ADR.
