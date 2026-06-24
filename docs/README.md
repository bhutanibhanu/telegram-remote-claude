# Docs index

Design records and per-feature specs for the Claude Telegram bot. Start with the
[top-level README](../README.md) for install + usage; this index is the map to the
deeper design material.

## Architecture Decision Records (ADRs)

Each ADR captures one load-bearing decision, its context, and its consequences.

- [ADR-001 — Session substrate](adr/ADR-001-session-substrate.md): how the bot drives
  an interactive Claude Code session from Python, and the `(session_id, cwd)` resume
  coupling everything else builds on.
- [ADR-002 — Async answer-hold](adr/ADR-002-async-answer-hold.md): how an interactive
  prompt (ask / plan) is held open while the operator answers from Telegram, bounded by
  the ~60-minute backstop.
- [ADR-003 — Per-tool permission gating](adr/ADR-003-permission-gating.md): the
  approve-each-tool model (safe tools auto-run; risky tools prompt), allow-once vs
  allow-session, and `/yolo`.
- [ADR-004 — Multi-project sessions](adr/ADR-004-multi-project-sessions.md): named
  projects, per-project session + working directory, and the persistence schema.
- [ADR-005 — Background concurrency & correlation](adr/ADR-005-concurrency-correlation.md):
  concurrent runs, the FIFO queue, and routing each inbound answer to the project that
  owns the pending prompt.

## Feature specs

Per-feature `design.md` (+ `progress.md` / `verify.md` / `qa.md`) for the phases that
built the current bot:

- [telegram-claude-bot](features/telegram-claude-bot/design.md) — the original P0
  one-shot bot.
- [session-substrate-feasibility](features/session-substrate-feasibility/design.md) — the
  P0 spike behind ADR-001.
- [streaming-engine](features/streaming-engine/design.md) — P1 streaming engine.
- [permission-gating](features/permission-gating/design.md) — P2 per-tool approval.
- [interactive-prompts](features/interactive-prompts/design.md) — interactive ask / plan
  prompts over Telegram.
- [p4-multi-project](features/p4-multi-project/design.md) — P4 named projects.
- [p5-concurrency](features/p5-concurrency/design.md) — P5 background concurrency + queue.
- [p6-security-audit](features/p6-security-audit/findings.md) — P6 security audit findings
  and remediation (read this for the exact security posture + the documented Bash caveat).
- [p7-packaging](features/p7-packaging/design.md) — P7 `pip install` packaging + launchd
  keep-alive.
- [p8-docs](features/p8-docs/design.md) — P8 docs refresh (this pass).

## Cross-cutting

- [cross-cutting-requirements.md](cross-cutting-requirements.md) — the SB/RB security &
  reliability invariants the code is held to.
- [interactive-remote-design.md](interactive-remote-design.md) — the overall product
  design across phases.
