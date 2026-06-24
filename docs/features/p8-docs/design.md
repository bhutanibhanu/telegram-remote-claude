# P8 — Docs & Final Polish

_Delta on the shipped P0–P7 codebase (`main` @ a3a0302). Goal: bring the user-facing docs up to the real (P1–P7) feature set, and clear the small UX/polish items deferred across P5–P7. Mostly docs + a little user-facing UX code. This is the last phase before P9 (the public-release hard stop), so the repo should read like something a stranger can install + run + trust._

## Problem
The README still largely describes the **P0 oneshot-only** model (single session, `claude -p`, only `/reset /pwd /cd`). Since then the bot gained: streaming mode with interactive Telegram approval, multi-project sessions, concurrency + notifications, the P6 security model (gate-on-default, path confinement), and the P7 install + launchd keep-alive. P6 corrected only the stale *security* claims; P7 fixed *install* accuracy. P8 is the comprehensive doc pass + the deferred UX polish, so a new user (and a P9 OSS visitor) gets accurate, complete docs.

## Success
- README accurately documents the current bot: modes (oneshot vs streaming), the full command set, multi-project + concurrency, the security model, and install + keep-alive — cross-checked against the code (commands actually registered, configs actually read).
- The deferred user-facing UX polish is done: tool-summary + permission-prompt paths render as `<code>` (no `/segment` fake-links anywhere a path shows), and project-name styling is consistent.
- A short docs/ index (the ADRs + the per-feature docs) so the repo is navigable.
- Gates green (863 floor); any UX code change live-verified on Telegram. No behavior change beyond the cosmetic rendering.

## Anti-goals
- No new features. No behavior changes beyond cosmetic message rendering. Not the P9 release act itself (LICENSE finalize / making-public is P9, a hard stop). No marketing fluff — accurate operator docs.

## Scope / build order (input to /plan)
1. **T1 — README/docs refresh (headline, docs-only).** Rewrite README to match P0–P7:
   - What it is + the security posture up front (gate-on-default, allowlist, path confinement — already corrected in P6, keep accurate).
   - **Modes:** oneshot (simple, non-interactive — note tools need the explicit bypass to run) vs **streaming** (interactive Telegram approval, multi-project, concurrency — recommended). How to choose (`ENGINE_MODE`).
   - **Full command set** (cross-check against the registered handlers in `bot.py`): `/help /reset /pwd /cd /projects /new /switch /rm /to /cancel /yolo` (+ any others actually registered).
   - **Multi-project + concurrency** (P4/P5): named projects, per-project sessions, concurrent runs + the queue, notifications.
   - **Config table:** every var (cross-ref `.env.example` — already complete from P7).
   - **Install + keep-alive:** `pip install .` / the console command / `run.sh` / the launchd keep-alive (`deploy/`). Point at `deploy/README.md`.
   - A `docs/` index (link the ADRs 001–005 + the per-feature `docs/features/*`).
   - **Accuracy is the bar** — a reviewer cross-checks every documented command/config against the code; no documented-but-nonexistent command, no missing real one.
2. **T2 — UX path-linkify polish (the one real CODE item).** The remaining `/segment` auto-linkify spots P6 deferred: the tool-use status line (`safe_input_summary` → the `▶️ Bash(...)`/`file_path=...` line) and the permission-prompt body (`render_event` permission text, currently `parse_mode=None`). Make these render path-like values as `<code>` — needs an HTML-aware summary + escaping, and must NOT break R5's `_TurnDedup` (which compares raw text) or the SB3 body-free posture. Supervised (Implementer + reviewer); **live-verify on Telegram** (the prompt/status paths render monospace, buttons still work).
3. **T3 — minor polish.** Project-name styling consistency (`{name!r}` → `<b>`/`<code>` across the ~11 echo sites); `notify_last` prune-on-resolve (bounded today, cheap hygiene — only if low-risk); and **document** (not necessarily fix) the `_is_resume_failure` text-heuristic limitation in an ADR/code-comment (confirming it against the real SDK needs a torn-resume, hard to force — record as a known heuristic). Keep tiny + low-risk.
4. **T4 — verify + merge.** Docs accuracy cross-check (commands/configs vs code); live-verify the T2 linkify renders correctly on the real bot (paths monospace in the tool-status + permission prompt, buttons functional, no dup); gates green → per-phase Codex QA → merge to `main`.

## Inherited facts / constraints
- Gates (worktree `.venv`): `pytest`, `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`. Floor: 863 tests.
- R5 `_TurnDedup` compares RAW `TextEvent.text`/`ErrorEvent.message` — any rendering change must leave the raw text path intact. SB3: keep notifications/errors body-free; don't put secrets/paths-with-content into logs.
- `.env` gitignored + holds the token; one bot instance per token; live-verify via the logged-in Telegram Web browser ([[telegram-browser-verify]]). Don't rotate the token (owner does at P9-ready).
- Feeds **P9** (public release — HARD STOP): P8's accurate README + docs index are what a public visitor reads; P9 finalizes LICENSE + makes it public (owner go required).
