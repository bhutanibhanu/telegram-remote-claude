# Progress: p8-docs

_From design.md · docs refresh + deferred UX polish · supervised. Baseline 863 tests._

## Task list
- [x] T1 — README/docs full refresh — cross-checked vs the code (14 commands ↔ handlers 1:1, 16 configs, security≈findings.md, install/launchd, docs index) (ac5f304)
- [x] T2 — UX path-linkify: tool-status + permission-prompt paths render as `<code>` (HTML-escaped, injection-proof, plain-text fallback; coalescer carries parse_mode) — reviewer AGREE + live-verified (4551cc4)
- [x] T3 — project-name styling consistency (`<b>`, escaped incl. pre-validation invalid-name) + ADR-006 (notify_last / _is_resume_failure documented as known-acceptable, not fixed) (f856efd)
- [x] T4 — Verify: Verifier SHIP (README accurate) + Codex SHIP + live-verify PASS (DOM: permission/tool paths render as real `<code>` entities, zero fake-links, buttons work, no literal-tag leak in a 181-poll mixed burst, no dup). + accuracy fix STATE_FILE→CLAUDE_STATE_FILE (57458ef). 875 tests green.

Legend: `[ ]` todo · `[x]` done (sha) · `[!]` blocked

## Tasks
### T1 — README/docs refresh (docs-only)
- **Files:** `README.md` (rewrite), maybe a `docs/README.md` index.
- **Acceptance:** README documents the CURRENT bot accurately — oneshot vs streaming modes + how to choose; the FULL command set (cross-checked against the handlers registered in `bot.py` — no documented-but-nonexistent command, no missing real one); multi-project + concurrency (P4/P5); the security model (gate-on-default, allowlist, path confinement — keep P6's accurate); a config table (cross-ref `.env.example`); install + keep-alive (`pip install .`/console/`run.sh`/launchd, pointing at `deploy/README.md`); a docs/ index linking ADR-001..005 + `docs/features/*`. No secrets.
- **Tests:** none (docs); accuracy verified by reviewer cross-check + T4.

### T2 — UX path-linkify (code)
- **Files:** `claude_tg/engine/types.py` (`safe_input_summary`) or `render.py`; permission-prompt render path (`render.py`/`stream_session.py`).
- **Acceptance:** path-like values in the `▶️` tool-use status line and the `🔐 permission` prompt body render as `<code>` (no `/segment` auto-linkify); HTML-escaped; R5 `_TurnDedup` (raw-text compare) unaffected; SB3 body-free posture intact (no file CONTENT, just the path/tool); foreground + background paths both correct.
- **Tests:** a render test that the tool-status / permission body wraps a path in `<code>` not bare; R5-dedup regression stays green. Live-verify in T4.

### T3 — minor polish
- **Acceptance:** project names render consistently (`<b>`/`<code>`, not `{name!r}`) across the echo sites; `notify_last` pruned on resolve/turn-end IF the change is small + low-risk (else document + defer); the `_is_resume_failure` text-heuristic limitation documented (ADR note / comment) — confirming vs the real SDK needs a torn resume (hard to force), so record as a known heuristic. Tiny, low-risk.
- **Tests:** name-styling assertions where cheap; keep existing green.

### T4 — verify + merge
- **Acceptance:** docs accuracy cross-check (every documented command/config exists in code); live-verify the T2 linkify on the real bot (paths monospace in tool-status + permission prompt, buttons work, no dup); gates green → Codex QA → merge to `main`.
