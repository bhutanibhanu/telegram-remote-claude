# P6 Security Audit — Findings

_Method: cross-model Codex audit of the merged P0–P5 tree against the SB/RB invariant checklist, + direct orchestrator corroboration of the two Critical findings (file:line verified). The same-model audit subagent was blocked by a content filter mid-run; Codex + targeted corroboration carried it. Register: does the code enforce the invariant its own ADRs/docstrings document?_

## Findings table
| Invariant | Status | Severity | Evidence |
|---|---|---|---|
| SB1 authn on every inbound | **ENFORCED** | — | message/command handlers gate on `filters.Chat` + `_ok`; callbacks recheck `_authorized` before `resolve_callback` (`bot.py:777,837,859,866`) |
| SB2 path confinement incl. SDK tools | **GAP** | **Critical** | cwd commands confined (`stream_session.py:1007`), but tool execution is name-only (`permissions.py:41`) — no path policy on tool inputs (`engine/engine.py:166`, `engine/types.py:379`) |
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

### C2 — SB2: tool execution is name-classified, not path-confined
The permission classifier is a pure function of the tool **name** (`permissions.py:41,46` — `SAFE_TOOLS={Read,Glob,Grep,LS,TodoWrite,WebSearch}` auto-run; `tool_input` "intentionally not inspected … no per-resource scoping" — an explicit P2 anti-goal per ADR-003). `resolve_within_roots` confines the bot's own `/cd`·`/new`·engine cwd (`stream_session.py:1007`) but does NOT constrain the paths the SDK's tools act on. **Consequence:** `Read/Glob/Grep/LS` run with no path check (out-of-root file reads are not rejected), and an approved/session-granted `Write`/`Bash`/`Edit` acts on whatever absolute path Claude chose — `ALLOWED_ROOTS` is not an SDK tool sandbox. This is the A2 (misaligned/injected Claude) boundary, and it's weaker than the threat model assumed. **Fix:** enforce a path/resource policy inside the permission decision (`can_use_tool`) — reject (or require explicit approval for) tool inputs whose `file_path`/`path`/glob/grep target / `Bash` working target resolves outside `ALLOWED_ROOTS`; treat `Bash` as unconfined unless sandboxed. (Reverses the P2 name-only anti-goal — a deliberate scope decision for the owner.)

### H1 — SB3: session ids + raw error bodies can reach logs/Telegram
Debug logs include raw `claude_session_id` (`engine/engine.py:318,323`, `stream_session.py:2253`); foreground error rendering emits raw `ErrorEvent.message` (`render.py:996`) and adapter/one-shot error text is derived from raw tool/SDK/CLI output (`engine/adapter_sdk.py:223`, `claude_runner.py:175`) — which can carry file content or secrets into a Telegram reply or a log. **Fix:** hash/redact session ids in logs; render body-free error summaries by default; keep raw errors only behind an explicit local debug artifact with a scrubber.

### H2 — RB2: a long human approval wedges the session (the P5 live-verify finding)
The streaming receive loop bounds each awaited message at 120s (`engine/adapter_sdk.py:336,352`); a human permission/ask/plan hold can legitimately last up to the answer backstop (default 60 min). The 120s liveness timeout fires during the hold → `driver_error`, and timeout is not treated as a resume failure (`claude_runner.py:199`, `stream_session.py:2896`), so the engine isn't rebuilt and the project stays unusable until restart. **Fix:** do not apply the per-message liveness timeout while a permission/ask/plan hold is open (the backstop governs human-wait); + stop/rebuild the engine on a verified-session `driver_error`. (Streaming-only.)

### M1 — RB5: one-shot replies have no per-chat send budget
Streaming has the P5 `ChatSendGate` + cap; the default oneshot reply path chunks in a tight loop (`render.py:1366`, `bot.py:692`) with no per-chat budget — a large reply can burst past Telegram's ~1 msg/s/chat ceiling. **Fix:** apply a per-chat send budget to the oneshot reply path too. (Lower priority — Telegram throttles rather than a security issue.)

## What's solid (don't re-litigate)
SB1 (authn on every inbound incl. routed callbacks), SB4 (name validation), RB1 (never-crash), RB3 (restart/resume), RB6 (atomic+0600 persistence) are enforced with evidence. The Telegram **ingress** boundary is well-built; the weaknesses are concentrated on the **bot→Claude/host** boundary (C2) and the **shipped default posture** (C1).

## Verdict
- **Owner's own use today — SAFE WITH CONDITIONS:** run `ENGINE_MODE=streaming` with `CLAUDE_SKIP_PERMISSIONS=false`, keep `ALLOWED_ROOTS` narrow, and treat the host as reachable by any approved or auto-allowed (`Read`/etc.) tool call — i.e. don't run untrusted prompts. Under those conditions the operator approves each risky tool and the ingress boundary holds.
- **Public OSS release (P9) — NOT YET.** Blockers: **C1** (default permission bypass) and **C2** (no SDK tool path confinement). Should also fix **H1** (leakage) and **H2** (long-approval wedge) before presenting it as hardened. C1 is a contained config-default change; C2 is the substantive one (a path-policy layer in the permission decision, reversing P2's name-only anti-goal).
