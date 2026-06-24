# P13 (trust layer) QA — cross-model Codex + orchestrator corroboration

## Round 1 — Codex: NO_SHIP (2 blockers)

### Blockers
1. **SB3 durable secret leak (audit):** `safe_input_summary` keeps the first 160 chars of an IDENT field (`command`/`path`/`url`) — correct for the *ephemeral* permission prompt, but the **durable audit log** (`_record_tool`/`_record_policy` → `audit.py`) then persists token-bearing Bash command text; a secret early in a command enters the on-disk log. The audit must be strongly body-free.
2. **Fail-OPEN for owner custom patterns:** a malformed `BASH_POLICY_EXTRA_PATTERNS` regex is silently dropped (`config.py`/`bash_policy.py`) → an owner rule meant to catch a command is lost → it auto-allows under grant/yolo.

### Non-blocking
- `AuditEvent.summary`/`decision` are arbitrary strings (safety depends on caller discipline — fixing #1 hardens the leak vector). `_record_policy` puts the summary in `decision` (muddy). Stale docs (design said "design only"; progress unchecked).

### Codex security verdict
- **ADDITIVE: AGREE** — the Bash policy is consulted at `on_tool_request:348-397` BEFORE the ordinary yolo/grant/safe auto-allow at `:419-427`; a matched command prompts/denies even under grant/yolo.
- **FAIL-CLOSED: DISAGREE** — a `classify_bash` raise IS handled fail-closed in the engine, but a malformed *custom* regex is silently dropped (fail-open for owner rules). → blocker #2.
- **SB3 audit body-free: DISAGREE** — file bodies collapse, but Bash command text (≤160 chars) is logged verbatim. → blocker #1.

## Orchestrator corroboration (same-model reviewer was content-filter-blocked — known issue [[security-review-content-filter-workaround]])
- **ADDITIVE confirmed in code:** `on_tool_request` checks the Bash policy first (Bash + mode≠off), escalates/denies a matched command before the auto-allow gate; non-matching falls through to the unchanged gate. (Matches Codex + the Implementer's mutation-probe: disabling the policy → flagged `rm -rf /` auto-allowed under yolo → tests red.)
- **FAIL-CLOSED (built-in) confirmed:** `_bash_policy_match`'s `except Exception:` → synthetic match (escalate/deny), never `None`/allow. (Codex's gap is the *custom-pattern* drop, not this path — both true.)
- **AuditEvent confirmed structural** (fields: `ts/kind/tool/summary/decision/chat_id/session_tag` — no body field) — BUT `summary` carried the prompt's command text (the leak Codex found). Fixing #1 = a stricter audit summary.

## Round 2 — fix
Audit gets a STRICTER body-free summary (collapse IDENT fields — command/path/url — to lengths, not raw text; keep the flagged pattern label) on both record paths; `BASH_POLICY_EXTRA_PATTERNS` validated at config load + fail-loud; `_record_policy` decision-token cleanup; docs refreshed. Re-QA + live-verify recorded after the fix lands.
