# P14 — Proactive & Integrated — design

> Roadmap-v2 **phase 6** — the LAST feature phase before the P15 public-release hard stop.
> A **delta** on the security-audited P0–P13 tree. Builds on **ADR-003** (the per-tool
> permission gate this feature runs *through*, never around), **ADR-007** (the durable
> body-free audit trail that must record proactive actions), **ADR-002** (the async
> answer-hold + 60-min backstop that fail-safes an unattended risky tool), **ADR-005**
> (the per-chat send gate + concurrency cap a proactive turn must reuse), and **ADR-001**
> (the atomic + `0600` persistence discipline a durable schedule store would reuse).
>
> _Scope discipline (P13 / ADR-006 style): the roadmap names THREE candidate areas. This
> design ships ONE high-value core (proactive scheduling) and **defers the other two with
> explicit triggers**. Investigation + spikes for all three are recorded below._

- **Status:** Draft — orchestrator reviews before `/plan`.
- **Date:** 2026-06-24
- **Deciders:** repo owner
- **Spikes:** MCP feasibility (`mcp_servers` wiring + gate routing) and the scheduler-fit
  probe (PTB `JobQueue` availability), both run against the installed SDK / PTB and
  recorded verbatim in §2.

---

## 1. Vision

Today the bot is **pull-only**: every turn is initiated by an operator message (or a tap).
P14 adds the **push** half — the bot does useful work **on a schedule** without being
prompted, and notifies the operator with the result. The headline capability the roadmap
calls "proactive": *"run my test suite at 9am and ping me if it's red,"* *"every hour,
check the repo and tell me if CI is failing,"* *"summarize what changed in this project
each evening."* The operator schedules a recurring (or one-shot) prompt; at fire time the
bot drives it as a normal turn through the **existing** streaming engine — same gate, same
audit, same send budget — and the result lands in the chat.

The dominant design concern, and the thing that makes this a **security-core** phase like
P13, is that **a proactive turn runs with no human present at fire time.** The whole design
is organized around one invariant: **proactive is NOT a bypass of the approval gate.** A
scheduled turn's tool-uses hit the exact same `on_tool_request` chokepoint as a typed turn;
a risky tool with no one there to tap *Allow* **holds until the 60-min answer-backstop
auto-DENIES it** (RB4). Proactive turns therefore *cannot* silently do risky things — they
fail safe. §5 reasons this through in full.

---

## 2. Spike findings (the de-risking probes)

Two uncertain capabilities were probed BEFORE scoping, per the roadmap's "spikes first"
rule. Both probes are read-only and were run against the **ready P13 venv**
(`/Users/ray/dev/claude-telegram-bot-p13/.venv/bin/python`): `claude-agent-sdk==0.2.105`,
`python-telegram-bot==21.11.1`. The probe script for MCP is preserved conceptually below;
nothing was installed or sent.

### 2.1 MCP integration — FEASIBLE, and it routes through the SAME gate (evidence)

**Q1 — Does `ClaudeAgentOptions` accept `mcp_servers`?** **YES.** The dataclass has the
field, default `{}`:

```
mcp_servers: dict[str, McpStdioServerConfig | McpSSEServerConfig | McpHttpServerConfig
                       | McpSdkServerConfig] | str | pathlib.Path  = {}
strict_mcp_config: bool = False
permission_prompt_tool_name: str | None = None
```

Constructing `ClaudeAgentOptions(mcp_servers={"github": {"type":"stdio","command":"npx",
"args":[...]}}, allowed_tools=["mcp__github__search_repositories"], permission_mode="default")`
**succeeds** (probe printed `construct_with_mcp_servers: OK`). The field accepts a dict of
typed server configs OR a path/str to an MCP config file. The SDK exposes the config types
`McpStdioServerConfig` / `McpSSEServerConfig` / `McpHttpServerConfig` / `McpSdkServerConfig`
and the in-process helpers `create_sdk_mcp_server(name, version, tools)` +
`tool(name, description, input_schema)`. A built in-process server is a plain dict
`{"type":"sdk","name":...,"instance":...}` that drops straight into `mcp_servers`.

**Q2 — Do MCP tools hit the SAME `can_use_tool` permission gate?** **YES — and the bot is
already wired for it.** The probe confirmed `can_use_tool`'s signature is **tool-name-based**:

```
can_use_tool: Callable[[str, dict[str, Any], ToolPermissionContext],
                       Awaitable[PermissionResultAllow | PermissionResultDeny]] | None
```

MCP tools surface to the model as tool names `mcp__<server>__<tool>` (e.g.
`mcp__github__search_repositories`). They flow through the **exact same** `can_use_tool`
callback the bot already installs (`adapter_sdk._make_can_use_tool` →
`engine.on_tool_request`). And the bot's classifier **already** fail-closes every MCP tool:
`claude_tg/permissions.py::is_risky` returns RISKY for *"ANY name starting `mcp__`"* (and
its docstring says so explicitly), and `path_needs_approval` documents `mcp__*` as a
"no path concept" tool that the name-only classifier governs. So **the moment `mcp_servers`
is wired, every MCP tool-use is held for operator approval exactly like Bash** — the P2/P6
gate, the P13 audit (`_record_tool` runs at the chokepoint), and the body-free
`PermissionEvent` prompt all apply with **zero new gate code**. (The Bash *policy* denylist
is Bash-only and does not apply to MCP tools — correct; MCP tools gate by name.)

**Caveat the spike surfaced (the one real wiring risk):** the engine's ask/plan **dedup**
in `engine._drain_substrate` assumes `AskUserQuestion`/`ExitPlanMode` always traverse
`can_use_tool`, and warns that a `permissions.allow` rule in `~/.claude` settings — or
`allowed_tools` — would *suppress* `can_use_tool` for a tool. **Implication for MCP:** if we
ever pre-allow an MCP tool via `allowed_tools` (or `strict_mcp_config` + a settings allow
rule), that tool would **bypass the gate**. So an MCP build MUST keep MCP tools OUT of
`allowed_tools` and rely on the name-only RISKY classification — i.e. MCP tools always
prompt. This is a documented constraint, not a blocker.

**Verdict:** MCP is **clean to wire** (one `mcp_servers` kwarg in `_build_options`) and
**already gated + audited** by construction. It is **DEFERRED** anyway — see §4 — because
the *gating* is free but the *config/auth/operational* surface (which servers, their
secrets, stdio subprocess lifecycle, per-project enablement) is a meaningful additive lift
that competes with the proactive core for review budget, and proactive is the distinctive
headline. The trigger to build it is in §4.

### 2.2 Scheduler fit — a plain asyncio task, NOT PTB JobQueue (evidence)

The bot is a long-running PTB **polling** app driven by `app.run_polling()` on a single
asyncio loop (`claude_tg/app.py`), with `concurrent_updates(True)`, a `post_init` hook
(registers the command menu) and a `post_shutdown` hook (stops every engine). Three ways a
scheduler could fire a turn into that loop were assessed:

- **PTB `JobQueue`** — the obvious candidate, BUT the probe shows it is **unavailable in
  this deployment**. PTB 21.11.1's `JobQueue` *imports*, but `application.job_queue` is
  **`None`** at runtime and PTB emits `PTBUserWarning: No JobQueue set up. To use JobQueue,
  you must install PTB via pip install "python-telegram-bot[job-queue]"`. The project pins
  `python-telegram-bot>=21,<22` **without** the `[job-queue]` extra, and `apscheduler` is
  **not installed** (`ModuleNotFoundError: No module named 'apscheduler'`). Using `JobQueue`
  would add an `apscheduler` runtime dependency. **Rejected** — avoids a new dep and the
  one-bot-per-token invariant means we don't need apscheduler's persistence/clustering.
- **`apscheduler` directly** — same new-dependency cost, more machinery than we need for a
  handful of per-chat interval/cron jobs. **Rejected.**
- **A plain `asyncio` scheduler task** — a single long-lived task (started in `post_init`,
  cancelled in `post_shutdown`) that sleeps until the next due job, fires it through the
  existing turn path, then re-computes the next wake. **CHOSEN.** It fits the existing loop
  with zero new dependencies, is trivially unit-testable with an injected clock + sleep (the
  same pattern `StreamingSession` already uses — `clock`/`sleep` are injectable), and mirrors
  the project's existing "pure-decide + caller-awaits" split (the `ChatSendGate` decides the
  wait; the session awaits it).

**How a fired turn reaches the engine (the reuse path — confirmed by reading the code):**
the scheduler does **not** re-implement turn-driving. It calls the **existing**
`StreamingSession.handle_message(chat_id, text, *, send, edit, delete, command_initiated=True)`
— the same method `_on_message_streaming` calls for a typed message. That single call gives
us, for free:

- the **per-chat send gate** (ADR-005 D8) — a proactive ping/stream funnels through the
  chat's `ChatSendGate`, so it can't flood (SB-flood);
- the **concurrency cap + FIFO queue** (ADR-005 D6) — a proactive turn fired while N turns
  run is *queued*, never dropped or run over-cap;
- the **per-project busy-guard** — a proactive turn for an already-running project raises
  `StreamingBusy` (we catch + skip-with-notice, never wedge);
- the **full gate + audit + path confinement** — `handle_message` → `_drive_turn` →
  `engine.send` → `on_tool_request` is the identical chokepoint a typed turn uses.

`command_initiated=True` is **load-bearing**: it makes the fired turn a FRESH turn that can
never be swallowed as the answer to some other project's pending free-text "Other"/reject
hold (the same reason a macro `/run` sets it — see `handle_message`'s docstring).

**The `send`/`edit`/`delete` closures** are the only new I/O. `_on_message_streaming` builds
them over `ctx.bot` + a `chat_id`; a scheduled turn has no incoming `update`/`ctx`, so it
builds the identical closures over the **`Application.bot`** captured in `post_init` (PTB
hands the `Application` there). The closures are byte-for-byte the same shape, so the render
layer and send gate are unchanged.

**Verdict:** a plain asyncio scheduler task is the **grounded, dependency-free** fit, and
the fired turn reuses the entire P5/P6/P13 stack via one existing method call.

---

## 3. Scope — what is IN

**P14 ships ONE cohesive primitive: an operator-managed proactive scheduler** that fires a
saved prompt as a normal, fully-gated turn on a recurring interval (or once), and notifies
the chat with the result. Concretely:

### 3.1 The schedule model (per chat, owner-only)

- A **scheduled task** = `(name, chat_id, schedule, prompt, project, enabled, next_run)`.
  - `schedule` is an **interval** (`every 30m` / `every 6h` / `every 1d`) in v1 — see the
    "calendar cron deferred" note in §4. Interval covers the headline use-cases ("every
    hour", "every morning" ≈ `every 24h` anchored) without a cron-expression parser.
  - `prompt` is the turn text (the same thing a macro stores — SB4: it is the engine's
    prompt, never interpolated into a shell command).
  - `project` is the **target project name** the turn runs against (defaults to the chat's
    active project at creation; pinned so a later `/switch` doesn't silently retarget it).
  - `enabled` lets the operator pause without deleting.
- **Commands** (all SB1-gated via the `_ok` allowlist recheck, mirroring `cmd_save`):
  - `/every <interval> <name> <prompt…>` — create/replace a recurring task. (Verb chosen so
    it reads naturally: `/every 1h ci "run the tests and tell me if anything is red"`.)
  - `/schedules` — list this chat's tasks (name, interval, next run, enabled, project) —
    body-free-safe (the prompt preview is the operator's own text, shown like `/macros`
    shows a macro preview; capped length).
  - `/unschedule <name>` — remove a task.
  - `/pause <name>` / `/resume <name>` — toggle `enabled` without losing the definition.
  - `/runnow <name>` — fire a scheduled task immediately (a manual proactive trigger; useful
    to test a task and as the "event" escape hatch — see §4 CI-hook deferral).
- These extend `COMMAND_MENU` + `HELP_TEXT` (the lock-step test in `tests/` pins the menu to
  the registered handlers — the new commands must be added to both).

### 3.2 Firing (the scheduler task)

- One asyncio task, started in `post_init`, cancelled in `post_shutdown` (RB1: a failure to
  start/stop never blocks the bot). Streaming mode only (one-shot has no `StreamingSession`,
  no per-chat send gate, no holds — a proactive turn there has nowhere safe to land; the
  commands reply the standard streaming-only notice in one-shot, exactly like `/projects`).
- The loop: compute the earliest `next_run` across all enabled tasks, sleep until then (via
  the injected `sleep`), fire every task now due, reschedule each (`next_run += interval`),
  repeat. A fire = build the chat's `send`/`edit`/`delete` closures over `app.bot` and call
  `streaming.handle_message(chat_id, prompt, send=…, edit=…, delete=…, command_initiated=True,
  project_override=name)`.
- **Overlap policy:** if the target project is already running (`StreamingBusy`) the fire is
  **skipped this tick** with a one-line body-free notice (`⏰ skipped <name> — still working`)
  and rescheduled for the next interval — never queued-on-top-of-itself, never wedged. (This
  is the proactive analogue of the same-project busy-guard.)
- **Result notification:** the turn's normal streamed output IS the notification (it renders
  to the chat through the gate like any turn). A small proactive **header** is prepended on
  fire (`⏰ <name> (scheduled):`) so the operator knows the turn was machine-initiated and
  which task. Body-free posture is inherited from the render layer (tool/command info already
  surfaces body-free via `PermissionEvent` / the status line — SB3).

### 3.3 Persistence (survive restart — RB3/RB6)

- Scheduled task **definitions** persist in the existing `JsonSessionStore` (ADR-001 atomic
  `0600` write discipline), namespaced per chat, alongside macros/projects. A restart
  reloads them and **re-arms** the scheduler (next_run recomputed from now — see §5 "missed
  fires"). When there is **no** state file (a stateless deploy) schedules are **in-memory +
  documented as transient** (mirrors how audit is off with no state file) — a clean,
  non-surprising degrade.
- `enabled`/`paused` is part of the persisted definition; `/yolo` and allow-session grants
  are **never** persisted (they remain in-memory per ADR-003 D7 — see the §5 security note on
  why a proactive turn must NOT inherit a stale yolo).

---

## 4. Scope — what is DEFERRED (with explicit triggers, ADR-006 style)

The roadmap's P14 line lists three areas. Two are deferred:

### 4.1 MCP servers — DEFERRED

- **Why deferred:** the spike (§2.1) proves MCP is *gated + audited for free* — but the
  *gating* is the easy 10%. The real lift is the **config + auth + operational** surface:
  which servers ship, where their secrets live (an MCP server's token is a new secret class
  the SB3/secret-scan posture must cover), the **stdio subprocess lifecycle** (a `npx`-spawned
  server is a child process the bot now owns — start/stop/health, a new RB surface), and
  per-project enablement. That is a cohesive *integration* phase of its own, orthogonal to
  proactive scheduling; bundling it would dilute the security review of the unattended-action
  core (the same reasoning P13 used to defer alerts/cost/backup).
- **Trigger to build:** the owner names ≥1 concrete MCP server they want (e.g. the GitHub MCP
  for `/pr` status, or a Sentry MCP) AND a place for its auth that the secret-scan posture
  accepts. The build is then small and well-scoped: thread `mcp_servers` into
  `_build_options`, add a config knob (`MCP_SERVERS` → a config-file path or a small typed
  map), keep MCP tools OUT of `allowed_tools` (so they always gate — §2.1 caveat), and add a
  test that an `mcp__*` tool-use is held for approval + audited. **Lowest-risk first cut:** an
  **in-process** `create_sdk_mcp_server` exposing a couple of read-only bot-defined tools (no
  external subprocess, no third-party secret) — proves the wiring end-to-end before adopting
  any external server.

### 4.2 Dedicated git/PR surface (`/diff` `/commit` `/pr` `/ci`) — DEFERRED (lowest value)

- **Why deferred (marginal value):** the bot **already drives git** — Claude runs `git
  status` / `git diff` / `git commit` / `gh pr create` via **Bash**, which since P13 is
  policy-guarded (force-push is flagged) AND audited. A typed turn *"commit this and open a
  PR"* already works, fully gated. A dedicated `/git` command would be a thin, lower-trust
  wrapper over what Bash covers — the **lowest-distinctiveness** of the three areas (the
  roadmap itself ranks it last). The one genuinely *new* thing a git feature could add is
  **proactive notify-on-git-event** (e.g. "ping me when CI goes red") — but that is just a
  **special case of the scheduler this phase ships** (`/every 10m ci "check CI status, ping
  me only if red"`), so the proactive core *subsumes* the high-value slice of git integration
  without a dedicated surface.
- **Trigger to build:** the owner wants a *one-tap* git affordance (a `/diff` that renders a
  diff as a file via the P10 `/get` machinery, or a `/pr` that wraps `gh`) frequently enough
  that typing the Bash turn is friction. Until then, Bash + a scheduled CI-check covers it.

### 4.3 True event-driven hooks (CI webhook → fire) — DEFERRED

- **Why deferred:** a real push/webhook listener means the bot opens an **inbound network
  surface** (an HTTP endpoint), which is a significant new attack surface and a posture change
  (the bot is currently outbound-only via PTB long-polling). The **interval scheduler covers
  the practical need** — a `/every 10m` CI check is the pragmatic, polling-based equivalent of
  a CI-fail hook, with no inbound surface. `/runnow` is the manual "event" trigger.
- **Trigger to build:** the owner needs sub-minute reaction to an external event AND accepts
  an inbound endpoint (with its own SB1-equivalent authentication). High bar; likely never for
  a single-operator personal bot.

---

## 5. Security model for unattended / proactive action ⭐

This is the heart of the phase. A proactive turn **runs with no human present at fire time**,
so it is the **highest-trust-stakes** path in the whole bot. The model:

### 5.1 Proactive is NOT a gate bypass — risky tools auto-DENY when unattended (the core)

A scheduled turn calls the **identical** `handle_message → engine.send → on_tool_request`
chokepoint a typed turn uses. There is **no proactive-specific tool path** and **no
pre-authorization that widens what runs.** Therefore:

- A **safe** tool (Read/Glob/Grep/LS/WebSearch/TodoWrite) auto-runs — exactly as for a typed
  turn (ADR-003 §1). A read-only proactive task ("summarize what changed") completes fully
  unattended, which is the common, valuable case.
- A **risky** tool (Write/Edit/Bash/WebFetch/`mcp__*`/unknown) with **no operator present**
  is **injected as a `PermissionEvent` and HELD** on the `PendingRegistry` — and since no one
  taps Allow, the **60-min answer-backstop auto-DENIES it** (ADR-002 / RB4) and the turn
  continues with that tool denied (the model adapts to the canned `DENIED_MESSAGE`). The
  proactive turn **cannot silently perform a risky action.** This is **fail-safe by
  construction** — it falls straight out of the existing gate; P14 adds nothing to enable it.
- An **out-of-root** file tool re-prompts → backstop-denies, same as above (P6/C2).
- A **Bash-policy-flagged** command (`rm -rf`, force-push, `curl|sh`) is escalated → held →
  backstop-denied (P13 T-BASH); in `deny` mode it is auto-denied outright.

**Decision: proactive turns run ON THE GATE; no "pre-authorized scope" is introduced in v1.**
We deliberately do **NOT** add a per-task "this task may write/run Bash unattended" grant.
Reasons: (a) the gate already gives the safe-and-useful behavior (reads run, risky holds-then-
denies) with **zero** new trust surface; (b) a per-task pre-authorization would be a *new,
persistent, unattended* allow-all-for-these-tools — precisely the posture ADR-003 D7 keeps
in-memory and restart-cleared *because* unattended allow-all is the cardinal risk. A proactive
task that genuinely needs to write/run is the operator's signal to **be present** (use
`/runnow` and tap Allow, or run it as a normal turn). The 60-min backstop on an unattended
hold means a write-heavy scheduled task simply **stalls on the first risky tool until it
backstop-denies** — annoying, not dangerous — which is the correct fail-safe bias.
*(If the owner later wants unattended writes, the trigger-able follow-up is a **narrow,
explicit, per-task read-only-OR-named-pre-approval** — e.g. "this task may run `pytest`
unattended" as a per-task allow-once-list — designed as its own reviewed delta, never an
allow-all. Flagged here, not built.)*

### 5.2 A proactive turn must NOT inherit a stale `/yolo` (the one new gate interaction)

`/yolo` is per-session, in-memory, restart-cleared (ADR-003 D7). There is a subtle hazard: if
the operator left a project in `/yolo` and a scheduled task then fires against it **while they
are away**, the proactive turn would inherit allow-all — an *unattended* allow-all, the exact
thing the posture forbids. **Decision: a proactive fire runs with the gate FORCED ON for that
turn regardless of the project's `/yolo` flag.** Mechanism: the scheduler passes a per-turn
"proactive" marker that the engine/session honors by treating `policy.yolo` as `False` for the
duration of that turn (the project's interactive `/yolo` is untouched for the operator's own
later typed turns). This is a small, explicit inversion — the proactive analogue of P13's
"a flagged Bash command re-prompts even under `/yolo`" — and it is **tested** (a proactive turn
under a yolo'd project still holds+backstop-denies a risky tool). Allow-session grants are
treated the same way for proactive turns (forced to gate), for the same reason.

### 5.3 The audit trail records proactive actions (ADR-007 tie-in)

Because a proactive turn goes through `on_tool_request`, **every tool decision it makes is
already audited** by the P13 `_record_tool` at the chokepoint (auto-allow / backstop_deny /
etc.) — body-free, with the redacted session tag. P14 **adds two `session_event`/`policy_event`
records** so the *proactive lifecycle itself* is on the durable trail (the owner wasn't
watching — they must be able to reconstruct what fired):

- a **fire** event when a scheduled turn starts (`kind=session_event`, `summary="proactive_fire
  (<name>)"`) — body-free (the task name is the operator's own label; NOT the prompt text);
- a **skip** event when a fire is skipped (busy / disabled-mid-flight) for completeness.

These reuse the **existing** `AuditLog` / `ChatBoundSink` (one writer, one schema — ADR-007),
recorded from the scheduler/session side exactly as plan/session events are today. No new audit
plumbing. `/audit` then shows the proactive fires interleaved with the tool decisions — the
operator can review "what did the bot do while I was away" body-free.

### 5.4 SB1 — only the allowlisted owner schedules/cancels

Every schedule command (`/every`, `/schedules`, `/unschedule`, `/pause`, `/resume`, `/runnow`)
is registered with the `allowed` chat filter AND does the explicit `_ok` allowlist recheck —
the same SB1 boundary as every other command. A non-allowlisted chat can neither create nor
fire a proactive task. A scheduled task is **bound to the chat that created it** and only ever
fires INTO that chat (it cannot target another chat) — so a proactive turn can never surface in
or act on behalf of a chat the owner didn't authorize.

### 5.5 Persistence / restart (RB3/RB6) + missed fires

- Definitions persist (ADR-001 `0600` atomic); on restart the scheduler re-arms. **Missed
  fires while the bot was down are NOT replayed** — on restart each task's `next_run` is
  recomputed from *now* (`next_run = now + interval`). Rationale: replaying a burst of missed
  proactive turns on startup (each an unattended, gated turn) is a flood + thundering-herd
  hazard and rarely what the operator wants ("run tests at 9am" missed because the Mac was
  asleep should fire at the *next* 9am, not immediately on wake with 14 stacked runs). This is
  the **abandon-and-lazy-resume** posture P5/RB3 already uses for in-flight turns, applied to
  schedules. Documented in HELP + the ADR.
- The schedule store write is **best-effort, never on a turn's critical path** (RB1): a failed
  schedule persist logs body-free and is swallowed — a proactive task that can't persist
  degrades to transient (fires this process, lost on restart), it never wedges a turn.

### 5.6 Flood / send-gate (SB3 surface)

A proactive turn's output flows through the **per-chat `ChatSendGate`** (ADR-005 D8) like any
turn, so concurrent proactive + typed turns can't burst past Telegram's ~1 msg/s/chat ceiling.
The proactive **header** and any skip notice are throttle-respecting sends. Tool/command info in
a proactive turn surfaces body-free through the existing `PermissionEvent` / status-line render
(SB3) — a scheduled turn never widens what is shown vs a typed turn.

---

## 6. Delta on P0–P13 (what changes, what is reused verbatim)

**New (P14):**

- `claude_tg/schedule.py` — a **pure-ish** module (no telegram, no SDK — mirrors
  `permissions.py` / `audit.py` isolation): a frozen `ScheduledTask`, an interval parser
  (`parse_interval("30m") -> seconds`, fail-loud on garbage like the other config parsers), a
  pure `next_due(tasks, now) -> (wake_at, due_tasks)` decision function, and a `ScheduleStore`
  facade over `JsonSessionStore` for persistence. All unit-testable with no I/O / no loop.
- A `Scheduler` driver (lives in `stream_session.py` or a small new module) — the single
  asyncio task: pure `next_due` decides the wake; the driver awaits the injected `sleep` and
  fires due tasks via `streaming.handle_message(..., command_initiated=True,
  proactive=True, project_override=name)`. Clock + sleep injected (deterministic tests).
- `StreamingSession.handle_message` grows two **optional, default-off** kwargs:
  `proactive: bool = False` (forces the gate on for the turn — §5.2) and
  `project_override: str | None = None` (pins the target project — a proactive task targets ITS
  project, not whatever is active). Both default to today's behavior, so every existing caller +
  test is unchanged (the same additive-kwarg discipline P10's `images` used).
- The engine honors `proactive` by treating `policy.yolo`/grants as off for that turn (a small
  flag threaded to `on_tool_request`'s allow-decision — defaults preserve current behavior).
- The six schedule **commands** + their `COMMAND_MENU`/`HELP_TEXT` entries (lock-step test).
- Two new audit records (`proactive_fire` / `proactive_skip`) via the existing sink.
- Config: `SCHEDULER_ENABLED` (default… see open question O1), `SCHEDULE_MIN_INTERVAL_SECONDS`
  (a floor so a typo `/every 1s` can't hammer the host — default e.g. 60s, fail-loud parse),
  `SCHEDULE_MAX_TASKS_PER_CHAT` (a bound — DoS-by-schedule guard).

**Reused verbatim (NOT rebuilt):** the streaming turn driver + per-project lock + concurrency
cap + FIFO queue + per-chat send gate (P5/ADR-005); the permission gate + classifier +
`PermissionEvent` + 60-min backstop (P2/P6/ADR-002/ADR-003); the durable body-free `AuditLog` +
`ChatBoundSink` (P13/ADR-007); the `JsonSessionStore` atomic `0600` persistence (ADR-001); the
`post_init`/`post_shutdown` lifecycle hooks; the SB1 `_ok` command-gating template (`cmd_save`).

**Explicitly NOT touched:** the SDK pin (`claude-agent-sdk==0.2.105`), the one-bot-per-token
invariant, the oneshot path (proactive is streaming-only), the existing gate semantics for typed
turns (proactive only *adds* "force-gate for the unattended turn", never relaxes anything).

---

## 7. Build-order task breakdown (for `/plan`)

Ordered so each task is independently testable and lands green. Acceptance criteria are
test-shaped (the gates are pytest/ruff/mypy/secret_scan — see §8).

**T1 — `schedule.py` pure core (parser + model + `next_due`).**
- Build: `ScheduledTask` (frozen dataclass: name, chat_id, interval_seconds, prompt, project,
  enabled, next_run), `parse_interval(str) -> int` (accepts `30s/15m/6h/2d`; **fail-loud** on
  garbage / below `SCHEDULE_MIN_INTERVAL_SECONDS`), `next_due(tasks, now) -> (wake_at|None,
  list[due])` (pure; only enabled tasks; ignores disabled; stable ordering).
- Acceptance: unit tests for parse (valid + each failure mode), `next_due` (empty → None;
  mixed enabled/disabled; several due at once; tie ordering); imports with **neither** SDK nor
  PTB present (mirrors `permissions.py`'s import-light test).

**T2 — `ScheduleStore` persistence over `JsonSessionStore` (RB1/RB3).**
- Build: per-chat CRUD (`add`/`remove`/`list`/`set_enabled`), atomic `0600` writes reusing the
  store's discipline; load-on-init; best-effort (a write failure is swallowed body-free).
- Acceptance: round-trip persist+reload; corrupt/missing store reads as empty (never raises);
  a write failure degrades to transient, never propagates; `SCHEDULE_MAX_TASKS_PER_CHAT`
  enforced (fail-closed: refuse the create with a clean message, never silently over-cap).

**T3 — config knobs (`SCHEDULER_ENABLED`, `SCHEDULE_MIN_INTERVAL_SECONDS`,
`SCHEDULE_MAX_TASKS_PER_CHAT`).**
- Build: parsers mirroring the existing fail-loud pattern in `config.py`; defaults documented
  in `.env.example`.
- Acceptance: unset → documented default; bad value → raises at startup (a typo can't silently
  disable the floor — same posture as `parse_bash_policy_mode`).

**T4 — `handle_message(proactive=…, project_override=…)` + engine force-gate.**
- Build: the two additive kwargs (default off); `project_override` pins the target runtime;
  `proactive=True` threads a flag so the engine treats `policy.yolo`/grants as off for the turn.
- Acceptance (the **security-core test**): a proactive turn against a `/yolo`'d project STILL
  holds a risky tool and the 60-min backstop **denies** it (no silent risky action); a proactive
  turn's **safe** tool still auto-runs; a typed turn's behavior + every existing
  `handle_message` test is byte-for-byte unchanged (defaults preserved); `project_override`
  targets the named project even when another is active.

**T5 — the `Scheduler` driver task (asyncio, injected clock/sleep).**
- Build: `next_due`-driven loop; fire via `handle_message(..., command_initiated=True,
  proactive=True, project_override=name)` with `send`/`edit`/`delete` over the captured
  `app.bot`; overlap → catch `StreamingBusy`, skip-with-notice, reschedule; per-fire
  `next_run += interval`; started in `post_init`, cancelled in `post_shutdown`.
- Acceptance: with a fake clock + fake streaming, a due task fires exactly once and reschedules;
  a busy project is skipped + rescheduled (not queued-on-itself, not wedged); the task is
  cancelled cleanly on shutdown (no orphaned task, RB1); a fire-time exception is caught and the
  loop survives (one bad task never kills the scheduler — RB1).

**T6 — audit records (`proactive_fire` / `proactive_skip`).**
- Build: record via the existing `AuditLog`/`ChatBoundSink` at fire + skip; body-free (task
  name only, never the prompt).
- Acceptance: a fire writes one body-free `session_event`; `/audit` shows it; a secret in a
  task's prompt does NOT appear in the record (the gate-blocking SB3 test, mirroring P13's).

**T7 — the six commands + menu/help (SB1).**
- Build: `cmd_every`/`cmd_schedules`/`cmd_unschedule`/`cmd_pause`/`cmd_resume`/`cmd_runnow`,
  each `_ok`-gated, streaming-only-notice in one-shot; register BEFORE the skill passthrough;
  add to `COMMAND_MENU` + `HELP_TEXT`.
- Acceptance: each command SB1-gated (a non-allowlisted chat is refused — the existing pattern);
  the menu↔handler lock-step test passes (every new command in both); one-shot replies the
  streaming-only notice; `/runnow` fires immediately and is gated/audited like a scheduled fire.

**T8 — live phone-verify + docs (ADR-008).**
- Build: an ADR-008 ("Proactive scheduler — unattended action on the gate") capturing §5;
  `.env.example` + README updates.
- Acceptance: **live phone-verify** (the relay path can't be probed in-process — see the memory
  note: `engine.resolve` probes bypass the PTB callback path, so a real phone-verify is
  mandatory): create `/every 1m t1 "say hi"`, see it fire on schedule; create a task that
  triggers a risky tool, confirm it **holds then backstop-denies unattended** (or, faster,
  verify the hold appears and `/cancel` it); confirm `/schedules`/`/pause`/`/runnow`/`/unschedule`
  behave; confirm a proactive fire appears in `/audit` body-free; confirm a schedule survives a
  bot restart with `next_run` recomputed.

---

## 8. Inherited constraints (the bar every task clears)

- **Gates:** `pytest` (the full suite stays green — every existing test unchanged by the
  default-off additive kwargs), `ruff`, `mypy` (the project ships `py.typed`; new modules are
  fully typed), `scripts/secret_scan.py` (committed fixtures secret-free; the SB3 "secret not
  leaked into a schedule/audit record" test is the gate-blocking bar).
- **SDK pinned:** `claude-agent-sdk==0.2.105` — no version bump. (MCP, if ever built, uses the
  already-present `mcp_servers` field on this pin — §2.1.)
- **One bot per token** — the scheduler is a single in-process asyncio task; no second process,
  no apscheduler clustering. The single-writer audit/store invariant is preserved.
- **Clean single-line commits, NO `Co-Authored-By`** (matches the existing P0–P13 history).
- **Cross-model Codex QA → SHIP** before merge (the memory note: same-model verification missed
  7 concurrency/lifecycle bugs Codex caught on state-heavy features — and a scheduler firing
  unattended turns through the concurrency stack is exactly that shape), then **live
  phone-verify**, then per-phase merge to main (oneshot default keeps the live bot safe).
- **Safety posture unchanged:** gate-on-default, path confinement, body-free, fail-closed — P14
  only *adds* the "force-gate the unattended turn" inversion (strictly safer), never relaxes.

---

## 9. Open questions for the orchestrator/owner

- **O1 — Scheduler default on/off?** Recommendation: **`SCHEDULER_ENABLED` default ON in
  streaming mode but with NO tasks** (so the loop runs but does nothing until the owner creates
  a task) — proactive is the headline, an empty scheduler is zero-risk, and requiring a flag to
  use the flagship feature is friction. (Counter-argument for default-off: it's a new unattended
  surface. Either is defensible; flagged for the owner.) The per-task `enabled` is the real
  control regardless.
- **O2 — Interval-only vs add a daily anchor?** v1 is pure interval (`every 24h`). A natural
  next step is an anchored daily (`/daily 09:00 …`) without a full cron parser. Recommend
  shipping interval-only in P14 and treating anchored-daily as a fast follow if the owner wants
  "at 9am" precisely (interval drifts relative to wall-clock across restarts).
- **O3 — Confirm "no per-task pre-authorized scope" (§5.1).** This is the central trust call:
  v1 runs proactive turns purely on the gate (risky tools backstop-deny unattended). If the
  owner expects unattended *writes/test-runs*, that's a separate, explicitly-scoped follow-up —
  not v1. Please confirm the gate-only posture for the shippable core.
