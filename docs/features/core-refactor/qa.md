# core-refactor QA — behavior-equivalence (a refactor; the bar is "nothing changed")

## Verifier (same-model, independent): SHIP
AST byte-compared the relocated methods: **41 methods identical** (Concurrency 9/9, Statusline 7/7, Callbacks 22/25 — the 3 "differs" are only `StreamingSession.X` → `CallbacksMixin.X` static-call rewrites to the same relocated targets; `__init__`/a few others differ only by `.x`→`..x` import depth). No `self`-binding lost; no MRO cross-mixin collision; no mixin imports `.core` at module scope (no cycle); `__init__` re-exports all 8 imported names. Knob registry behavior-identical (asymmetries preserved). ADR-001/005 honored (engine/render/leaf modules untouched; concurrency relocated intact). Gates green (1663).

## Codex (cross-model, after clearing a 1h `codex exec` hang + retrying): SHIP
Compared method bodies vs the pre-refactor source: "Moved methods are source-identical except the static callback references rewritten to `CallbacksMixin`, with no MRO collisions, no mixin `.core` imports, and bot/test imports re-exported. The knob registry preserves old resolver defaults and warm-engine identity: thinking stays transient, effort has no config default, and model is excluded from rebuild matching." 0 blockers.

## Orchestrator corroboration: confirms
No mixin imports `.core` at module scope (cycle-free). `PROJECT_KNOBS`: model `session_identity=False` (no rebuild on model change), effort `config_default=lambda c: None` (no default), thinking `field="" persisted=False` (transient/RB3) — all three asymmetries exactly preserved.

## The structural proof
**1663 tests passed at every task with ZERO test-file edits** — for a pure relocation + behavior-identical consolidation, that is the strongest equivalence evidence.

## Verdict: SHIP (3/3 reviewers + zero-test-edit proof). Note: the first Codex run hung ~1h (recurring `codex exec` flakiness, not a code issue) — killed + a fresh retry succeeded.
