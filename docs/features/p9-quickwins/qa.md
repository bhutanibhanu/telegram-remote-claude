# P9 QA — quick wins

_Per-batch reviewers + a whole-bot UX/bug audit (the owner's "make it perfect" pass) + cross-model Codex, iterated to SHIP. **1003 tests**, ruff/mypy/secret_scan clean._

## Per-batch independent reviewers — all AGREE
- **Batch 1** (command menu + onboarding, `/status`, cost): AGREE. Closed its soft spots before commit — cost-on-captured-project test teeth (mutation-proven), `/status` multi-project + yolo coverage, "1 turn" singular.
- **Batch 2** (model routing, macros): AGREE. Engine-factory threading sound (Protocol intact, injected factories never get `model`, P6 path-confinement preserved, model applies only on a fresh session); macro `/run` through the normal permission gate; SB1 on all 7 commands; 3 mutations caught.
- **Batch 3** (notification polish + `[Open]` switch callback + chips): orchestrator self-review (the same-model reviewer hit the content filter) — verified the new switch callback's full chain: SB1 (auth recheck before switch), decode rejects malformed names, SB2 cwd re-validation (shared `_switch_active`), no kind-collision; + the implementer's 27 tests + mutation probes.

## Whole-bot UX + bug audit ("make it perfect")
Found **1 P0 + 4 P1 + P2 polish** — all fixed:
- **P0:** macro case-collision (`/save Work` + `/save work` made duplicates) → `save_macro` overwrites the case-insensitive key.
- **P1:** `/status` missing from `/help` (+ a HELP↔menu lock-step test); inconsistent project-name styling (unified to `<b>`+escaped everywhere); `/cancel all` wording (project-scoped); `/status` gate posture was active-project-only (now per-project `⚠️ yolo` marker).
- **P2:** dropped a redundant `html.escape`; refusal-glyph consistency. (Confirmed NOT bugs: cost is per-turn, "N turns" wording left as-is.)

## Cross-model Codex — NO_SHIP ×2 → fixed → clean
- **Round 1 blocker:** `/run` routed through the message path could **consume a pending free-text prompt** instead of starting a turn → fixed with a `command_initiated` flag (red-green + mutation-probed); a plain message still answers a capture.
- **Round 2 blocker:** `/help` (Markdown) carried a literal `$*` → the bold markers went **odd/unbalanced** → Telegram rejected the send → code-spanned the `$*` placeholder + added a balanced-markers guard test. (Deterministic fix; confirmed live — `/help` sends clean.)

## Live phone-verify — ALL PASS
All 6 features verified end-to-end on the real bot (see `verify.md`), including the two Codex-blocker fixes (`/help` sends clean; `/run` during a pending prompt starts a fresh turn) and the `[Open]` switch. Two non-blocking observations recorded for follow-up (background-ping timing race = the un-verified P5 scenario (f); the C2 gate correctly catching a model path-choice).

## Decision
Both Codex blockers + the audit's P0/P1 closed; live-verify PASS → **merge P9 to `main`.** Recorded follow-up: the background-ping timing race (P5 scenario f) deserves a small polish fix.
