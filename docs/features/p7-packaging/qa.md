# P7 QA — packaging & keep-alive

_Cross-model Codex QA on the P7 diff (`git diff d5596e4..HEAD`). **Verdict: NO_SHIP** — 2 blockers + 1 non-blocking. NOT merged. Functional verify (T5) PASSED (clean install + live launchd restart-on-kill), but Codex found real robustness/footgun issues the same-model T1/T5 verifiers dismissed as "transient" — cross-model earned its keep again. **Fix these on restart → re-QA → merge.**_

## Blockers (must fix before merge)
- **B1 — `claude_tg/cli.py` import is not self-contained (cwd-shadow).** `cli.py` does `from main import main` at import time; `main` is shipped as a top-level py-module. Running the installed `claude-telegram-bot` / `python -m claude_tg` from a directory that contains an UNRELATED `main.py` imports that file instead of the shipped one (CWD precedes site-packages on `sys.path`). The T1/T5 subagents hit this and called it a "transient cwd artifact" — Codex is right that it's a real packaging-robustness bug for an installed tool.
  - **Fix:** make the package self-contained — move `main.py`'s startup logic INTO the package (e.g. `claude_tg/app.py` or expand `claude_tg/cli.py`), and make root `main.py` a thin shim the OTHER direction (`from claude_tg.cli import main; main()`). Then no entry depends on a top-level `main` module. Update `tests/test_main.py` (it patches `main.*`) + the `[tool.setuptools] py-modules=["main"]` line (likely no longer needed). Re-verify both install paths + `python -m claude_tg --version` from `/tmp`.
- **B2 — `deploy/install-launchd.sh` can create a second poller.** It unloads any existing agent then loads a new always-on poller, but does NOT check for an already-running MANUAL bot (`./run.sh` / `python main.py`). Installing while a manual bot is live → two pollers on one token → Telegram `Conflict`.
  - **Fix:** before loading, `ps`-check for a running bot process (`main.py`/`claude_tg`/`claude-telegram-bot`) and warn+abort (or offer to stop it) — not just unload the LaunchAgent. Mirror the README's one-instance rule in the script itself.

## Non-blocking (fix with B1/B2)
- **`deploy/install-launchd.sh` plist substitution is unescaped** (~lines 113/131): argv/path values are injected into the plist XML without XML-escaping, so a path containing `&`/`<`/`>` renders an invalid plist. **Fix:** XML-escape the substituted values.

## State
- All 5 P7 tasks built; **861 tests green**, ruff/mypy/secret_scan clean; functional T5 live-verify PASSED. The above are the only items between here and merge. Phase reset to `built` (NOT approved — has blockers). `main` is unaffected (P7 never merged).
