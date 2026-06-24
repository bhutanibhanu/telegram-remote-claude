# P8 QA — docs & final polish

_Two-reviewer QA + live verify. Both reviewers **SHIP**, live-verify **PASS**._

## Verifier subagent (same-model, full diff) — SHIP
- **README accuracy (primary deliverable) verified:** every command in the README ↔ a real registered handler in `bot.py` (14 ↔ 14, exact 1:1 — no documented-but-nonexistent, no registered-but-undocumented; the old P0-era README was missing `/unyolo` + the 8 streaming commands); 16-row config table ↔ `config.py from_env` + `.env.example` (defaults correct); security section consistent with P6 `findings.md` (gate-on-default, path confinement, Bash documented-unconfined, oneshot non-interactive — no overclaiming); install/launchd match `pyproject`/`deploy`/`run.sh`; docs index links resolve (ADR-001..006 present).
- **T2/T3 sound:** every interpolated field escaped once; permission prompt has a plain fallback; the pre-validation invalid-name echo escapes arbitrary input; no behavior change beyond cosmetic rendering; R5 `_TurnDedup` untouched; no secret.
- **Non-blocking (fixed):** the `cmd_new` persistence hint said "STATE_FILE" not "CLAUDE_STATE_FILE" (pre-existing; outside the diff) → corrected (`57458ef`) since P8 is the accuracy phase.

## Codex (cross-model, full diff) — SHIP
- **Blockers: none. Non-blocking: none.** Confirmed README accuracy, T2 escaping/fallback, T3 invalid-name escape, no regression to the test floor, no secret leak.

## T2 path-linkify — independently reviewed (AGREE) earlier
The injection surface (attacker-influenceable tool input → the permission prompt) was hammered with a real HTMLParser oracle: every field escaped once, valid Telegram HTML on all hostile inputs, plain fallback fires (prompt never dropped), coalescer newest-wins, R5/SB3 intact, teeth mutation-confirmed.

## Live verify (T4) — PASS (DOM-level)
Real bot, booted via the NEW entry (`python -m claude_tg`), gate ON:
- Permission prompt `Write(file_path=/private/tmp/.../note.txt, content=<7 chars>)` renders as a **real Telegram `<code>` entity** (monospace), **zero `<a>` anchors** (Telegram did NOT auto-linkify the path into fake `/segment` commands), no literal `<code>`/`&lt;` text; Allow/Allow-session/Deny buttons present + functional → Allow wrote `PURPLE` to disk.
- Tool-status `▶️ …(path)` line monospace (captured once; transient).
- Mixed status burst (tool→thinking→tool): 181 DOM polls over 22s → **zero** literal/escaped-tag leak (coalescer carries `parse_mode` correctly). A dir path embedded in answer prose also rendered monospace, no fake-links.
- No duplicate messages (P6 fix holds).
- Evidence: `verify-p8-{permission-path,tool-status,mixed-burst}.png`.

## Final gates
- `pytest -q` → **875 passed** · `ruff check .` clean · `mypy claude_tg` clean · `secret_scan.py` clean.

## Decision
Both reviewers SHIP, live-verify PASS → **merge P8 to `main`.** Next = **P9 (OSS public release) — 🛑 HARD STOP** for explicit owner go (LICENSE finalize + final scrub + the irreversible publish act; token rotation).
