# Session-substrate feasibility -- C1-C6 x {A,B} verdict matrix (T17)

- **SDK:** claude-agent-sdk==0.2.105
- **CLI:** claude CLI 2.1.183 (preflight) / 2.1.185 (C-checks, observed)
- **Substrate A:** claude-agent-sdk (in-process Agent SDK)
- **Substrate B:** raw `claude` CLI over stream-json (no SDK import)
- **Generated at:** 2026-06-20T23:58:43.207076+00:00
- **Format:** `run_all_matrix/1`

> **GATE-1:** GATE-1 GREEN: both make-or-break criteria (C3, C4) PASS on BOTH substrates (A and B). The design's constrained-mode / re-plan risk is retired.

- _Substrate-neutral:_ C2/C3/C4 are substrate-NEUTRAL: each PASSes on BOTH A and B via the same permission/answer/plan mechanism, so they do not by themselves decide A vs B.
- _Asymmetry:_ A-vs-B asymmetry: B couples directly to the UNDOCUMENTED `--permission-prompt-tool stdio` stdio flag plus hand-rolled NDJSON/threads/timeouts (the `updatedInput`-on-allow workaround), whereas A uses the SDK's in-process callback. Per design S1, B coverage is limited to the make-or-break/backbone C2/C3/C4.

## Matrix

| Criterion | Make-or-break | A | B | Description |
|-----------|---------------|---|---|-------------|
| C1 | no | `PASS` | `N-A` | bidirectional streaming |
| C2 | no | `PASS` | `PASS` | per-tool permission decision |
| C3 | yes | `PASS` | `PASS` [^1] | AskUserQuestion answered programmatically |
| C4 | yes | `PASS` [^1] | `PASS` [^1] | ExitPlanMode approve/reject + feedback |
| C5 | no | `PASS` [^1] | `N-A` | skill invocation through the same channels |
| C6 | no | `PASS` | `N-A` | session resume by id across process runs |

### Footnotes

[^1]: source evidence notes model/substrate variability (preserved, not hidden)

## B-contingency rule (design S1)

design S1: A is exercised on all of C1-C6; B is exercised on C2/C3/C4 unconditionally and on the remaining criteria (C1/C5/C6) ONLY where A is FAIL/PARTIAL. Where A PASSes a non-make-or-break criterion, B is N-A.

> **S1 B-contingency check: OK** -- substrate A PASSes every contingent criterion (C1/C5/C6), so showing B as N-A on those is correct (no B run required).

## Per-cell evidence

| Cell | Verdict | Evidence | Reason |
|------|---------|----------|--------|
| C1(A) | `PASS` | `c1.json` | 2 turns over one stable session_id=8685b77c-60a7-447d-a950-2e202c3d55c9; each emitted a clean terminal result; genuinely incremental StreamEvent deltas streamed OUT before completion in BOTH turns (t… |
| C1(B) | `N-A` | _(no evidence file)_ | substrate A PASS on a non-make-or-break criterion; per design S1, B is exercised only where A is FAIL/PARTIAL -- B beyond C2/C3/C4 intentionally not run |
| C2(A) | `PASS` | `c2.json` | Genuine per-tool SDK permission decision honored: Write to denied-marker was DENIED via can_use_tool and did NOT execute (file absent); Write to allowed-marker was ALLOWED and executed (file present… |
| C2(B) | `PASS` | `c2_cli.json` | Genuine per-tool permission decision honored OVER THE WIRE (substrate B, raw CLI stream-json control protocol): a can_use_tool control_request for Write to denied-marker arrived over the wire, our de… |
| C3(A) | `PASS` | `c3.json` | C3 ACHIEVED NATIVELY on substrate A. AskUserQuestion is answered programmatically (no TTY) by returning a PermissionResultAllow whose updated_input carries the documented AskUserQuestionOutput `answe… |
| C3(B) | `PASS` | `c3_cli.json` | C3 ACHIEVED NATIVELY on substrate B (over the wire). AskUserQuestion arrives as an ordinary can_use_tool control_request (no special subtype) and is answered programmatically (no TTY) by returning an… |
| C4(A) | `PASS` | `c4.json` | C4 honored both verdicts via the real SDK permission callback (can_use_tool). APPROVE (PermissionResultAllow) -> native 'User has approved your plan'. REJECT (PermissionResultDeny message=feedback) -… |
| C4(B) | `PASS` | `c4_cli.json` | C4 honored both verdicts on substrate B over the wire via the real CLI permission control protocol (can_use_tool, permission_mode=plan, no bypass). ExitPlanMode arrives as an ordinary can_use_tool co… |
| C5(A) | `PASS` | `c5.json` | C5 honored: the spike-c5-probe SKILL was invoked in-session (Skill tool fired) and its interactive prompts flowed through the SAME can_use_tool channels as C2/C3. Its per-tool permission request (Wri… |
| C5(B) | `N-A` | _(no evidence file)_ | substrate A PASS on a non-make-or-break criterion; per design S1, B is exercised only where A is FAIL/PARTIAL -- B beyond C2/C3/C4 intentionally not run |
| C6(A) | `PASS` | `c6.json` | Cross-process resume PROVEN: phase A (process 1) planted code-generated codeword C6_6707a26f in session 0d84f7b1-eb68-4c7c-b3d2-38bbedc521f6; phase B (SEPARATE process, fresh interpreter) resumed BY… |
| C6(B) | `N-A` | _(no evidence file)_ | substrate A PASS on a non-make-or-break criterion; per design S1, B is exercised only where A is FAIL/PARTIAL -- B beyond C2/C3/C4 intentionally not run |

