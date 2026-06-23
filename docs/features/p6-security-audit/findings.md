# P6 Security Audit — Findings

_Method: cross-model Codex audit of the merged P0–P5 tree against the SB/RB invariant checklist, + direct orchestrator corroboration of the two Critical findings (file:line verified). The same-model audit subagent was blocked by a content filter mid-run; Codex + targeted corroboration carried it. Register: does the code enforce the invariant its own ADRs/docstrings document?_

## Findings table
| Invariant | Status | Severity | Evidence |
|---|---|---|---|
| SB1 authn on every inbound | **ENFORCED** | — | message/command handlers gate on `filters.Chat` + `_ok`; callbacks recheck `_authorized` before `resolve_callback` (`bot.py:777,837,859,866`) |
| SB2 path confinement incl. SDK tools | **REMEDIATED (R2)** | was Critical | cwd commands confined (`stream_session.py:1007`); SDK file/search tools now path-confined via `permissions.path_needs_approval` consulted in `engine/engine.py` `on_tool_request` (out-of-root → approval, even for SAFE/granted; yolo/ALLOW_ANY_PATH opt-outs). Bash stays unconfined by design (no static target) — see C2 detail |
| SB3 no-secret / body-free logging | **GAP** | High | session ids in debug logs (`engine/engine.py:318,323`, `stream_session.py:2253`); raw error bodies rendered to Telegram (`render.py:996`, `engine/adapter_sdk.py:223`, `claude_runner.py:175`) |
| SB4 name validation | **ENFORCED** | — | project names regex-validated before store writes (`session_store.py:48,66,295`, `bot.py:508`) |
| SB5 bypass off-default + loud | **GAP** | **Critical** | `skip_permissions` defaults True (`config.py:207`), env default True (`config.py:267`); default oneshot appends `--dangerously-skip-permissions` (`claude_runner.py:89-90`) |
| SB6 fail-closed | **GAP** | High | classifier/parsers fail closed, but the default permission bypass (SB5) + name-only auto-allow (SB2) are fail-OPEN |
| RB1 never-crash | **ENFORCED** | — | bad callbacks/handler errors caught/no-op or routed to PTB error handling (`bot.py:781,818`, `stream_session.py:1953,2162`) |
| RB2 clean-failure | **GAP** | High | a permission hold >120s → `driver_error`; timeout excluded from resume recovery → project wedges until restart (`engine/adapter_sdk.py:336,352`, `stream_session.py:2896`, `claude_runner.py:199`) |
| RB3 restart/resume | **ENFORCED** | — | `(session_id,cwd)` resumes lazily; stale ids cleared/fresh-started (`stream_session.py:1044,1080,1877`) |
| RB5 rate-limit | **GAP** | Med | streaming has cap + send-gate; default **oneshot** chunks replies in a tight loop with no per-chat send budget (`render.py:1366`, `bot.py:692`) |
| RB6 persistence integrity | **ENFORCED** | — | temp+atomic-replace, 0600; corrupt/unknown-version loads empty (`session_store.py:115,135,140,150,155`) |

## Critical & High (detail)

### C1 — SB5: the default mode bypasses the permission gate (fail-open default)
`Config.skip_permissions` defaults `True` (`config.py:207`) and `CLAUDE_SKIP_PERMISSIONS` defaults `True` (`config.py:267`); the default `ENGINE_MODE=oneshot` path appends `--dangerously-skip-permissions` (`claude_runner.py:89-90`). **Consequence:** a fresh install on defaults runs Claude's tools with NO operator-approval gate — confinement reduces to `ALLOWED_ROOTS` only (and see C2: that doesn't bind the SDK's tools either). Streaming mode + `/yolo` is correctly loud/transient; the issue is specifically the shipped DEFAULT. Contradicts SB5 ("bypass off-by-default + loud"). **Fix:** default `skip_permissions=False` (gate by default); require an explicit, documented opt-in; make an allow-all state loud at startup + in `/projects`/status; remove the dangerous flag from the normal path.

### C2 — SB2: tool execution is name-classified, not path-confined — **REMEDIATED (R2)**
The permission classifier is a pure function of the tool **name** (`permissions.py:41,46` — `SAFE_TOOLS={Read,Glob,Grep,LS,TodoWrite,WebSearch}` auto-run; `tool_input` "intentionally not inspected … no per-resource scoping" — an explicit P2 anti-goal per ADR-003). `resolve_within_roots` confines the bot's own `/cd`·`/new`·engine cwd (`stream_session.py:1007`) but did NOT constrain the paths the SDK's tools act on. **Consequence (pre-fix):** `Read/Glob/Grep/LS` ran with no path check (out-of-root file reads were not rejected), and an approved/session-granted `Write`/`Bash`/`Edit` acted on whatever absolute path Claude chose — `ALLOWED_ROOTS` was not an SDK tool sandbox. This is the A2 (misaligned/injected Claude) boundary, and it was weaker than the threat model assumed.

**Fix (R2 — owner-approved posture: PROMPT-ON-OUT-OF-ROOT).** A pure path predicate `permissions.path_needs_approval` extracts each file/search tool's target from `tool_input` (Read/Write/Edit/MultiEdit→`file_path`, NotebookEdit→`notebook_path`, Glob/Grep/LS→`path`), resolves it CANONICALLY against `allowed_roots` via the existing `resolve_within_roots` (so `..`/symlink traversal can't escape), and returns True (require approval) when the target is out-of-root. The engine (`engine/engine.py` `on_tool_request`) consults it with the wired config (`stream_session._default_engine_factory` passes `config.allowed_roots`/`allow_any_path` + `cwd` into the `Engine`). **Ordering:** `/yolo` (allow-all opt-out) is checked FIRST and still bypasses; then the path check (out-of-root → hold for approval **even for the auto SAFE tools and even with a session grant** — an out-of-root call always re-prompts); `ALLOW_ANY_PATH=true` disables the path policy entirely (the other opt-out). In-root behavior is UNCHANGED (safe→auto, risky→grant-or-prompt). Fail-closed (SB6): a required path that is absent/None/non-str → approval; a present-but-malformed optional path (Glob/Grep/LS `path` supplied as None) → approval, while an *omitted* optional path means "search the cwd" (an allowed root) → auto.

**Bash limitation (the honest C2 boundary — NOT confined).** An arbitrary shell command has no reliable static target, so `Bash` is deliberately NOT path-parsed. `Bash` remains RISKY name-only (prompts unless granted/`/yolo`), and a session-**granted** `Bash` is therefore **UNCONFINED** — it can act on any path. We do not pretend otherwise (documented in `permissions.path_needs_approval` / `engine.on_tool_request` docstrings). The path layer's guarantee covers exactly the file/search tools with an explicit `path`/`file_path`/`notebook_path` input. `WebFetch`/`mcp__*` are likewise not path-checked (no local-path concept; the name-only classifier gates them as RISKY).

### H1 — SB3: session ids + raw error bodies can reach logs/Telegram
Debug logs include raw `claude_session_id` (`engine/engine.py:318,323`, `stream_session.py:2253`); foreground error rendering emits raw `ErrorEvent.message` (`render.py:996`) and adapter/one-shot error text is derived from raw tool/SDK/CLI output (`engine/adapter_sdk.py:223`, `claude_runner.py:175`) — which can carry file content or secrets into a Telegram reply or a log. **Fix:** hash/redact session ids in logs; render body-free error summaries by default; keep raw errors only behind an explicit local debug artifact with a scrubber.

### H2 — RB2: a long human approval wedges the session (the P5 live-verify finding)
The streaming receive loop bounds each awaited message at 120s (`engine/adapter_sdk.py:336,352`); a human permission/ask/plan hold can legitimately last up to the answer backstop (default 60 min). The 120s liveness timeout fires during the hold → `driver_error`, and timeout is not treated as a resume failure (`claude_runner.py:199`, `stream_session.py:2896`), so the engine isn't rebuilt and the project stays unusable until restart. **Fix:** do not apply the per-message liveness timeout while a permission/ask/plan hold is open (the backstop governs human-wait); + stop/rebuild the engine on a verified-session `driver_error`. (Streaming-only.)

### M1 — RB5: one-shot replies have no per-chat send budget
Streaming has the P5 `ChatSendGate` + cap; the default oneshot reply path chunks in a tight loop (`render.py:1366`, `bot.py:692`) with no per-chat budget — a large reply can burst past Telegram's ~1 msg/s/chat ceiling. **Fix:** apply a per-chat send budget to the oneshot reply path too. (Lower priority — Telegram throttles rather than a security issue.)

## What's solid (don't re-litigate)
SB1 (authn on every inbound incl. routed callbacks), SB4 (name validation), RB1 (never-crash), RB3 (restart/resume), RB6 (atomic+0600 persistence) are enforced with evidence. The Telegram **ingress** boundary is well-built; the weaknesses are concentrated on the **bot→Claude/host** boundary (C2) and the **shipped default posture** (C1).

## Verdict — original (pre-remediation)
- **Owner's own use today — SAFE WITH CONDITIONS:** run `ENGINE_MODE=streaming` with `CLAUDE_SKIP_PERMISSIONS=false`, keep `ALLOWED_ROOTS` narrow, don't run untrusted prompts.
- **Public OSS release (P9) — NOT YET.** Blockers: **C1** (default permission bypass) + **C2** (no SDK tool path confinement); also **H1** (leakage) + **H2** (long-approval wedge).

## ✅ Remediation complete (R1–R6) — re-audited CLOSED + live-verified
All four findings fixed (supervised build, each red-green + independent reviewer; the engine/path-confinement fixes got full adversarial review). Plus the owner-reported duplicate-message bug + a Telegram UX pass.
- **C1/SB5 → CLOSED** (`b695dc8`): permission gate ON by default (`skip_permissions`/`CLAUDE_SKIP_PERMISSIONS` default False); bypass is an explicit opt-in + loud startup WARNING. Live: startup logged `skip_permissions: False`, no gate-disabled warning, and an in-root Write **prompted**.
- **C2/SB2 → CLOSED** (`78cfccd`): SDK file/search tools confined to `ALLOWED_ROOTS` (prompt-on-out-of-root, canonical resolve, even SAFE/granted; `/yolo`+`ALLOW_ANY_PATH` opt-outs; Bash documented-unconfined); live-wiring regression guard. Live: out-of-root `/etc/hosts` read **prompted**; in-root read auto-ran.
- **H1/SB3 → CLOSED** (`8428ce1`): session ids redacted in logs; raw external (`tool`/`turn`) error bodies render body-free (raw → scrubbed local log); bot-authored errors stay readable.
- **H2/RB2 → CLOSED** (`2d7f2b7`): liveness timeout suspended during approval holds; generous configurable per-message bound (300s, `STREAM_MESSAGE_TIMEOUT_SECONDS`) so long approved tools don't spuriously error; verified-session `driver_error` rebuilds the engine (no wedge); boundary-race guard. Live: a **~207s** approval **completed** (no driver_error).
- **Duplicate-message bug → FIXED** (`e77b1f3`): `result_text` re-rendered the assistant prose every turn (+ orphaned status line + double error block). Live: answer rendered **once**.
- **Telegram UX → polished** (`1cadcaa`): operator-facing paths wrapped in `<code>` (no more `/segment` fake-command-links) + HTML escaping. Live: DOM-verified monospace.
- **README → corrected** (`36925b4`): the stale "permissions bypassed by default" claims now match the secure defaults.
- **Cross-model Codex re-audit: all four CLOSED, no new issues.** Full gates green (854 tests, ruff/mypy/secret_scan clean).

### Verdict now
- **Owner's own use — SAFE** (streaming mode, the now-default gate + path confinement + allowlist hold; live-verified end-to-end).
- **Public OSS release (P9) — the security blockers C1+C2 are CLOSED.** Remaining before publishing are **not** security-code blockers but P7/P8 scope: a full **README/docs refresh** (commands, streaming/multi-project/concurrency, all configs — only the security claims were corrected here), and the recorded P8 polish (tool-summary/permission-prompt path linkify; name-styling consistency; `notify_last` prune; `_is_resume_failure` live-text confirmation). P9 remains a hard stop for explicit owner go.
