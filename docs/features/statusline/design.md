# STATUSLINE — a live mobile statusline pinned at the top of the chat

- **Status:** Draft — orchestrator reviews before `/plan`.
- **Date:** 2026-06-24
- **Deciders:** repo owner
- **Branch / base:** `feat/statusline` off `main` `cc7bb8b`.
- **Spikes:** (1) context-window-used % feasibility and (2) the reasoning-EFFORT
  representation, both run against the installed SDK (`claude-agent-sdk==0.2.105`) and
  PTB `21.11.1` with **real probes** (live `query()` turn + `ClaudeAgentOptions`
  introspection), recorded verbatim in §2. **Both unknowns resolved — no fallback needed.**

---

## 1. Vision

Claude Code's **terminal** statusline is a single always-present line that condenses the
session's state — cwd, model, context-window usage, mode. This feature brings that to the
**phone**: one message **pinned at the top of the Telegram chat and edited in place**, so
the operator always sees the bot's current state at a glance without scrolling, and without
a re-ping on every update.

```
📁 claude-telegram-bot-statusline · 🤖 opus·max · 🧠 ctx 6% · 🔒 gate
```

(with a small working/idle marker during a turn, e.g. a leading `⚙️` while a turn runs.)

It **replaces** the per-turn `✅ done (success) · 1 turn · $0.20` footer and **removes
dollar amounts from all routine output**. Cost survives only on `/status` (the explicit
health view). The statusline is the calm, persistent answer to "what is the bot doing and
how is it configured right now," updated silently as state changes.

**Fields (owner-locked format):**

| Field | Source | Notes |
|---|---|---|
| `📁 <worktree>` | active project's name (or cwd-basename) | already tracked: `store.get_active` + `_project_label`/`_basename_of` |
| `🤖 <model>·<effort>` | per-project model override → `CLAUDE_MODEL` → SDK default; per-project effort knob | model already tracked (`/fast`·`/deep`·`/auto`); **effort is a NEW knob** (§3.2) |
| `🧠 ctx <X%>` | live `ClaudeSDKClient.get_context_usage()['percentage']` | **honest**, matches the CLI `/context`; spike-proven (§2.1) |
| `🔒 <mode>` | permission posture: `gate` / `yolo` / `plan` | already tracked (`get_project_yolo`, `plan_next`) |
| working/idle | per-project `_ProjectRuntime.status` enum (ADR-005 D7) | `running`/`awaiting_*` → working; `idle` → idle |

---

## 2. Spike findings (with probe evidence)

Both spikes were run with the READY main venv python against the installed SDK. Evidence is
the **raw probe output**, not an assumption.

### 2.1 ctx (context-window used %) — **FEASIBLE, honest, first-class**

The SDK exposes a dedicated method on the **live client** the bot already keeps open for the
whole turn (`StreamingSession`/`adapter_sdk` drives `ClaudeSDKClient` and holds `self._client`
across `connect()` → `query()` → `receive_response()`):

```python
ClaudeSDKClient.get_context_usage() -> ContextUsageResponse  # confirmed present on the class
```

`ContextUsageResponse` (TypedDict, `claude_agent_sdk/types.py:759`) — the **exact** shape:

```
categories: list[ContextUsageCategory]   # per-category breakdown (system tools, messages, …)
totalTokens: int                          # tokens currently in the context window
maxTokens: int                            # effective limit (may be reduced by autocompact buffer)
rawMaxTokens: int                         # RAW model context-window size
percentage: float                         # % of context used (0-100)  ← exactly what we show
```

**Live probe** (real opus-4-6 turn at `effort="max"`, then `await client.get_context_usage()`
after the response drained, client still connected):

```
=== get_context_usage() keys === ['categories','totalTokens','maxTokens','rawMaxTokens',
   'autocompactSource','percentage','gridRows','model','memoryFiles','mcpTools','agents',
   'slashCommands','skills','autoCompactThreshold','isAutoCompactEnabled','messageBreakdown','apiUsage']
CU summary: {"totalTokens": 12998, "maxTokens": 200000, "rawMaxTokens": 200000,
             "percentage": 6, "model": "claude-opus-4-6"}
```

**Findings:**

- **`percentage` is the honest figure.** It is the same number the CLI `/context` command
  shows (the SDK docstring says so explicitly). We display `round(percentage)` → `ctx 6%`.
  No fabrication, no derived ratio of our own.
- **The model's context WINDOW is exposed** — `rawMaxTokens: 200000` (and per-model
  `contextWindow: 200000` appears in `ResultMessage.model_usage`, see below). **No model-id→
  window mapping table is needed.** (If the owner enables the `context-1m-2025-08-07` beta via
  `ClaudeAgentOptions.betas`, the SDK's `rawMaxTokens`/`percentage` track it automatically —
  we never hard-code 200k vs 1M.)
- The method requires only a **connected client** — no `include_partial_messages`, no special
  mode. The probe called it right after `receive_response()` completed and it returned cleanly.

**The honest fallback (only if `get_context_usage()` is ever unavailable / raises):** the bot
ALSO has the raw token figures on every `ResultMessage.usage` (already plumbed — see §2.3).
`ResultMessage.usage` from the same probe:

```json
{"input_tokens": 3, "cache_creation_input_tokens": 12995, "cache_read_input_tokens": 0,
 "output_tokens": 5, ...}
```

and `ResultMessage.model_usage`:

```json
{"claude-opus-4-6": {"inputTokens": 3, "cacheReadInputTokens": 0,
   "cacheCreationInputTokens": 12995, "contextWindow": 200000, "maxOutputTokens": 64000, ...}}
```

So the honest fallback `ctx %` (when the live method is unavailable) is
`(input_tokens + cache_read_input_tokens + cache_creation_input_tokens) / contextWindow`
using the per-model `contextWindow` the SDK reports — the LAST turn's input tokens ≈ the
current context size, exactly as the brief reasoned. **But the primary path is the
first-class `percentage`; the fallback is a defensive RB1 branch, not the design.**

**Decision (ctx):** show `🧠 ctx <round(percentage)>%` from `get_context_usage()`, refreshed
at turn end (when the client is alive and the context just grew). If the call raises, fall
back to the usage-derived %; if that is also unavailable (no turn yet), show `🧠 ctx —`
(an em dash, not a fake 0%).

### 2.2 reasoning EFFORT — **FIRST-CLASS, settable, distinct from `/thinking`**

`ClaudeAgentOptions` has a dedicated `effort` field (introspected live):

```
effort: typing.Literal['low', 'medium', 'high', 'xhigh', 'max'] | None  (default=None)
```

The SDK exports the alias `EffortLevel = Literal['low','medium','high','xhigh','max']`
(`types.py:33`). The field docstring (`types.py:1929`):

> Controls how much effort Claude puts into its response. **Works with adaptive thinking to
> guide thinking depth.** low → minimal/fastest … high → deep reasoning (default) … xhigh →
> extended (Opus 4.7 only; falls back to high) … **max → maximum effort.**

**Live probe:** a real `ClaudeSDKClient` turn built with
`ClaudeAgentOptions(model="claude-opus-4-6", effort="max", …)` connected and completed
successfully (the init frame even carried a `fast_mode_state` key) — so `effort="max"` is
**accepted by the installed SDK end-to-end**, not just present as a field.

**`effort` vs the existing P12 `/thinking`:**

- **`/thinking` (P12)** sets `thinking={"type":"adaptive","display":"summarized"}` +
  `include_partial_messages=True`. It is a **visibility** toggle — it makes Claude's reasoning
  *streamable as the 🧠 line*; it does **not** dial reasoning depth, and it forces partial-
  message wire traffic. It is sticky per-project, default OFF.
- **`effort`** is a **depth/intensity** dial (low→max). It is orthogonal: it changes how hard
  Claude thinks, costs nothing extra in wire traffic, and is what "opus at **max**" refers to.
  `max_thinking_tokens` is the **deprecated** budget knob (the docstring says "Use `thinking`
  instead… 0=disabled, any other value=adaptive"); **effort is the modern, level-based knob** —
  this is the one to use, not `max_thinking_tokens`.

**Mapping "opus at max":** `model = DEFAULT_DEEP_MODEL ("claude-opus-4-8")` + `effort = "max"`.
Display: `🤖 opus·max`.

**Decision (effort):** add a NEW per-project effort knob (a `/effort` command, §3.2) that
threads `effort=<level>` into `ClaudeAgentOptions` via `_build_options` (exactly parallel to
how `model`/`thinking` thread in today), persisted on the project like the model override,
and DISPLAYED in the statusline. Default = unset → omit the `effort` kwarg → SDK default
(`high`). This is the cleanest mapping of the owner's "opus at max" to a concrete SDK setting.

### 2.3 Existing usage/cost plumbing (reuse, don't reinvent)

- The adapter maps `ResultMessage` → `ResultEvent(num_turns, total_cost_usd, …)`
  (`adapter_sdk.py:243`). `ResultEvent` carries `total_cost_usd` (`engine/types.py:194`).
- The turn loop already accumulates cost: on each `ResultEvent` it calls
  `store.add_cost(chat_id, turn_name, event.total_cost_usd)` (`stream_session.py:3544`),
  persisted per-project, surfaced on `/status` (`bot.py:463`).
- **The done-footer we replace** is `render._render_result` + `done_footer_suffix`
  (`render.py:1823`,`1853`): it appends `· N turns · $X.XX` to the result. **This is the exact
  string the statusline supersedes** for routine output.

**Conclusion:** the bot already has every datum the statusline needs. Effort is the only NEW
piece of state to track; everything else (worktree, model, mode, status enum, cost-for-`/status`)
is already plumbed.

---

## 3. Grounded scope

### 3.0 What changes vs. what is reused

**Reused as-is (no change):**
- `ChatSendGate` (`render.py:2328`) + `_gated_send`/`_gated_edit` (`stream_session.py:884`,`916`)
  — the per-chat ~1 msg/s budget. **The statusline's pin/edit go through this same gate** so it
  can never flood (RB5), and as a **non-verbatim** edit it yields to real output (a prompt/answer
  is never starved by a statusline refresh).
- The per-project `_ProjectRuntime.status` enum + `project_status` (`stream_session.py:2709`)
  for the working/idle marker.
- `store.get_active` / `_project_label` / `_basename_of` for the worktree name.
- `get_project_yolo` / `plan_next` for the mode field.
- `code_path` (`render.py:1513`, the P8 fix) — **the worktree name is rendered through nothing
  that linkifies**; see SB3 (§4) — but any path-shaped value MUST use `<code>`.

**New:**
1. A pure **statusline formatter** in `render.py` (`format_statusline(...) -> str`) — body-free,
   unit-testable, no I/O. Mirrors the existing pure-render helpers (`done_footer_suffix`,
   `notify_*`).
2. A **pin/edit-in-place mechanism** on `StreamingSession` (a per-chat pinned-message id +
   text, parallel to the per-project transient status line), driven through the send-gate.
3. A per-project **effort knob** (`_ProjectRuntime.effort` + `store` persistence +
   `_build_options` threading) and a **`/effort` command**.
4. **Removal** of the done-footer dollar amounts from routine output (`_render_result` /
   `done_footer_suffix`), keeping cost on `/status` only.

### 3.1 The pinned-statusline mechanism

**Pin once, edit silently thereafter.** Per chat the bot keeps `statusline_message_id` +
`statusline_text` (new fields on `_ChatState`, transient/in-memory — RB3, like `send_gate`):

- **First update for a chat:** `send_message(text=…)` then `pin_chat_message(message_id=…,
  disable_notification=True)`. Telegram shows it in the chat's pinned bar at the top.
- **Subsequent updates:** `edit_message_text(message_id=…, text=…)` only — **no re-pin, no
  re-send, no notification.** A pinned message edited in place stays pinned and silent.
- **Identical-text skip:** like `_edit_status` (`stream_session.py:3928`), if the new text
  equals `statusline_text` we **skip the edit entirely** (Telegram raises "message is not
  modified" on a no-op edit, and it needlessly consumes a send slot).

**One pinned message per chat.** Telegram allows multiple pins but shows ONE "current" pin in
the bar; we keep exactly one statusline message and only ever edit it. We never pin a second.

**WHEN it updates** (the trigger set — kept small so it can't churn):
- **Turn start** — flip the working marker on; refresh model/effort/mode/worktree.
- **Turn end** — flip the working marker off; refresh `ctx %` (the context just grew, and the
  client is alive for `get_context_usage()`).
- **Project switch** (`/switch`, `[Open <project>]` tap) — worktree + per-project model/effort/
  mode all change.
- **Model / effort / mode change** (`/fast`·`/deep`·`/auto`, `/effort`, `/yolo`·`/unyolo`,
  `/plan`) — the changed field.
- **NOT** on every event/delta — the statusline is **state**, not a progress bar. Mid-turn
  progress stays on the existing transient 💭/🧠/▶️ status line (unchanged). This bounds edits
  to a handful per turn, far under the gate budget.

**Foreground only.** Under concurrency (N projects per chat, ADR-005) the statusline reflects
the **chat's active (foreground) project** — the one `store.get_active` returns, the one the
operator is watching. A background project's turn start/end does NOT rewrite the statusline
(its progress is the 🔔/✅ ping, D4). This keeps the single pinned line coherent: it always
describes "what you're looking at."

### 3.2 The `/effort` command (RECOMMENDED — new)

Parallel to `/fast`·`/deep`·`/auto` and `/thinking`:

- `/effort <low|medium|high|xhigh|max>` — set the active project's effort level (persisted).
- `/effort` (bare) or `/effort default` — clear the override → SDK default (`high`).
- Streaming-mode only (like `/fast`·`/thinking`); a clean "applies to streaming mode" notice
  otherwise.
- **Applies on the NEXT fresh session, never mid-turn** — effort is a session-creation param
  baked into `ClaudeAgentOptions` (identical lifecycle to `model`/`thinking`; the warm-engine
  fast-path must include effort in its match-key so a change rebuilds on the next turn — see
  the `engine_thinking`/`engine_mode` match pattern at `stream_session.py:446`).

**Why a command (not auto):** effort is a real knob the owner wants to *drive* ("opus at max"),
not just observe. It belongs with the other per-turn routing knobs and is displayed in the
statusline so the current level is always visible. **Decision: add `/effort`.**

### 3.3 Removing the done-footer + dollars

- `_render_result` (`render.py:1853`): drop `done_footer_suffix` from the result render. A
  result with prose renders the prose (no `· N turns · $X.XX` tail); a result with no prose
  renders a bare `✅ done (<subtype>)` (no dollar suffix). The **statusline** is now the
  persistent "state after the turn" surface.
- `done_footer_suffix` (`render.py:1823`): either delete it or repurpose to turns-only for
  `/status`. **Dollars are removed from every routine path.** Cost stays ONLY on `/status`
  (`bot.py:463`, unchanged — still reads `store.get_cost`).
- The transient 💭/🧠/▶️ status line and its turn-end delete (`stream_session.py:3654`) are
  **unchanged** — that is mid-turn progress, separate from the pinned statusline.

### 3.4 Explicitly OUT of scope (deferred, P13/ADR-006 discipline)

- **`/context` full breakdown.** `get_context_usage()` returns per-category detail
  (`categories`, `mcpTools`, `memoryFiles`). The statusline shows only the headline `%`. A
  future `/context` command could surface the breakdown; **deferred** (trigger: owner asks to
  see per-category usage).
- **Rate-limit display.** ADR-001 names a `rate_limit` status carrier; the SDK exposes
  `RateLimitInfo`/`RateLimitStatus`. **Not** in the locked format; deferred (trigger: owner
  hits rate limits and wants them on the line).
- **Session-id / cwd in the line.** The locked format shows the worktree *name*, not the full
  cwd or session id (those stay on `/status`/`/projects`). Keeps the line phone-sized.
- **Proactive `⏰` marker.** A proactive turn (P14) could mark the statusline; deferred unless
  the owner asks (the per-turn `⏰` header already exists for proactive fires).

---

## 4. SB / RB rules (security + robustness)

The statusline is a render output → the same boundaries that govern every other output apply.
The **most load-bearing constraint is SB3 (body-free)**.

- **SB1 (allowlisted chat only).** The statusline is sent/pinned/edited ONLY in the operator's
  allowlisted chat. It is driven from `StreamingSession` turn/command paths that are already
  behind the bot's `_ok`/`_authorized` SB1 recheck; the pin/edit closures target the same
  `chat_id` as every other send. **No new outbound surface** — it reuses the existing
  send/edit closures the bot injects. A statusline update is never sent to a non-allowlisted
  chat (there is no code path that would).
- **SB3 (body-free; no secret / no path-as-fake-link).** Every field is **bot-derived state,
  not a body or secret:**
  - `worktree` = an SB4-validated project NAME (`^[A-Za-z0-9_-]{1,32}$`) or a cwd-basename
    sanitized to that charset (`_sanitize_attach_name`). **No raw cwd, no full path** → no
    `/segment` linkification risk. (If a basename ever needs showing and could contain path-
    shaped text, it MUST go through `code_path` per the P8 fix — but the SB4 charset has no
    `/`, so the validated name is inert. The formatter asserts/sanitizes to the SB4 charset.)
  - `model` = a config/SDK constant id reduced to `opus`/`sonnet`/`haiku`/`default` (a regex
    over the id) — never user input.
  - `effort` = one of the five fixed SDK literals — never user input.
  - `ctx %` = an integer 0–100 from the SDK — a number, never a body.
  - `mode` = one of the three fixed words `gate`/`yolo`/`plan`.
  - **No tool input, no file content, no command text, no session id, no dollar amount** ever
    reaches the statusline. There is structurally nothing in it from which a secret could leak.
  - The formatter is **pure** and HTML-escapes any interpolated value defensively (the names
    are SB4-clean, but escape-once is cheap insurance — mirrors `cmd_status`). Sent with
    `parse_mode=None` (plain) unless a `<code>`-wrap is needed, in which case `parse_mode="HTML"`
    with `code_path`.
- **SB4 (name validation).** The worktree name is the store's already-validated project name;
  the formatter does not accept an arbitrary string (it pulls from `get_active`/the record).
- **RB1 (never crash a turn).** **A pin/edit failure must NEVER break a turn.** Every
  statusline I/O is **best-effort**, wrapped in try/except that logs at debug and swallows —
  identical discipline to the transient-status-line delete (`stream_session.py:3654`), the
  cost-accumulate (`:3549`), and `_notify_*`. The statusline is an **observer off the turn's
  critical path**: if `get_context_usage()` raises, the turn is unaffected and the line shows
  `ctx —`; if `pin_chat_message`/`edit_message_text` raises (message deleted, too old, API
  hiccup), it is swallowed and the next update re-creates the line.
  - **If the user unpins it / deletes it:** the next update's `edit_message_text` will raise
    ("message to edit not found"); we catch it, clear `statusline_message_id`, and **re-send +
    re-pin** a fresh line (mirrors the orphaned-status-line recovery at `_edit_status`,
    `stream_session.py:3942`). The operator can unpin freely; the line reappears on the next
    state change. We do NOT fight the user by re-pinning on every edit — only when the edit
    target is gone.
  - **One pinned message invariant:** we hold exactly one id; on a re-send recovery we
    best-effort `unpin`/leave the stale one and pin the new (Telegram's "current pin" is the
    newest, so the bar self-corrects).
- **RB3 (transient).** `statusline_message_id`/`text` and the per-project `effort` flag are
  **in-memory only, never persisted** for the runtime marker — a restart drops the pin
  reference (the bot re-creates the line on the first post-restart update). **The effort
  *override* IS persisted** on the project (like the model override) so it survives a restart;
  only the live pin id is transient. (Consistent with ADR-004 D3 / ADR-003 D7: posture knobs
  that the owner sets deliberately persist; live-turn scaffolding does not.)
- **RB5 (rate-limit safe).** All statusline I/O funnels through `ChatSendGate` as non-verbatim
  edits; the trigger set (§3.1) is a handful of updates per turn. It cannot flood and cannot
  starve verbatim output.

**No ADR is violated.** ADR-001 (model coupling): model stays a session-creation param read
back for display, never hot-swapped. ADR-003 (gate/yolo loud): the statusline makes the mode
*more* visible (always-on `🔒 yolo`), reinforcing D6. ADR-005 (send-gate, per-project status):
reused directly. The Explore sub-audit confirmed compatibility with all eight ADRs; SB3 is the
binding constraint and the design is structurally body-free.

---

## 5. Build-order task breakdown

Small, independently-verifiable tasks. Gates per task: **pytest + ruff + mypy + secret_scan**
all green. SDK stays pinned. Clean single-line commits, **NO Co-Authored-By trailer**. Each
task lands only on green + approval (supervised `/build`).

### T1 — Effort knob: store persistence
- **Add** `JsonSessionStore.set_effort(chat_id, name, effort)` / `get_effort(chat_id, name)`
  to `session_store.py` — exact parallel of `set_model`/`get_model` (atomic + 0600, RB6,
  case-insensitive, `None` clears, bad value → `None`).
- **Validate** effort against `{low,medium,high,xhigh,max}`; an unknown value normalizes to
  `None` (RB1).
- **AC:** round-trips a value; clears on `None`; unknown → `None`; unknown project raises
  `UnknownProject`; never crashes on a sparse record. **Tests:** unit on the store (mirror the
  existing `test_session_store` model-override cases).

### T2 — Effort knob: thread into `ClaudeAgentOptions`
- **Add** an `effort` param to `Engine`/`adapter_sdk` session construction; in `_build_options`
  set `kwargs["effort"] = self._effort` when not `None` (parallel to `model`/`thinking`).
- **Add** `_ProjectRuntime.effort` + thread `_resolve_project_effort(chat_id, name)`
  (override → `None`; effort has no `CLAUDE_*` global default — SDK default is `high`) into
  `_ensure_engine`, and add `effort` to the warm-engine match-key so a change rebuilds next
  turn (mirror `engine_thinking`).
- **AC:** a project with `effort="max"` builds options containing `effort="max"`; default →
  no `effort` kwarg; a change forces a rebuild on the next turn, not mid-turn. **Tests:** unit
  on `_build_options` (assert the kwarg) + the warm-engine rebuild path (mirror the thinking-
  flag rebuild test).

### T3 — `/effort` command + `set_effort` on the session
- **Add** `StreamingSession.set_effort(chat_id, level)` (parallel to `set_model`/`set_thinking`:
  resolve/auto-create active project, persist, RB1-swallow).
- **Add** `cmd_effort` to `bot.py`; register it; add to `HELP_TEXT` + `COMMAND_MENU`.
  Streaming-mode-only guard + a clean notice otherwise; SB1 `_ok` recheck.
- **AC:** `/effort max` confirms + persists; `/effort` (bare) shows usage / clears; bad level →
  clean error; one-shot mode → notice; unauthorized chat → no-op. **Tests:** command unit tests
  (mirror `cmd_thinking`/`cmd_fast` tests) with a fake session.

### T4 — Pure statusline formatter (`render.py`)
- **Add** `format_statusline(*, worktree, model_label, effort, ctx_pct, mode, working) -> str`
  — pure, body-free, the locked format. A helper `model_short_label(model_id)` (regex →
  `opus`/`sonnet`/`haiku`/`default`). `ctx_pct=None` → `ctx —`. HTML-escape defensively.
- **AC:** exact format for a full set; `ctx —` when `None`; `opus·max`; working marker present/
  absent; an unexpected/odd model id falls back to the raw id (RB1); no path/secret can appear
  (the inputs are constrained). **Tests:** pure unit tests over the formatter + label helper
  (no I/O), including SB3 cases (a name with `<`/`&` is escaped; a path-shaped name is rejected/
  sanitized).

### T5 — ctx % source (`get_context_usage` + honest fallback)
- **Add** a best-effort `Engine.context_percentage()` (or a method on the adapter) that calls
  `self._client.get_context_usage()` and returns `round(resp["percentage"])`; on any exception
  or no client → `None`. Add the **usage-derived fallback** from the last `ResultMessage.usage`
  (carry the last-turn `input+cache_read+cache_creation` tokens and per-model `contextWindow`
  on the runtime) so a `None` from the live call can still produce an honest %.
- **AC:** returns the SDK `percentage` when available; `None` (never a fabricated number) when
  the call raises or there's no client; the fallback computes the honest ratio when usage is
  present. **Tests:** unit with a fake client returning a `ContextUsageResponse`; a fake that
  raises → `None`; the usage-fallback math.

### T6 — Pin/edit-in-place mechanism (`StreamingSession`)
- **Add** `_ChatState.statusline_message_id` / `statusline_text` (transient).
- **Add** `async def _update_statusline(chat_id, *, pin, edit, unpin, send)` — builds the
  formatter inputs from current state (active project, model/effort/mode/status, ctx%), skips
  on identical text, sends+pins on first use, edits thereafter, and on an edit failure clears
  the id + re-sends+re-pins (orphan recovery). **All through `_gated_*` (non-verbatim) and all
  best-effort (RB1).** Inject `pin`/`unpin` closures from `bot.py` (like the existing
  `send`/`edit`/`delete` closures).
- **AC:** first call sends + pins (notification disabled); second call with changed state edits
  in place (no re-pin, no re-send); identical state → no I/O; an edit raising → re-send + re-pin;
  a pin/edit raising → swallowed, turn unaffected; only one id is ever held. **Tests:** unit with
  fake send/edit/pin closures asserting the call sequence + the failure-recovery branch.

### T7 — Wire the triggers (turn start/end, switch, knob changes)
- **Call** `_update_statusline` at: turn start (working on) + turn end (working off + ctx
  refresh) in `_drive_loop`; in `/switch`'s shared core; and after `set_model`/`set_effort`/
  `set_yolo`/`unyolo`/`arm_plan`. Foreground-only (skip for a background turn).
- **AC:** a foreground turn pins then refreshes at end; a `/switch` rewrites the line; a `/yolo`
  flips `🔒 gate`→`🔒 yolo`; a background turn does NOT rewrite the line; the working marker is
  on during a turn, off after. **Tests:** integration-style on the session with fakes (assert the
  statusline text at each trigger); a concurrency test (background turn leaves the foreground
  line intact).

### T8 — Remove the done-footer dollars
- **Edit** `_render_result` to drop `done_footer_suffix`; remove/repurpose `done_footer_suffix`
  (dollars gone everywhere routine). Keep `/status` cost (`bot.py:463`) untouched.
- **AC:** a result render no longer contains `$`; `✅ done (success)` has no `· N turns · $X`
  tail; `/status` still shows cost. **Tests:** update the existing `_render_result`/footer tests
  to assert no dollar suffix; assert `/status` still includes cost.

### T9 — Docs + ADR + live phone-verify
- **ADR** for the statusline (pinned-message lifecycle, the effort knob decision, ctx-% source,
  the done-footer removal). **README**: the line format, `/effort`, "cost moved to /status."
  Docs index.
- **Live phone-verify** (mandatory per project memory — the relay/pin path can't be proven by
  unit tests alone): drive Telegram Web (bot `@your_test_bot`, chat `<your-chat-id>`) and confirm:
  pin appears once (no re-ping on edits), the line updates on turn start/end/switch/`/effort`/
  `/yolo`, `ctx %` moves as context grows, unpin → reappears on next change, a failure never
  wedges a turn. Scrub evidence (UUID-grep the evidence dir before committing — memory note).
- **AC:** Codex QA iterated to SHIP (state-heavy feature — concurrency + lifecycle); reviewer
  AGREE; live-verify PASS. **Tests:** the whole suite green; gates green.

**Suggested order:** T1→T2→T3 (effort end-to-end) ‖ T4→T5 (pure pieces, parallelizable) →
T6→T7 (the pin mechanism + wiring) → T8 (footer removal) → T9 (docs + QA + phone-verify).

---

## 6. Open questions for the orchestrator

1. **Working marker glyph + placement.** Proposed: a leading `⚙️` while a turn runs (dropped when
   idle), e.g. `⚙️ 📁 … · 🤖 …`. Alternative: a trailing `· ⏳` during a turn. Owner preference?
2. **`/effort` level set.** Expose all five SDK levels (`low/medium/high/xhigh/max`) or just the
   useful subset (`low/high/max`)? `xhigh` is Opus-4.7-only (falls back to `high`). Recommend
   exposing all five (the SDK validates) but documenting `xhigh` as model-dependent.
3. **Ctx refresh cadence.** Refresh `ctx %` at turn end only (proposed — cheap, accurate), or also
   on a `/status`-style on-demand? Turn-end keeps edits minimal; on-demand `/context` is the
   deferred §3.4 item.
