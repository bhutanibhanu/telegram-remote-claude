Reading additional input from stdin...
OpenAI Codex v0.134.0
--------
workdir: /Users/ray/dev/claude-telegram-bot-statusline
model: gpt-5.5
provider: openai
approval: never
sandbox: danger-full-access
reasoning effort: high
reasoning summaries: none
session id: 019efbfa-3bdf-7881-80b3-0f24f0a2ecea
--------
user
You previously reviewed the STATUSLINE feature and returned NO_SHIP with 3 blockers + 1 non-blocking. They're now fixed; verify each is CLOSED and nothing regressed. Run `git diff $(git merge-base HEAD main)..HEAD` (latest commit is the fix).

Blocker 1 was: `ClaudeSDKClient.get_context_usage()` is async but `context_percentage()` called it without await → ctx % never used the real SDK API. Claimed fix: `context_percentage()` is now async and awaits it (accepts awaitable or dict); `Engine.context_percentage()` async; `_statusline_text`/`_update_statusline` await it; the test fakes were made async so the awaited path is exercised.
Blocker 2 was: a /switch between the statusline snapshot and the awaited send/edit wrote a stale previous-project line. Claimed fix: the line body is rebuilt from current state AFTER the gate-wait (in `_statusline_gated_edit`/`_statusline_send_and_pin`), so whatever is foreground at write-time lands.
Blocker 3 was: /plan mode never showed (plan_next consumed before the turn). Claimed fix: a transient `_ProjectRuntime.in_plan_turn` set from the consumed plan_turn at turn start, cleared in finally; `_statusline_text` reads `in_plan_turn OR plan_next` → `plan`.
Non-blocking was: pin-fails-after-send left it unpinned. Claimed fix: `_ChatState.statusline_pinned` tracked separately; pin retried on next update if it failed.

Verify:
1. ctx %: is `get_context_usage()` now actually awaited, and does the live SDK % win over the usage-fallback? Is it still best-effort (raise/no-client → fallback → None, never fabricated)?
2. foreground race: is the line rebuilt AFTER the gate-wait so a mid-flight /switch can't write a stale line? Any remaining await-straddle?
3. /plan: does the line show `🔒 plan` DURING a plan turn now, and revert after?
4. pin-retry: does a failed-pin-after-send get retried (not skipped by identical-text)?
5. No regression: RB1 (statusline failures never break a turn) still holds; foreground-only concurrency intact; SB3 body-free intact; default turn unchanged.

Respond ONLY in this format, no preamble:
## Blocker status
- B1 (ctx await): CLOSED | STILL-OPEN — why
- B2 (foreground race): CLOSED | STILL-OPEN — why
- B3 (/plan mode): CLOSED | STILL-OPEN — why
- NB (pin retry): CLOSED | STILL-OPEN — why
## Any new issues
- (or none)
## Verdict
SHIP | NO_SHIP
## Reasoning
(2-3 sentences)
codex
I’ll inspect the branch diff against `main`, then check the specific implementation paths and tests around statusline behavior. After that I’ll give the requested review verdict in exactly your format.
exec
/bin/zsh -lc 'git status --short && git log --oneline --decorate -5' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
?? docs/features/statusline/qa-recheck.md
59b102f (HEAD -> feat/statusline) chore(statusline): phase=built (Codex blockers fixed, re-QA pending)
67dd965 fix(statusline): close Codex blockers — await the ASYNC get_context_usage so ctx % uses the real SDK API (test fakes made async to exercise it) [B1]; rebuild the line AFTER the gate-wait so a mid-flight /switch can't write a stale line [B2]; track in_plan_turn so the line shows 🔒 plan during a plan turn (plan_next is consumed pre-turn) [B3]; track statusline_pinned separately + retry pin if it failed post-send [NB]
7309dcd qa(statusline): Verifier SHIP + Codex NO_SHIP (3 blockers — ctx not awaited, foreground-switch race, /plan mode not shown)
81cb1a8 handoff(statusline): pre-QA handoff
13a8306 chore(statusline): phase=built (T-EFFORT + T-SL-CORE + T-SL-WIRE done)

exec
/bin/zsh -lc 'git diff $(git merge-base HEAD main)..HEAD' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
diff --git a/claude_tg/bot.py b/claude_tg/bot.py
index 0c9c96a..1c6bbc7 100644
--- a/claude_tg/bot.py
+++ b/claude_tg/bot.py
@@ -57,6 +57,7 @@ from .scheduler import (
 )
 from .scheduler_driver import Scheduler
 from .session_store import (
+    EFFORT_LEVELS,
     DuplicateProject,
     InvalidProjectName,
     MaxSchedulesExceeded,
@@ -93,6 +94,9 @@ HELP_TEXT = (
     "(it executes, still per-tool gated) or Reject with feedback (it revises) (streaming mode)\n"
     "/thinking on|off — stream Claude's reasoning as a 🧠 line while it works; off by default "
     "(streaming mode)\n"
+    "/effort low|medium|high|xhigh|max — set how hard Claude reasons for this project (depth, "
+    "not visibility); persisted, applies to your next turn; bare /effort clears to the default "
+    "(streaming mode)\n"
     "/fast — use the fast model (Haiku) for this project's next turn (streaming mode)\n"
     "/deep — use the deep model (Opus) for this project's next turn (streaming mode)\n"
     "/auto (or /model default) — clear the model override, back to the default (streaming mode)\n"
@@ -149,6 +153,7 @@ COMMAND_MENU: tuple[tuple[str, str], ...] = (
     ("unyolo", "Restore the per-tool permission gate"),
     ("plan", "Run the next message in plan mode (approve the plan first)"),
     ("thinking", "Toggle the live reasoning stream: /thinking on|off (default off)"),
+    ("effort", "Set reasoning effort: /effort low|medium|high|xhigh|max"),
     ("fast", "Use the fast model (Haiku) for this project's next turn"),
     ("deep", "Use the deep model (Opus) for this project's next turn"),
     ("auto", "Clear the model override (back to the default)"),
@@ -578,7 +583,7 @@ class TelegramClaudeBot:
         else:
             await update.message.reply_text("Nothing in flight to cancel.")
 
-    async def cmd_yolo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
+    async def cmd_yolo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
         """Turn ON ``/yolo`` — every tool runs with NO approval prompt this session (P2, D6).
 
         Streaming mode only (the permission gate is a streaming-engine concept; one-shot
@@ -597,8 +602,10 @@ class TelegramClaudeBot:
             return
         self.streaming.set_yolo(update.effective_chat.id, True)
         await update.message.reply_text(yolo_banner())
+        # STATUSLINE T-SL-WIRE: 🔒 gate → 🔒 yolo flips live on the pinned line.
+        await self._refresh_statusline(ctx.bot, update.effective_chat.id)
 
-    async def cmd_unyolo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
+    async def cmd_unyolo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
         """Turn OFF ``/yolo`` — restore the fail-closed per-tool permission gate (P2, D6).
 
         Streaming mode only (mirrors :meth:`cmd_yolo`). After this, risky tools are held
@@ -616,8 +623,10 @@ class TelegramClaudeBot:
         await update.message.reply_text(
             "✅ Gating restored — risky tools will ask for approval again (/yolo is off)."
         )
+        # STATUSLINE T-SL-WIRE: 🔒 yolo → 🔒 gate flips live on the pinned line.
+        await self._refresh_statusline(ctx.bot, update.effective_chat.id)
 
-    async def cmd_plan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
+    async def cmd_plan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
         """``/plan`` — run the NEXT message in plan mode and show the plan for approval (P12).
 
         Arms a per-project, ONE-SHOT plan marker on the active project (via
@@ -645,6 +654,8 @@ class TelegramClaudeBot:
         await update.message.reply_text(
             "📋 Next message runs in plan mode — I'll show the plan for approval."
         )
+        # STATUSLINE T-SL-WIRE: 🔒 gate → 🔒 plan flips live (the armed-next-turn marker).
+        await self._refresh_statusline(ctx.bot, update.effective_chat.id)
 
     async def cmd_thinking(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
         """``/thinking on|off`` — toggle the live reasoning stream for this project (P12 T-THINK).
@@ -690,8 +701,69 @@ class TelegramClaudeBot:
                 "Applies to your next message."
             )
 
+    async def cmd_effort(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
+        """``/effort <low|medium|high|xhigh|max>`` — set the per-project reasoning EFFORT (T-EFFORT).
+
+        Sets the active project's reasoning-EFFORT level (how hard Claude thinks: ``low`` =
+        fastest/minimal … ``max`` = maximum effort). Distinct from ``/thinking`` (which only
+        makes the reasoning VISIBLE as the 🧠 line); ``/effort`` dials the DEPTH and costs no
+        extra wire traffic. Per-project + **persisted** (survives a restart, like the ``/fast``
+        model override). A bare ``/effort`` (or ``/effort default``) CLEARS the override back to
+        the SDK default. **Applies on the NEXT fresh session**, never mid-turn (it's a
+        session-creation knob, like ``/fast``·``/deep``).
+
+        Streaming mode only — effort is baked into the streaming engine's ``ClaudeAgentOptions``
+        (one-shot has no per-project session knob), so one-shot replies a clear notice rather
+        than half-working (mirrors :meth:`cmd_thinking` / :meth:`cmd_fast`). SB1: ``_ok``
+        allowlist recheck first, exactly like every command (an unauthorized chat does nothing).
+        An unrecognized level shows a clean error listing the valid levels (RB1 — never a crash);
+        ``xhigh`` is documented as Opus-4.7-only (the SDK falls back to ``high`` elsewhere).
+        """
+        if not await self._ok(update) or update.message is None:
+            return
+        if self.streaming is None:
+            await update.message.reply_text(
+                "Reasoning effort (/effort) applies to streaming mode only — one-shot mode "
+                "has no per-project session knob."
+            )
+            return
+        levels = " · ".join(EFFORT_LEVELS)
+        arg = (ctx.args[0].strip().lower() if ctx.args else "")
+        # Bare /effort (or /effort default) CLEARS the override → SDK default. A bare invocation
+        # also shows the usage so the operator sees the valid levels (mirrors /thinking's bare
+        # usage), but it DOES clear (the documented "/effort default" UX), so it is not a no-op.
+        if arg in ("", "default"):
+            self.streaming.set_effort(update.effective_chat.id, None)
+            await update.message.reply_text(
+                f"🧠 Effort cleared — your next turn uses the default reasoning effort. "
+                f"(Applies to the next session; a turn in flight keeps its current effort.)\n"
+                f"Usage: /effort <{levels}>",
+            )
+            # STATUSLINE T-SL-WIRE: the 🤖 model·effort suffix updates live (effort cleared).
+            await self._refresh_statusline(ctx.bot, update.effective_chat.id)
+            return
+        if arg not in EFFORT_LEVELS:
+            # RB1: an unrecognized level is a clean error listing the valid levels — never a
+            # crash, and the override is NOT touched (we don't guess a level).
+            await update.message.reply_text(
+                f"Unknown effort level {arg!r}. Valid levels: {levels}.\n"
+                f"Usage: /effort <{levels}>  —  or  /effort default to clear "
+                f"(xhigh is Opus-4.7-only; it falls back to high on other models)."
+            )
+            return
+        chosen = self.streaming.set_effort(update.effective_chat.id, arg)
+        # ``chosen`` is one of the five fixed SDK literals (validated above), never user input —
+        # safe to interpolate, but escape defensively for HTML (mirrors _set_model).
+        await update.message.reply_text(
+            f"🧠 Effort set to <b>{html.escape(chosen or arg, quote=False)}</b> — applies to "
+            "your next turn. A turn in flight keeps its current effort.",
+            parse_mode="HTML",
+        )
+        # STATUSLINE T-SL-WIRE: the 🤖 model·effort suffix updates live (e.g. opus·max).
+        await self._refresh_statusline(ctx.bot, update.effective_chat.id)
+
     async def _set_model(
-        self, update: Update, label: str, model: str | None
+        self, update: Update, label: str, model: str | None, *, bot=None
     ) -> None:
         """Shared body for ``/fast`` · ``/deep`` · ``/auto`` (T4 / P9; streaming mode only).
 
@@ -702,6 +774,9 @@ class TelegramClaudeBot:
         a session-creation param; never hot-swapped mid-turn). One-shot mode has no per-project
         registry, so the model toggles apply to the streaming engine only. ``model`` is a fixed,
         operator-chosen id (a config/SDK constant), never interpolated into a shell command.
+
+        STATUSLINE T-SL-WIRE: ``bot`` (the caller's ``ctx.bot``) drives the live statusline
+        refresh so the 🤖 model label flips immediately; ``None`` (defensive) skips it.
         """
         if self.streaming is None:
             await update.message.reply_text(
@@ -721,18 +796,22 @@ class TelegramClaudeBot:
                 "applies to your next turn. A turn in flight keeps its current model.",
                 parse_mode="HTML",
             )
+        # STATUSLINE T-SL-WIRE: the 🤖 model label updates live (/fast → haiku, /deep → opus,
+        # /auto → the configured default's label).
+        if bot is not None:
+            await self._refresh_statusline(bot, update.effective_chat.id)
 
-    async def cmd_fast(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
+    async def cmd_fast(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
         """``/fast`` — route the active project to the fast model (Haiku) on the next turn."""
         if not await self._ok(update) or update.message is None:
             return
-        await self._set_model(update, "fast", self.config.fast_model)
+        await self._set_model(update, "fast", self.config.fast_model, bot=ctx.bot)
 
-    async def cmd_deep(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
+    async def cmd_deep(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
         """``/deep`` — route the active project to the deep model (Opus) on the next turn."""
         if not await self._ok(update) or update.message is None:
             return
-        await self._set_model(update, "deep", self.config.deep_model)
+        await self._set_model(update, "deep", self.config.deep_model, bot=ctx.bot)
 
     async def cmd_auto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
         """``/auto`` (and ``/model default``) — clear the per-project model override.
@@ -744,7 +823,7 @@ class TelegramClaudeBot:
         """
         if not await self._ok(update) or update.message is None:
             return
-        await self._set_model(update, "default", None)
+        await self._set_model(update, "default", None, bot=ctx.bot)
 
     async def cmd_pwd(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
         if not await self._ok(update) or update.message is None:
@@ -1094,6 +1173,9 @@ class TelegramClaudeBot:
             return
         reply, parse_mode = self._switch_active(chat_id, name)
         await update.message.reply_text(reply, parse_mode=parse_mode)
+        # STATUSLINE T-SL-WIRE: rewrite the pinned line for the newly-active project (its
+        # worktree + per-project model/effort/mode all change). Best-effort (RB1).
+        await self._refresh_statusline(ctx.bot, chat_id)
 
     def _switch_active(self, chat_id: int, name: str) -> tuple[str, str | None]:
         """Switch the chat's active project to ``name``; return ``(reply, parse_mode)`` (T6/P9).
@@ -1957,21 +2039,26 @@ class TelegramClaudeBot:
             )
             return
         assert self.streaming is not None  # _require_scheduling guaranteed it
-        send, edit, delete = self._make_chat_io(ctx.bot, chat_id)
+        send, edit, delete, pin, unpin = self._make_chat_io(ctx.bot, chat_id)
         # fire_schedule sends the header, audits, drives the gated turn, and is RB1-total
         # (a busy project / fire error is a clean body-free skip — never propagates here).
-        await self.streaming.fire_schedule(schedule, send=send, edit=edit, delete=delete)
+        await self.streaming.fire_schedule(
+            schedule, send=send, edit=edit, delete=delete, pin=pin, unpin=unpin
+        )
 
     def _make_chat_io(self, bot, chat_id: int):
-        """Build ``(send, edit, delete)`` Telegram closures over ``bot`` for ``chat_id`` (P14 T-FIRE).
+        """Build ``(send, edit, delete, pin, unpin)`` Telegram closures over ``bot`` for ``chat_id``.
 
         The proactive firing path (the scheduler driver + ``/runnow``) has no incoming
-        ``update``/``ctx`` to build the per-chat send/edit/delete closures from (a scheduled
-        fire is machine-initiated), so it builds the IDENTICAL closures the message path builds
+        ``update``/``ctx`` to build the per-chat closures from (a scheduled fire is
+        machine-initiated), so it builds the IDENTICAL closures the message path builds
         (:meth:`on_message`) over the persistent :class:`~telegram.Bot` captured at startup
         (PTB hands the ``Application`` — and thus ``app.bot`` — to ``post_init``). The closures
         are byte-for-byte the same shape the render layer + per-chat send gate expect, so a
-        proactive turn renders exactly like a typed one. Pure factory (no I/O here).
+        proactive turn renders exactly like a typed one. **STATUSLINE T-SL-WIRE:** the ``pin``/
+        ``unpin`` closures (over ``Bot.pin_chat_message``/``unpin_chat_message``) let a proactive
+        turn refresh the pinned statusline through the SAME path a typed turn does — SB1-confined
+        to this ``chat_id`` (no new outbound surface). Pure factory (no I/O here).
         """
 
         async def send(
@@ -1994,7 +2081,45 @@ class TelegramClaudeBot:
         async def delete(*, message_id: int) -> None:
             await bot.delete_message(chat_id=chat_id, message_id=message_id)
 
-        return send, edit, delete
+        async def pin(*, message_id: int, disable_notification: bool = True) -> None:
+            # STATUSLINE T-SL-WIRE: silent pin (no re-ping — design §3.1); SB1-confined to this
+            # chat_id. Best-effort upstream (the session swallows a pin failure, RB1).
+            await bot.pin_chat_message(
+                chat_id=chat_id, message_id=message_id,
+                disable_notification=disable_notification,
+            )
+
+        async def unpin(*, message_id: int) -> None:
+            # STATUSLINE T-SL-WIRE: unpin a stale statusline message on orphan-recovery (the
+            # one-pin invariant). SB1-confined to this chat_id; best-effort upstream (RB1).
+            await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
+
+        return send, edit, delete, pin, unpin
+
+    async def _refresh_statusline(self, bot, chat_id: int) -> None:
+        """Refresh the chat's pinned statusline after a COMMAND-driven state change (T-SL-WIRE).
+
+        The ``/switch`` + knob commands (``/yolo``·``/unyolo``, ``/effort``, ``/fast``·``/deep``·
+        ``/auto``, ``/plan``) change a field the statusline shows (worktree / mode / model /
+        effort), so the pinned line is re-rendered to reflect it live (design §3.1). These
+        commands ALWAYS act on the chat's ACTIVE (foreground) project, so the foreground gate is
+        bypassed (``for_project=None``) — the command path IS the foreground by definition.
+
+        Builds this chat's pin/edit/send closures over the persistent ``bot`` (SB1-confined to
+        ``chat_id``) and delegates to the session's :meth:`_update_statusline` (fully best-effort,
+        RB1 — a pin/edit failure can never break the command). A no-op in one-shot mode (no
+        streaming session) and wrapped so a build/dispatch error never escapes the command.
+        """
+        if self.streaming is None:
+            return
+        try:
+            send, edit, _delete, pin, unpin = self._make_chat_io(bot, chat_id)
+            await self.streaming._maybe_update_statusline(
+                chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=None,
+            )
+        except Exception:
+            # RB1: a statusline refresh must never break the command that triggered it.
+            log.debug("command statusline refresh failed for chat %s (ignored)", chat_id, exc_info=True)
 
     # ---- messages -----------------------------------------------------------
     @staticmethod
@@ -2690,6 +2815,21 @@ class TelegramClaudeBot:
             # stale one does not linger (best-effort; the session swallows failures).
             await bot.delete_message(chat_id=chat_id, message_id=message_id)
 
+        async def pin(*, message_id: int, disable_notification: bool = True) -> None:
+            # STATUSLINE T-SL-WIRE: pin the chat's statusline message SILENTLY (no re-ping —
+            # design §3.1). SB1: targets THIS allowlisted chat_id only (same boundary as the
+            # send/edit closures — no new outbound surface). Best-effort upstream (the session
+            # swallows a pin failure, RB1).
+            await bot.pin_chat_message(
+                chat_id=chat_id, message_id=message_id,
+                disable_notification=disable_notification,
+            )
+
+        async def unpin(*, message_id: int) -> None:
+            # STATUSLINE T-SL-WIRE: unpin a STALE statusline message on orphan-recovery (the
+            # one-pin invariant). SB1-confined to this chat_id; best-effort upstream (RB1).
+            await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
+
         try:
             # P10 T1: pass ``images`` ONLY when present, so a pure TEXT turn calls
             # handle_message with the EXACT pre-P10 signature — every existing FakeStreaming
@@ -2703,6 +2843,7 @@ class TelegramClaudeBot:
             extra: dict[str, Any] = {"images": images} if images else {}
             captured_free_text = await self.streaming.handle_message(
                 chat_id, text, send=send, edit=edit, delete=delete,
+                pin=pin, unpin=unpin,
                 reply_to_message_id=reply_to_message_id,
                 command_initiated=command_initiated,
                 **extra,
@@ -2724,7 +2865,7 @@ class TelegramClaudeBot:
             except Exception:
                 log.debug("quick-reply chip dismissal failed", exc_info=True)
 
-    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
+    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
         """Inline-keyboard tap handler — **the SB1 security boundary**.
 
         A callback tap is new attack surface (SB1). PTB's ``CallbackQueryHandler`` cannot
@@ -2777,6 +2918,9 @@ class TelegramClaudeBot:
                 await query.message.reply_text(reply, parse_mode=parse_mode)
             except Exception:
                 log.debug("switch-button reply send failed", exc_info=True)
+            # STATUSLINE T-SL-WIRE: the [Open <project>] tap shares /switch's core, so refresh
+            # the pinned line for the newly-active project here too (parallel to cmd_switch).
+            await self._refresh_statusline(ctx.bot, chat.id)
             return
         # P11 T2: an [Attach] tap routes by session id. The session decoded + validated the id
         # (the session-id shape) and returned it on ``attach_session_id``; the bot performs the
@@ -2892,9 +3036,9 @@ class TelegramClaudeBot:
         """
         if self.streaming is None:
             return False
-        send, edit, delete = self._make_chat_io(bot, schedule.chat_id)
+        send, edit, delete, pin, unpin = self._make_chat_io(bot, schedule.chat_id)
         return await self.streaming.fire_schedule(
-            schedule, send=send, edit=edit, delete=delete
+            schedule, send=send, edit=edit, delete=delete, pin=pin, unpin=unpin
         )
 
     async def _post_shutdown(self, _app: Application) -> None:
@@ -2960,6 +3104,11 @@ class TelegramClaudeBot:
         # P12 T-THINK: /thinking on|off toggles the per-project live reasoning stream
         # (streaming mode only; default OFF — the explicit opt-in, transient RB3).
         app.add_handler(CommandHandler("thinking", self.cmd_thinking, filters=allowed))
+        # T-EFFORT (STATUSLINE): /effort <low…max> sets the per-project reasoning-EFFORT level
+        # (streaming mode only; persisted, applies on the next session). Same `allowed` chat
+        # filter (SB1) + registered BEFORE the on_skill_command COMMAND passthrough so it is
+        # consumed here, not forwarded to the session as a skill.
+        app.add_handler(CommandHandler("effort", self.cmd_effort, filters=allowed))
         # T4 (P9): per-project model routing (streaming mode only; the handlers reply a
         # streaming-only notice in one-shot). /model is the alias of /auto (so /model default
         # clears the override); both names route to cmd_auto. Registered with the SAME
diff --git a/claude_tg/engine/adapter_sdk.py b/claude_tg/engine/adapter_sdk.py
index 874299f..84e2262 100644
--- a/claude_tg/engine/adapter_sdk.py
+++ b/claude_tg/engine/adapter_sdk.py
@@ -26,6 +26,8 @@ is unit-testable against constructed/fake SDK objects with no live session.
 from __future__ import annotations
 
 import asyncio
+import inspect
+import logging
 import os
 from typing import Any, AsyncIterator, Optional, Sequence
 
@@ -44,6 +46,8 @@ from .types import (
     ToolUseEvent,
 )
 
+log = logging.getLogger(__name__)
+
 # StreamEvent.event["type"] values that are genuine incremental model output
 # (everything else — message_start/content_block_start/stop — is framing, not text).
 # Mirrors c1_streaming.INCREMENTAL_EVENT_TYPES (the proven C1 contract).
@@ -130,6 +134,81 @@ def _session_id_of(msg: Any) -> Optional[str]:
     return str(sid) if sid else None
 
 
+def _field(obj: Any, key: str) -> Any:
+    """Read ``key`` from ``obj`` whether it is a dict or an attribute object (defensive).
+
+    The SDK's ``usage`` / ``get_context_usage()`` shapes are TypedDicts at the type level but
+    may surface as plain dicts OR attribute objects at runtime depending on the build; this
+    reads either uniformly. Returns ``None`` when absent. Pure; never raises (STATUSLINE ctx-%).
+    """
+    if isinstance(obj, dict):
+        return obj.get(key)
+    return getattr(obj, key, None)
+
+
+def _coerce_int(value: Any) -> Optional[int]:
+    """Coerce a numeric token/percentage value to ``int``, or ``None`` (defensive, RB1)."""
+    if isinstance(value, bool):  # bool is an int subclass — never a token count.
+        return None
+    if isinstance(value, (int, float)):
+        return int(value)
+    return None
+
+
+def _usage_tokens(usage: Any) -> Optional[int]:
+    """Sum the context-relevant token fields of a ``ResultMessage.usage`` (ctx-% fallback §2.1).
+
+    ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens`` — the last turn's
+    INPUT side ≈ the current context size (design §2.1; output tokens are NOT part of the
+    context the next turn carries). Missing fields read as 0. Returns ``None`` only when the
+    whole ``usage`` is absent/odd (so the caller leaves the cached value untouched). Pure;
+    never raises.
+    """
+    if usage is None:
+        return None
+    total = 0
+    seen = False
+    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
+        n = _coerce_int(_field(usage, key))
+        if n is not None:
+            total += n
+            seen = True
+    return total if seen else None
+
+
+def _context_window_of(model_usage: Any) -> Optional[int]:
+    """The per-model ``contextWindow`` from a ``ResultMessage.model_usage`` (ctx-% fallback).
+
+    ``model_usage`` maps ``model_id -> {…, contextWindow: int, …}`` (design §2.1). One model
+    runs per turn, so the FIRST entry carrying a positive ``contextWindow`` is taken (no
+    model-id→window table is hard-coded — the SDK reports the model's true window, and a ``1M``
+    beta tracks automatically). Returns ``None`` when absent/odd. Pure; never raises.
+    """
+    if not isinstance(model_usage, dict):
+        return None
+    for entry in model_usage.values():
+        window = _coerce_int(_field(entry, "contextWindow"))
+        if window is not None and window > 0:
+            return window
+    return None
+
+
+def _percentage_of(resp: Any) -> Optional[int]:
+    """``round(resp["percentage"])`` from a live ``ContextUsageResponse``, or ``None`` (§2.1).
+
+    The spike-proven primary ctx source: the SDK's ``percentage`` (0–100, the same figure the
+    CLI ``/context`` shows). Reads the value defensively (dict or attribute object), ROUNDS
+    (``6.4`` → ``6``, ``6.6`` → ``7`` — design §2.1 says ``round(percentage)``, never truncate),
+    and clamps to ``[0, 100]`` (a number outside that range is an unexpected shape → bounded,
+    never shown raw). ``None`` when the field is absent/non-numeric (the caller then uses the
+    usage fallback). Pure; never raises.
+    """
+    raw = _field(resp, "percentage")
+    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
+        return None
+    return max(0, min(100, round(raw)))
+
+
 def normalize(msg: Any) -> Optional[Event]:
     """Map ONE raw SDK message/block-bearing message to a normalized event.
 
@@ -346,6 +425,7 @@ class SdkSubstrate:
         disallowed_tools: Optional[list[str]] = None,
         model: Optional[str] = None,
         thinking: bool = False,
+        effort: Optional[str] = None,
     ) -> None:
         self._cwd = str(cwd) if cwd is not None else None
         self._permission_mode = permission_mode
@@ -369,6 +449,17 @@ class SdkSubstrate:
         # built, so it applies to THIS session for its whole life — a change takes effect on
         # the NEXT fresh session, never mid-session (the session is rebuilt with new options).
         self._model = str(model).strip() if isinstance(model, str) and str(model).strip() else None
+        # T-EFFORT (STATUSLINE): the per-project reasoning-EFFORT override threaded into
+        # ClaudeAgentOptions(effort=…) at session-creation time (start/resume). None → omit
+        # `effort` entirely so the SDK applies its own default (`high`), exactly as before this
+        # knob. Like `model` it is a session-creation param: baked into the options when the
+        # client is built, so it applies to THIS session for its whole life — a change takes
+        # effect on the NEXT fresh session (the warm engine is rebuilt with new options), never
+        # mid-session. There is NO CLAUDE_* global default for effort (the session resolves the
+        # per-project override → None and lets the SDK default stand). Distinct from `thinking`
+        # (P12), which is a VISIBILITY toggle (display="summarized" + partials); effort is the
+        # DEPTH dial (low→max) and adds no wire traffic.
+        self._effort = str(effort).strip() if isinstance(effort, str) and str(effort).strip() else None
 
         self._client: Any = None  # ClaudeSDKClient | None (lazily typed)
         self.session_id: Optional[str] = None
@@ -383,6 +474,17 @@ class SdkSubstrate:
         # concurrent hold without a premature un-suspend. Single asyncio task per the
         # substrate contract, so no lock is needed.
         self._hold_depth = 0
+        # STATUSLINE T-SL-CORE: the honest ctx-% usage fallback (design §2.1/§5 T5). The live
+        # ``get_context_usage()`` is the primary source (``context_percentage()`` below); when
+        # it is unavailable/raises, the bot can still compute an honest % from the LAST turn's
+        # usage — the last ``ResultMessage`` carries ``usage`` (input + cache_read +
+        # cache_creation tokens ≈ the current context size) and ``model_usage[…].contextWindow``
+        # (the model's window). We stash those two numbers as each ResultMessage drains
+        # (``_capture_usage``), so a None from the live call has a derived figure to fall back to.
+        # Both default None → no fallback before the first turn completes (→ ``ctx —``, never a
+        # fabricated 0%). In-memory only (RB3); reset on stop. Single asyncio task → no lock.
+        self._last_usage_tokens: Optional[int] = None
+        self._last_context_window: Optional[int] = None
 
     # -- options -------------------------------------------------------------
 
@@ -412,6 +514,13 @@ class SdkSubstrate:
             # session-creation path (start AND resume) so a resumed session honors the
             # project's current model from the next session onward.
             kwargs["model"] = self._model
+        if self._effort is not None:
+            # T-EFFORT (STATUSLINE): per-project reasoning-EFFORT override (/effort low…max).
+            # Set ONLY when an override is present — a default (no-effort) turn NEVER sets the
+            # kwarg, so its options stay byte-for-byte the pre-knob baseline and the SDK's own
+            # default effort applies. Set on every session-creation path (start AND resume), so
+            # a resumed session honors the project's current effort from the next session onward.
+            kwargs["effort"] = self._effort
         if self._decision_callback is not None:
             kwargs["can_use_tool"] = self._make_can_use_tool()
         if self._allowed_tools is not None:
@@ -562,6 +671,7 @@ class SdkSubstrate:
                 except StopAsyncIteration:
                     break
                 self._capture_session_id(msg)
+                self._capture_usage(msg)
                 for ev in self._events_from(msg):
                     yield ev
         except asyncio.TimeoutError:
@@ -667,6 +777,84 @@ class SdkSubstrate:
         if sid and not self.session_id:
             self.session_id = sid
 
+    def _capture_usage(self, msg: Any) -> None:
+        """Stash the last ``ResultMessage``'s token usage + context window (ctx-% fallback).
+
+        STATUSLINE T-SL-CORE (design §2.1/§5 T5). Only a terminal ``ResultMessage`` carries
+        ``usage`` + ``model_usage``; for one we record the honest fallback inputs so a None
+        from the live ``get_context_usage()`` can still produce a % (:meth:`context_percentage`):
+
+        * ``tokens`` = ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``
+          from ``msg.usage`` — the LAST turn's input ≈ the current context size (design §2.1).
+        * ``window`` = the per-model ``contextWindow`` from ``msg.model_usage[<model>]`` (the SDK
+          reports the model's raw window; no model-id→window table is hard-coded — a ``1M`` beta
+          tracks automatically). We take the FIRST model entry's window (one model per turn).
+
+        **Best-effort + fully defensive (RB1):** any missing key / odd shape / exception leaves
+        the stored values UNCHANGED (we never overwrite a good figure with a broken one, and we
+        never raise on the hot receive path). ``usage`` may be a dict or an attribute object, so
+        both are probed. A non-positive/absent window is ignored (a 0 window would divide-by-zero
+        downstream). Pure-ish: only mutates the two cached ints; no I/O.
+        """
+        from claude_agent_sdk import ResultMessage  # lazy
+
+        if not isinstance(msg, ResultMessage):
+            return
+        try:
+            usage = getattr(msg, "usage", None)
+            tokens = _usage_tokens(usage)
+            window = _context_window_of(getattr(msg, "model_usage", None))
+            if tokens is not None:
+                self._last_usage_tokens = tokens
+            if window is not None and window > 0:
+                self._last_context_window = window
+        except Exception:  # pragma: no cover - defensive; never break the receive loop (RB1)
+            log.debug("usage capture failed (ignored)", exc_info=True)
+
+    async def context_percentage(self) -> Optional[int]:
+        """Best-effort % of the context window currently used — the honest ctx figure (§2.1).
+
+        **Primary path:** call the LIVE client's ``get_context_usage()`` and return
+        ``round(resp["percentage"])`` — the same number the CLI ``/context`` shows (spike-proven,
+        design §2.1). **Fallback:** if there is no live client, the method is absent, or it
+        raises, derive ``round(100 * tokens / window)`` from the LAST ``ResultMessage``'s usage
+        captured by :meth:`_capture_usage` (an honest ratio, not a fabricated number). If neither
+        is available (no client AND no completed turn yet) → ``None`` (the caller shows ``ctx —``,
+        NEVER a fake 0%).
+
+        ⭐ **ASYNC — the installed SDK's ``ClaudeSDKClient.get_context_usage()`` is a COROUTINE**
+        (verified: ``inspect.iscoroutinefunction`` is True), so it MUST be awaited or the headline
+        percentage is never read (it would return an un-awaited coroutine that the dict-extractor
+        rejects, silently degrading to the usage fallback). We await it when it returns an
+        awaitable, and still accept a plain dict (defensive — a future/sync build keeps working).
+
+        Fully best-effort (RB1): this is an observer off the turn's critical path — it NEVER
+        raises (any error / no client → usage fallback → ``None``).
+        """
+        client = self._client
+        if client is not None:
+            try:
+                getter = getattr(client, "get_context_usage", None)
+                if getter is not None:
+                    resp = getter()
+                    if inspect.isawaitable(resp):
+                        resp = await resp  # ⭐ the SDK call is a coroutine — AWAIT it (B1 fix).
+                    pct = _percentage_of(resp)
+                    if pct is not None:
+                        return pct
+            except Exception:
+                # The live call is best-effort; fall through to the usage-derived fallback.
+                log.debug("get_context_usage() failed; using usage fallback", exc_info=True)
+        # Fallback: the honest ratio from the last completed turn's usage (§2.1).
+        tokens = self._last_usage_tokens
+        window = self._last_context_window
+        if tokens is not None and window is not None and window > 0:
+            try:
+                return round(100 * tokens / window)
+            except Exception:  # pragma: no cover - arithmetic guard (RB1)
+                return None
+        return None
+
     async def stop(self) -> None:
         if self._client is None:
             return
@@ -674,6 +862,11 @@ class SdkSubstrate:
             await self._client.disconnect()
         finally:
             self._client = None
+            # STATUSLINE T-SL-CORE: drop the ctx-% fallback cache with the session — it
+            # described THAT session's context; a fresh session starts with no figure (→ ctx —
+            # until its first turn completes), never a stale carryover. RB3 (in-memory only).
+            self._last_usage_tokens = None
+            self._last_context_window = None
 
 
 __all__ = [
diff --git a/claude_tg/engine/engine.py b/claude_tg/engine/engine.py
index 92a5751..f2f482c 100644
--- a/claude_tg/engine/engine.py
+++ b/claude_tg/engine/engine.py
@@ -49,6 +49,7 @@ the engine does.
 from __future__ import annotations
 
 import asyncio
+import inspect
 import logging
 from pathlib import Path
 from typing import Any, AsyncIterator, Optional, Sequence
@@ -319,6 +320,36 @@ class Engine:
         """The current Claude session id (None before the substrate reports one)."""
         return self._substrate.session_id
 
+    # -- ctx % for the statusline (STATUSLINE T-SL-CORE) ---------------------
+
+    async def context_percentage(self) -> Optional[int]:
+        """Best-effort % of the context window currently used, or ``None`` (design §2.1/§5 T5).
+
+        Delegates to the substrate's ``context_percentage`` (live ``get_context_usage()`` →
+        honest usage-derived fallback). The statusline shows ``🧠 ctx <X>%`` when this is an
+        int and ``🧠 ctx —`` when it is ``None`` — NEVER a fabricated number. Read defensively
+        via ``getattr`` so a substrate that predates this method (or a fake in a test) simply
+        yields ``None`` (the additive-seam discipline, mirroring the optional ``fork`` keyword);
+        the call is fully best-effort and NEVER raises — it is an observer off the turn's
+        critical path (RB1).
+
+        ⭐ **ASYNC (B1 fix):** the substrate awaits the SDK's coroutine ``get_context_usage()``,
+        so this is async too. We accept either a coroutine (await it — the real path) or a plain
+        ``int``/``None`` (a sync fake / a predating substrate), so every existing seam keeps
+        working while the real awaited SDK percentage is actually read.
+        """
+        getter = getattr(self._substrate, "context_percentage", None)
+        if getter is None:
+            return None
+        try:
+            value = getter()
+            if inspect.isawaitable(value):
+                value = await value
+        except Exception:  # pragma: no cover - the substrate is already best-effort
+            log.debug("context_percentage() failed (ignored)", exc_info=True)
+            return None
+        return value if isinstance(value, int) and not isinstance(value, bool) else None
+
     # -- the decision seam (the async answer-hold) ---------------------------
 
     async def on_tool_request(
diff --git a/claude_tg/render.py b/claude_tg/render.py
index 8b693c7..45e5f90 100644
--- a/claude_tg/render.py
+++ b/claude_tg/render.py
@@ -1525,6 +1525,108 @@ def code_path(path: object) -> str:
     return f"<code>{html.escape(str(path), quote=False)}</code>"
 
 
+# ---------------------------------------------------------------------------
+# STATUSLINE — the pinned, edited-in-place mobile statusline (T-SL-CORE / design §5)
+# ---------------------------------------------------------------------------
+
+#: Map a model **id** to its short statusline label by family. Each pattern is matched
+#: case-insensitively as a substring of the id (``claude-opus-4-8`` → ``opus``); the first
+#: hit wins. An id matching NONE of these falls back to the raw id (RB1 — an unrecognized /
+#: future model is shown verbatim rather than mislabelled or crashing). Order does not matter
+#: (the families are disjoint), but opus/sonnet/haiku are the only ids the routing ever sets
+#: (``/fast`` = haiku, ``/deep`` = opus, plus a custom ``CLAUDE_MODEL``).
+_MODEL_FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
+    ("opus", "opus"),
+    ("sonnet", "sonnet"),
+    ("haiku", "haiku"),
+)
+
+#: The em dash shown for ``ctx`` when the percentage is unknown — NEVER a fabricated ``0%``
+#: (design §2.1: "an em dash, not a fake 0%"). A turn that has not yet produced a usage figure
+#: (no live client, no last ``ResultMessage.usage``) shows ``🧠 ctx —``.
+_CTX_UNKNOWN = "—"
+
+
+def model_short_label(model_id: object) -> str:
+    """Reduce a model **id** to its short statusline label (``opus``/``sonnet``/``haiku``).
+
+    A regex/substring match over the id by family (case-insensitive): ``claude-opus-4-8`` →
+    ``opus``, ``claude-haiku-4-5`` → ``haiku``, a sonnet id → ``sonnet``. An id matching NONE
+    of the known families (an unexpected / future / custom ``CLAUDE_MODEL``) falls back to the
+    **raw id** verbatim (RB1 — never mislabel, never crash). A ``None``/blank/odd value reads
+    as ``""`` (the caller — :func:`format_statusline` — never passes one; the active model is
+    always a config/SDK constant). Pure; no I/O. The returned label is NOT HTML-escaped here —
+    :func:`format_statusline` escapes every interpolated field once (SB3).
+    """
+    if not model_id:
+        return ""
+    raw = str(model_id).strip()
+    if not raw:
+        return ""
+    low = raw.casefold()
+    for needle, label in _MODEL_FAMILY_PATTERNS:
+        if needle in low:
+            return label
+    return raw  # RB1: an unrecognized id is shown verbatim, never mislabelled.
+
+
+def format_statusline(
+    *,
+    worktree: str,
+    model_label: str,
+    effort: str | None,
+    ctx_pct: int | None,
+    mode: str,
+    working: bool,
+) -> str:
+    """Build the pinned mobile statusline body (pure; no I/O) — the owner-LOCKED format.
+
+    ::
+
+        📁 <worktree> · 🤖 <model>·<effort> · 🧠 ctx <X%> · 🔒 <mode>
+
+    with a leading ``⚙️ `` when ``working`` (a turn is running). Field rules (design §1/§5):
+
+    * ``effort=None`` → show just the model (``🤖 opus``), no ``·<effort>`` (a default-effort
+      turn never invents a level).
+    * ``ctx_pct=None`` → ``🧠 ctx —`` (an em dash — design §2.1 forbids a fabricated ``0%``;
+      a turn with no usage figure yet shows the dash, not a wrong number). An ``int`` →
+      ``🧠 ctx <X>%``.
+    * ``working=True`` → a leading ``⚙️ `` marker; ``False`` → none.
+
+    **SB3 (body-free + no path-as-fake-link).** Every interpolated value is bot-derived state,
+    not a body/secret, but is HTML-escaped here defensively (escape-once insurance, mirroring
+    ``cmd_status``) so a ``<``/``&`` in any field can never break the message or inject a tag.
+    The ``worktree`` is an SB4-validated project NAME (``^[A-Za-z0-9_-]{1,32}$``) which has no
+    ``/`` so it is inert — but if a path-SHAPED value (one containing ``/``) is ever passed, it
+    is wrapped via :func:`code_path` (``<code>…</code>``) so Telegram renders it as inert
+    monospace and its ``/segment`` runs do NOT linkify into fake command-links (the P8 fix).
+    The result is therefore valid HTML and MUST be sent with ``parse_mode="HTML"``.
+
+    Pure string; no I/O. ``model_label`` should already be the short label
+    (:func:`model_short_label`); ``effort``/``mode`` are fixed SDK/posture words.
+    """
+    # SB3: the worktree is normally an SB4-clean NAME (no slash). If it is ever path-shaped
+    # (contains a "/"), render it through code_path so its segments can't linkify into fake
+    # command-links (P8) and any odd character is escaped inside the <code> wrap. Otherwise
+    # escape-once as a plain field. Either branch yields valid, parse_mode="HTML" output.
+    if "/" in str(worktree):
+        wt = code_path(worktree)
+    else:
+        wt = _escape_html(str(worktree))
+    # SB3: every other field is a fixed word / a number, but escape-once defensively anyway.
+    model_part = _escape_html(str(model_label))
+    if effort:
+        model_part = f"{model_part}·{_escape_html(str(effort))}"
+    ctx_part = _CTX_UNKNOWN if ctx_pct is None else f"{int(ctx_pct)}%"
+    ctx_part = _escape_html(ctx_part)  # the digits/dash are safe; escape-once for consistency.
+    mode_part = _escape_html(str(mode))
+    line = f"📁 {wt} · 🤖 {model_part} · 🧠 ctx {ctx_part} · 🔒 {mode_part}"
+    if working:
+        return f"⚙️ {line}"
+    return line
+
+
 def _chunk(text: str, limit: int = TELEGRAM_MAX) -> tuple[str, ...]:
     """Split to Telegram-safe UTF-16 chunks (reuses :func:`split_message`)."""
     return tuple(split_message(text, limit=limit))
@@ -1820,53 +1922,19 @@ def _render_error(event: ErrorEvent) -> RenderAction:
     return RenderAction(op="new", chunks=_chunk(body), verbatim=True)
 
 
-def done_footer_suffix(event: ResultEvent) -> str:
-    """The ``· N turns · $X.XX`` usage suffix for a done message (T3 / P9), or ``""``.
-
-    Surfaces the SDK-provided usage the engine already carries on a
-    :class:`~claude_tg.engine.types.ResultEvent` — ``num_turns`` + ``total_cost_usd`` —
-    which the per-turn done render previously dropped whenever there was ``result_text``.
-    Each field is included only WHEN the SDK provided it (``None`` → omitted gracefully —
-    oneshot / a partial result may carry neither), so:
-
-    * both present → ``" · 3 turns · $0.01"``
-    * only turns   → ``" · 3 turns"``
-    * neither      → ``""`` (no suffix at all — never a dangling separator).
-
-    The leading ``" · "`` lets a caller append it straight onto a done line / the last
-    prose chunk. The cost is rendered to cents (``$X.XX``) per the design; **no secret is
-    in this line** (SB3 — it is two numbers the SDK reported, never tool input/output).
-    Pure string; no I/O.
-    """
-    bits: list[str] = []
-    if event.num_turns is not None:
-        # Pluralize: "1 turn" (singular) vs "N turns" — never the ungrammatical "1 turns".
-        unit = "turn" if event.num_turns == 1 else "turns"
-        bits.append(f"{event.num_turns} {unit}")
-    if event.total_cost_usd is not None:
-        bits.append(f"${event.total_cost_usd:.2f}")
-    if not bits:
-        return ""
-    return " · " + " · ".join(bits)
-
-
 def _render_result(event: ResultEvent) -> RenderAction:
     # Terminal per-turn frame. The result_text (if any) is the final answer — it is
     # Claude-authored CommonMark, so render it as Telegram HTML (with a raw fallback);
-    # otherwise a compact, bot-generated status footer stays plain text.
+    # otherwise a compact, bot-generated done line stays plain text.
     #
-    # T3 (P9): surface the SDK-provided usage (num_turns + total_cost_usd) the done frame
-    # used to drop whenever there was result_text. The ``· N turns · $X.XX`` suffix
-    # (done_footer_suffix; "" when the SDK gave neither — oneshot may not) is appended to
-    # the LAST prose chunk so the answer ends with a compact, secret-free usage line. The
-    # suffix is plain bot scaffolding (digits + glyph) so it is HTML-safe to append onto the
-    # converted HTML chunk; the parallel plain fallback gets it too (positionally parallel).
+    # STATUSLINE T-SL-WIRE (design §1/§3.3): NO dollar amounts in routine output. The
+    # per-turn ``· N turns · $X.XX`` footer is GONE — the pinned statusline is now the
+    # persistent "state after the turn" surface (worktree · model · ctx % · mode), and the
+    # turn's cumulative cost survives ONLY on the explicit ``/status`` health view
+    # (``bot.cmd_status`` reads ``store.get_cost`` directly — untouched here). So the result
+    # render is the answer prose alone, or a bare ``✅ done (<subtype>)`` when there is none.
     if event.result_text:
         html_chunks, plain = _html_chunks(event.result_text)
-        suffix = done_footer_suffix(event)
-        if suffix and html_chunks:
-            html_chunks = (*html_chunks[:-1], html_chunks[-1] + suffix)
-            plain = (*plain[:-1], plain[-1] + suffix)
         return RenderAction(
             op="new",
             chunks=html_chunks,
@@ -1874,7 +1942,7 @@ def _render_result(event: ResultEvent) -> RenderAction:
             parse_mode="HTML",
             verbatim=True,
         )
-    body = f"✅ done ({event.subtype})" + done_footer_suffix(event)
+    body = f"✅ done ({event.subtype})"
     return RenderAction(op="new", chunks=_chunk(body), verbatim=True)
 
 
@@ -2500,6 +2568,9 @@ __all__ = [
     # per-project status labels for /projects (D7)
     "ProjectStatus",
     "project_status_label",
+    # pinned mobile statusline (STATUSLINE T-SL-CORE)
+    "format_statusline",
+    "model_short_label",
     # /sessions listing (P11 T1) + attach keyboard (P11 T2)
     "sessions_listing",
     "sessions_keyboard",
diff --git a/claude_tg/session_store.py b/claude_tg/session_store.py
index 6c72f88..cfa23ad 100644
--- a/claude_tg/session_store.py
+++ b/claude_tg/session_store.py
@@ -51,6 +51,20 @@ DEFAULT_PROJECT = "default"
 #: underscore/hyphen only — no spaces, slashes, dots, ``..``, or unicode.
 _NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
 
+#: The reasoning-EFFORT levels the SDK accepts (``ClaudeAgentOptions.effort`` —
+#: ``EffortLevel = Literal['low','medium','high','xhigh','max']``), in ascending order
+#: (the canonical ordering for the ``/effort`` usage string + the statusline). The
+#: per-project ``/effort`` override (T-EFFORT) is validated against this; anything else
+#: normalizes to ``None`` (a cleared override → the SDK default), so a garbage value can
+#: never wedge the project on an effort the SDK would reject (RB1). Matched
+#: case-insensitively (the stored value is the lowercased canonical level). Public so the
+#: bot's ``/effort`` command + the streaming session validate against ONE source of truth.
+EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
+
+#: Set form for O(1) membership tests (validation). Kept in lock-step with
+#: :data:`EFFORT_LEVELS` (the ordered display form).
+_EFFORT_LEVELS = frozenset(EFFORT_LEVELS)
+
 
 # ---- registry errors (raised by the typed CRUD API, T3) --------------------
 
@@ -465,6 +479,57 @@ class JsonSessionStore:
         value = record.get("model")
         return value.strip() if isinstance(value, str) and value.strip() else None
 
+    def set_effort(self, chat_id: int, name: str, effort: str | None) -> None:
+        """Write the per-project reasoning-EFFORT override to the **named** project (case-insensitive).
+
+        T-EFFORT (STATUSLINE): ``/effort <level>`` stores one of the five SDK levels here;
+        a bare ``/effort`` (or ``/effort default``) clears it (``None``) back to the SDK
+        default. Exactly parallel to :meth:`set_model` (the model override): targets a named
+        project (the active project can move mid-turn now ``/switch`` is free), atomic +
+        ``0600`` (RB6) via :meth:`_save_raw`, and bumps ``last_active``; the project's fixed
+        ``cwd``/``session_id`` are left untouched (effort applies on the NEXT fresh session — it
+        is a session-creation param baked into ``ClaudeAgentOptions``). The value is **validated
+        against** ``{low, medium, high, xhigh, max}`` (case-insensitively) and stored lowercased;
+        a non-string, empty, or unrecognized value normalizes to ``None`` (a cleared override) so
+        a garbage level can never wedge the project on an effort the SDK would reject (RB1) — the
+        turn falls back to the SDK default. Raises :class:`UnknownProject` if no such project
+        exists (the bot only ever passes a project it just resolved/created). Persists.
+        """
+        normalized = (
+            effort.strip().lower()
+            if isinstance(effort, str) and effort.strip().lower() in _EFFORT_LEVELS
+            else None
+        )
+        raw = self._load_raw()
+        _chat, projects, key = self._resolve(raw, chat_id, name)
+        record = projects[key]
+        if not isinstance(record, dict):
+            raise UnknownProject(name)
+        if normalized is None:
+            record.pop("effort", None)
+        else:
+            record["effort"] = normalized
+        record["last_active"] = _now()
+        self._save_raw(raw)
+
+    def get_effort(self, chat_id: int, name: str) -> str | None:
+        """The named project's per-project effort override (case-insensitive), or ``None``.
+
+        Read-only (never raises, RB1): an unknown project, a missing ``effort`` field, or a
+        stored value that is NOT one of ``{low, medium, high, xhigh, max}`` (e.g. a hand-edited
+        garbage level) all read as ``None`` (meaning "no override", so the turn omits the
+        ``effort`` kwarg and the SDK default applies). Matched/returned as the lowercased
+        canonical level. Used by the turn path (thread into ``ClaudeAgentOptions(effort=…)``)
+        and the statusline display.
+        """
+        record = self.get_project(chat_id, name)
+        if not isinstance(record, dict):
+            return None
+        value = record.get("effort")
+        if isinstance(value, str) and value.strip().lower() in _EFFORT_LEVELS:
+            return value.strip().lower()
+        return None
+
     def get_cost(self, chat_id: int, name: str) -> float:
         """The named project's cumulative cost in USD (case-insensitive), or ``0.0``.
 
diff --git a/claude_tg/stream_session.py b/claude_tg/stream_session.py
index 3bb30b5..d87b4ce 100644
--- a/claude_tg/stream_session.py
+++ b/claude_tg/stream_session.py
@@ -112,6 +112,8 @@ from .render import (
     code_path,
     decode_callback,
     error_is_raw_external,
+    format_statusline,
+    model_short_label,
     notify_attention,
     notify_done,
     notify_error,
@@ -128,6 +130,7 @@ from .session_mirror import (
     transcript_path,
 )
 from .session_store import (
+    _EFFORT_LEVELS,
     DEFAULT_PROJECT,
     DuplicateProject,
     InvalidProjectName,
@@ -145,6 +148,14 @@ EditFn = Callable[..., Awaitable[None]]
 #: A coroutine that deletes a message by id (best-effort; used to clear the transient
 #: "💭 Claude is thinking…" status line at the end of a turn so it does not linger).
 DeleteFn = Callable[..., Awaitable[None]]
+#: A coroutine that PINS a message by id (STATUSLINE T-SL-CORE). The bot's closure forwards
+#: to ``Bot.pin_chat_message`` with ``disable_notification=True`` (a silent pin — design §3.1).
+#: Best-effort: a failure is swallowed (RB1) and never breaks a turn.
+PinFn = Callable[..., Awaitable[None]]
+#: A coroutine that UNPINS a message by id (STATUSLINE T-SL-CORE). Used on orphan-recovery to
+#: best-effort drop the stale pin before re-pinning the fresh one (the "one pinned message"
+#: invariant; Telegram's current pin is the newest, so the bar self-corrects). Best-effort (RB1).
+UnpinFn = Callable[..., Awaitable[None]]
 
 #: The three operator verdicts the engine understands (mirrors PermissionDecision.verdict).
 PermissionVerdictName = Literal["allow_once", "allow_session", "deny"]
@@ -227,6 +238,7 @@ def _default_engine_factory(
     model: Optional[str] = None,
     permission_mode: str = "default",
     thinking: bool = False,
+    effort: Optional[str] = None,
     audit_sink: Optional[AuditSink] = None,
     bash_policy_mode: str = "off",
     bash_policy_extra_patterns: tuple[str, ...] = (),
@@ -284,6 +296,17 @@ def _default_engine_factory(
     Off by default (cost + flood posture); toggled per project by ``/thinking`` (transient,
     RB3). SB3: the reasoning TEXT is shown; the opaque signature is dropped in ``normalize``.
 
+    **T-EFFORT (STATUSLINE):** ``effort`` is the per-project reasoning-EFFORT override
+    (``/effort low…max``) baked into the substrate's ``ClaudeAgentOptions(effort=…)`` at
+    session-creation time (mirrors ``model`` — a session-creation knob, distinct from the P12
+    ``thinking`` VISIBILITY toggle). ``None`` (the default here, and what a bare ``/effort``
+    clears to) omits ``effort`` entirely so behavior is byte-for-byte unchanged when no
+    override is set and the SDK's own default effort (``high``) applies. There is NO
+    ``CLAUDE_*`` global default for effort: the session resolves the per-project override (else
+    ``None``) and passes it via ``_bound_factory`` at each ``_ensure_engine`` build, so an
+    ``/effort`` change takes effect on the NEXT fresh session for that project (never hot-swapped
+    mid-session).
+
     **P13 T-AUDIT:** ``audit_sink`` is the optional, BODY-FREE audit sink the engine records
     every gate decision to (a :class:`~claude_tg.audit.ChatBoundSink` over the process
     :class:`~claude_tg.audit.AuditLog`, bound per chat by ``StreamingSession._build_engine``).
@@ -312,6 +335,7 @@ def _default_engine_factory(
         decision_callback=decision_callback,
         model=model,
         thinking=thinking,
+        effort=effort,
     )
     engine = Engine(
         substrate,
@@ -424,6 +448,15 @@ class _ProjectRuntime:
     # :meth:`_ensure_engine`. ADR-001 C4: arming plan mode greenlights NOTHING about tools —
     # an approved plan's later risky tools still hit the permission gate independently.
     plan_next: bool = False
+    # STATUSLINE T-SL-WIRE (B3 fix): True WHILE a plan-mode turn is actually running on this
+    # project, so the statusline shows ``🔒 plan`` for the live plan turn's duration. The
+    # one-shot ``plan_next`` above is CONSUMED (read + cleared) in ``handle_message`` BEFORE
+    # ``_drive_turn`` runs, so by the time the plan turn is streaming ``plan_next`` is already
+    # False — reading it in :meth:`_statusline_text` would wrongly show ``gate`` DURING the plan
+    # turn. So ``_drive_turn`` sets this from the consumed ``plan_turn`` local at turn start and
+    # CLEARS it in its finally (turn end) — the line reads THIS for the live mode. Transient
+    # in-memory (RB3); a restart drops it (no turn is running across a restart anyway).
+    in_plan_turn: bool = False
     # P12 T-PLAN: the SDK ``permission_mode`` the CURRENT live engine (``engine``) was built
     # with — ``"default"`` for an ordinary session, ``"plan"`` for the fresh session built for
     # an armed ``/plan`` turn. ``_ensure_engine`` records it at build time and consults it in
@@ -451,6 +484,17 @@ class _ProjectRuntime:
     # (off→on streams from the next turn; on→off stops the wire traffic from the next turn).
     # Transient (RB3); a restart rebuilds in the default OFF.
     engine_thinking: bool = False
+    # T-EFFORT (STATUSLINE): the reasoning-EFFORT level the CURRENT live engine was built with
+    # (the resolved per-project override, else ``None`` = SDK default). ``_ensure_engine``
+    # records it at build time and the warm fast-path reuses the engine ONLY when it matches the
+    # turn's requested effort — so changing ``/effort`` rebuilds the session on the NEXT turn
+    # (effort is a session-creation knob baked into ``ClaudeAgentOptions``; it can't be
+    # hot-switched), in either direction. UNLIKE ``engine_thinking`` the override itself is
+    # PERSISTED (on the project, like the model override) — only this built-with marker is
+    # transient (RB3): a restart resolves the persisted effort fresh and rebuilds. ``None`` (no
+    # override) matches ``None`` → a back-to-back no-effort turn reuses the warm engine
+    # byte-for-byte (the default-turn path is unchanged).
+    engine_effort: Optional[str] = None
     # P11 T2 (attach-fork): True iff this project was ADOPTED from an external session that
     # was LIVE in another process at attach time, so its NEXT resume MUST fork (resume into a
     # fresh id, transcript copied) rather than continue the live id — two writers on one
@@ -669,6 +713,23 @@ class _ChatState:
     # wins — the name-echoed prompt said which). Bumped by :meth:`_next_armed_seq`; never
     # reset (strictly increasing within the process is all the ordering needs).
     armed_seq: int = 0
+    # STATUSLINE T-SL-CORE (design §3.1 / §4 RB3) — the ONE pinned statusline message per chat.
+    # ``statusline_message_id`` is the Telegram id of the pinned line (None before the first
+    # update / after an orphan-recovery clears it); ``statusline_text`` is the last body shown,
+    # for the identical-text skip (no-op edits raise "message is not modified" AND waste a send
+    # slot — mirrors the transient status line's ``status_text``). EXACTLY ONE id is ever held
+    # (we only edit it; on recovery we re-point it). Transient/in-memory only (RB3): a restart
+    # drops the reference (the bot re-creates the line on the first post-restart update) — like
+    # ``send_gate``/``status_message_id``, the live pin id is never persisted.
+    statusline_message_id: Optional[int] = None
+    statusline_text: Optional[str] = None
+    # STATUSLINE T-SL-WIRE (pin-retry fix): whether the held ``statusline_message_id`` is
+    # actually PINNED. The send and the pin are separate Telegram calls — a send can succeed
+    # (id stored) while the pin RAISES (rate-limit, perms, hiccup), leaving the line sent but
+    # UNPINNED. Without this flag the identical-text skip would short-circuit every later update
+    # and the line would stay unpinned forever. So on a failed pin we leave this False and RETRY
+    # the pin on the next update even when the text is unchanged. Transient in-memory (RB3).
+    statusline_pinned: bool = False
 
 
 class StreamingBusy(Exception):
@@ -780,6 +841,7 @@ class StreamingSession:
                 model: Optional[str] = None,
                 permission_mode: str = "default",
                 thinking: bool = False,
+                effort: Optional[str] = None,
                 audit_sink: Optional[AuditSink] = None,
             ) -> Engine:
                 return _default_engine_factory(
@@ -792,6 +854,7 @@ class StreamingSession:
                     model=model,
                     permission_mode=permission_mode,
                     thinking=thinking,
+                    effort=effort,
                     audit_sink=audit_sink,
                     # P13 T-BASH: bind the live config's Bash policy (default flag) into the
                     # production engine — consulted ADDITIVELY for Bash in on_tool_request. A
@@ -1442,6 +1505,43 @@ class StreamingSession:
                 log.exception("failed to persist model override for chat %s", chat_id)
         return normalized
 
+    def set_effort(self, chat_id: int, level: Optional[str]) -> Optional[str]:
+        """Set (or clear) the ACTIVE project's per-project reasoning-EFFORT override (T-EFFORT).
+
+        ``/effort <low|medium|high|xhigh|max>`` stores the level; a bare ``/effort`` (or
+        ``/effort default``) clears it (``None``) back to the SDK default (``high``). Exactly
+        parallel to :meth:`set_model`: persisted on the active project via the store (atomic +
+        ``0600``, RB6) so it survives a restart and a store reload; with no store it is a no-op
+        (a single implicit project, no persistence) — returns the NORMALIZED level regardless so
+        the bot can confirm. Auto-creates ``default`` if there is no active project (consistent
+        with ``set_model`` / ``set_yolo`` / ``arm_plan``).
+
+        The store VALIDATES the level against ``{low, medium, high, xhigh, max}`` and normalizes
+        anything else to ``None`` (a cleared override) — but the bot's ``cmd_effort`` rejects a
+        bad level with a clean error *before* calling this, so a stored garbage level can't arise
+        from the command path; this method simply returns what was persisted. **Applies on the
+        NEXT fresh session, never mid-turn** (effort is a session-creation param baked into
+        ``ClaudeAgentOptions``; a turn in flight keeps its current effort, and the warm fast-path
+        rebuilds on the next turn because ``engine_effort`` no longer matches). Returns the
+        normalized override that was stored (``None`` for a clear).
+        """
+        normalized = (
+            level.strip().lower()
+            if isinstance(level, str) and level.strip().lower() in _EFFORT_LEVELS
+            else None
+        )
+        # Resolve (and if needed auto-create) the active project so /effort before any turn works.
+        name, _rt = self._active_runtime(chat_id, create_default=True)
+        if self.store is not None and name is not None:
+            try:
+                self.store.set_effort(chat_id, name, normalized)
+            except Exception:
+                # RB1: never crash the command over a persist failure (e.g. the project was
+                # /rm'd in a race). The override simply isn't recorded; the next turn uses the
+                # SDK default. Mirrors set_model / _persist's swallow-and-log discipline.
+                log.exception("failed to persist effort override for chat %s", chat_id)
+        return normalized
+
     def arm_plan(self, chat_id: int) -> None:
         """Arm the ACTIVE project's NEXT turn as a plan turn (``/plan``; P12 T-PLAN-2).
 
@@ -1522,6 +1622,26 @@ class StreamingSession:
                 return override
         return self.config.model
 
+    def _resolve_project_effort(self, chat_id: int, name: str) -> Optional[str]:
+        """The reasoning-EFFORT level to bake into ``name``'s next session (override → ``None``).
+
+        T-EFFORT (STATUSLINE): the per-project override (``/effort low…max``) if set, else
+        ``None`` (omit ``effort`` → the SDK's own default, ``high``). UNLIKE
+        :meth:`_resolve_project_model` there is NO ``CLAUDE_*`` global default for effort — when
+        unset we return ``None`` so the kwarg is omitted entirely. Read-only + fail-safe (RB1):
+        a missing store / project / field (or a garbage stored level — :meth:`get_effort`
+        validates) reads as no override. Called by :meth:`_ensure_engine` for the project it is
+        building.
+        """
+        if self.store is not None:
+            try:
+                override = self.store.get_effort(chat_id, name)
+            except Exception:  # RB1: a bad/odd record never wedges the build
+                override = None
+            if override:
+                return override
+        return None
+
     def active_run_count(self) -> int:
         """The number of turns currently RUNNING across the whole process (T2 /status).
 
@@ -1620,6 +1740,13 @@ class StreamingSession:
         # fast-path below reuses the engine only when its built-with flag matches, so a toggle
         # rebuilds the session on the next turn (thinking is a session-creation knob).
         thinking = rt.thinking
+        # T-EFFORT (STATUSLINE): resolve THIS project's reasoning-EFFORT override (/effort
+        # low…max), else None (SDK default — no CLAUDE_* global). Like ``model`` it is fixed for
+        # the life of the fresh session built below (a session-creation param); the warm
+        # fast-path reuses the engine only when its built-with level matches, so an /effort
+        # change rebuilds on the next turn (never hot-swapped). Resolved here (not on the
+        # runtime) so the persisted override is read fresh each build (it survives a restart).
+        effort = self._resolve_project_effort(chat_id, name)
         # P5 / ADR-005 D1 (T5): no cross-project stop here. A different project's started
         # engine is left running so N runs can be concurrent (T5 removed P4's
         # _stop_other_started). Only the SAME project's stale/non-started engine is handled
@@ -1640,11 +1767,19 @@ class StreamingSession:
         # not hot-switchable). So /thinking on→off (or off→on) rebuilds the session on the next
         # turn; a back-to-back same-thinking turn still reuses the warm engine byte-for-byte
         # (both False pre-P12 → matched → reuse, so a thinking-OFF project is unchanged).
+        #
+        # T-EFFORT (STATUSLINE): and the built-with reasoning-EFFORT level must match too — for
+        # the SAME reason (effort is a session-creation knob baked into ClaudeAgentOptions, not
+        # hot-switchable). So changing /effort (e.g. high→max, or set→cleared) rebuilds the
+        # session on the next turn; a back-to-back same-effort turn still reuses the warm engine
+        # byte-for-byte (None == None for a no-override project → matched → reuse, so the
+        # default-turn path is unchanged).
         if (
             rt.engine is not None
             and rt.started
             and rt.engine_permission_mode == permission_mode
             and rt.engine_thinking == thinking
+            and rt.engine_effort == effort
         ):
             return rt.engine, False
         # Past the warm fast-path: rt is either fresh (engine None), holds a NON-started
@@ -1686,11 +1821,13 @@ class StreamingSession:
         # mode on the runtime so the warm fast-path reuses this engine only for a same-mode turn
         # and rebuilds back to ``"default"`` after the one-shot plan turn (the mismatch path).
         engine = self._build_engine(
-            chat_id, rt.cwd, rt.policy, model, permission_mode=permission_mode, thinking=thinking
+            chat_id, rt.cwd, rt.policy, model,
+            permission_mode=permission_mode, thinking=thinking, effort=effort,
         )
         rt.engine = engine
         rt.engine_permission_mode = permission_mode
         rt.engine_thinking = thinking  # P12 T-THINK: track the built-with thinking flag
+        rt.engine_effort = effort  # T-EFFORT: track the built-with reasoning-effort level
         resume_id = self._resume_id(chat_id, name)
         # ⭐ P11 T2 (B2+B3) — the BINDING fork-vs-continue decision, made HERE at the first
         # write from a FRESH liveness re-probe (not frozen at attach time). When this project
@@ -1767,12 +1904,16 @@ class StreamingSession:
                 #     plan mode (the marker was already consumed above; this re-uses the value).
                 #     P12 T-THINK: and the SAME thinking flag — a thinking-ON project whose
                 #     resume failed still starts fresh with live reasoning on (sticky flag).
+                #     T-EFFORT: and the SAME reasoning-effort level (resolved once above) — a
+                #     project with an /effort override starts fresh at that effort too.
                 engine = self._build_engine(
-                    chat_id, rt.cwd, rt.policy, model, permission_mode=permission_mode, thinking=thinking
+                    chat_id, rt.cwd, rt.policy, model,
+                    permission_mode=permission_mode, thinking=thinking, effort=effort,
                 )
                 rt.engine = engine
                 rt.engine_permission_mode = permission_mode  # P12 T-PLAN: track the fresh mode
                 rt.engine_thinking = thinking  # P12 T-THINK: track the fresh thinking flag
+                rt.engine_effort = effort  # T-EFFORT: track the fresh reasoning-effort level
                 # (d) Start the FRESH engine — a clean fresh session (the dead id is gone).
                 await engine.start()
                 # (e) Signal the caller so handle_message posts the T7 "couldn't resume,
@@ -1882,6 +2023,7 @@ class StreamingSession:
         *,
         permission_mode: str = "default",
         thinking: bool = False,
+        effort: Optional[str] = None,
     ) -> Engine:
         """Call the engine factory, passing the T4 per-project ``model`` only when supported.
 
@@ -1908,6 +2050,12 @@ class StreamingSession:
         configured) makes the engine's hook a no-op. An injected test factory keeps its 3-kwarg
         contract and never receives it, so every existing test factory is unaffected (and the
         no-op default keeps the 1288 floor).
+
+        **T-EFFORT (STATUSLINE):** ``effort`` rides the SAME default-factory-only gate — a level
+        bakes ``ClaudeAgentOptions(effort=…)`` into the FRESH session for a project with an
+        ``/effort`` override, ``None`` (the default) omits it so a no-override turn is
+        byte-for-byte unchanged (the SDK default effort applies). An injected test factory keeps
+        its 3-kwarg contract and never receives it, so every existing test factory is unaffected.
         """
         if self._factory_accepts_model:
             return self._engine_factory(
@@ -1917,6 +2065,7 @@ class StreamingSession:
                 model=model,  # type: ignore[call-arg]  # default factory accepts model (T4)
                 permission_mode=permission_mode,  # default factory accepts it too (P12 T-PLAN-1)
                 thinking=thinking,  # default factory accepts it too (P12 T-THINK)
+                effort=effort,  # default factory accepts it too (T-EFFORT)
                 audit_sink=self._audit_sink_for(chat_id),  # default factory accepts it too (P13)
             )
         return self._engine_factory(
@@ -2778,6 +2927,8 @@ class StreamingSession:
         send: SendFn,
         edit: EditFn,
         delete: Optional[DeleteFn] = None,
+        pin: Optional[PinFn] = None,
+        unpin: Optional[UnpinFn] = None,
     ) -> bool:
         """Fire ONE scheduled task as a fully-gated PROACTIVE turn (P14 T-FIRE ⭐).
 
@@ -2867,6 +3018,8 @@ class StreamingSession:
                 send=send,
                 edit=edit,
                 delete=delete,
+                pin=pin,
+                unpin=unpin,
                 command_initiated=True,
                 proactive=True,
                 project_override=schedule.project,
@@ -2903,6 +3056,8 @@ class StreamingSession:
         send: SendFn,
         edit: EditFn,
         delete: Optional[DeleteFn] = None,
+        pin: Optional[PinFn] = None,
+        unpin: Optional[UnpinFn] = None,
         reply_to_message_id: Optional[int] = None,
         command_initiated: bool = False,
         images: Optional[Sequence[ImageInput]] = None,
@@ -3193,8 +3348,9 @@ class StreamingSession:
                         )
                     await self._drive_turn(
                         state, chat_id, engine, text,
-                        send=send, edit=edit, delete=delete, target=target,
-                        images=images, proactive=proactive,
+                        send=send, edit=edit, delete=delete,
+                        pin=pin, unpin=unpin, target=target,
+                        images=images, proactive=proactive, plan_turn=plan_turn,
                     )
             finally:
                 # SLOT-LEAK SAFETY: release the slot this turn held — exactly once, on every
@@ -3384,9 +3540,12 @@ class StreamingSession:
         send: SendFn,
         edit: EditFn,
         delete: Optional[DeleteFn] = None,
+        pin: Optional[PinFn] = None,
+        unpin: Optional[UnpinFn] = None,
         target: Optional[tuple[str, _ProjectRuntime]] = None,
         images: Optional[Sequence[ImageInput]] = None,
         proactive: bool = False,
+        plan_turn: bool = False,
     ) -> None:
         """Iterate ``engine.send`` → render → Telegram send/edit (coalesced).
 
@@ -3459,6 +3618,20 @@ class StreamingSession:
         turn_rt.status_message_id = None
         turn_rt.status_text = None
         turn_rt.status = "running"
+        # STATUSLINE T-SL-WIRE (B3 fix): mark the LIVE plan-mode flag for the statusline's
+        # duration so the line shows 🔒 plan WHILE the plan turn runs. ``plan_turn`` is the value
+        # ``handle_message`` consumed from the one-shot ``plan_next`` (already cleared there), so
+        # this transient flag is the only honest "this turn is a plan turn" signal at render
+        # time. Cleared in the finally (turn end → back to gate/yolo). Set BEFORE the turn-start
+        # statusline trigger so that first render already reads ``plan``.
+        turn_rt.in_plan_turn = plan_turn
+        # STATUSLINE T-SL-WIRE (design §3.1): turn START → flip the working ⚙️ marker ON (and
+        # refresh model/effort/mode/worktree). FOREGROUND-ONLY — gated on ``turn_name`` so a
+        # BACKGROUND concurrent turn never stomps the foreground line (the make-or-break
+        # invariant). Best-effort (RB1): pins/edits can't break the turn (the helper swallows).
+        await self._maybe_update_statusline(
+            chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=turn_name,
+        )
         # D6 "loud throughout" — but only inline for a FOREGROUND turn (a backgrounded run is
         # silent inline, D4; its yolo posture still shows on each foreground turn + via
         # /projects is not yolo-aware, so this is the loud surface when watched). Verbatim
@@ -3661,6 +3834,21 @@ class StreamingSession:
             # ADR-005 D7: the turn is over → this project is idle again (no runtime → idle is
             # the /projects default; a running/awaiting project that just ended → idle).
             turn_rt.status = "idle"
+            # STATUSLINE T-SL-WIRE (B3 fix): the plan turn is over → clear the live plan flag so
+            # the turn-end render (below) and every idle refresh show 🔒 gate/yolo again, not a
+            # lingering 🔒 plan. Cleared BEFORE the turn-end statusline trigger. (A freshly-armed
+            # /plan for the NEXT turn re-shows 🔒 plan via the command refresh's ``plan_next``.)
+            turn_rt.in_plan_turn = False
+            # STATUSLINE T-SL-WIRE (design §3.1): turn END → flip the working ⚙️ marker OFF and
+            # refresh ctx % (the context just grew, and the engine is still alive here — its
+            # teardown for a driver_error/resume-failure happens AFTER this finally — so
+            # _statusline_text's engine.context_percentage() reads the fresh figure). FOREGROUND-
+            # ONLY (``turn_name``) so a background turn's end never stomps the foreground line.
+            # In the finally + fully best-effort (RB1), so it fires on EVERY exit path (clean
+            # end, mid-stream raise, cancel) and can never mask the turn's own exception.
+            await self._maybe_update_statusline(
+                chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=turn_name,
+            )
             # ADR-005 D3: drop any pending-index entries this turn's project left open (an
             # ask/plan/permission the operator never answered — the engine has stopped
             # awaiting it now the stream drained / the turn died, so a late tap on it is a
@@ -3966,6 +4154,288 @@ class StreamingSession:
             rt.status_message_id = mid
             rt.status_text = body
 
+    # -- the pinned mobile statusline (STATUSLINE T-SL-CORE, design §3.1/§4) --
+
+    async def _maybe_update_statusline(
+        self,
+        chat_id: int,
+        *,
+        send: Optional[SendFn],
+        edit: Optional[EditFn],
+        pin: Optional[PinFn],
+        unpin: Optional[UnpinFn],
+        for_project: Optional[str] = None,
+    ) -> None:
+        """Refresh the pinned statusline IFF this is the chat's FOREGROUND project (T-SL-WIRE).
+
+        ⭐ **The make-or-break wiring invariant (design §3.1).** The pinned line reflects the
+        chat's ACTIVE (foreground) project — the one the operator is watching. A BACKGROUND
+        concurrent turn (a non-active project running under P5 concurrency) must NEVER rewrite
+        the line, or two concurrent turns would stomp each other's state and the single pinned
+        line would stop describing "what you're looking at". So the turn-start / turn-end
+        triggers route through HERE, which:
+
+        * **skips** when ``for_project`` is not the chat's foreground (:meth:`_is_foreground`) —
+          a background turn leaves the foreground line untouched;
+        * **skips** when any closure is missing (a caller/test that didn't inject pin/unpin —
+          back-compat: the statusline simply isn't driven, the turn is unaffected);
+        * otherwise delegates to :meth:`_update_statusline` (itself fully best-effort, RB1).
+
+        ``for_project=None`` means "the caller already knows this is foreground" (the command
+        paths: ``/switch`` + the knob setters always act on the active project), so the
+        foreground gate is bypassed but the closure-presence gate still applies. The whole call
+        is wrapped so a foreground-check / build error can never escape to the turn (RB1) — the
+        statusline is an observer off the turn's critical path.
+        """
+        if send is None or edit is None or pin is None or unpin is None:
+            return  # no closures injected (a test / a caller that didn't wire them) → no-op.
+        try:
+            if for_project is not None and not self._is_foreground(chat_id, for_project):
+                # ⭐ Foreground-only: a BACKGROUND turn never rewrites the foreground line.
+                return
+            await self._update_statusline(
+                chat_id, send=send, edit=edit, pin=pin, unpin=unpin
+            )
+        except Exception:
+            # RB1: a foreground-check / dispatch error must never break the turn (the inner
+            # _update_statusline already swallows its own I/O; this guards the gate itself).
+            log.debug("statusline trigger failed for chat (ignored)", exc_info=True)
+
+    async def _statusline_text(self, chat_id: int) -> Optional[str]:
+        """Build the CURRENT statusline body for ``chat_id``'s foreground project (live read).
+
+        Reads the chat's ACTIVE (foreground) project's live state — the worktree NAME, the
+        effective model + effort, the permission mode, the working/idle marker, and the ctx %
+        — and renders it through :func:`~claude_tg.render.format_statusline`. Foreground-only
+        (design §3.1): a background project's turn never rewrites the line, so the single pinned
+        line always describes "what you're looking at".
+
+        **Read-only / fail-safe (RB1):** resolves the active runtime with ``create_default=
+        False`` so a statusline refresh NEVER creates a project as a side effect; with no active
+        project (nothing run yet) returns ``None`` (nothing to show). Each field read is
+        defensive — a missing store / odd record / ctx call that raises degrades to a safe
+        default (``ctx —``, ``gate``) rather than raising. Returns the formatted body, or
+        ``None`` when there is no foreground project to describe.
+
+        ⭐ **ASYNC (B1 fix):** the ctx % comes from ``Engine.context_percentage()`` which AWAITS
+        the SDK's coroutine ``get_context_usage()`` — so this method is async and awaits it. The
+        await is still fully best-effort (any raise → ``ctx —``, never a fabricated number); it
+        is the only await here (every other field is a pure in-memory read).
+
+        * ``worktree`` — the active project NAME (SB4-validated charset, so inert — SB3).
+        * ``model`` — :meth:`_resolve_project_model` reduced by :func:`model_short_label`.
+        * ``effort`` — :meth:`_resolve_project_effort` (``None`` → model-only).
+        * ``mode`` — ``yolo`` if the project's policy is allow-all, else ``plan`` if a plan turn
+          is RUNNING (``in_plan_turn`` — B3) OR a ``/plan`` is armed for the next turn
+          (``plan_next``), else ``gate`` (the fail-closed default).
+        * ``working`` — the per-project status enum is a working state (``running`` /
+          ``awaiting_*`` / ``queued``) vs ``idle``.
+        * ``ctx_pct`` — the live engine's :meth:`~claude_tg.engine.engine.Engine.context_percentage`
+          (``None`` → ``ctx —``, never a fabricated number).
+        """
+        name, rt = self._active_runtime(chat_id, create_default=False)
+        if name is None or rt is None:
+            return None
+        worktree = name  # the SB4-validated project name (no path; SB3-inert).
+        model_label = model_short_label(self._resolve_project_model(chat_id, name))
+        effort = self._resolve_project_effort(chat_id, name)
+        # mode: yolo (allow-all) wins; else plan — either a plan turn is RUNNING NOW
+        # (``in_plan_turn``, B3 — ``plan_next`` is already consumed by the time the turn streams)
+        # OR a ``/plan`` is armed for the NEXT turn (``plan_next``); else the fail-closed gate.
+        if bool(getattr(rt.policy, "yolo", False)):
+            mode = "yolo"
+        elif bool(getattr(rt, "in_plan_turn", False)) or bool(getattr(rt, "plan_next", False)):
+            mode = "plan"
+        else:
+            mode = "gate"
+        working = rt.status in ("running", "awaiting_approval", "awaiting_answer", "awaiting_plan", "queued")
+        ctx_pct: Optional[int] = None
+        engine = rt.engine
+        if engine is not None:
+            try:
+                ctx_pct = await engine.context_percentage()
+            except Exception:  # pragma: no cover - the engine call is already best-effort (RB1)
+                ctx_pct = None
+        return format_statusline(
+            worktree=worktree,
+            model_label=model_label,
+            effort=effort,
+            ctx_pct=ctx_pct,
+            mode=mode,
+            working=working,
+        )
+
+    async def _update_statusline(
+        self,
+        chat_id: int,
+        *,
+        send: SendFn,
+        edit: EditFn,
+        pin: PinFn,
+        unpin: UnpinFn,
+    ) -> None:
+        """Refresh the chat's ONE pinned statusline — send+pin on first use, edit thereafter.
+
+        STATUSLINE T-SL-CORE (design §3.1/§4). Builds the current foreground statusline body
+        (:meth:`_statusline_text`) and reconciles it with the chat's pinned line:
+
+        * **identical text** → skip entirely (no I/O — a no-op edit raises "message is not
+          modified" AND wastes a send slot; mirrors :meth:`_edit_status`).
+        * **first update** (no id held) → SEND the body then PIN it with the notification
+          DISABLED (a silent pin — design §3.1); store the id + text.
+        * **subsequent update** → EDIT in place only (no re-pin, no re-send; a pinned message
+          edited in place stays pinned and silent).
+        * **edit FAILURE** (the operator unpinned/deleted it → "message to edit not found", an
+          API hiccup, too old) → ORPHAN RECOVERY: clear the stored id, best-effort UNPIN the
+          stale one (the "one pinned message" invariant — Telegram's current pin is the newest,
+          so the bar self-corrects), then re-SEND + re-PIN a fresh line (mirrors the orphaned
+          status-line recovery in :meth:`_edit_status`).
+
+        **⭐ RB1 — a pin/edit/send failure NEVER breaks or wedges a turn.** This is an observer
+        OFF the turn's critical path: the WHOLE body is wrapped so ANY exception (a raising
+        ``send``/``edit``/``pin``/``unpin``, a build error) is logged at debug and swallowed —
+        the caller (the turn loop / a command) is unaffected. **RB5** — every send/edit funnels
+        through the per-chat gate as the **non-verbatim** kind (:meth:`_gated_send`/
+        :meth:`_gated_edit`), so the statusline can never flood and never starves a real
+        answer/prompt. **One id invariant** — exactly one ``statusline_message_id`` is ever held
+        per chat; we only ever edit it, and on recovery re-point it.
+
+        ``send``/``edit``/``pin``/``unpin`` are injected by ``bot.py`` (the same pattern as the
+        existing send/edit/delete closures) targeting THIS chat — so the line is SB1-confined to
+        the operator's allowlisted chat (no new outbound surface).
+
+        **⭐ B2 fix — no stale line across a ``/switch``.** The body is built from the FOREGROUND
+        project's state, but the gated send/edit ``await``s the gate's wait — a ``/switch`` in
+        that window would change the foreground. So the body is REBUILT from CURRENT state right
+        before the actual edit/send (inside the gated helpers, AFTER the gate wait); whatever the
+        foreground is at write time, the line that lands describes IT, never a pre-switch
+        snapshot. **Pin-retry** — a send that succeeded while its pin RAISED leaves the line
+        UNPINNED (``statusline_pinned`` False); a later update RETRIES the pin even if the text is
+        unchanged, so a transient pin failure self-heals instead of sticking unpinned forever.
+        """
+        try:
+            body = await self._statusline_text(chat_id)  # async (B1: awaits the SDK ctx %)
+            if not body:
+                return  # no foreground project to describe — nothing to pin/edit.
+            state = self._chat(chat_id)
+            # Pin-retry: if we hold a sent id whose pin FAILED, retry the pin even on identical
+            # text (the identical-text skip below would otherwise leave it unpinned forever).
+            if (
+                state.statusline_message_id is not None
+                and not state.statusline_pinned
+                and body == state.statusline_text
+            ):
+                await self._statusline_pin(state, state.statusline_message_id, pin=pin)
+                return
+            if body == state.statusline_text:
+                # Identical to what's pinned — skip BEFORE the gate so an unchanged refresh
+                # never consumes a send slot and never triggers a no-op "not modified" edit.
+                return
+            if state.statusline_message_id is None:
+                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
+                return
+            try:
+                # B2: rebuild the body AFTER the gate wait (inside _statusline_gated_edit) so a
+                # /switch during the wait writes the now-current line, never the stale snapshot.
+                await self._statusline_gated_edit(
+                    chat_id, state, state.statusline_message_id, edit=edit
+                )
+            except Exception:
+                # Orphan recovery (design §4 RB1): the pinned line is gone (unpinned/deleted by
+                # the operator) / too old / an API hiccup. Clear the dead id, best-effort UNPIN
+                # the stale one (one-pin invariant), then re-send + re-pin a fresh line. The
+                # turn is unaffected either way (this whole method is best-effort).
+                log.debug("statusline edit failed for chat; re-sending + re-pinning", exc_info=True)
+                stale_id = state.statusline_message_id
+                state.statusline_message_id = None
+                state.statusline_text = None
+                state.statusline_pinned = False
+                try:
+                    await unpin(message_id=stale_id)
+                except Exception:
+                    log.debug("stale statusline unpin failed (ignored)", exc_info=True)
+                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
+        except Exception:
+            # ⭐ The make-or-break swallow (RB1): NOTHING the statusline does may escape to the
+            # turn. A build/gate/closure failure is logged at debug and dropped — the next state
+            # change re-creates the line.
+            log.debug("statusline update failed for chat (ignored)", exc_info=True)
+
+    async def _statusline_gated_edit(
+        self, chat_id: int, state: _ChatState, message_id: int, *, edit: EditFn
+    ) -> None:
+        """Edit the pinned line through the gate, REBUILDING the body AFTER the gate wait (B2).
+
+        Reserves the per-chat gate slot and awaits its wait (non-verbatim — RB5), THEN re-derives
+        the statusline body from CURRENT state and performs the raw edit. Rebuilding after the
+        wait closes the ``/switch``-during-wait race: the line that lands always describes the
+        foreground project AT WRITE TIME, never the pre-wait snapshot. If the rebuilt body is
+        empty (the foreground project vanished mid-wait — e.g. ``/rm``) or identical to what is
+        already shown, the edit is SKIPPED (no stale write, no no-op "not modified"). A raise
+        propagates to the caller's orphan-recovery (the message may be gone).
+        """
+        wait = self._gate(state).reserve(verbatim=False)
+        if wait > 0:
+            await self._sleep(wait)
+        body = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
+        if not body or body == state.statusline_text:
+            return  # foreground vanished mid-wait, or nothing changed → no stale/no-op write.
+        await edit(message_id=message_id, text=body, parse_mode="HTML")
+        state.statusline_text = body
+
+    async def _statusline_send_and_pin(
+        self,
+        chat_id: int,
+        state: _ChatState,
+        *,
+        send: SendFn,
+        pin: PinFn,
+    ) -> None:
+        """Send the statusline body (gated, non-verbatim) then PIN it silently (design §3.1).
+
+        The first-use + orphan-recovery primitive: reserve the gate slot, await its wait, THEN
+        rebuild the body from CURRENT state (B2 — a ``/switch`` during the wait sends the
+        now-current line, never the pre-wait snapshot) and send it; a best-effort silent pin
+        follows (``disable_notification=True`` — a pin must never re-ping the operator). The
+        id/text are stored on the chat ONLY when the send returns an id (so a send that yields
+        ``None`` does not leave a half-set state). A PIN failure is swallowed (RB1) AND records
+        ``statusline_pinned=False`` so the next update retries the pin (the line is still sent +
+        tracked; only the bar placement is deferred, never the turn). Called from
+        :meth:`_update_statusline` inside its best-effort guard, so a raising ``send`` propagates
+        to that guard's swallow.
+        """
+        wait = self._gate(state).reserve(verbatim=False)
+        if wait > 0:
+            await self._sleep(wait)
+        body = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
+        if not body:
+            return  # foreground vanished mid-wait — nothing to send.
+        mid = await send(text=body, reply_markup=None, parse_mode="HTML")
+        if mid is None:
+            # The send produced no id (a closure that returns None) — don't store a half state;
+            # the next update will try a fresh send.
+            return
+        state.statusline_message_id = mid
+        state.statusline_text = body
+        state.statusline_pinned = False  # not pinned until the pin call below succeeds.
+        await self._statusline_pin(state, mid, pin=pin)
+
+    async def _statusline_pin(self, state: _ChatState, message_id: int, *, pin: PinFn) -> None:
+        """Best-effort SILENT pin of the statusline message; record whether it stuck (pin-retry).
+
+        A pin must never re-ping (``disable_notification=True``) and never break the turn (RB1).
+        On success ``statusline_pinned`` is set True; on failure it stays/!becomes False and is
+        swallowed — :meth:`_update_statusline` then RETRIES the pin on the next update (even with
+        unchanged text) so a transient pin failure self-heals instead of leaving the line unpinned
+        forever. Only the pinned-bar placement is ever at stake here, never the turn.
+        """
+        try:
+            await pin(message_id=message_id, disable_notification=True)
+            state.statusline_pinned = True
+        except Exception:
+            state.statusline_pinned = False
+            log.debug("statusline pin failed (will retry on next update)", exc_info=True)
+
     # -- the callback resolve path (LOCK-FREE: SB1 enforced at the bot) ------
 
     def resolve_callback(self, chat_id: int, data: object) -> "CallbackOutcome":
diff --git a/docs/features/statusline/design.md b/docs/features/statusline/design.md
new file mode 100644
index 0000000..7c9231f
--- /dev/null
+++ b/docs/features/statusline/design.md
@@ -0,0 +1,464 @@
+# STATUSLINE — a live mobile statusline pinned at the top of the chat
+
+- **Status:** Draft — orchestrator reviews before `/plan`.
+- **Date:** 2026-06-24
+- **Deciders:** repo owner
+- **Branch / base:** `feat/statusline` off `main` `cc7bb8b`.
+- **Spikes:** (1) context-window-used % feasibility and (2) the reasoning-EFFORT
+  representation, both run against the installed SDK (`claude-agent-sdk==0.2.105`) and
+  PTB `21.11.1` with **real probes** (live `query()` turn + `ClaudeAgentOptions`
+  introspection), recorded verbatim in §2. **Both unknowns resolved — no fallback needed.**
+
+---
+
+## 1. Vision
+
+Claude Code's **terminal** statusline is a single always-present line that condenses the
+session's state — cwd, model, context-window usage, mode. This feature brings that to the
+**phone**: one message **pinned at the top of the Telegram chat and edited in place**, so
+the operator always sees the bot's current state at a glance without scrolling, and without
+a re-ping on every update.
+
+```
+📁 claude-telegram-bot-statusline · 🤖 opus·max · 🧠 ctx 6% · 🔒 gate
+```
+
+(with a small working/idle marker during a turn, e.g. a leading `⚙️` while a turn runs.)
+
+It **replaces** the per-turn `✅ done (success) · 1 turn · $0.20` footer and **removes
+dollar amounts from all routine output**. Cost survives only on `/status` (the explicit
+health view). The statusline is the calm, persistent answer to "what is the bot doing and
+how is it configured right now," updated silently as state changes.
+
+**Fields (owner-locked format):**
+
+| Field | Source | Notes |
+|---|---|---|
+| `📁 <worktree>` | active project's name (or cwd-basename) | already tracked: `store.get_active` + `_project_label`/`_basename_of` |
+| `🤖 <model>·<effort>` | per-project model override → `CLAUDE_MODEL` → SDK default; per-project effort knob | model already tracked (`/fast`·`/deep`·`/auto`); **effort is a NEW knob** (§3.2) |
+| `🧠 ctx <X%>` | live `ClaudeSDKClient.get_context_usage()['percentage']` | **honest**, matches the CLI `/context`; spike-proven (§2.1) |
+| `🔒 <mode>` | permission posture: `gate` / `yolo` / `plan` | already tracked (`get_project_yolo`, `plan_next`) |
+| working/idle | per-project `_ProjectRuntime.status` enum (ADR-005 D7) | `running`/`awaiting_*` → working; `idle` → idle |
+
+---
+
+## 2. Spike findings (with probe evidence)
+
+Both spikes were run with the READY main venv python against the installed SDK. Evidence is
+the **raw probe output**, not an assumption.
+
+### 2.1 ctx (context-window used %) — **FEASIBLE, honest, first-class**
+
+The SDK exposes a dedicated method on the **live client** the bot already keeps open for the
+whole turn (`StreamingSession`/`adapter_sdk` drives `ClaudeSDKClient` and holds `self._client`
+across `connect()` → `query()` → `receive_response()`):
+
+```python
+ClaudeSDKClient.get_context_usage() -> ContextUsageResponse  # confirmed present on the class
+```
+
+`ContextUsageResponse` (TypedDict, `claude_agent_sdk/types.py:759`) — the **exact** shape:
+
+```
+categories: list[ContextUsageCategory]   # per-category breakdown (system tools, messages, …)
+totalTokens: int                          # tokens currently in the context window
+maxTokens: int                            # effective limit (may be reduced by autocompact buffer)
+rawMaxTokens: int                         # RAW model context-window size
+percentage: float                         # % of context used (0-100)  ← exactly what we show
+```
+
+**Live probe** (real opus-4-6 turn at `effort="max"`, then `await client.get_context_usage()`
+after the response drained, client still connected):
+
+```
+=== get_context_usage() keys === ['categories','totalTokens','maxTokens','rawMaxTokens',
+   'autocompactSource','percentage','gridRows','model','memoryFiles','mcpTools','agents',
+   'slashCommands','skills','autoCompactThreshold','isAutoCompactEnabled','messageBreakdown','apiUsage']
+CU summary: {"totalTokens": 12998, "maxTokens": 200000, "rawMaxTokens": 200000,
+             "percentage": 6, "model": "claude-opus-4-6"}
+```
+
+**Findings:**
+
+- **`percentage` is the honest figure.** It is the same number the CLI `/context` command
+  shows (the SDK docstring says so explicitly). We display `round(percentage)` → `ctx 6%`.
+  No fabrication, no derived ratio of our own.
+- **The model's context WINDOW is exposed** — `rawMaxTokens: 200000` (and per-model
+  `contextWindow: 200000` appears in `ResultMessage.model_usage`, see below). **No model-id→
+  window mapping table is needed.** (If the owner enables the `context-1m-2025-08-07` beta via
+  `ClaudeAgentOptions.betas`, the SDK's `rawMaxTokens`/`percentage` track it automatically —
+  we never hard-code 200k vs 1M.)
+- The method requires only a **connected client** — no `include_partial_messages`, no special
+  mode. The probe called it right after `receive_response()` completed and it returned cleanly.
+
+**The honest fallback (only if `get_context_usage()` is ever unavailable / raises):** the bot
+ALSO has the raw token figures on every `ResultMessage.usage` (already plumbed — see §2.3).
+`ResultMessage.usage` from the same probe:
+
+```json
+{"input_tokens": 3, "cache_creation_input_tokens": 12995, "cache_read_input_tokens": 0,
+ "output_tokens": 5, ...}
+```
+
+and `ResultMessage.model_usage`:
+
+```json
+{"claude-opus-4-6": {"inputTokens": 3, "cacheReadInputTokens": 0,
+   "cacheCreationInputTokens": 12995, "contextWindow": 200000, "maxOutputTokens": 64000, ...}}
+```
+
+So the honest fallback `ctx %` (when the live method is unavailable) is
+`(input_tokens + cache_read_input_tokens + cache_creation_input_tokens) / contextWindow`
+using the per-model `contextWindow` the SDK reports — the LAST turn's input tokens ≈ the
+current context size, exactly as the brief reasoned. **But the primary path is the
+first-class `percentage`; the fallback is a defensive RB1 branch, not the design.**
+
+**Decision (ctx):** show `🧠 ctx <round(percentage)>%` from `get_context_usage()`, refreshed
+at turn end (when the client is alive and the context just grew). If the call raises, fall
+back to the usage-derived %; if that is also unavailable (no turn yet), show `🧠 ctx —`
+(an em dash, not a fake 0%).
+
+### 2.2 reasoning EFFORT — **FIRST-CLASS, settable, distinct from `/thinking`**
+
+`ClaudeAgentOptions` has a dedicated `effort` field (introspected live):
+
+```
+effort: typing.Literal['low', 'medium', 'high', 'xhigh', 'max'] | None  (default=None)
+```
+
+The SDK exports the alias `EffortLevel = Literal['low','medium','high','xhigh','max']`
+(`types.py:33`). The field docstring (`types.py:1929`):
+
+> Controls how much effort Claude puts into its response. **Works with adaptive thinking to
+> guide thinking depth.** low → minimal/fastest … high → deep reasoning (default) … xhigh →
+> extended (Opus 4.7 only; falls back to high) … **max → maximum effort.**
+
+**Live probe:** a real `ClaudeSDKClient` turn built with
+`ClaudeAgentOptions(model="claude-opus-4-6", effort="max", …)` connected and completed
+successfully (the init frame even carried a `fast_mode_state` key) — so `effort="max"` is
+**accepted by the installed SDK end-to-end**, not just present as a field.
+
+**`effort` vs the existing P12 `/thinking`:**
+
+- **`/thinking` (P12)** sets `thinking={"type":"adaptive","display":"summarized"}` +
+  `include_partial_messages=True`. It is a **visibility** toggle — it makes Claude's reasoning
+  *streamable as the 🧠 line*; it does **not** dial reasoning depth, and it forces partial-
+  message wire traffic. It is sticky per-project, default OFF.
+- **`effort`** is a **depth/intensity** dial (low→max). It is orthogonal: it changes how hard
+  Claude thinks, costs nothing extra in wire traffic, and is what "opus at **max**" refers to.
+  `max_thinking_tokens` is the **deprecated** budget knob (the docstring says "Use `thinking`
+  instead… 0=disabled, any other value=adaptive"); **effort is the modern, level-based knob** —
+  this is the one to use, not `max_thinking_tokens`.
+
+**Mapping "opus at max":** `model = DEFAULT_DEEP_MODEL ("claude-opus-4-8")` + `effort = "max"`.
+Display: `🤖 opus·max`.
+
+**Decision (effort):** add a NEW per-project effort knob (a `/effort` command, §3.2) that
+threads `effort=<level>` into `ClaudeAgentOptions` via `_build_options` (exactly parallel to
+how `model`/`thinking` thread in today), persisted on the project like the model override,
+and DISPLAYED in the statusline. Default = unset → omit the `effort` kwarg → SDK default
+(`high`). This is the cleanest mapping of the owner's "opus at max" to a concrete SDK setting.
+
+### 2.3 Existing usage/cost plumbing (reuse, don't reinvent)
+
+- The adapter maps `ResultMessage` → `ResultEvent(num_turns, total_cost_usd, …)`
+  (`adapter_sdk.py:243`). `ResultEvent` carries `total_cost_usd` (`engine/types.py:194`).
+- The turn loop already accumulates cost: on each `ResultEvent` it calls
+  `store.add_cost(chat_id, turn_name, event.total_cost_usd)` (`stream_session.py:3544`),
+  persisted per-project, surfaced on `/status` (`bot.py:463`).
+- **The done-footer we replace** is `render._render_result` + `done_footer_suffix`
+  (`render.py:1823`,`1853`): it appends `· N turns · $X.XX` to the result. **This is the exact
+  string the statusline supersedes** for routine output.
+
+**Conclusion:** the bot already has every datum the statusline needs. Effort is the only NEW
+piece of state to track; everything else (worktree, model, mode, status enum, cost-for-`/status`)
+is already plumbed.
+
+---
+
+## 3. Grounded scope
+
+### 3.0 What changes vs. what is reused
+
+**Reused as-is (no change):**
+- `ChatSendGate` (`render.py:2328`) + `_gated_send`/`_gated_edit` (`stream_session.py:884`,`916`)
+  — the per-chat ~1 msg/s budget. **The statusline's pin/edit go through this same gate** so it
+  can never flood (RB5), and as a **non-verbatim** edit it yields to real output (a prompt/answer
+  is never starved by a statusline refresh).
+- The per-project `_ProjectRuntime.status` enum + `project_status` (`stream_session.py:2709`)
+  for the working/idle marker.
+- `store.get_active` / `_project_label` / `_basename_of` for the worktree name.
+- `get_project_yolo` / `plan_next` for the mode field.
+- `code_path` (`render.py:1513`, the P8 fix) — **the worktree name is rendered through nothing
+  that linkifies**; see SB3 (§4) — but any path-shaped value MUST use `<code>`.
+
+**New:**
+1. A pure **statusline formatter** in `render.py` (`format_statusline(...) -> str`) — body-free,
+   unit-testable, no I/O. Mirrors the existing pure-render helpers (`done_footer_suffix`,
+   `notify_*`).
+2. A **pin/edit-in-place mechanism** on `StreamingSession` (a per-chat pinned-message id +
+   text, parallel to the per-project transient status line), driven through the send-gate.
+3. A per-project **effort knob** (`_ProjectRuntime.effort` + `store` persistence +
+   `_build_options` threading) and a **`/effort` command**.
+4. **Removal** of the done-footer dollar amounts from routine output (`_render_result` /
+   `done_footer_suffix`), keeping cost on `/status` only.
+
+### 3.1 The pinned-statusline mechanism
+
+**Pin once, edit silently thereafter.** Per chat the bot keeps `statusline_message_id` +
+`statusline_text` (new fields on `_ChatState`, transient/in-memory — RB3, like `send_gate`):
+
+- **First update for a chat:** `send_message(text=…)` then `pin_chat_message(message_id=…,
+  disable_notification=True)`. Telegram shows it in the chat's pinned bar at the top.
+- **Subsequent updates:** `edit_message_text(message_id=…, text=…)` only — **no re-pin, no
+  re-send, no notification.** A pinned message edited in place stays pinned and silent.
+- **Identical-text skip:** like `_edit_status` (`stream_session.py:3928`), if the new text
+  equals `statusline_text` we **skip the edit entirely** (Telegram raises "message is not
+  modified" on a no-op edit, and it needlessly consumes a send slot).
+
+**One pinned message per chat.** Telegram allows multiple pins but shows ONE "current" pin in
+the bar; we keep exactly one statusline message and only ever edit it. We never pin a second.
+
+**WHEN it updates** (the trigger set — kept small so it can't churn):
+- **Turn start** — flip the working marker on; refresh model/effort/mode/worktree.
+- **Turn end** — flip the working marker off; refresh `ctx %` (the context just grew, and the
+  client is alive for `get_context_usage()`).
+- **Project switch** (`/switch`, `[Open <project>]` tap) — worktree + per-project model/effort/
+  mode all change.
+- **Model / effort / mode change** (`/fast`·`/deep`·`/auto`, `/effort`, `/yolo`·`/unyolo`,
+  `/plan`) — the changed field.
+- **NOT** on every event/delta — the statusline is **state**, not a progress bar. Mid-turn
+  progress stays on the existing transient 💭/🧠/▶️ status line (unchanged). This bounds edits
+  to a handful per turn, far under the gate budget.
+
+**Foreground only.** Under concurrency (N projects per chat, ADR-005) the statusline reflects
+the **chat's active (foreground) project** — the one `store.get_active` returns, the one the
+operator is watching. A background project's turn start/end does NOT rewrite the statusline
+(its progress is the 🔔/✅ ping, D4). This keeps the single pinned line coherent: it always
+describes "what you're looking at."
+
+### 3.2 The `/effort` command (RECOMMENDED — new)
+
+Parallel to `/fast`·`/deep`·`/auto` and `/thinking`:
+
+- `/effort <low|medium|high|xhigh|max>` — set the active project's effort level (persisted).
+- `/effort` (bare) or `/effort default` — clear the override → SDK default (`high`).
+- Streaming-mode only (like `/fast`·`/thinking`); a clean "applies to streaming mode" notice
+  otherwise.
+- **Applies on the NEXT fresh session, never mid-turn** — effort is a session-creation param
+  baked into `ClaudeAgentOptions` (identical lifecycle to `model`/`thinking`; the warm-engine
+  fast-path must include effort in its match-key so a change rebuilds on the next turn — see
+  the `engine_thinking`/`engine_mode` match pattern at `stream_session.py:446`).
+
+**Why a command (not auto):** effort is a real knob the owner wants to *drive* ("opus at max"),
+not just observe. It belongs with the other per-turn routing knobs and is displayed in the
+statusline so the current level is always visible. **Decision: add `/effort`.**
+
+### 3.3 Removing the done-footer + dollars
+
+- `_render_result` (`render.py:1853`): drop `done_footer_suffix` from the result render. A
+  result with prose renders the prose (no `· N turns · $X.XX` tail); a result with no prose
+  renders a bare `✅ done (<subtype>)` (no dollar suffix). The **statusline** is now the
+  persistent "state after the turn" surface.
+- `done_footer_suffix` (`render.py:1823`): either delete it or repurpose to turns-only for
+  `/status`. **Dollars are removed from every routine path.** Cost stays ONLY on `/status`
+  (`bot.py:463`, unchanged — still reads `store.get_cost`).
+- The transient 💭/🧠/▶️ status line and its turn-end delete (`stream_session.py:3654`) are
+  **unchanged** — that is mid-turn progress, separate from the pinned statusline.
+
+### 3.4 Explicitly OUT of scope (deferred, P13/ADR-006 discipline)
+
+- **`/context` full breakdown.** `get_context_usage()` returns per-category detail
+  (`categories`, `mcpTools`, `memoryFiles`). The statusline shows only the headline `%`. A
+  future `/context` command could surface the breakdown; **deferred** (trigger: owner asks to
+  see per-category usage).
+- **Rate-limit display.** ADR-001 names a `rate_limit` status carrier; the SDK exposes
+  `RateLimitInfo`/`RateLimitStatus`. **Not** in the locked format; deferred (trigger: owner
+  hits rate limits and wants them on the line).
+- **Session-id / cwd in the line.** The locked format shows the worktree *name*, not the full
+  cwd or session id (those stay on `/status`/`/projects`). Keeps the line phone-sized.
+- **Proactive `⏰` marker.** A proactive turn (P14) could mark the statusline; deferred unless
+  the owner asks (the per-turn `⏰` header already exists for proactive fires).
+
+---
+
+## 4. SB / RB rules (security + robustness)
+
+The statusline is a render output → the same boundaries that govern every other output apply.
+The **most load-bearing constraint is SB3 (body-free)**.
+
+- **SB1 (allowlisted chat only).** The statusline is sent/pinned/edited ONLY in the operator's
+  allowlisted chat. It is driven from `StreamingSession` turn/command paths that are already
+  behind the bot's `_ok`/`_authorized` SB1 recheck; the pin/edit closures target the same
+  `chat_id` as every other send. **No new outbound surface** — it reuses the existing
+  send/edit closures the bot injects. A statusline update is never sent to a non-allowlisted
+  chat (there is no code path that would).
+- **SB3 (body-free; no secret / no path-as-fake-link).** Every field is **bot-derived state,
+  not a body or secret:**
+  - `worktree` = an SB4-validated project NAME (`^[A-Za-z0-9_-]{1,32}$`) or a cwd-basename
+    sanitized to that charset (`_sanitize_attach_name`). **No raw cwd, no full path** → no
+    `/segment` linkification risk. (If a basename ever needs showing and could contain path-
+    shaped text, it MUST go through `code_path` per the P8 fix — but the SB4 charset has no
+    `/`, so the validated name is inert. The formatter asserts/sanitizes to the SB4 charset.)
+  - `model` = a config/SDK constant id reduced to `opus`/`sonnet`/`haiku`/`default` (a regex
+    over the id) — never user input.
+  - `effort` = one of the five fixed SDK literals — never user input.
+  - `ctx %` = an integer 0–100 from the SDK — a number, never a body.
+  - `mode` = one of the three fixed words `gate`/`yolo`/`plan`.
+  - **No tool input, no file content, no command text, no session id, no dollar amount** ever
+    reaches the statusline. There is structurally nothing in it from which a secret could leak.
+  - The formatter is **pure** and HTML-escapes any interpolated value defensively (the names
+    are SB4-clean, but escape-once is cheap insurance — mirrors `cmd_status`). Sent with
+    `parse_mode=None` (plain) unless a `<code>`-wrap is needed, in which case `parse_mode="HTML"`
+    with `code_path`.
+- **SB4 (name validation).** The worktree name is the store's already-validated project name;
+  the formatter does not accept an arbitrary string (it pulls from `get_active`/the record).
+- **RB1 (never crash a turn).** **A pin/edit failure must NEVER break a turn.** Every
+  statusline I/O is **best-effort**, wrapped in try/except that logs at debug and swallows —
+  identical discipline to the transient-status-line delete (`stream_session.py:3654`), the
+  cost-accumulate (`:3549`), and `_notify_*`. The statusline is an **observer off the turn's
+  critical path**: if `get_context_usage()` raises, the turn is unaffected and the line shows
+  `ctx —`; if `pin_chat_message`/`edit_message_text` raises (message deleted, too old, API
+  hiccup), it is swallowed and the next update re-creates the line.
+  - **If the user unpins it / deletes it:** the next update's `edit_message_text` will raise
+    ("message to edit not found"); we catch it, clear `statusline_message_id`, and **re-send +
+    re-pin** a fresh line (mirrors the orphaned-status-line recovery at `_edit_status`,
+    `stream_session.py:3942`). The operator can unpin freely; the line reappears on the next
+    state change. We do NOT fight the user by re-pinning on every edit — only when the edit
+    target is gone.
+  - **One pinned message invariant:** we hold exactly one id; on a re-send recovery we
+    best-effort `unpin`/leave the stale one and pin the new (Telegram's "current pin" is the
+    newest, so the bar self-corrects).
+- **RB3 (transient).** `statusline_message_id`/`text` and the per-project `effort` flag are
+  **in-memory only, never persisted** for the runtime marker — a restart drops the pin
+  reference (the bot re-creates the line on the first post-restart update). **The effort
+  *override* IS persisted** on the project (like the model override) so it survives a restart;
+  only the live pin id is transient. (Consistent with ADR-004 D3 / ADR-003 D7: posture knobs
+  that the owner sets deliberately persist; live-turn scaffolding does not.)
+- **RB5 (rate-limit safe).** All statusline I/O funnels through `ChatSendGate` as non-verbatim
+  edits; the trigger set (§3.1) is a handful of updates per turn. It cannot flood and cannot
+  starve verbatim output.
+
+**No ADR is violated.** ADR-001 (model coupling): model stays a session-creation param read
+back for display, never hot-swapped. ADR-003 (gate/yolo loud): the statusline makes the mode
+*more* visible (always-on `🔒 yolo`), reinforcing D6. ADR-005 (send-gate, per-project status):
+reused directly. The Explore sub-audit confirmed compatibility with all eight ADRs; SB3 is the
+binding constraint and the design is structurally body-free.
+
+---
+
+## 5. Build-order task breakdown
+
+Small, independently-verifiable tasks. Gates per task: **pytest + ruff + mypy + secret_scan**
+all green. SDK stays pinned. Clean single-line commits, **NO Co-Authored-By trailer**. Each
+task lands only on green + approval (supervised `/build`).
+
+### T1 — Effort knob: store persistence
+- **Add** `JsonSessionStore.set_effort(chat_id, name, effort)` / `get_effort(chat_id, name)`
+  to `session_store.py` — exact parallel of `set_model`/`get_model` (atomic + 0600, RB6,
+  case-insensitive, `None` clears, bad value → `None`).
+- **Validate** effort against `{low,medium,high,xhigh,max}`; an unknown value normalizes to
+  `None` (RB1).
+- **AC:** round-trips a value; clears on `None`; unknown → `None`; unknown project raises
+  `UnknownProject`; never crashes on a sparse record. **Tests:** unit on the store (mirror the
+  existing `test_session_store` model-override cases).
+
+### T2 — Effort knob: thread into `ClaudeAgentOptions`
+- **Add** an `effort` param to `Engine`/`adapter_sdk` session construction; in `_build_options`
+  set `kwargs["effort"] = self._effort` when not `None` (parallel to `model`/`thinking`).
+- **Add** `_ProjectRuntime.effort` + thread `_resolve_project_effort(chat_id, name)`
+  (override → `None`; effort has no `CLAUDE_*` global default — SDK default is `high`) into
+  `_ensure_engine`, and add `effort` to the warm-engine match-key so a change rebuilds next
+  turn (mirror `engine_thinking`).
+- **AC:** a project with `effort="max"` builds options containing `effort="max"`; default →
+  no `effort` kwarg; a change forces a rebuild on the next turn, not mid-turn. **Tests:** unit
+  on `_build_options` (assert the kwarg) + the warm-engine rebuild path (mirror the thinking-
+  flag rebuild test).
+
+### T3 — `/effort` command + `set_effort` on the session
+- **Add** `StreamingSession.set_effort(chat_id, level)` (parallel to `set_model`/`set_thinking`:
+  resolve/auto-create active project, persist, RB1-swallow).
+- **Add** `cmd_effort` to `bot.py`; register it; add to `HELP_TEXT` + `COMMAND_MENU`.
+  Streaming-mode-only guard + a clean notice otherwise; SB1 `_ok` recheck.
+- **AC:** `/effort max` confirms + persists; `/effort` (bare) shows usage / clears; bad level →
+  clean error; one-shot mode → notice; unauthorized chat → no-op. **Tests:** command unit tests
+  (mirror `cmd_thinking`/`cmd_fast` tests) with a fake session.
+
+### T4 — Pure statusline formatter (`render.py`)
+- **Add** `format_statusline(*, worktree, model_label, effort, ctx_pct, mode, working) -> str`
+  — pure, body-free, the locked format. A helper `model_short_label(model_id)` (regex →
+  `opus`/`sonnet`/`haiku`/`default`). `ctx_pct=None` → `ctx —`. HTML-escape defensively.
+- **AC:** exact format for a full set; `ctx —` when `None`; `opus·max`; working marker present/
+  absent; an unexpected/odd model id falls back to the raw id (RB1); no path/secret can appear
+  (the inputs are constrained). **Tests:** pure unit tests over the formatter + label helper
+  (no I/O), including SB3 cases (a name with `<`/`&` is escaped; a path-shaped name is rejected/
+  sanitized).
+
+### T5 — ctx % source (`get_context_usage` + honest fallback)
+- **Add** a best-effort `Engine.context_percentage()` (or a method on the adapter) that calls
+  `self._client.get_context_usage()` and returns `round(resp["percentage"])`; on any exception
+  or no client → `None`. Add the **usage-derived fallback** from the last `ResultMessage.usage`
+  (carry the last-turn `input+cache_read+cache_creation` tokens and per-model `contextWindow`
+  on the runtime) so a `None` from the live call can still produce an honest %.
+- **AC:** returns the SDK `percentage` when available; `None` (never a fabricated number) when
+  the call raises or there's no client; the fallback computes the honest ratio when usage is
+  present. **Tests:** unit with a fake client returning a `ContextUsageResponse`; a fake that
+  raises → `None`; the usage-fallback math.
+
+### T6 — Pin/edit-in-place mechanism (`StreamingSession`)
+- **Add** `_ChatState.statusline_message_id` / `statusline_text` (transient).
+- **Add** `async def _update_statusline(chat_id, *, pin, edit, unpin, send)` — builds the
+  formatter inputs from current state (active project, model/effort/mode/status, ctx%), skips
+  on identical text, sends+pins on first use, edits thereafter, and on an edit failure clears
+  the id + re-sends+re-pins (orphan recovery). **All through `_gated_*` (non-verbatim) and all
+  best-effort (RB1).** Inject `pin`/`unpin` closures from `bot.py` (like the existing
+  `send`/`edit`/`delete` closures).
+- **AC:** first call sends + pins (notification disabled); second call with changed state edits
+  in place (no re-pin, no re-send); identical state → no I/O; an edit raising → re-send + re-pin;
+  a pin/edit raising → swallowed, turn unaffected; only one id is ever held. **Tests:** unit with
+  fake send/edit/pin closures asserting the call sequence + the failure-recovery branch.
+
+### T7 — Wire the triggers (turn start/end, switch, knob changes)
+- **Call** `_update_statusline` at: turn start (working on) + turn end (working off + ctx
+  refresh) in `_drive_loop`; in `/switch`'s shared core; and after `set_model`/`set_effort`/
+  `set_yolo`/`unyolo`/`arm_plan`. Foreground-only (skip for a background turn).
+- **AC:** a foreground turn pins then refreshes at end; a `/switch` rewrites the line; a `/yolo`
+  flips `🔒 gate`→`🔒 yolo`; a background turn does NOT rewrite the line; the working marker is
+  on during a turn, off after. **Tests:** integration-style on the session with fakes (assert the
+  statusline text at each trigger); a concurrency test (background turn leaves the foreground
+  line intact).
+
+### T8 — Remove the done-footer dollars
+- **Edit** `_render_result` to drop `done_footer_suffix`; remove/repurpose `done_footer_suffix`
+  (dollars gone everywhere routine). Keep `/status` cost (`bot.py:463`) untouched.
+- **AC:** a result render no longer contains `$`; `✅ done (success)` has no `· N turns · $X`
+  tail; `/status` still shows cost. **Tests:** update the existing `_render_result`/footer tests
+  to assert no dollar suffix; assert `/status` still includes cost.
+
+### T9 — Docs + ADR + live phone-verify
+- **ADR** for the statusline (pinned-message lifecycle, the effort knob decision, ctx-% source,
+  the done-footer removal). **README**: the line format, `/effort`, "cost moved to /status."
+  Docs index.
+- **Live phone-verify** (mandatory per project memory — the relay/pin path can't be proven by
+  unit tests alone): drive Telegram Web (bot `@your_test_bot`, chat `<your-chat-id>`) and confirm:
+  pin appears once (no re-ping on edits), the line updates on turn start/end/switch/`/effort`/
+  `/yolo`, `ctx %` moves as context grows, unpin → reappears on next change, a failure never
+  wedges a turn. Scrub evidence (UUID-grep the evidence dir before committing — memory note).
+- **AC:** Codex QA iterated to SHIP (state-heavy feature — concurrency + lifecycle); reviewer
+  AGREE; live-verify PASS. **Tests:** the whole suite green; gates green.
+
+**Suggested order:** T1→T2→T3 (effort end-to-end) ‖ T4→T5 (pure pieces, parallelizable) →
+T6→T7 (the pin mechanism + wiring) → T8 (footer removal) → T9 (docs + QA + phone-verify).
+
+---
+
+## 6. Open questions for the orchestrator
+
+1. **Working marker glyph + placement.** Proposed: a leading `⚙️` while a turn runs (dropped when
+   idle), e.g. `⚙️ 📁 … · 🤖 …`. Alternative: a trailing `· ⏳` during a turn. Owner preference?
+2. **`/effort` level set.** Expose all five SDK levels (`low/medium/high/xhigh/max`) or just the
+   useful subset (`low/high/max`)? `xhigh` is Opus-4.7-only (falls back to `high`). Recommend
+   exposing all five (the SDK validates) but documenting `xhigh` as model-dependent.
+3. **Ctx refresh cadence.** Refresh `ctx %` at turn end only (proposed — cheap, accurate), or also
+   on a `/status`-style on-demand? Turn-end keeps edits minimal; on-demand `/context` is the
+   deferred §3.4 item.
diff --git a/docs/features/statusline/handoff.md b/docs/features/statusline/handoff.md
new file mode 100644
index 0000000..27b7f44
--- /dev/null
+++ b/docs/features/statusline/handoff.md
@@ -0,0 +1,38 @@
+# Feature Handoff: statusline
+
+## Goal
+Bring Claude Code's terminal statusline to the phone — one message **pinned at the top of the chat, edited in place**, showing `📁 worktree · 🤖 model·effort · 🧠 ctx X% · 🔒 mode` — replacing the per-turn `✅ done · $0.20` footer and removing dollars from routine output.
+
+## Files changed
+```
+ claude_tg/bot.py                | 187 +   (/effort cmd, pin/unpin closures, _refresh_statusline)
+ claude_tg/engine/adapter_sdk.py | 186 +   (effort kwarg, context_percentage() + usage fallback)
+ claude_tg/engine/engine.py      |  24 +   (context_percentage delegate)
+ claude_tg/render.py             | 155 +   (format_statusline + model_short_label; done-footer $ removed)
+ claude_tg/session_store.py      |  65 +   (set/get_effort)
+ claude_tg/stream_session.py     | 384 +   (effort runtime, _update_statusline lifecycle, trigger wiring)
+ + tests across 6 files                    (1577 → 1649)
+```
+
+## How to run
+- Gates (worktree `.venv`): `pytest -q` (1649), `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`.
+- Live (streaming): `ENGINE_MODE=streaming CLAUDE_STATE_FILE=… .venv/bin/python main.py` → a status message pins at the top and updates as you work; `/effort max` sets reasoning effort.
+
+## Expected behavior
+- A single **pinned** message at the top, edited in place (pin once silently → silent edits; identical text → no I/O; if you unpin/delete it, the next change re-sends + re-pins). Fields: worktree (active project), `model·effort`, `ctx %` (live `get_context_usage().percentage`, `ctx —` if unavailable — never fabricated), `mode` (gate/yolo/plan), `⚙️` while a turn runs.
+- Updates on: turn start (⚙️ on) / turn end (⚙️ off + ctx refresh), `/switch`, `/yolo`·`/unyolo`, `/plan`, `/effort`, `/fast`·`/deep`·`/auto`. **Foreground-only** — a background concurrent turn never rewrites the line.
+- **`/effort low|medium|high|xhigh|max`** — per-project reasoning effort (persisted; rebuilds the session next turn; default unset = SDK default).
+- The `✅ done` result no longer shows `· N turns · $X`; **cost stays on `/status`**.
+- **RB1:** every pin/edit/send is best-effort — a Telegram failure never breaks or wedges a turn. **SB1:** statusline only to the allowlisted chat. **SB3:** the line is structurally body-free (name/model/effort/ctx-int/mode; HTML-escaped; path-shaped name → `<code>`; no tool body/path/secret/dollar can appear).
+
+## Test plan
+- **Automated (1649):** effort store round-trip/validate/RB6; `_build_options` effort kwarg only-when-set + warm-rebuild; `cmd_effort` SB1/menu-lock-step; pure `format_statusline` (incl. SB3 escape/path cases) + `ctx —` fallback; `context_percentage` (SDK %/raises→None/usage-fallback); `_update_statusline` pin/edit/skip-identical/orphan-recovery + **RB1 all-closures-raise→returns-normally**; trigger wiring at each trigger; **concurrency: a background turn does NOT rewrite the foreground line** (mutation-probed); `_render_result` no-`$` + `/status` still has cost.
+- **Manual (live phone-verify, T-VERIFY):** pin appears once (no re-ping on edits), updates on turn/switch/effort/yolo, `ctx %` moves as context grows, unpin → reappears, a failure never wedges a turn.
+
+## Known risks
+- Pin spam / rate limits — mitigated by identical-text skip + the per-chat send-gate (non-verbatim) + state-change-only updates. Telegram shows one pinned message in the bar.
+- ctx %: `get_context_usage()` is on the live `ClaudeSDKClient`; if a future SDK changes it, `context_percentage()` degrades to the usage-fallback then to `ctx —` (never fabricated). SDK pinned.
+- Effort `xhigh` is Opus-4.7-only — on other models it may no-op (the SDK handles it; the bot just passes the level).
+
+## Open questions
+- Proactive/scheduler turns also pin/update the line (the deferred `⏰` marker from design §3.4 is not added — basic line only). Acceptable for v1.
diff --git a/docs/features/statusline/progress.md b/docs/features/statusline/progress.md
new file mode 100644
index 0000000..f52edc4
--- /dev/null
+++ b/docs/features/statusline/progress.md
@@ -0,0 +1,31 @@
+# Progress: statusline
+
+_From design.md · live mobile statusline (pinned, edited-in-place) replacing the done/$ footer · supervised build. Baseline 1577 tests. Both spikes proven (`get_context_usage().percentage` + `ClaudeAgentOptions.effort`). Owner-confirmed: ⚙️ working marker, all 5 effort levels, ctx refresh at turn-end._
+
+## Task list
+- [ ] T-EFFORT — the `/effort` knob (design T1+T2+T3): per-project effort persistence + thread into `ClaudeAgentOptions` (+ warm-engine rebuild key) + `/effort low|medium|high|xhigh|max` command (SB1, streaming-only, menu+HELP lock-step). Parallel to `/fast·/deep·/auto` + `/thinking`.
+- [ ] T-SL-CORE — the statusline machinery (design T4+T5+T6): pure `format_statusline(...)` formatter + `model_short_label` (body-free, HTML-escaped, `ctx —` fallback) + best-effort `context_percentage()` (`get_context_usage().percentage`, usage-derived fallback, `None` never fabricated) + the pin/edit-in-place lifecycle (`_update_statusline`: pin-once-silent → edit-in-place → skip-identical → orphan-recovery on edit-fail; all via the send-gate non-verbatim; **RB1 best-effort — never breaks a turn**).
+- [ ] T-SL-WIRE — wire triggers + remove the footer (design T7+T8): call `_update_statusline` at turn start/end (working on/off + ctx refresh at end), `/switch`, and after `set_model`/`set_effort`/`set_yolo`/`unyolo`/`arm_plan`; **foreground-only** (a background turn doesn't rewrite the line); remove the done-footer `$`/turn-count (cost stays on `/status`).
+- [ ] T-VERIFY — docs (ADR + README + index) + Codex QA (iterate to SHIP — state-heavy: concurrency + pin lifecycle) + **live phone-verify** (pin appears once no re-ping; updates on turn/switch/effort/yolo; ctx % moves; unpin→reappears; failure never wedges) + merge.
+
+Legend: `[ ]` todo · `[>]` in progress · `[x]` done (sha) · `[!]` blocked
+
+## Tasks
+
+### T-EFFORT — `/effort` knob
+- **Files:** `session_store.py` (`set_effort`/`get_effort` — parallel to `set_model`, atomic 0600, validate `{low,medium,high,xhigh,max}`, unknown→None, RB6), `engine/adapter_sdk.py` + `engine/engine.py` (`effort` param → `_build_options` sets `kwargs["effort"]` when not None; + warm-engine match-key like `engine_thinking`), `stream_session.py` (`_ProjectRuntime.effort` + `_resolve_project_effort` + `set_effort`), `bot.py` (`cmd_effort` + register + COMMAND_MENU + HELP).
+- **Accept:** `/effort max` (SB1) persists + confirms; bare `/effort` shows usage/clears; bad level → clean error; one-shot → notice; unauth → no-op; a project with effort builds `ClaudeAgentOptions(effort=…)` (default → no kwarg); a change rebuilds next turn (not mid-turn). RB6 persists across restart.
+- **Tests:** store round-trip/clear/unknown→None (mirror model-override); `_build_options` sets the kwarg only when set; warm-engine rebuild on change; `cmd_effort` (mirror `cmd_thinking`) SB1 + menu/HELP lock-step.
+
+### T-SL-CORE — statusline machinery
+- **Files:** `render.py` (`format_statusline(*, worktree, model_label, effort, ctx_pct, mode, working) -> str` pure + `model_short_label`), `engine` (`context_percentage()` best-effort via `get_context_usage()` + usage fallback), `stream_session.py` (`_ChatState.statusline_message_id`/`_text`; `_update_statusline(...)` pin/edit lifecycle with injected pin/unpin closures).
+- **Accept:** formatter emits the exact body-free format (`📁 … · 🤖 opus·max · 🧠 ctx 6% · 🔒 gate`), `ctx —` when None, working marker present/absent, odd model→raw id (RB1), HTML-escaped (SB3 — a `<`/`&` name escaped; a path-shaped name sanitized/`<code>`, no fake-link); `context_percentage()` returns the SDK % or None (never fabricated); `_update_statusline` sends+pins (silent) first, edits in place after, skips identical, re-sends+re-pins on edit-fail (orphan recovery), all best-effort (a pin/edit raise never breaks a turn), only one id held.
+- **Tests:** pure formatter + label (incl. SB3 escape/path cases); `context_percentage` with a fake client (returns %/raises→None/usage-fallback math); `_update_statusline` call-sequence + identical-skip + failure-recovery with fake send/edit/pin closures.
+
+### T-SL-WIRE — triggers + footer removal
+- **Files:** `stream_session.py` (call `_update_statusline` at turn start/end in the drive loop, in `/switch`'s core, after the knob setters; foreground-only), `bot.py` (inject pin/unpin closures), `render.py` (`_render_result` drops the `$`/turn-count footer suffix).
+- **Accept:** a foreground turn pins then refreshes (working on→off + ctx at end); `/switch` rewrites the line; `/yolo` flips `🔒 gate`→`🔒 yolo`; a background turn leaves the foreground line intact (concurrency); the result render contains no `$` (cost still on `/status`).
+- **Tests:** trigger integration (assert statusline text at each trigger, with fakes); concurrency (background turn doesn't rewrite); `_render_result` no-`$` + `/status` still has cost.
+
+### T-VERIFY — docs + QA + live-verify + merge
+ADR-009 (pinned-statusline lifecycle, effort knob, ctx-% source, footer removal) + README (the line format, `/effort`, "cost moved to /status") + docs index → cross-model Codex QA (iterate to SHIP) → independent reviewer → **live phone-verify** (pin once/no re-ping, updates on turn/switch/effort/yolo, ctx % moves, unpin→reappears, failure never wedges; scrub evidence) → 4 gates → merge to `main`.
diff --git a/docs/features/statusline/qa.md b/docs/features/statusline/qa.md
new file mode 100644
index 0000000..e9c22ad
--- /dev/null
+++ b/docs/features/statusline/qa.md
@@ -0,0 +1,13 @@
+# Statusline QA — Verifier + cross-model Codex
+
+## Verifier (same-model, independent): SHIP — 0 blockers
+RB1 (defense-in-depth swallow), foreground-only concurrency, SB3 body-free, SB1, effort warm-rebuild, footer-$ removed — all mutation-probed. Non-blocking: cosmetic empty-model double-space; coverage gaps (proactive-statusline test, empty-model test, create_default=False side-effect test).
+
+## Codex (cross-model): NO_SHIP — 3 blockers
+1. **ctx % not awaited** — `adapter_sdk.py:834`: `ClaudeSDKClient.get_context_usage()` is ASYNC in the SDK but `context_percentage()` calls it WITHOUT await → the real SDK % is never read (silently falls to the usage-fallback). The sync test fake hid it. The headline ctx feature doesn't use the real API.
+2. **Foreground-switch race** — `stream_session.py:4164`: foreground gated before dispatch, but `_update_statusline` snapshots state then awaits gated I/O; a `/switch` between snapshot and send/edit can write a stale previous-project line.
+3. **/plan mode never displayed** — `stream_session.py:3245`/`4211`: `rt.plan_next` is consumed before `_drive_turn`, but `_statusline_text` reads plan-mode from `rt.plan_next` → during the plan turn the line shows `🔒 gate` not `🔒 plan`.
+
+Non-blocking: send-succeeds-but-pin-fails stores the id+text → identical updates skip → stays unpinned until a later edit fails (a transient pin failure leaves it unpinned).
+
+### Verdict: NO_SHIP (Codex) — 3 real blockers; the cross-model reviewer caught the async/race/lifecycle bugs the same-model Verifier's SHIP missed.
diff --git a/docs/features/statusline/state.json b/docs/features/statusline/state.json
new file mode 100644
index 0000000..12aea1f
--- /dev/null
+++ b/docs/features/statusline/state.json
@@ -0,0 +1,7 @@
+{
+  "slug": "statusline",
+  "phase": "built",
+  "branch": "feat/statusline",
+  "worktree": "/Users/ray/dev/claude-telegram-bot-statusline",
+  "updated": "2026-06-24T15:20:00Z"
+}
diff --git a/tests/test_bot_streaming.py b/tests/test_bot_streaming.py
index a460648..3adaef8 100644
--- a/tests/test_bot_streaming.py
+++ b/tests/test_bot_streaming.py
@@ -110,6 +110,7 @@ class FakeStreaming:
         self.model_calls = []
         self.plan_calls = []
         self.thinking_calls = []
+        self.effort_calls = []
         self.reply_prompt_calls = []
         self.to_calls = []
         self.attach_calls = []
@@ -131,13 +132,13 @@ class FakeStreaming:
         self.store = None
 
     async def handle_message(
-        self, chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
-        command_initiated=False, images=None,
+        self, chat_id, text, *, send, edit, delete=None, pin=None, unpin=None,
+        reply_to_message_id=None, command_initiated=False, images=None,
     ):
         # P5/T9: handle_message gained reply_to_message_id (the D5 reply-to escape hatch);
         # P9 fix: + command_initiated (a macro /run skips free-text capture). P10 T1: + images
-        # (the multimodal photo/screenshot). Record them so the wiring tests can assert they
-        # are threaded through from on_message / cmd_run / on_photo.
+        # (the multimodal photo/screenshot). STATUSLINE T-SL-WIRE: + pin/unpin (the statusline
+        # closures threaded down to _drive_turn). Record what the wiring tests assert.
         self.handle_message_calls.append((chat_id, text, reply_to_message_id))
         self.command_initiated_calls.append(command_initiated)
         self.images_calls.append(images)
@@ -148,13 +149,21 @@ class FakeStreaming:
         # False; the free-text-capture behavior is covered against a REAL session.
         return False
 
-    async def fire_schedule(self, schedule, *, send, edit, delete=None):
-        # P14 T-FIRE: /runnow delegates here (the proactive fire path). Record the schedule so
-        # the wiring test asserts delegation; the deep fire behavior is covered against a REAL
-        # session in test_stream_session.
+    async def fire_schedule(self, schedule, *, send, edit, delete=None, pin=None, unpin=None):
+        # P14 T-FIRE: /runnow delegates here (the proactive fire path). STATUSLINE T-SL-WIRE: +
+        # pin/unpin (accepted so the bot's _make_chat_io 5-tuple threads through). Record the
+        # schedule so the wiring test asserts delegation; deep fire behavior is covered against
+        # a REAL session in test_stream_session.
         self.fire_schedule_calls.append(schedule)
         return True
 
+    async def _maybe_update_statusline(self, chat_id, *, send, edit, pin, unpin, for_project=None):
+        # STATUSLINE T-SL-WIRE: the command-trigger refresh (_refresh_statusline) calls this on
+        # the streaming session after /switch + the knob commands. This stand-in is a no-op (the
+        # statusline pin/edit lifecycle is covered against a REAL session in test_stream_session);
+        # accepting the call keeps the bot's command-handler wiring exercised here without I/O.
+        return None
+
     def resolve_callback(self, chat_id, data):
         self.resolve_calls.append((chat_id, data))
         return self._outcome
@@ -228,6 +237,13 @@ class FakeStreaming:
         self.thinking_calls.append((chat_id, on))
         return on
 
+    def set_effort(self, chat_id, level):
+        # T-EFFORT (STATUSLINE): /effort <level> sets (or clears, on None) the active project's
+        # reasoning-EFFORT override. Record the (chat_id, level) and echo it back (the real
+        # method returns the normalized level — None for a clear).
+        self.effort_calls.append((chat_id, level))
+        return level
+
     def get_cwd(self, chat_id):
         # P9/T1: the first-run welcome reads the active cwd via this accessor.
         # P10/T3: on_document saves into — and /get resolves against — this cwd.
@@ -358,11 +374,13 @@ async def test_streaming_passes_working_delete_closure():
 
     class CapturingStreaming(FakeStreaming):
         async def handle_message(
-            self, chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
-            command_initiated=False,
+            self, chat_id, text, *, send, edit, delete=None, pin=None, unpin=None,
+            reply_to_message_id=None, command_initiated=False,
         ):
             self.handle_message_calls.append((chat_id, text, reply_to_message_id))
             captured["delete"] = delete
+            captured["pin"] = pin
+            captured["unpin"] = unpin
 
     streaming = CapturingStreaming()
     bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
@@ -689,6 +707,93 @@ async def test_cmd_thinking_unauthorized_ignored():
     upd.message.reply_text.assert_not_awaited()
 
 
+# ---- T-EFFORT (STATUSLINE): /effort <low…max> (SB1, persisted, streaming-only) -------
+
+
+async def test_cmd_effort_valid_level_persists_and_confirms():
+    streaming = FakeStreaming()
+    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
+    upd = make_update(1, "/effort max")
+    await bot.cmd_effort(upd, make_ctx(args=["max"]))
+    assert streaming.effort_calls == [(1, "max")]
+    reply = upd.message.reply_text.await_args.args[0]
+    assert "🧠" in reply and "max" in reply.lower()
+
+
+async def test_cmd_effort_is_case_insensitive():
+    # The level is normalized to lowercase before set_effort (matches the store's canonical form).
+    streaming = FakeStreaming()
+    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
+    upd = make_update(1, "/effort MAX")
+    await bot.cmd_effort(upd, make_ctx(args=["MAX"]))
+    assert streaming.effort_calls == [(1, "max")]
+
+
+async def test_cmd_effort_bad_level_clean_error_lists_valid_and_does_not_set():
+    # RB1: an unrecognized level is a clean error listing the valid levels — the override is
+    # NEVER touched (set_effort is not called).
+    streaming = FakeStreaming()
+    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
+    upd = make_update(1, "/effort turbo")
+    await bot.cmd_effort(upd, make_ctx(args=["turbo"]))
+    assert streaming.effort_calls == []
+    reply = upd.message.reply_text.await_args.args[0]
+    assert "turbo" in reply.lower()
+    # The clean error lists every valid level.
+    for level in ("low", "medium", "high", "xhigh", "max"):
+        assert level in reply.lower()
+
+
+async def test_cmd_effort_bare_clears_to_default():
+    # A bare /effort (no arg) CLEARS the override → SDK default (set_effort called with None).
+    streaming = FakeStreaming()
+    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
+    upd = make_update(1, "/effort")
+    await bot.cmd_effort(upd, make_ctx(args=[]))
+    assert streaming.effort_calls == [(1, None)]
+    reply = upd.message.reply_text.await_args.args[0]
+    assert "default" in reply.lower() and "low" in reply.lower()  # usage lists the levels too
+
+
+async def test_cmd_effort_default_keyword_clears():
+    # /effort default is the explicit clear (same as bare).
+    streaming = FakeStreaming()
+    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
+    upd = make_update(1, "/effort default")
+    await bot.cmd_effort(upd, make_ctx(args=["default"]))
+    assert streaming.effort_calls == [(1, None)]
+
+
+async def test_cmd_effort_oneshot_is_explained_not_applied():
+    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
+    upd = make_update(1, "/effort max")
+    await bot.cmd_effort(upd, make_ctx(args=["max"]))
+    assert "streaming" in upd.message.reply_text.await_args.args[0].lower()
+
+
+async def test_cmd_effort_unauthorized_ignored():
+    # SB1: an un-allowlisted chat is rejected by _ok BEFORE any effect — set_effort is never
+    # called and no reply is sent (mirrors test_cmd_thinking_unauthorized_ignored).
+    streaming = FakeStreaming()
+    bot = TelegramClaudeBot(
+        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
+    )
+    upd = make_update(999, "/effort max")
+    await bot.cmd_effort(upd, make_ctx(args=["max"]))
+    assert streaming.effort_calls == []
+    upd.message.reply_text.assert_not_awaited()
+
+
+def test_effort_in_command_menu_and_help_lockstep():
+    # T-EFFORT: /effort must be in the native menu AND documented in HELP_TEXT (the lock-step
+    # guards in test_command_menu_matches_registered_handlers + the HELP⊇menu test enforce
+    # both globally; this pins the specific command).
+    from claude_tg.bot import COMMAND_MENU, HELP_TEXT
+
+    assert "effort" in {cmd for cmd, _desc in COMMAND_MENU}
+    assert "/effort" in HELP_TEXT
+
+
 async def test_cmd_yolo_unauthorized_ignored():
     streaming = FakeStreaming()
     bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
@@ -3130,8 +3235,8 @@ async def test_free_text_capture_dismisses_chips():
     streaming = FakeStreaming()
 
     async def captured_handle(
-        chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
-        command_initiated=False,
+        chat_id, text, *, send, edit, delete=None, pin=None, unpin=None,
+        reply_to_message_id=None, command_initiated=False,
     ):
         return True  # this message was a free-text capture
 
@@ -4750,7 +4855,7 @@ def _make_plan_recording_session(store):
 
     def factory(
         *, cwd, backstop_seconds, permission_policy, model=None, permission_mode="default",
-        thinking=False, audit_sink=None,
+        thinking=False, effort=None, audit_sink=None,
     ):
         modes.append(permission_mode)
         return HoldEngine(
@@ -4849,7 +4954,7 @@ async def test_plan_turn_rebuild_resumes_persisted_session_for_continuity(tmp_pa
 
     def factory(
         *, cwd, backstop_seconds, permission_policy, model=None, permission_mode="default",
-        thinking=False, audit_sink=None,
+        thinking=False, effort=None, audit_sink=None,
     ):
         eng = HoldEngine(
             [ResultEvent(session_id="sid-keep", is_error=False, subtype="success", result_text="ok")]
@@ -4944,7 +5049,7 @@ async def test_plan_marker_consumed_even_when_sb2_refuses_turn(tmp_path):
 
     def factory(
         *, cwd, backstop_seconds, permission_policy, model=None, permission_mode="default",
-        thinking=False, audit_sink=None,
+        thinking=False, effort=None, audit_sink=None,
     ):
         modes.append(permission_mode)
         return HoldEngine(
diff --git a/tests/test_engine.py b/tests/test_engine.py
index f5ecc55..16d722b 100644
--- a/tests/test_engine.py
+++ b/tests/test_engine.py
@@ -573,6 +573,42 @@ def test_sdk_build_options_no_model_omits_it():
     assert SdkSubstrate(model="   ")._build_options().model is None
 
 
+# ---- T-EFFORT (STATUSLINE): effort threads into ClaudeAgentOptions(effort=…) ----
+
+
+def test_sdk_build_options_threads_effort():
+    # T-EFFORT: a per-project effort override is carried into ClaudeAgentOptions(effort=…) at
+    # session-creation time (start AND resume paths use _build_options), parallel to `model`.
+    sub = SdkSubstrate(effort="max")
+    assert sub._build_options().effort == "max"
+    # And it rides on the resume path too (the resumed session honors the project's effort).
+    assert sub._build_options(resume="sess-123").effort == "max"
+
+
+def test_sdk_build_options_no_effort_omits_it_byte_for_byte():
+    # The headline invariant: a default (no-effort) turn omits `effort` entirely so the SDK's
+    # own default applies — byte-for-byte the pre-knob baseline. Asserted against the SDK's
+    # ClaudeAgentOptions default for the field (so this can't silently drift if the SDK changes
+    # its default), and an empty/whitespace effort is normalized to None (never an empty value).
+    from claude_agent_sdk import ClaudeAgentOptions
+
+    default_effort = ClaudeAgentOptions().effort
+    assert SdkSubstrate()._build_options().effort == default_effort
+    assert SdkSubstrate(cwd="/work")._build_options().effort == default_effort
+    assert SdkSubstrate(effort="   ")._build_options().effort == default_effort
+
+
+def test_sdk_build_options_effort_is_independent_of_model_and_thinking():
+    # effort is orthogonal: it can be set with or without a model override / thinking, and
+    # setting it never disturbs those fields (guards the three session-creation knobs stay
+    # independent — only the effort kwarg is added when an effort is present).
+    sub = SdkSubstrate(model="claude-haiku-4-5", thinking=True, effort="low")
+    opts = sub._build_options()
+    assert opts.effort == "low"
+    assert opts.model == "claude-haiku-4-5"
+    assert opts.thinking == {"type": "adaptive", "display": "summarized"}
+
+
 def test_sdk_build_options_threads_permission_mode_plan():
     # P12 T-PLAN-1 (mechanism a): an armed plan turn builds the session with
     # permission_mode="plan" baked into ClaudeAgentOptions at session-creation time — on BOTH
@@ -1217,3 +1253,216 @@ def test_adapter_does_not_import_sdk_at_module_top_level():
         name == "claude_agent_sdk" or name.startswith("claude_agent_sdk.")
         for name in top_level_imports
     ), f"SDK must be imported lazily; found top-level import in {top_level_imports}"
+
+
+# ---------------------------------------------------------------------------
+# STATUSLINE T-SL-CORE — the ctx-% source (live get_context_usage() + the honest
+# usage-derived fallback). The live call is the primary path; the fallback derives an
+# honest ratio from the LAST ResultMessage.usage. A None is NEVER a fabricated number.
+# ---------------------------------------------------------------------------
+
+
+class _FakeUsageClient:
+    """A fake ClaudeSDKClient exposing only get_context_usage (the ctx-% spike shape).
+
+    ⭐ T-SL-WIRE (B1): ``get_context_usage`` is **ASYNC** — the installed SDK's real
+    ``ClaudeSDKClient.get_context_usage()`` is a coroutine (``inspect.iscoroutinefunction`` is
+    True). The substrate MUST await it; a non-awaited regression now FAILS here (the awaited
+    coroutine yields the dict; an un-awaited call would yield a coroutine the %-extractor
+    rejects → the test's expected % would not be returned).
+    """
+
+    def __init__(self, resp):
+        self._resp = resp
+        self.calls = 0
+
+    async def get_context_usage(self):
+        self.calls += 1
+        return self._resp
+
+
+class _BoomUsageClient:
+    async def get_context_usage(self):
+        raise RuntimeError("get_context_usage unavailable")
+
+
+async def test_context_percentage_live_client_returns_rounded_percentage():
+    # ⭐ Primary path (B1): a connected client's AWAITED get_context_usage()['percentage'] →
+    # round(%). Asserts the awaited coroutine was actually called + its % returned (the
+    # make-or-break: the headline figure comes from the real SDK call, not the fallback).
+    sub = SdkSubstrate()
+    client = _FakeUsageClient(
+        {"percentage": 6, "maxTokens": 200000, "totalTokens": 12998, "model": "claude-opus-4-6"}
+    )
+    sub._client = client
+    assert await sub.context_percentage() == 6
+    assert client.calls == 1, "the SDK get_context_usage() coroutine must be awaited (B1)"
+
+
+async def test_context_percentage_uses_live_call_over_usage_fallback():
+    # ⭐ B1 regression probe: when BOTH a live client AND a stale usage fallback are present, the
+    # AWAITED live % wins. If get_context_usage() were not awaited, the live path would silently
+    # yield None and this would return the (different) fallback % — so this pins "live, awaited".
+    sub = SdkSubstrate()
+    sub._client = _FakeUsageClient({"percentage": 3})  # live says 3%
+    sub._last_usage_tokens = 100000  # a stale fallback that would compute 50%
+    sub._last_context_window = 200000
+    assert await sub.context_percentage() == 3, "the awaited live % must win over the fallback"
+
+
+async def test_context_percentage_rounds_a_float_percentage():
+    # design §2.1: round(percentage) — 6.6 → 7, not truncated to 6.
+    sub = SdkSubstrate()
+    sub._client = _FakeUsageClient({"percentage": 6.6})
+    assert await sub.context_percentage() == 7
+    sub._client = _FakeUsageClient({"percentage": 6.4})
+    assert await sub.context_percentage() == 6
+
+
+async def test_context_percentage_no_client_no_usage_is_none():
+    # No live client AND no completed turn → None (the caller shows "ctx —", never a fake 0%).
+    sub = SdkSubstrate()
+    assert await sub.context_percentage() is None
+
+
+async def test_context_percentage_raising_client_falls_back_to_none_without_usage():
+    # The live call raises and there is no usage yet → None (NOT a fabricated number).
+    sub = SdkSubstrate()
+    sub._client = _BoomUsageClient()
+    assert await sub.context_percentage() is None
+
+
+async def test_context_percentage_usage_fallback_math():
+    # The honest fallback: round(100 * tokens / window) from the last turn's usage.
+    sub = SdkSubstrate()
+    sub._last_usage_tokens = 12998
+    sub._last_context_window = 200000
+    # No client → fallback used directly.
+    assert await sub.context_percentage() == round(100 * 12998 / 200000)  # == 6
+
+
+async def test_context_percentage_raising_client_uses_usage_fallback():
+    # The live call raises BUT a last-turn usage is present → the fallback % (not None).
+    sub = SdkSubstrate()
+    sub._client = _BoomUsageClient()
+    sub._last_usage_tokens = 100000
+    sub._last_context_window = 200000
+    assert await sub.context_percentage() == 50
+
+
+async def test_capture_usage_records_tokens_and_window_from_result_message():
+    # _capture_usage carries the fallback inputs off a real ResultMessage (usage + model_usage).
+    sub = SdkSubstrate()
+    msg = sdk.ResultMessage(
+        subtype="success",
+        duration_ms=10,
+        duration_api_ms=8,
+        is_error=False,
+        num_turns=1,
+        session_id="S1",
+        total_cost_usd=0.01,
+        result="ok",
+        usage={
+            "input_tokens": 3,
+            "cache_creation_input_tokens": 12995,
+            "cache_read_input_tokens": 0,
+            "output_tokens": 5,
+        },
+        model_usage={
+            "claude-opus-4-6": {
+                "inputTokens": 3,
+                "cacheReadInputTokens": 0,
+                "cacheCreationInputTokens": 12995,
+                "contextWindow": 200000,
+                "maxOutputTokens": 64000,
+            }
+        },
+    )
+    sub._capture_usage(msg)
+    assert sub._last_usage_tokens == 12998  # 3 + 12995 + 0
+    assert sub._last_context_window == 200000
+    # With no live client, context_percentage (awaited — B1) derives from the captured usage.
+    assert await sub.context_percentage() == 6
+
+
+def test_capture_usage_ignores_non_result_message():
+    # A non-terminal message never touches the fallback cache.
+    sub = SdkSubstrate()
+    sub._capture_usage(object())
+    assert sub._last_usage_tokens is None
+    assert sub._last_context_window is None
+
+
+def test_capture_usage_never_overwrites_good_with_broken():
+    # A ResultMessage with no usage/window leaves a previously-captured good figure intact (RB1).
+    sub = SdkSubstrate()
+    sub._last_usage_tokens = 12998
+    sub._last_context_window = 200000
+    msg = sdk.ResultMessage(
+        subtype="success",
+        duration_ms=10,
+        duration_api_ms=8,
+        is_error=False,
+        num_turns=1,
+        session_id="S1",
+        result="ok",
+        usage=None,
+        model_usage=None,
+    )
+    sub._capture_usage(msg)
+    assert sub._last_usage_tokens == 12998
+    assert sub._last_context_window == 200000
+
+
+def test_stop_clears_ctx_usage_cache():
+    # The fallback cache describes THIS session; stop() drops it (RB3 — no stale carryover).
+    sub = SdkSubstrate()
+    sub._last_usage_tokens = 12998
+    sub._last_context_window = 200000
+
+    class _FakeClient:
+        async def disconnect(self):
+            return None
+
+    sub._client = _FakeClient()
+    asyncio.run(sub.stop())
+    assert sub._last_usage_tokens is None
+    assert sub._last_context_window is None
+
+
+async def test_engine_context_percentage_delegates_to_async_substrate():
+    # ⭐ B1: Engine.context_percentage() AWAITS the substrate's async context_percentage()
+    # (the real SdkSubstrate is async now). The awaited int is returned.
+    class _Sub(FakeSubstrate):
+        async def context_percentage(self):
+            return 42
+
+    eng = Engine(_Sub())
+    assert await eng.context_percentage() == 42
+
+
+async def test_engine_context_percentage_accepts_sync_substrate_value():
+    # Defensive seam: a substrate whose context_percentage is SYNC (a predating substrate / a
+    # fake) returning a plain int still works — Engine only awaits when the value is awaitable.
+    class _Sub(FakeSubstrate):
+        def context_percentage(self):
+            return 7
+
+    eng = Engine(_Sub())
+    assert await eng.context_percentage() == 7
+
+
+async def test_engine_context_percentage_none_when_substrate_lacks_method():
+    # A substrate predating the method (additive seam) → None, never an error.
+    eng = Engine(FakeSubstrate())
+    assert await eng.context_percentage() is None
+
+
+async def test_engine_context_percentage_swallows_substrate_error():
+    # A raising substrate method → None (RB1; an observer off the critical path never raises).
+    class _Sub(FakeSubstrate):
+        async def context_percentage(self):
+            raise RuntimeError("boom")
+
+    eng = Engine(_Sub())
+    assert await eng.context_percentage() is None
diff --git a/tests/test_render.py b/tests/test_render.py
index 273fa4e..f20d222 100644
--- a/tests/test_render.py
+++ b/tests/test_render.py
@@ -54,11 +54,12 @@ from claude_tg.render import (
     coalesce_stream,
     code_path,
     decode_callback,
-    done_footer_suffix,
     encode_attach_callback,
     encode_callback,
     encode_switch_callback,
+    format_statusline,
     free_text_prompt,
+    model_short_label,
     notify_attention,
     notify_done,
     notify_error,
@@ -240,13 +241,16 @@ def test_result_with_text_renders_verbatim_final_answer():
     assert "The answer is 42" in action.text
 
 
-def test_result_without_text_renders_compact_footer():
+def test_result_without_text_renders_bare_done():
+    # STATUSLINE T-SL-WIRE: a result with no prose renders a bare "✅ done (success)" — the
+    # "· N turns · $X.XX" footer is gone (cost moved to /status), even with SDK usage present.
     res = ResultEvent(
         session_id="s1", is_error=False, subtype="success", num_turns=3, total_cost_usd=0.0123
     )
     action = render_event(res)
     assert action.op == "new"
-    assert "done" in action.text and "3 turns" in action.text
+    assert action.text == "✅ done (success)"
+    assert "$" not in action.text and "turns" not in action.text
 
 
 def test_assembled_text_is_new_message_verbatim():
@@ -1277,15 +1281,17 @@ def test_prose_html_escapes_stray_angle_brackets_from_claude():
     assert "a < b && c" in "".join(action.plain_chunks)  # raw preserved verbatim
 
 
-def test_done_footer_stays_plain_text_no_html():
-    # The bot-generated done-footer (no result_text) is NOT prose -> plain, no parse_mode.
+def test_bare_done_stays_plain_text_no_html():
+    # The bot-generated bare done line (no result_text) is NOT prose -> plain, no parse_mode.
+    # STATUSLINE T-SL-WIRE: it is now a bare "✅ done (success)" — no turn-count / $ footer.
     res = ResultEvent(
         session_id="s1", is_error=False, subtype="success", num_turns=3, total_cost_usd=0.0123
     )
     action = render_event(res)
     assert action.parse_mode is None
     assert action.plain_chunks == ()
-    assert "done" in action.text and "3 turns" in action.text
+    assert action.text == "✅ done (success)"
+    assert "$" not in action.text and "turns" not in action.text
 
 
 def test_error_block_stays_plain_text():
@@ -1506,56 +1512,38 @@ def test_chat_send_gate_rejects_negative_interval():
 
 
 # ===========================================================================
-# P9 / T3 — cost + usage surfacing on the done message (num_turns + cost).
+# STATUSLINE T-SL-WIRE (design §1/§3.3) — the done-footer DOLLARS are GONE.
 #
-# ResultEvent already carries total_cost_usd + num_turns; the per-turn done render
-# used to drop them whenever there was result_text. done_footer_suffix builds the
-# "· N turns · $X.XX" suffix (only the fields the SDK provided), and _render_result
-# appends it onto the prose's last chunk (and the bare footer).
+# The per-turn "· N turns · $X.XX" footer is removed from routine output: the pinned
+# statusline is now the persistent "state after the turn" surface, and the turn's
+# cumulative cost survives ONLY on the explicit /status health view (test_bot_streaming
+# covers that /status still shows cost). _render_result therefore renders the answer prose
+# alone, or a bare "✅ done (<subtype>)" — never a $ suffix. (These replace the old P9/T3
+# done_footer_suffix tests, which asserted the now-removed dollar footer.)
 # ===========================================================================
 
 
-def test_done_footer_suffix_both_present():
-    res = ResultEvent(
-        session_id="s", is_error=False, subtype="success", num_turns=3, total_cost_usd=0.012
-    )
-    assert done_footer_suffix(res) == " · 3 turns · $0.01"
-
-
-def test_done_footer_suffix_only_turns():
-    res = ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=5)
-    assert done_footer_suffix(res) == " · 5 turns"
-
-
-def test_done_footer_suffix_only_cost():
-    res = ResultEvent(
-        session_id="s", is_error=False, subtype="success", total_cost_usd=1.5
-    )
-    assert done_footer_suffix(res) == " · $1.50"
-
-
-def test_done_footer_suffix_absent_is_empty():
-    # oneshot / a partial result may carry neither — omit gracefully (no dangling separator).
-    res = ResultEvent(session_id="s", is_error=False, subtype="success")
-    assert done_footer_suffix(res) == ""
-
-
-def test_result_with_text_appends_turns_and_cost():
-    # T3: the turns+cost are surfaced on the done message even WHEN there is result_text
-    # (previously dropped). The suffix lands on the last chunk; the plain fallback gets it
-    # too (positionally parallel).
+def test_result_with_text_has_no_dollar_or_turn_footer():
+    # ⭐ Even when the SDK reports num_turns + total_cost_usd, the result render is the prose
+    # ALONE — no "· N turns · $X.XX" tail anywhere (the dollars moved to /status). Mutation
+    # probe: if _render_result re-appended the footer, both the "$" and "turns" asserts fail.
     res = ResultEvent(
         session_id="s", is_error=False, subtype="success",
         num_turns=2, total_cost_usd=0.0734, result_text="All done — see **above**.",
     )
     action = render_event(res)
-    assert action.text.endswith(" · 2 turns · $0.07")
-    assert "above" in action.text
-    assert action.plain_chunks[-1].endswith(" · 2 turns · $0.07")
-
-
-def test_result_with_text_omits_suffix_when_sdk_absent():
-    # No num_turns + no cost (oneshot-shaped) → the prose is sent UNCHANGED, no suffix.
+    assert "$" not in action.text
+    assert "turns" not in action.text
+    assert "0.07" not in action.text
+    assert "above" in action.text  # the prose itself is unchanged
+    # The plain-text fallback is equally dollar-free (positionally parallel).
+    assert "$" not in action.plain_chunks[-1]
+    assert "turns" not in action.plain_chunks[-1]
+
+
+def test_result_with_text_renders_prose_unchanged():
+    # A prose result (no SDK usage) renders exactly the answer — no suffix (as before, but now
+    # this is the rule for ALL results, not just SDK-absent ones).
     res = ResultEvent(
         session_id="s", is_error=False, subtype="success", result_text="Just the answer.",
     )
@@ -1564,24 +1552,32 @@ def test_result_with_text_omits_suffix_when_sdk_absent():
     assert action.plain_chunks == ("Just the answer.",)
 
 
-def test_done_footer_suffix_carries_no_secret():
-    # SB3: the suffix is two SDK-reported numbers — never tool input/output or a path.
+def test_bare_done_has_no_dollar_footer():
+    # A result with NO prose renders a bare "✅ done (success)" — no "· N turns · $X.XX" tail,
+    # even when the SDK provided both figures (the footer used to append here too).
     res = ResultEvent(
-        session_id="s", is_error=False, subtype="success", num_turns=1, total_cost_usd=0.01
+        session_id="s", is_error=False, subtype="success", num_turns=3, total_cost_usd=0.012
     )
-    suffix = done_footer_suffix(res)
-    assert suffix == " · 1 turn · $0.01"  # only digits + the $ glyph (singular: "1 turn")
-
-
-def test_done_footer_suffix_pluralizes_turn():
-    # Cosmetic (UX): "1 turn" (singular) but "N turns" for N != 1 — never the ungrammatical
-    # "1 turns". Cover the singular, the plural, and the zero-edge (also plural: "0 turns").
-    one = ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=1)
-    assert done_footer_suffix(one) == " · 1 turn"
-    many = ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=2)
-    assert done_footer_suffix(many) == " · 2 turns"
-    zero = ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=0)
-    assert done_footer_suffix(zero) == " · 0 turns"
+    action = render_event(res)
+    assert action.text == "✅ done (success)"
+    assert "$" not in action.text
+    assert "turns" not in action.text
+
+
+def test_no_render_path_emits_a_dollar_amount():
+    # SB3 / the "no dollars anywhere routine" invariant: sweep the common result renders and
+    # assert NONE carries a "$". (Cost lives on /status only — covered in test_bot_streaming.)
+    for res in (
+        ResultEvent(session_id="s", is_error=False, subtype="success",
+                    num_turns=1, total_cost_usd=0.01, result_text="prose"),
+        ResultEvent(session_id="s", is_error=False, subtype="success",
+                    num_turns=5, total_cost_usd=1.5),
+        ResultEvent(session_id="s", is_error=False, subtype="success"),
+    ):
+        action = render_event(res)
+        assert "$" not in (action.text or "")
+        for chunk in (action.plain_chunks or ()):
+            assert "$" not in chunk
 
 
 # ===========================================================================
@@ -2079,3 +2075,166 @@ def test_schedule_listing_skips_malformed_entry():
 def test_schedule_listing_due_now_when_past():
     out = schedule_listing([_sch(next_run=5.0)], now=10_000.0)
     assert "due now" in out
+
+
+# ---------------------------------------------------------------------------
+# STATUSLINE T-SL-CORE — the pure formatter + the model_short_label helper.
+# The format is owner-LOCKED:
+#   📁 <worktree> · 🤖 <model>·<effort> · 🧠 ctx <X%> · 🔒 <mode>
+# (with a leading "⚙️ " when working). The pieces under test are PURE (no I/O), so they
+# are exercised directly. SB3 is the binding constraint: every field is escaped once, and a
+# path-shaped name is wrapped in <code> so no /segment fake-link can appear.
+# ---------------------------------------------------------------------------
+
+
+def test_format_statusline_full_set_exact_format():
+    # The complete, owner-locked line for a working turn: worktree · model·effort · ctx% · mode.
+    line = format_statusline(
+        worktree="claude-telegram-bot",
+        model_label="opus",
+        effort="max",
+        ctx_pct=6,
+        mode="gate",
+        working=True,
+    )
+    assert line == "⚙️ 📁 claude-telegram-bot · 🤖 opus·max · 🧠 ctx 6% · 🔒 gate"
+
+
+def test_format_statusline_idle_has_no_working_marker():
+    # working=False → NO leading ⚙️ (the marker is present iff a turn is running).
+    line = format_statusline(
+        worktree="proj", model_label="sonnet", effort="high", ctx_pct=42, mode="yolo", working=False
+    )
+    assert line == "📁 proj · 🤖 sonnet·high · 🧠 ctx 42% · 🔒 yolo"
+    assert not line.startswith("⚙️")
+
+
+def test_format_statusline_effort_none_shows_model_only():
+    # effort=None → just the model (🤖 opus), never an invented ·<effort>.
+    line = format_statusline(
+        worktree="proj", model_label="opus", effort=None, ctx_pct=10, mode="gate", working=False
+    )
+    assert "🤖 opus ·" in line
+    assert "opus·" not in line  # no dot-effort suffix at all
+
+
+def test_format_statusline_ctx_none_is_em_dash_never_zero():
+    # ctx_pct=None → "🧠 ctx —" (em dash). NEVER a fabricated "0%" (design §2.1).
+    line = format_statusline(
+        worktree="proj", model_label="opus", effort=None, ctx_pct=None, mode="gate", working=False
+    )
+    assert "🧠 ctx —" in line
+    assert "0%" not in line
+    assert "ctx —%" not in line  # the dash replaces the WHOLE figure, not just the number
+
+
+def test_format_statusline_ctx_zero_is_a_real_zero_not_a_dash():
+    # A genuine 0 (an int) is shown as 0% — only None becomes the dash. (0 is a real reading,
+    # the dash means "unknown".)
+    line = format_statusline(
+        worktree="proj", model_label="opus", effort=None, ctx_pct=0, mode="gate", working=False
+    )
+    assert "🧠 ctx 0%" in line
+    assert "—" not in line
+
+
+def test_format_statusline_working_marker_on_off():
+    on = format_statusline(
+        worktree="p", model_label="opus", effort=None, ctx_pct=None, mode="gate", working=True
+    )
+    off = format_statusline(
+        worktree="p", model_label="opus", effort=None, ctx_pct=None, mode="gate", working=False
+    )
+    assert on.startswith("⚙️ 📁")
+    assert off.startswith("📁")
+    # The only difference is the leading marker.
+    assert on == "⚙️ " + off
+
+
+def test_format_statusline_all_three_modes():
+    for mode in ("gate", "yolo", "plan"):
+        line = format_statusline(
+            worktree="p", model_label="opus", effort=None, ctx_pct=None, mode=mode, working=False
+        )
+        assert f"🔒 {mode}" in line
+
+
+# --- SB3 (the binding constraint): escape-once + no path-as-fake-link --------
+
+
+def test_format_statusline_escapes_angle_and_amp_in_name():
+    # SB3: a name carrying < / > / & is HTML-escaped exactly once so it can't break the HTML
+    # message or inject a tag. (The SB4 charset forbids these, but escape-once is the insurance.)
+    line = format_statusline(
+        worktree="a<b>&c", model_label="opus", effort=None, ctx_pct=None, mode="gate", working=False
+    )
+    assert "&lt;" in line and "&gt;" in line and "&amp;" in line
+    # The raw, unescaped sequence must NOT appear (no tag injection).
+    assert "<b>" not in line
+
+
+def test_format_statusline_path_shaped_name_wrapped_in_code_no_fake_link():
+    # SB3 / P8: a path-shaped value (one containing "/") is wrapped in <code>…</code> so
+    # Telegram renders it as inert monospace — its /segment runs CANNOT linkify into fake
+    # command-links. (Defensive: the validated name has no "/", but if one ever leaks through
+    # it is rendered safely, never as a bare path.)
+    line = format_statusline(
+        worktree="/tmp/secret/proj", model_label="opus", effort=None, ctx_pct=None, mode="gate", working=False
+    )
+    assert "<code>/tmp/secret/proj</code>" in line
+    # The path is NOT emitted bare (which Telegram would linkify each /segment of).
+    assert "📁 /tmp/secret/proj " not in line
+
+
+def test_format_statusline_path_with_special_chars_escaped_inside_code():
+    # A path-shaped name containing HTML metacharacters is escaped INSIDE the <code> wrap
+    # (code_path escapes exactly once) — valid HTML, no injection.
+    line = format_statusline(
+        worktree="/x/<a>&/y", model_label="opus", effort=None, ctx_pct=None, mode="gate", working=False
+    )
+    assert "<code>/x/&lt;a&gt;&amp;/y</code>" in line
+
+
+def test_format_statusline_escapes_odd_effort_and_mode_defensively():
+    # effort/mode are fixed words in practice, but the formatter escapes every interpolated
+    # field once — a stray < in any of them can never break the message (defense in depth).
+    line = format_statusline(
+        worktree="p", model_label="m<x", effort="e&y", ctx_pct=None, mode="z>w", working=False
+    )
+    assert "m&lt;x" in line and "e&amp;y" in line and "z&gt;w" in line
+
+
+# --- model_short_label mapping (regex/contains → family; unknown → raw id) ----
+
+
+def test_model_short_label_known_families():
+    assert model_short_label("claude-opus-4-8") == "opus"
+    assert model_short_label("claude-sonnet-4-5") == "sonnet"
+    assert model_short_label("claude-haiku-4-5") == "haiku"
+
+
+def test_model_short_label_case_insensitive():
+    assert model_short_label("CLAUDE-OPUS-4-8") == "opus"
+    assert model_short_label("Claude-Haiku-4-5") == "haiku"
+
+
+def test_model_short_label_unknown_returns_raw_id():
+    # RB1: an id matching no known family is shown VERBATIM (never mislabelled / crashed).
+    assert model_short_label("some-future-model-x9") == "some-future-model-x9"
+    assert model_short_label("gpt-4o") == "gpt-4o"
+
+
+def test_model_short_label_none_and_blank_are_empty():
+    assert model_short_label(None) == ""
+    assert model_short_label("") == ""
+    assert model_short_label("   ") == ""
+
+
+def test_statusline_carries_no_dollar_or_secret():
+    # SB3 structural: the line is bot-derived STATE — no dollar amount, no body. The fields are
+    # a name, a model word, an effort word, a number, a mode word. Nothing here can carry a
+    # secret (proven by construction; this guards a regression that adds a body field).
+    line = format_statusline(
+        worktree="proj", model_label="opus", effort="max", ctx_pct=6, mode="yolo", working=True
+    )
+    assert "$" not in line
diff --git a/tests/test_security_reliability.py b/tests/test_security_reliability.py
index dcb98b0..4e8f0e6 100644
--- a/tests/test_security_reliability.py
+++ b/tests/test_security_reliability.py
@@ -136,9 +136,10 @@ class FakeStreaming:
         self.resolve_calls: list[tuple[int, object]] = []
 
     async def handle_message(
-        self, chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
-        command_initiated=False,
+        self, chat_id, text, *, send, edit, delete=None, pin=None, unpin=None,
+        reply_to_message_id=None, command_initiated=False,
     ):
+        # STATUSLINE T-SL-WIRE: + pin/unpin (the statusline closures threaded by the bot).
         self.handle_message_calls.append((chat_id, text))
 
     def resolve_callback(self, chat_id, data):
diff --git a/tests/test_session_store.py b/tests/test_session_store.py
index 7045353..a2f8fd0 100644
--- a/tests/test_session_store.py
+++ b/tests/test_session_store.py
@@ -671,6 +671,87 @@ def test_set_model_writes_0600(tmp_path):
     assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
 
 
+# ---- T-EFFORT (STATUSLINE): per-project reasoning-EFFORT override -------------
+#   Mirrors the model-override cases above: round-trip + persist, clear-on-None,
+#   case-insensitive, unknown→None (RB1), unknown-project-raises, sparse-safe, 0600.
+
+
+def test_set_get_effort_roundtrip_and_persist(tmp_path):
+    path = tmp_path / "state.json"
+    store = JsonSessionStore(path)
+    store.create(1, "alpha", "/work/alpha", make_active=True)
+    assert store.get_effort(1, "alpha") is None  # no override by default
+    store.set_effort(1, "alpha", "max")
+    assert store.get_effort(1, "alpha") == "max"
+    # Persists across a fresh store over the same file (RB6).
+    store2 = JsonSessionStore(path)
+    assert store2.get_effort(1, "alpha") == "max"
+
+
+def test_set_effort_clear_removes_override(tmp_path):
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", "/work/alpha", make_active=True)
+    store.set_effort(1, "alpha", "high")
+    assert store.get_effort(1, "alpha") == "high"
+    store.set_effort(1, "alpha", None)  # bare /effort clears
+    assert store.get_effort(1, "alpha") is None
+
+
+def test_set_effort_case_insensitive_value_and_unknown_clears(tmp_path):
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "Alpha", "/work/alpha", make_active=True)
+    store.set_effort(1, "alpha", "MAX")  # value is normalized to lowercase; key case-insensitive
+    assert store.get_effort(1, "ALPHA") == "max"
+    # An unknown/garbage level normalizes to None (a cleared override) — never a bad stored value.
+    store.set_effort(1, "alpha", "turbo")
+    assert store.get_effort(1, "alpha") is None
+    store.set_effort(1, "alpha", "xhigh")
+    assert store.get_effort(1, "alpha") == "xhigh"
+    store.set_effort(1, "alpha", "   ")  # whitespace/empty normalizes to a clear
+    assert store.get_effort(1, "alpha") is None
+
+
+def test_set_effort_unknown_project_raises(tmp_path):
+    store = JsonSessionStore(tmp_path / "state.json")
+    with pytest.raises(UnknownProject):
+        store.set_effort(1, "nope", "max")
+
+
+def test_get_effort_unknown_or_unset_is_none(tmp_path):
+    store = JsonSessionStore(tmp_path / "state.json")
+    assert store.get_effort(1, "nope") is None
+    store.create(1, "alpha", "/work/alpha", make_active=True)
+    assert store.get_effort(1, "alpha") is None
+
+
+def test_get_effort_sparse_record_and_garbage_value_are_safe(tmp_path):
+    # RB1: a hand-edited / sparse record never crashes get_effort — a non-string or
+    # unrecognized stored value (and a record with no effort field) all read as None.
+    path = tmp_path / "state.json"
+    store = JsonSessionStore(path)
+    store.create(1, "alpha", "/work/alpha", make_active=True)
+    raw = json.loads(path.read_text())
+    raw["chats"]["1"]["projects"]["alpha"]["effort"] = 12345  # non-string garbage
+    raw["chats"]["1"]["projects"]["sparse"] = {}  # sparse record, no fields at all
+    raw["chats"]["1"]["projects"]["bad"] = {"effort": "ludicrous"}  # unrecognized level
+    path.write_text(json.dumps(raw))
+    store2 = JsonSessionStore(path)
+    assert store2.get_effort(1, "alpha") is None  # non-string → None
+    assert store2.get_effort(1, "sparse") is None  # sparse → None
+    assert store2.get_effort(1, "bad") is None  # unrecognized stored level → None
+
+
+def test_set_effort_writes_0600(tmp_path):
+    import os
+    import stat
+
+    path = tmp_path / "state.json"
+    store = JsonSessionStore(path)
+    store.create(1, "alpha", "/work/alpha", make_active=True)
+    store.set_effort(1, "alpha", "max")
+    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
+
+
 # ---- T5 (P9): per-chat macros -------------------------------------------------
 
 
diff --git a/tests/test_skill_launch.py b/tests/test_skill_launch.py
index 1f2390e..2e05b51 100644
--- a/tests/test_skill_launch.py
+++ b/tests/test_skill_launch.py
@@ -74,12 +74,13 @@ class FakeStreaming:
         self.handle_message_calls: list[tuple[int, str]] = []
 
     async def handle_message(
-        self, chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
-        command_initiated=False,
+        self, chat_id, text, *, send, edit, delete=None, pin=None, unpin=None,
+        reply_to_message_id=None, command_initiated=False,
     ):
         # P5/T9: handle_message gained reply_to_message_id (D5); P9 fix added
-        # command_initiated (a macro /run skips free-text capture). The skill-launch tests
-        # don't exercise either, so we keep recording just (chat_id, text).
+        # command_initiated (a macro /run skips free-text capture); STATUSLINE T-SL-WIRE added
+        # pin/unpin (the statusline closures). The skill-launch tests don't exercise them, so we
+        # keep recording just (chat_id, text).
         self.handle_message_calls.append((chat_id, text))
 
     def reset(self, chat_id):
diff --git a/tests/test_stream_session.py b/tests/test_stream_session.py
index 722d41c..8a8124c 100644
--- a/tests/test_stream_session.py
+++ b/tests/test_stream_session.py
@@ -90,9 +90,12 @@ HOLD = object()  # sentinel in a script: park send() here until a resolve/cancel
 
 
 class FakeEngine:
-    def __init__(self, script: list, *, session_id="sess-1", resolve_result=True):
+    def __init__(self, script: list, *, session_id="sess-1", resolve_result=True, ctx_pct=None):
         self._script = script
         self.session_id = session_id
+        # STATUSLINE T-SL-CORE: the ctx % the statusline reads via engine.context_percentage().
+        # Default None (→ "ctx —"); a test sets it to assert the figure flows into the line.
+        self._ctx_pct = ctx_pct
         self.resolve_calls: list[tuple[str, object]] = []
         self.cancel_calls: list = []
         # P14 T-FIRE: records the ``proactive`` flag passed to each send() (the force-gate
@@ -140,6 +143,13 @@ class FakeEngine:
         self._gate.set()
         return 1
 
+    async def context_percentage(self):
+        # STATUSLINE T-SL-CORE / T-SL-WIRE (B1): the best-effort ctx % the statusline reads
+        # (None → "ctx —"). ASYNC to mirror the real Engine.context_percentage(), which awaits
+        # the SDK's coroutine get_context_usage() — so the live awaited path is exercised (a
+        # non-awaited regression would fail: awaiting a sync int raises).
+        return self._ctx_pct
+
 
 class Recorder:
     """Captures the send/edit/delete calls the driver performs.
@@ -6250,6 +6260,155 @@ def test_injected_factory_never_receives_model_kwarg(tmp_path):
     assert session._build_engine(1, str(tmp_path), PermissionPolicy(), model) is sentinel
 
 
+# ===========================================================================
+# T-EFFORT (STATUSLINE) — the per-project reasoning-EFFORT knob: the default
+# factory bakes the resolved override into ClaudeAgentOptions(effort=…), the
+# session resolves/persists it, and an /effort change rebuilds the warm engine
+# on the NEXT turn (effort is a session-creation knob, mirroring model/thinking).
+# ===========================================================================
+
+
+def test_default_factory_threads_per_project_effort_into_substrate(tmp_path):
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path), make_active=True)
+    store.set_effort(1, "alpha", "max")  # an /effort max override on alpha
+
+    session = StreamingSession(
+        _make_config_with_model(tmp_path), session_store=store, clock=lambda: 0.0
+    )
+    # The session resolves alpha's override...
+    assert session._resolve_project_effort(1, "alpha") == "max"
+    # ...and the engine it builds carries it into the substrate's ClaudeAgentOptions.
+    engine = session._build_engine(
+        1, str(tmp_path), PermissionPolicy(), None, effort="max"
+    )
+    assert engine._substrate._effort == "max"
+
+
+def test_resolve_project_effort_no_override_is_none_no_config_default(tmp_path):
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path), make_active=True)
+    # No override → None (there is NO CLAUDE_* global default for effort — the SDK default
+    # applies, so the kwarg is omitted). Even with a configured model (which DOES default),
+    # effort stays None.
+    session = StreamingSession(
+        _make_config_with_model(tmp_path, model="claude-opus-4-8"),
+        session_store=store,
+        clock=lambda: 0.0,
+    )
+    assert session._resolve_project_effort(1, "alpha") is None
+    # And the engine then builds with effort=None (SDK default), never an empty value.
+    assert (
+        session._build_engine(1, str(tmp_path), PermissionPolicy(), None, effort=None)
+        ._substrate._effort
+        is None
+    )
+
+
+def test_set_effort_persists_on_active_project_and_resolves_it(tmp_path):
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path), make_active=True)
+    session = StreamingSession(
+        _make_config_with_model(tmp_path), session_store=store, clock=lambda: 0.0
+    )
+    # /effort max sets the override on the active project + persists.
+    assert session.set_effort(1, "max") == "max"
+    assert store.get_effort(1, "alpha") == "max"
+    assert session._resolve_project_effort(1, "alpha") == "max"
+    # bare /effort (None) clears it → resolve falls back to None (SDK default).
+    assert session.set_effort(1, None) is None
+    assert store.get_effort(1, "alpha") is None
+    assert session._resolve_project_effort(1, "alpha") is None
+
+
+def test_set_effort_bad_value_is_safe_clears(tmp_path):
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path), make_active=True)
+    session = StreamingSession(
+        _make_config_with_model(tmp_path), session_store=store, clock=lambda: 0.0
+    )
+    # An unrecognized/garbage level normalizes to a clear (no override) — never a bad id.
+    assert session.set_effort(1, "turbo") is None
+    assert store.get_effort(1, "alpha") is None
+    assert session._resolve_project_effort(1, "alpha") is None
+
+
+def test_injected_factory_never_receives_effort_kwarg(tmp_path):
+    # An injected (test) factory keeps the 3-kwarg contract; _build_engine must NOT pass
+    # `effort` to it (it would TypeError). The fixed-3-kwarg lambda below would raise on an
+    # unexpected `effort=` — its clean return proves the gate (_factory_accepts_model) works.
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path), make_active=True)
+    store.set_effort(1, "alpha", "max")  # override present, but must not be passed
+    sentinel = object()
+    session = StreamingSession(
+        _make_config_with_model(tmp_path),
+        session_store=store,
+        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: sentinel,
+        clock=lambda: 0.0,
+    )
+    assert session._factory_accepts_model is False
+    effort = session._resolve_project_effort(1, "alpha")
+    assert effort == "max"
+    # Builds via the 3-kwarg injected factory WITHOUT effort (the gate strips it).
+    assert (
+        session._build_engine(1, str(tmp_path), PermissionPolicy(), None, effort=effort)
+        is sentinel
+    )
+
+
+async def test_ensure_engine_rebuilds_on_effort_change_next_turn(tmp_path):
+    # T-EFFORT: changing /effort must rebuild the session on the NEXT turn (effort is a
+    # session-creation knob baked into ClaudeAgentOptions — not hot-switchable). A back-to-back
+    # SAME-effort turn reuses the warm engine (the match-key includes engine_effort); an effort
+    # change drops the warm engine and builds a fresh one. Mirrors the warm-reuse regression
+    # test but proves the INVERSE (a change forces the rebuild).
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "api", str(tmp_path), make_active=True)
+
+    eng1 = FakeEngine(
+        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
+        session_id="s",
+    )
+    eng2 = FakeEngine(
+        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
+        session_id="s",
+    )
+    session = make_sequential_session({str(tmp_path): [eng1, eng2]}, store=store)
+
+    # Turn 1: builds eng1 with the current effort (None — no override yet).
+    e1, _ = await session._ensure_engine(1)
+    assert e1 is eng1 and eng1.started is True
+    rt = session._chat(1).runtimes["api"]
+    assert rt.engine_effort is None
+
+    # A same-effort second call reuses the warm engine (no rebuild, no second pop).
+    e1b, _ = await session._ensure_engine(1)
+    assert e1b is eng1, "a warm engine at the same effort must be reused"
+    assert eng1.stopped is False
+
+    # Now change /effort → the persisted override no longer matches engine_effort.
+    assert session.set_effort(1, "max") == "max"
+    # Next turn rebuilds: the warm eng1 is discarded (best-effort stop) and eng2 is built fresh
+    # with the new effort baked in.
+    e2, _ = await session._ensure_engine(1)
+    assert e2 is eng2, "an effort change must rebuild the session on the next turn"
+    assert eng1.stopped is True, "the stale-effort engine is torn down before the rebuild"
+    assert rt.engine_effort == "max"
+
+
 # ===========================================================================
 # T6 (P9) — notification polish + chips (SESSION level, mock-only).
 #   1. no link previews on background pings
@@ -7134,3 +7293,834 @@ async def test_fire_schedule_busy_skip_sends_only_skip_notice_not_header(tmp_pat
 
     engine.cancel()
     await asyncio.wait_for(first, timeout=2.0)
+
+
+# ---------------------------------------------------------------------------
+# STATUSLINE T-SL-CORE — the pinned statusline pin/edit lifecycle.
+#
+# _update_statusline builds the foreground statusline body from CURRENT state and reconciles
+# it with the chat's ONE pinned line: first use SENDS + PINS (silently); a changed state EDITS
+# in place; an identical state is a no-op; an edit FAILURE clears the id + re-sends + re-pins
+# (orphan recovery); and a pin/edit/send raising is SWALLOWED (RB1 — never breaks a turn). All
+# I/O funnels through the per-chat gate as the non-verbatim kind (RB5). Only ONE id is held.
+#
+# These exercise the machinery directly with fake send/edit/pin/unpin closures (T-SL-WIRE will
+# call _update_statusline from the live turn path; this unit is machinery-only).
+# ---------------------------------------------------------------------------
+
+
+class StatuslineRecorder:
+    """Captures the send/edit/pin/unpin calls _update_statusline performs (with fault injection).
+
+    ``fail_edit`` makes the FIRST edit raise (the orphan-recovery trigger — the operator
+    unpinned/deleted the line). ``fail_pin`` / ``fail_send`` make pin / send raise (the RB1
+    swallow probe). ``fail_pin_times=N`` makes only the first N pins raise (then succeed — the
+    pin-retry probe). Each call is recorded so the sequence + the silent-pin flag are assertable.
+    """
+
+    def __init__(self, *, fail_edit=False, fail_pin=False, fail_send=False, fail_pin_times=0):
+        self.sends: list[dict] = []
+        self.edits: list[dict] = []
+        self.pins: list[dict] = []
+        self.unpins: list[dict] = []
+        self._next_id = 500
+        self._fail_edit_first = fail_edit
+        self._fail_pin = fail_pin
+        self._fail_send = fail_send
+        self._fail_pin_remaining = fail_pin_times
+
+    async def send(self, *, text, reply_markup=None, parse_mode=None, **kwargs) -> int:
+        self.sends.append({"text": text, "parse_mode": parse_mode})
+        if self._fail_send:
+            raise RuntimeError("Telegram error: send failed")
+        self._next_id += 1
+        return self._next_id
+
+    async def edit(self, *, message_id, text, parse_mode=None) -> None:
+        if self._fail_edit_first:
+            self._fail_edit_first = False
+            raise RuntimeError("Telegram BadRequest: message to edit not found")
+        self.edits.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})
+
+    async def pin(self, *, message_id, disable_notification=None) -> None:
+        self.pins.append({"message_id": message_id, "disable_notification": disable_notification})
+        if self._fail_pin:
+            raise RuntimeError("Telegram error: pin failed")
+        if self._fail_pin_remaining > 0:
+            self._fail_pin_remaining -= 1
+            raise RuntimeError("Telegram error: pin failed (transient)")
+
+    async def unpin(self, *, message_id) -> None:
+        self.unpins.append({"message_id": message_id})
+
+
+def _prime_statusline_project(session, *, chat_id=1, engine=None, status="running"):
+    """Resolve the chat's active project + give its runtime an engine + status (statusline read).
+
+    _update_statusline reads the FOREGROUND project's live state. With no store this auto-creates
+    the implicit ``default`` runtime; we attach a (fake) engine for the ctx-% read and set the
+    status enum (working vs idle). Returns the (name, runtime).
+    """
+    name, rt = session._active_runtime(chat_id, create_default=True)
+    if engine is not None:
+        rt.engine = engine
+    rt.status = status
+    return name, rt
+
+
+async def test_update_statusline_first_use_sends_then_pins_silently():
+    # First update for a chat: SEND the body, then PIN it with the notification disabled.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    name, _rt = _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder()
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+
+    assert len(rec.sends) == 1, f"first use must SEND exactly once, sends={rec.sends!r}"
+    assert len(rec.pins) == 1, f"first use must PIN exactly once, pins={rec.pins!r}"
+    # The pin is SILENT (disable_notification=True) — design §3.1 (no re-ping).
+    assert rec.pins[0]["disable_notification"] is True
+    # The pinned id is the just-sent id (501 — the recorder hands out 501, 502, …).
+    assert rec.pins[0]["message_id"] == 501
+    # No edit on first use.
+    assert rec.edits == []
+    # The body is the locked format (working marker on, the project name, the ctx %, the gate).
+    body = rec.sends[0]["text"]
+    assert body.startswith("⚙️ 📁 ")
+    assert "🧠 ctx 6%" in body
+    assert "🔒 gate" in body
+    assert rec.sends[0]["parse_mode"] == "HTML"
+    # The id + text are tracked on the chat (the one-pin invariant).
+    state = session._chat(1)
+    assert state.statusline_message_id == 501
+    assert state.statusline_text == body
+
+
+async def test_update_statusline_second_changed_edits_in_place_no_repin():
+    # A SUBSEQUENT update with changed state EDITS in place — no re-pin, no re-send.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder()
+    # First update → send + pin.
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    # Change state (turn ends → idle; ctx grows to 7) and update again.
+    rt = active_rt(session)
+    rt.status = "idle"
+    eng._ctx_pct = 7
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+
+    assert len(rec.sends) == 1, "the second update must NOT re-send (edit in place)"
+    assert len(rec.pins) == 1, "the second update must NOT re-pin"
+    assert len(rec.edits) == 1, f"the second update must EDIT once, edits={rec.edits!r}"
+    edited = rec.edits[0]
+    assert edited["message_id"] == 501  # the SAME pinned message is edited
+    assert "🧠 ctx 7%" in edited["text"]
+    assert not edited["text"].startswith("⚙️")  # idle → no working marker
+    assert edited["parse_mode"] == "HTML"
+    # The tracked text is the new body.
+    assert session._chat(1).statusline_text == edited["text"]
+
+
+async def test_update_statusline_identical_state_is_no_io():
+    # Identical text → skip entirely (no send, no edit — a no-op edit raises "not modified" and
+    # wastes a send slot).
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder()
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    # Nothing changed — a second update must be a pure no-op.
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+
+    assert len(rec.sends) == 1, "an identical state must not re-send"
+    assert rec.edits == [], "an identical state must not edit (no-op skip)"
+    assert len(rec.pins) == 1
+
+
+async def test_update_statusline_edit_failure_resends_and_repins_orphan_recovery():
+    # The operator unpinned/deleted the line → the in-place edit raises "message to edit not
+    # found". Recovery: clear the dead id, best-effort UNPIN the stale one, re-SEND + re-PIN.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder(fail_edit=True)  # the first edit will raise
+    # First update → send (id 501) + pin.
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert session._chat(1).statusline_message_id == 501
+    # Change state → an EDIT is attempted; it fails → recovery re-sends (id 502) + re-pins.
+    active_rt(session).status = "idle"
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+
+    assert len(rec.sends) == 2, f"recovery must re-SEND a fresh line, sends={rec.sends!r}"
+    assert len(rec.pins) == 2, f"recovery must re-PIN the fresh line, pins={rec.pins!r}"
+    # The stale id (501) was best-effort UNPINNED before re-pinning (one-pin invariant).
+    assert {u["message_id"] for u in rec.unpins} == {501}
+    # The chat now holds the NEW id (502), and only one.
+    assert session._chat(1).statusline_message_id == 502
+    assert rec.pins[-1]["message_id"] == 502
+    assert rec.pins[-1]["disable_notification"] is True
+
+
+async def test_update_statusline_pin_failure_is_swallowed_turn_unaffected():
+    # ⭐ RB1: a PIN that raises must NEVER escape — _update_statusline returns normally, the
+    # line is still sent + tracked (only the bar placement is lost).
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder(fail_pin=True)
+    # Must NOT raise.
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert len(rec.sends) == 1  # the line was still sent
+    assert len(rec.pins) == 1  # the pin was attempted (and raised, swallowed)
+    # The id is still tracked (the send succeeded), so the next update edits in place.
+    assert session._chat(1).statusline_message_id == 501
+
+
+async def test_update_statusline_send_failure_is_swallowed_turn_unaffected():
+    # ⭐ RB1: a SEND that raises must NEVER escape — _update_statusline returns normally and no
+    # id is left half-set.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder(fail_send=True)
+    # Must NOT raise.
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    # The send raised before any id was returned → nothing pinned, nothing tracked.
+    assert rec.pins == []
+    assert session._chat(1).statusline_message_id is None
+    assert session._chat(1).statusline_text is None
+
+
+async def test_update_statusline_only_one_id_ever_held_across_many_updates():
+    # The one-pin invariant: across many state changes, exactly one id is held and only edits
+    # happen after the first pin.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=1)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder()
+    for pct in (1, 2, 3, 4, 5):
+        eng._ctx_pct = pct
+        await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+
+    assert len(rec.sends) == 1, "only the FIRST update sends; the rest edit"
+    assert len(rec.pins) == 1, "only ONE pin ever"
+    assert len(rec.edits) == 4, "the 4 changed updates each edit in place"
+    # All edits target the single held id.
+    assert {e["message_id"] for e in rec.edits} == {501}
+    assert session._chat(1).statusline_message_id == 501
+
+
+async def test_update_statusline_no_foreground_project_is_noop(tmp_path):
+    # With no active project (read-only resolve, create_default=False) there is nothing to
+    # describe → no I/O, no created runtime.
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    assert store.get_active(1) is None  # nothing active yet
+    session = make_session(FakeEngine([]), store=store)
+    rec = StatuslineRecorder()
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert rec.sends == [] and rec.pins == [] and rec.edits == []
+    # The read-only resolve must NOT have created a runtime.
+    assert store.get_active(1) is None
+
+
+async def test_update_statusline_yolo_mode_shows_in_line():
+    # The mode field reflects the project's posture: a yolo (allow-all) policy → "🔒 yolo".
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _name, rt = _prime_statusline_project(session, engine=eng, status="running")
+    rt.policy.yolo = True
+    rec = StatuslineRecorder()
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert "🔒 yolo" in rec.sends[0]["text"]
+
+
+async def test_update_statusline_plan_armed_shows_in_line():
+    # An armed /plan (plan_next) → "🔒 plan" (when not yolo).
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _name, rt = _prime_statusline_project(session, engine=eng, status="idle")
+    rt.plan_next = True
+    rec = StatuslineRecorder()
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert "🔒 plan" in rec.sends[0]["text"]
+
+
+async def test_update_statusline_ctx_none_when_no_engine_shows_em_dash():
+    # No live engine on the runtime → ctx is unknown → "🧠 ctx —" (never a fabricated 0%).
+    session = make_session(FakeEngine([]))
+    _prime_statusline_project(session, engine=None, status="idle")
+    rec = StatuslineRecorder()
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert "🧠 ctx —" in rec.sends[0]["text"]
+    assert "0%" not in rec.sends[0]["text"]
+
+
+async def test_update_statusline_engine_ctx_raises_is_swallowed_shows_dash():
+    # ⭐ RB1: a context_percentage() that raises is swallowed (the line still renders, ctx —).
+    class _BoomCtxEngine(FakeEngine):
+        async def context_percentage(self):
+            raise RuntimeError("ctx boom")
+
+    session = make_session(FakeEngine([]))
+    _prime_statusline_project(session, engine=_BoomCtxEngine([]), status="running")
+    rec = StatuslineRecorder()
+    # Must NOT raise.
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert len(rec.sends) == 1
+    assert "🧠 ctx —" in rec.sends[0]["text"]
+
+
+async def test_update_statusline_edit_and_resend_both_raise_still_swallowed():
+    # ⭐ The make-or-break RB1 mutation probe: the in-place edit raises AND the recovery re-send
+    # ALSO raises (the chat is fully wedged at the Telegram layer). _update_statusline must STILL
+    # return normally — the outermost best-effort guard swallows everything; the turn is
+    # unaffected. (Proves no exception can escape via the recovery path either.)
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+
+    # First update with a working send/pin to establish the pinned id.
+    good = StatuslineRecorder()
+    await session._update_statusline(1, send=good.send, edit=good.edit, pin=good.pin, unpin=good.unpin)
+    assert session._chat(1).statusline_message_id == 501
+
+    # Now: the edit raises (orphan trigger) AND the re-send raises too.
+    async def boom_edit(*, message_id, text, parse_mode=None):
+        raise RuntimeError("edit not found")
+
+    async def boom_send(*, text, reply_markup=None, parse_mode=None, **kwargs):
+        raise RuntimeError("send failed too")
+
+    async def boom_unpin(*, message_id):
+        raise RuntimeError("unpin failed too")
+
+    active_rt(session).status = "idle"  # change state → an edit is attempted
+    # Must NOT raise despite every closure failing.
+    await session._update_statusline(1, send=boom_send, edit=boom_edit, pin=good.pin, unpin=boom_unpin)
+    # The dead id was cleared on the failed-edit path (recovery couldn't re-establish one).
+    assert session._chat(1).statusline_message_id is None
+
+
+# ===========================================================================
+# STATUSLINE T-SL-WIRE — the statusline wired into the LIVE turn lifecycle.
+#
+# These drive REAL turns / commands through the session and assert the pinned line is
+# updated at the right moments:
+#   * turn START pins the line with the working ⚙️ marker ON; turn END edits it OFF + ctx %;
+#   * /switch (the session-level _maybe_update_statusline, for_project=None) rewrites the line
+#     to the now-active project; the knob refreshes (/yolo, /effort, /fast) flip the field live;
+#   * ⭐ a BACKGROUND turn (a non-active project running) does NOT rewrite the foreground line
+#     (the make-or-break foreground-only invariant — mutation probe).
+# All triggers are best-effort (a pin/edit failure never breaks the turn).
+# ---------------------------------------------------------------------------
+
+
+class PinRecorder:
+    """Captures pin/unpin calls (the statusline's send/edit ride the regular Recorder).
+
+    In a real turn the SAME send/edit closures carry BOTH the turn's output AND the
+    statusline, so the integration tests use the regular :class:`Recorder` for send/edit
+    (statusline lines are identified by the 📁 glyph) and THIS for pin/unpin.
+    """
+
+    def __init__(self):
+        self.pins: list[dict] = []
+        self.unpins: list[dict] = []
+
+    async def pin(self, *, message_id, disable_notification=None) -> None:
+        self.pins.append({"message_id": message_id, "disable_notification": disable_notification})
+
+    async def unpin(self, *, message_id) -> None:
+        self.unpins.append({"message_id": message_id})
+
+
+def _statusline_sends(rec: "Recorder") -> list[dict]:
+    """The subset of ``rec.sends`` that are statusline lines (carry the 📁 worktree glyph)."""
+    return [s for s in rec.sends if "📁" in (s.get("text") or "")]
+
+
+def _statusline_edits(rec: "Recorder") -> list[dict]:
+    """The subset of ``rec.edits`` that are statusline lines (carry the 📁 worktree glyph)."""
+    return [e for e in rec.edits if "📁" in (e.get("text") or "")]
+
+
+async def test_foreground_turn_pins_at_start_then_refreshes_at_end():
+    # ⭐ A FOREGROUND turn: the line is PINNED at turn start with the working ⚙️ marker ON, then
+    # EDITED in place at turn end with the marker OFF and the ctx % refreshed. This is the core
+    # turn-lifecycle wiring (T7): _drive_turn calls _update_statusline at start + end.
+    engine = FakeEngine(
+        [
+            TextEvent(text="working", incremental=False),
+            ResultEvent(session_id="sess-1", is_error=False, subtype="success", result_text="done!"),
+        ],
+        ctx_pct=12,
+    )
+    session = make_session(engine)
+    # Prime the active project's runtime with the SAME engine so _statusline_text reads ctx 12%.
+    name, rt = session._active_runtime(1, create_default=True)
+    rt.engine = engine
+    rec = Recorder()
+    pins = PinRecorder()
+    state = session._chat(1)
+    await asyncio.wait_for(
+        session._drive_turn(
+            state, 1, engine, "go",
+            send=rec.send, edit=rec.edit, delete=rec.delete,
+            pin=pins.pin, unpin=pins.unpin, target=(name, rt),
+        ),
+        timeout=2.0,
+    )
+    sl_sends = _statusline_sends(rec)
+    sl_edits = _statusline_edits(rec)
+    # Turn START: exactly one statusline SEND, PINNED silently, with the working ⚙️ marker ON.
+    assert len(sl_sends) == 1, f"turn start must pin the line once, statusline sends={sl_sends!r}"
+    assert sl_sends[0]["text"].startswith("⚙️ 📁 "), "turn start → working ⚙️ marker ON"
+    assert "🧠 ctx 12%" in sl_sends[0]["text"]
+    assert len(pins.pins) == 1 and pins.pins[0]["disable_notification"] is True
+    # Turn END: the line is EDITED in place (same pinned id), marker OFF (idle), ctx refreshed.
+    assert sl_edits, "turn end must edit the statusline (marker off + ctx refresh)"
+    end = sl_edits[-1]
+    assert not end["text"].startswith("⚙️"), "turn end → working marker OFF (idle)"
+    assert "🧠 ctx 12%" in end["text"]
+    assert end["message_id"] == pins.pins[0]["message_id"], "the SAME pinned line is edited"
+    # Only ONE pin across the whole turn (the one-pin invariant holds through the lifecycle).
+    assert len(pins.pins) == 1
+
+
+async def test_turn_without_pin_closures_still_runs_no_statusline():
+    # Back-compat: a caller that does NOT inject pin/unpin (every pre-T-SL-WIRE path / test)
+    # drives the turn normally — the statusline is simply not pinned/edited, the turn is
+    # unaffected. (_maybe_update_statusline no-ops when any closure is missing.)
+    engine = FakeEngine(
+        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
+        ctx_pct=5,
+    )
+    session = make_session(engine)
+    rec = Recorder()
+    await asyncio.wait_for(
+        session.handle_message(1, "go", send=rec.send, edit=rec.edit),  # no pin/unpin
+        timeout=2.0,
+    )
+    assert any("ok" in s["text"] for s in rec.sends)  # the turn ran + rendered its result
+    assert _statusline_sends(rec) == [], "no pin closures → no statusline send"
+    assert _statusline_edits(rec) == []
+
+
+async def test_background_turn_does_not_rewrite_foreground_statusline(tmp_path):
+    # ⭐⭐ THE MAKE-OR-BREAK WIRING INVARIANT (design §3.1): a BACKGROUND turn (alpha runs while
+    # BETA is the active/foreground project) must NEVER touch the pinned statusline — the line
+    # describes the FOREGROUND project only. _drive_turn gates its start/end statusline triggers
+    # on _is_foreground(turn_name); a background turn skips them.
+    #
+    # MUTATION PROBE: if the turn-start/turn-end triggers were NOT foreground-gated (i.e. a
+    # background turn rewrote the line), this test FAILS — the recorder would capture a 📁
+    # statusline send/edit/pin for the backgrounded alpha turn.
+    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
+    eng_alpha._script = [
+        ResultEvent(session_id="alpha-sid", is_error=False, subtype="success", result_text="bg done"),
+    ]
+    eng_alpha._ctx_pct = 9
+    rt_alpha = session._chat(1).runtimes["alpha"]
+    rec = Recorder()
+    pins = PinRecorder()
+    state = session._chat(1)
+    # Drive ALPHA (a BACKGROUND project — beta is active) to completion WITH pin/unpin wired.
+    await asyncio.wait_for(
+        session._drive_turn(
+            state, 1, eng_alpha, "go",
+            send=rec.send, edit=rec.edit, delete=rec.delete,
+            pin=pins.pin, unpin=pins.unpin, target=("alpha", rt_alpha),
+        ),
+        timeout=2.0,
+    )
+    # The turn RAN as a BACKGROUND turn (its terminal is a "✅ alpha — done" ping, NOT inline
+    # result text — the P5 background-notify path; this also confirms it took the background
+    # branch, the exact scenario the foreground gate must cover) …
+    assert any(s["text"].startswith("✅ alpha — done") for s in rec.sends)
+    # … but the foreground statusline was NEVER written — no 📁 send/edit, no pin.
+    assert _statusline_sends(rec) == [], "a BACKGROUND turn must NOT pin/send the foreground line"
+    assert _statusline_edits(rec) == [], "a BACKGROUND turn must NOT edit the foreground line"
+    assert pins.pins == [], "a BACKGROUND turn must NOT pin the foreground line"
+    # And no statusline id was established for the chat (nothing was pinned).
+    assert session._chat(1).statusline_message_id is None
+
+
+async def test_foreground_turn_among_two_projects_updates_line(tmp_path):
+    # The complement of the background probe: when the RUNNING project IS the foreground (alpha
+    # active), its turn DOES pin/refresh the line — so the gate keys on foreground, not on
+    # "two projects exist". (Together with the background test this pins the invariant exactly.)
+    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="alpha")
+    eng_alpha._script = [
+        ResultEvent(session_id="alpha-sid", is_error=False, subtype="success", result_text="fg done"),
+    ]
+    eng_alpha._ctx_pct = 4
+    rt_alpha = session._chat(1).runtimes["alpha"]
+    rec = Recorder()
+    pins = PinRecorder()
+    state = session._chat(1)
+    await asyncio.wait_for(
+        session._drive_turn(
+            state, 1, eng_alpha, "go",
+            send=rec.send, edit=rec.edit, delete=rec.delete,
+            pin=pins.pin, unpin=pins.unpin, target=("alpha", rt_alpha),
+        ),
+        timeout=2.0,
+    )
+    sl_sends = _statusline_sends(rec)
+    assert len(sl_sends) == 1, "the FOREGROUND project's turn pins the line"
+    assert "📁 alpha" in sl_sends[0]["text"], "the line names the foreground project (alpha)"
+    assert len(pins.pins) == 1
+
+
+async def test_switch_rewrites_statusline_to_new_project(tmp_path):
+    # /switch's session-level refresh (_maybe_update_statusline with for_project=None — the
+    # command path is foreground by definition) REWRITES the pinned line for the NOW-active
+    # project. Establish a line on alpha, switch active to beta, refresh → the line names beta.
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
+    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
+    (tmp_path / "a").mkdir()
+    (tmp_path / "b").mkdir()
+    eng = FakeEngine([], ctx_pct=3)
+    session = make_multi_session({str(tmp_path / "a"): eng, str(tmp_path / "b"): eng}, store=store)
+    rec = Recorder()
+    pins = PinRecorder()
+    # First refresh (alpha active) → pin a line naming alpha.
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
+    )
+    assert _statusline_sends(rec) and "📁 alpha" in _statusline_sends(rec)[0]["text"]
+    # /switch → beta is now the active/foreground project; refresh rewrites the SAME line.
+    store.switch(1, "beta")
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
+    )
+    sl_edits = _statusline_edits(rec)
+    assert sl_edits, "the switch must EDIT the existing pinned line (not re-send)"
+    assert "📁 beta" in sl_edits[-1]["text"], "the line now names the switched-to project (beta)"
+    assert len(pins.pins) == 1, "switch edits in place — no re-pin"
+
+
+async def test_yolo_change_flips_mode_on_statusline():
+    # /yolo flips the mode field 🔒 gate → 🔒 yolo live (the knob refresh path: set_yolo then
+    # _maybe_update_statusline for_project=None).
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="idle")
+    rec = Recorder()
+    pins = PinRecorder()
+    # Initial line → 🔒 gate (the fail-closed default).
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
+    )
+    assert "🔒 gate" in _statusline_sends(rec)[0]["text"]
+    # /yolo → set_yolo(True) → refresh → 🔒 yolo.
+    session.set_yolo(1, True)
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
+    )
+    assert "🔒 yolo" in _statusline_edits(rec)[-1]["text"]
+
+
+async def test_effort_change_flips_model_suffix_on_statusline(tmp_path):
+    # /effort max → the 🤖 model·effort suffix updates live (set_effort persists, refresh shows
+    # ·max). Uses a real store so set_effort persists the override the statusline reads back.
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path), make_active=True)
+    eng = FakeEngine([], ctx_pct=6)
+    session = make_multi_session({str(tmp_path): eng}, store=store)
+    rt = session._runtime(1, "alpha", str(tmp_path))
+    rt.engine = eng
+    rec = Recorder()
+    pins = PinRecorder()
+    # Initial line (no effort override) → model only, no ·effort suffix.
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
+    )
+    assert "·max" not in _statusline_sends(rec)[0]["text"]
+    # /effort max → set_effort persists → refresh → the suffix shows ·max.
+    session.set_effort(1, "max")
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
+    )
+    assert "·max" in _statusline_edits(rec)[-1]["text"], "the 🤖 model·effort suffix flips to ·max"
+
+
+async def test_fast_model_change_flips_label_on_statusline(tmp_path):
+    # /fast → the 🤖 model label flips to the fast model's family label (haiku) live.
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path), make_active=True)
+    eng = FakeEngine([], ctx_pct=6)
+    session = make_multi_session({str(tmp_path): eng}, store=store)
+    rt = session._runtime(1, "alpha", str(tmp_path))
+    rt.engine = eng
+    rec = Recorder()
+    pins = PinRecorder()
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
+    )
+    # /fast → set_model to a haiku id → refresh → the label reads "haiku".
+    session.set_model(1, "claude-haiku-4-5")
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
+    )
+    assert "🤖 haiku" in _statusline_edits(rec)[-1]["text"], "/fast → the model label flips to haiku"
+
+
+async def test_maybe_update_statusline_missing_closures_is_noop():
+    # The closure-presence gate: if ANY of send/edit/pin/unpin is None (a caller that didn't
+    # wire the statusline), _maybe_update_statusline is a pure no-op (the turn is unaffected).
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = Recorder()
+    pins = PinRecorder()
+    # pin=None → no-op (no send/edit/pin attempted).
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=None, unpin=pins.unpin, for_project=None
+    )
+    assert rec.sends == [] and rec.edits == [] and pins.pins == []
+
+
+async def test_maybe_update_statusline_background_gate_is_noop(tmp_path):
+    # The foreground gate at the helper level: _maybe_update_statusline with a for_project that
+    # is NOT the chat's foreground is a no-op (this is what the turn-start/end triggers rely on).
+    session, _store, _eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
+    rec = Recorder()
+    pins = PinRecorder()
+    # alpha is NOT foreground (beta is active) → the helper skips.
+    await session._maybe_update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project="alpha"
+    )
+    assert rec.sends == [] and rec.edits == [] and pins.pins == []
+
+
+# ===========================================================================
+# STATUSLINE T-SL-WIRE — Codex NO_SHIP follow-up fixes (B1/B2/B3 + pin-retry).
+# ---------------------------------------------------------------------------
+
+
+async def test_ctx_percentage_is_awaited_end_to_end_via_async_engine():
+    # ⭐ B1 (make-or-break): the statusline body reads the ctx % via an AWAITED async
+    # engine.context_percentage(). FakeEngine.context_percentage is now async; if the session
+    # ever stopped awaiting it, the line would show "ctx —" and this FAILS. Proves the headline
+    # SDK percentage actually reaches the rendered line through the awaited chain.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=37)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder()
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert "🧠 ctx 37%" in rec.sends[0]["text"], "the awaited async ctx % must reach the line (B1)"
+
+
+async def test_statusline_text_is_async_and_awaits_ctx():
+    # B1 at the builder level: _statusline_text is a coroutine that awaits the async ctx source.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=21)
+    _prime_statusline_project(session, engine=eng, status="idle")
+    body = await session._statusline_text(1)
+    assert body is not None and "🧠 ctx 21%" in body
+
+
+def _two_project_statusline_session(tmp_path):
+    """A real-store session with alpha (active) + beta, each engine ready for a statusline read."""
+    from claude_tg.session_store import JsonSessionStore
+
+    store = JsonSessionStore(tmp_path / "state.json")
+    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
+    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
+    (tmp_path / "a").mkdir()
+    (tmp_path / "b").mkdir()
+    eng = FakeEngine([], ctx_pct=5)
+    cfg = make_config(allowed_roots=(str(tmp_path),), allow_any_path=False)
+    session = StreamingSession(
+        cfg, session_store=store,
+        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
+        clock=lambda: 0.0,
+    )
+    session._runtime(1, "alpha", str(tmp_path / "a")).engine = eng
+    session._runtime(1, "beta", str(tmp_path / "b")).engine = eng
+    return session, store
+
+
+async def test_switch_after_snapshot_writes_current_line_not_stale(tmp_path):
+    # ⭐⭐ B2 (the foreground-switch race): _update_statusline snapshots the body, THEN the gated
+    # send awaits — a /switch in that window must NOT write the stale previous-project line. The
+    # fix REBUILDS the body from current state right before the write. We wrap _statusline_text
+    # so the SWITCH lands between the snapshot (1st call) and the rebuild (2nd call) — exactly the
+    # race window — and assert the line that LANDS names the NEW project (beta), not alpha.
+    #
+    # MUTATION PROBE: revert the rebuild-after-wait and the SEND carries alpha (the snapshot) →
+    # this FAILS (it requires beta, the post-switch foreground).
+    session, store = _two_project_statusline_session(tmp_path)
+    real_text = session._statusline_text
+    calls = {"n": 0}
+
+    async def racing_text(chat_id):
+        calls["n"] += 1
+        body = await real_text(chat_id)  # 1st call → alpha (the snapshot); 2nd → beta (rebuild)
+        if calls["n"] == 1:
+            # The snapshot read just returned alpha; a /switch lands BEFORE the rebuild read.
+            store.switch(1, "beta")
+        return body
+
+    session._statusline_text = racing_text
+    rec = Recorder()
+    pins = PinRecorder()
+    await session._update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
+    )
+    assert calls["n"] >= 2, "the body must be REBUILT after the snapshot (B2)"
+    sl_sends = _statusline_sends(rec)
+    assert sl_sends, "the line was sent"
+    assert "📁 beta" in sl_sends[0]["text"], "B2: the line names the POST-switch foreground (beta)"
+    assert "📁 alpha" not in sl_sends[0]["text"], "B2: never the stale pre-switch project (alpha)"
+
+
+async def test_switch_after_snapshot_on_edit_writes_current_line(tmp_path):
+    # B2 on the EDIT path: an established line, then a /switch between the edit's snapshot and its
+    # rebuild → the now-current project (beta) is edited in, never the stale snapshot (alpha).
+    session, store = _two_project_statusline_session(tmp_path)
+    # alpha starts RUNNING so its first pinned line differs from the idle line the 2nd update
+    # builds → the 2nd update reaches the EDIT path (not the identical-text skip).
+    session._runtime(1, "alpha", str(tmp_path / "a")).status = "running"
+    rec = Recorder()
+    pins = PinRecorder()
+    # Establish a pinned line on alpha first (no racing wrapper yet).
+    await session._update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
+    )
+    assert "📁 alpha" in _statusline_sends(rec)[0]["text"]
+    # Now wrap _statusline_text so a /switch lands between the edit's snapshot and its rebuild.
+    real_text = session._statusline_text
+    calls = {"n": 0}
+
+    async def racing_text(chat_id):
+        calls["n"] += 1
+        body = await real_text(chat_id)
+        if calls["n"] == 1:
+            store.switch(1, "beta")  # switch AFTER the snapshot read, BEFORE the rebuild
+        return body
+
+    session._statusline_text = racing_text
+    session._runtime(1, "alpha", str(tmp_path / "a")).status = "idle"  # alpha line now differs
+    await session._update_statusline(
+        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
+    )
+    sl_edits = _statusline_edits(rec)
+    assert sl_edits, "an edit happened"
+    assert "📁 beta" in sl_edits[-1]["text"], "B2 (edit): the now-current project is written"
+    assert "📁 alpha" not in sl_edits[-1]["text"]
+
+
+async def test_plan_turn_shows_plan_mode_while_running_then_gate(tmp_path):
+    # ⭐ B3: during an ACTUAL plan-mode turn the line shows 🔒 plan (not 🔒 gate). plan_next is
+    # consumed by handle_message BEFORE _drive_turn, so the live flag is in_plan_turn (set at
+    # turn start from the consumed plan_turn, cleared at turn end). Turn start → plan; end → gate.
+    #
+    # MUTATION PROBE: if _statusline_text still read only plan_next (consumed → False), the turn
+    # would show 🔒 gate and this FAILS.
+    engine = FakeEngine(
+        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="planned")],
+        ctx_pct=8,
+    )
+    session = make_session(engine)
+    name, rt = session._active_runtime(1, create_default=True)
+    rt.engine = engine
+    rec = Recorder()
+    pins = PinRecorder()
+    state = session._chat(1)
+    # Drive a PLAN turn (plan_turn=True — the value handle_message would pass after consuming
+    # the one-shot plan_next).
+    await asyncio.wait_for(
+        session._drive_turn(
+            state, 1, engine, "go",
+            send=rec.send, edit=rec.edit, delete=rec.delete,
+            pin=pins.pin, unpin=pins.unpin, target=(name, rt), plan_turn=True,
+        ),
+        timeout=2.0,
+    )
+    sl_sends = _statusline_sends(rec)
+    sl_edits = _statusline_edits(rec)
+    # Turn START line → 🔒 plan (the live plan turn).
+    assert sl_sends and "🔒 plan" in sl_sends[0]["text"], "B3: a running plan turn shows 🔒 plan"
+    # Turn END line → back to 🔒 gate (in_plan_turn cleared; plan_next was already consumed).
+    assert sl_edits and "🔒 gate" in sl_edits[-1]["text"], "B3: after the plan turn → 🔒 gate"
+    # The live flag is cleared after the turn (no lingering plan mode).
+    assert rt.in_plan_turn is False
+
+
+async def test_non_plan_turn_does_not_show_plan_mode():
+    # B3 complement: a NORMAL turn (plan_turn=False) never shows 🔒 plan — it shows 🔒 gate.
+    engine = FakeEngine(
+        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
+        ctx_pct=8,
+    )
+    session = make_session(engine)
+    name, rt = session._active_runtime(1, create_default=True)
+    rt.engine = engine
+    rec = Recorder()
+    pins = PinRecorder()
+    state = session._chat(1)
+    await asyncio.wait_for(
+        session._drive_turn(
+            state, 1, engine, "go",
+            send=rec.send, edit=rec.edit, delete=rec.delete,
+            pin=pins.pin, unpin=pins.unpin, target=(name, rt),  # plan_turn defaults False
+        ),
+        timeout=2.0,
+    )
+    for s in _statusline_sends(rec):
+        assert "🔒 plan" not in s["text"]
+    assert "🔒 gate" in _statusline_sends(rec)[0]["text"]
+    assert rt.in_plan_turn is False
+
+
+async def test_pin_fails_then_retried_on_next_update():
+    # ⭐ Pin-retry (non-blocking): the SEND succeeds but the first PIN raises → the line is sent
+    # + tracked but UNPINNED (statusline_pinned False). A later update RETRIES the pin even when
+    # the text is unchanged — so a transient pin failure self-heals instead of sticking unpinned.
+    #
+    # MUTATION PROBE: without the retry, the identical-text skip would short-circuit and the
+    # second update would NOT pin (pins stays length 1) → this FAILS.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder(fail_pin_times=1)  # the FIRST pin raises, later pins succeed
+    # First update: send ok, pin raises → tracked but not pinned.
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert len(rec.sends) == 1
+    assert len(rec.pins) == 1, "the first pin was attempted (and raised)"
+    state = session._chat(1)
+    assert state.statusline_message_id is not None
+    assert state.statusline_pinned is False, "a failed pin leaves the line UNPINNED"
+    # Second update with IDENTICAL state: must RETRY the pin (not skip past the unpinned state).
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert len(rec.sends) == 1, "no re-send (the line is already sent)"
+    assert len(rec.pins) == 2, "the pin was RETRIED on the next update (pin-retry fix)"
+    assert rec.pins[-1]["message_id"] == state.statusline_message_id
+    assert state.statusline_pinned is True, "the retry succeeded → now pinned"
+    # A THIRD identical update is now a true no-op (pinned + identical → skip).
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert len(rec.pins) == 2, "once pinned, an identical update skips (no needless re-pin)"
+
+
+async def test_successful_pin_sets_pinned_flag():
+    # The happy path of the pin-retry bookkeeping: a successful first pin sets statusline_pinned
+    # True so subsequent identical updates correctly skip.
+    session = make_session(FakeEngine([]))
+    eng = FakeEngine([], ctx_pct=6)
+    _prime_statusline_project(session, engine=eng, status="running")
+    rec = StatuslineRecorder()
+    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
+    assert session._chat(1).statusline_pinned is True
+    assert len(rec.pins) == 1

codex
The branch has the expected fix commit on top, plus one untracked QA note I’ll ignore. The diff is large, so I’m narrowing to `context_percentage`, statusline write gates, plan-turn state, and pin bookkeeping before running the targeted tests.
exec
/bin/zsh -lc 'rg -n "context_percentage|get_context_usage|_statusline_text|_update_statusline|_statusline_gated_edit|_statusline_send_and_pin|statusline_pinned|in_plan_turn|plan_turn|plan_next|_maybe_update_statusline" claude_tg tests' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
tests/test_engine.py:1259:# STATUSLINE T-SL-CORE — the ctx-% source (live get_context_usage() + the honest
tests/test_engine.py:1266:    """A fake ClaudeSDKClient exposing only get_context_usage (the ctx-% spike shape).
tests/test_engine.py:1268:    ⭐ T-SL-WIRE (B1): ``get_context_usage`` is **ASYNC** — the installed SDK's real
tests/test_engine.py:1269:    ``ClaudeSDKClient.get_context_usage()`` is a coroutine (``inspect.iscoroutinefunction`` is
tests/test_engine.py:1279:    async def get_context_usage(self):
tests/test_engine.py:1285:    async def get_context_usage(self):
tests/test_engine.py:1286:        raise RuntimeError("get_context_usage unavailable")
tests/test_engine.py:1289:async def test_context_percentage_live_client_returns_rounded_percentage():
tests/test_engine.py:1290:    # ⭐ Primary path (B1): a connected client's AWAITED get_context_usage()['percentage'] →
tests/test_engine.py:1298:    assert await sub.context_percentage() == 6
tests/test_engine.py:1299:    assert client.calls == 1, "the SDK get_context_usage() coroutine must be awaited (B1)"
tests/test_engine.py:1302:async def test_context_percentage_uses_live_call_over_usage_fallback():
tests/test_engine.py:1304:    # AWAITED live % wins. If get_context_usage() were not awaited, the live path would silently
tests/test_engine.py:1310:    assert await sub.context_percentage() == 3, "the awaited live % must win over the fallback"
tests/test_engine.py:1313:async def test_context_percentage_rounds_a_float_percentage():
tests/test_engine.py:1317:    assert await sub.context_percentage() == 7
tests/test_engine.py:1319:    assert await sub.context_percentage() == 6
tests/test_engine.py:1322:async def test_context_percentage_no_client_no_usage_is_none():
tests/test_engine.py:1325:    assert await sub.context_percentage() is None
tests/test_engine.py:1328:async def test_context_percentage_raising_client_falls_back_to_none_without_usage():
tests/test_engine.py:1332:    assert await sub.context_percentage() is None
tests/test_engine.py:1335:async def test_context_percentage_usage_fallback_math():
tests/test_engine.py:1341:    assert await sub.context_percentage() == round(100 * 12998 / 200000)  # == 6
tests/test_engine.py:1344:async def test_context_percentage_raising_client_uses_usage_fallback():
tests/test_engine.py:1350:    assert await sub.context_percentage() == 50
tests/test_engine.py:1384:    # With no live client, context_percentage (awaited — B1) derives from the captured usage.
tests/test_engine.py:1385:    assert await sub.context_percentage() == 6
tests/test_engine.py:1433:async def test_engine_context_percentage_delegates_to_async_substrate():
tests/test_engine.py:1434:    # ⭐ B1: Engine.context_percentage() AWAITS the substrate's async context_percentage()
tests/test_engine.py:1437:        async def context_percentage(self):
tests/test_engine.py:1441:    assert await eng.context_percentage() == 42
tests/test_engine.py:1444:async def test_engine_context_percentage_accepts_sync_substrate_value():
tests/test_engine.py:1445:    # Defensive seam: a substrate whose context_percentage is SYNC (a predating substrate / a
tests/test_engine.py:1448:        def context_percentage(self):
tests/test_engine.py:1452:    assert await eng.context_percentage() == 7
tests/test_engine.py:1455:async def test_engine_context_percentage_none_when_substrate_lacks_method():
tests/test_engine.py:1458:    assert await eng.context_percentage() is None
tests/test_engine.py:1461:async def test_engine_context_percentage_swallows_substrate_error():
tests/test_engine.py:1464:        async def context_percentage(self):
tests/test_engine.py:1468:    assert await eng.context_percentage() is None
claude_tg/stream_session.py:450:    plan_next: bool = False
claude_tg/stream_session.py:453:    # one-shot ``plan_next`` above is CONSUMED (read + cleared) in ``handle_message`` BEFORE
claude_tg/stream_session.py:454:    # ``_drive_turn`` runs, so by the time the plan turn is streaming ``plan_next`` is already
claude_tg/stream_session.py:455:    # False — reading it in :meth:`_statusline_text` would wrongly show ``gate`` DURING the plan
claude_tg/stream_session.py:456:    # turn. So ``_drive_turn`` sets this from the consumed ``plan_turn`` local at turn start and
claude_tg/stream_session.py:459:    in_plan_turn: bool = False
claude_tg/stream_session.py:471:    # flood posture; the SB5-style explicit opt-in). UNLIKE the one-shot ``plan_next`` this is
claude_tg/stream_session.py:732:    statusline_pinned: bool = False
claude_tg/stream_session.py:1548:        Sets the per-project, ONE-SHOT, in-memory ``plan_next`` marker on the active project's
claude_tg/stream_session.py:1565:            rt.plan_next = True
claude_tg/stream_session.py:1662:        plan_turn: bool = False,
claude_tg/stream_session.py:1671:        **P12 T-PLAN-2 (/plan).** ``plan_turn`` is passed in by the caller, which ALREADY
claude_tg/stream_session.py:1672:        consumed (read + cleared) the project's one-shot ``plan_next`` marker BEFORE this call
claude_tg/stream_session.py:1727:        # above — so this method just RECEIVES the verdict as ``plan_turn`` and never touches
claude_tg/stream_session.py:1728:        # ``rt.plan_next`` itself. (Consuming inside here was the bug: the SB2 raise above and
claude_tg/stream_session.py:1735:        plan_mode = plan_turn
claude_tg/stream_session.py:3250:        # ``plan_turn`` that is threaded DOWN to _ensure_engine (which no longer reads/clears
claude_tg/stream_session.py:3259:        # not a new turn) so it doesn't consume either. RB3: ``plan_turn`` is a local; the
claude_tg/stream_session.py:3261:        plan_turn = target_rt.plan_next
claude_tg/stream_session.py:3262:        target_rt.plan_next = False
claude_tg/stream_session.py:3313:                            chat_id, target=target, plan_turn=plan_turn
claude_tg/stream_session.py:3353:                        images=images, proactive=proactive, plan_turn=plan_turn,
claude_tg/stream_session.py:3548:        plan_turn: bool = False,
claude_tg/stream_session.py:3622:        # duration so the line shows 🔒 plan WHILE the plan turn runs. ``plan_turn`` is the value
claude_tg/stream_session.py:3623:        # ``handle_message`` consumed from the one-shot ``plan_next`` (already cleared there), so
claude_tg/stream_session.py:3627:        turn_rt.in_plan_turn = plan_turn
claude_tg/stream_session.py:3632:        await self._maybe_update_statusline(
claude_tg/stream_session.py:3840:            # /plan for the NEXT turn re-shows 🔒 plan via the command refresh's ``plan_next``.)
claude_tg/stream_session.py:3841:            turn_rt.in_plan_turn = False
claude_tg/stream_session.py:3845:            # _statusline_text's engine.context_percentage() reads the fresh figure). FOREGROUND-
claude_tg/stream_session.py:3849:            await self._maybe_update_statusline(
claude_tg/stream_session.py:4159:    async def _maybe_update_statusline(
claude_tg/stream_session.py:4182:        * otherwise delegates to :meth:`_update_statusline` (itself fully best-effort, RB1).
claude_tg/stream_session.py:4196:            await self._update_statusline(
claude_tg/stream_session.py:4201:            # _update_statusline already swallows its own I/O; this guards the gate itself).
claude_tg/stream_session.py:4204:    async def _statusline_text(self, chat_id: int) -> Optional[str]:
claude_tg/stream_session.py:4220:        ⭐ **ASYNC (B1 fix):** the ctx % comes from ``Engine.context_percentage()`` which AWAITS
claude_tg/stream_session.py:4221:        the SDK's coroutine ``get_context_usage()`` — so this method is async and awaits it. The
claude_tg/stream_session.py:4229:          is RUNNING (``in_plan_turn`` — B3) OR a ``/plan`` is armed for the next turn
claude_tg/stream_session.py:4230:          (``plan_next``), else ``gate`` (the fail-closed default).
claude_tg/stream_session.py:4233:        * ``ctx_pct`` — the live engine's :meth:`~claude_tg.engine.engine.Engine.context_percentage`
claude_tg/stream_session.py:4243:        # (``in_plan_turn``, B3 — ``plan_next`` is already consumed by the time the turn streams)
claude_tg/stream_session.py:4244:        # OR a ``/plan`` is armed for the NEXT turn (``plan_next``); else the fail-closed gate.
claude_tg/stream_session.py:4247:        elif bool(getattr(rt, "in_plan_turn", False)) or bool(getattr(rt, "plan_next", False)):
claude_tg/stream_session.py:4256:                ctx_pct = await engine.context_percentage()
claude_tg/stream_session.py:4268:    async def _update_statusline(
claude_tg/stream_session.py:4280:        (:meth:`_statusline_text`) and reconciles it with the chat's pinned line:
claude_tg/stream_session.py:4313:        UNPINNED (``statusline_pinned`` False); a later update RETRIES the pin even if the text is
claude_tg/stream_session.py:4317:            body = await self._statusline_text(chat_id)  # async (B1: awaits the SDK ctx %)
claude_tg/stream_session.py:4325:                and not state.statusline_pinned
claude_tg/stream_session.py:4335:                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
claude_tg/stream_session.py:4338:                # B2: rebuild the body AFTER the gate wait (inside _statusline_gated_edit) so a
claude_tg/stream_session.py:4340:                await self._statusline_gated_edit(
claude_tg/stream_session.py:4352:                state.statusline_pinned = False
claude_tg/stream_session.py:4357:                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
claude_tg/stream_session.py:4364:    async def _statusline_gated_edit(
claude_tg/stream_session.py:4380:        body = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
claude_tg/stream_session.py:4386:    async def _statusline_send_and_pin(
claude_tg/stream_session.py:4402:        ``statusline_pinned=False`` so the next update retries the pin (the line is still sent +
claude_tg/stream_session.py:4404:        :meth:`_update_statusline` inside its best-effort guard, so a raising ``send`` propagates
claude_tg/stream_session.py:4410:        body = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
claude_tg/stream_session.py:4420:        state.statusline_pinned = False  # not pinned until the pin call below succeeds.
claude_tg/stream_session.py:4427:        On success ``statusline_pinned`` is set True; on failure it stays/!becomes False and is
claude_tg/stream_session.py:4428:        swallowed — :meth:`_update_statusline` then RETRIES the pin on the next update (even with
claude_tg/stream_session.py:4434:            state.statusline_pinned = True
claude_tg/stream_session.py:4436:            state.statusline_pinned = False
tests/test_bot_streaming.py:160:    async def _maybe_update_statusline(self, chat_id, *, send, edit, pin, unpin, for_project=None):
tests/test_bot_streaming.py:4877:async def _drive_plan_turn(session, chat_id, text):
tests/test_bot_streaming.py:4898:    await _drive_plan_turn(session, 1, "build me X")  # the armed plan turn
tests/test_bot_streaming.py:4901:    await _drive_plan_turn(session, 1, "now do Y")  # the NEXT turn — no re-arm
tests/test_bot_streaming.py:4914:    await _drive_plan_turn(session, 1, "hello")
tests/test_bot_streaming.py:4915:    await _drive_plan_turn(session, 1, "again")
tests/test_bot_streaming.py:4929:    # No plan_next / permission_mode / plan key leaked into the persisted record.
tests/test_bot_streaming.py:4933:    await _drive_plan_turn(session, 1, "plan it")
tests/test_bot_streaming.py:4942:    assert rt is not None and rt.plan_next is False
tests/test_bot_streaming.py:4945:async def test_plan_turn_rebuild_resumes_persisted_session_for_continuity(tmp_path):
tests/test_bot_streaming.py:4973:    await _drive_plan_turn(session, 1, "first")  # normal turn → persists "sid-keep"
tests/test_bot_streaming.py:4977:    await _drive_plan_turn(session, 1, "plan it")  # plan turn → rebuild, MUST resume "sid-keep"
tests/test_bot_streaming.py:4989:    await _drive_plan_turn(session, 1, "first ever message")
tests/test_bot_streaming.py:4995:# The bug: rt.plan_next was read+cleared LATE (inside _ensure_engine, after its SB2 check),
tests/test_bot_streaming.py:5023:    await _drive_plan_turn(session, 1, "build me X")  # armed, but aborts before the engine
tests/test_bot_streaming.py:5026:    assert rt.plan_next is False  # ⭐ the marker WAS consumed despite the abort
tests/test_bot_streaming.py:5031:    await _drive_plan_turn(session, 1, "an unrelated message")
tests/test_bot_streaming.py:5084:    assert rt.plan_next is False  # ⭐ the marker WAS consumed despite the SB2 raise
tests/test_bot_streaming.py:5107:    assert rt.plan_next is True  # ⭐ still armed — the command did NOT consume it
tests/test_bot_streaming.py:5110:    await _drive_plan_turn(session, 1, "build me X")
tests/test_bot_streaming.py:5112:    assert rt.plan_next is False  # consumed by the prompt turn
claude_tg/engine/adapter_sdk.py:140:    The SDK's ``usage`` / ``get_context_usage()`` shapes are TypedDicts at the type level but
claude_tg/engine/adapter_sdk.py:478:        # ``get_context_usage()`` is the primary source (``context_percentage()`` below); when
claude_tg/engine/adapter_sdk.py:785:        from the live ``get_context_usage()`` can still produce a % (:meth:`context_percentage`):
claude_tg/engine/adapter_sdk.py:814:    async def context_percentage(self) -> Optional[int]:
claude_tg/engine/adapter_sdk.py:817:        **Primary path:** call the LIVE client's ``get_context_usage()`` and return
claude_tg/engine/adapter_sdk.py:825:        ⭐ **ASYNC — the installed SDK's ``ClaudeSDKClient.get_context_usage()`` is a COROUTINE**
claude_tg/engine/adapter_sdk.py:837:                getter = getattr(client, "get_context_usage", None)
claude_tg/engine/adapter_sdk.py:847:                log.debug("get_context_usage() failed; using usage fallback", exc_info=True)
claude_tg/bot.py:2109:        ``chat_id``) and delegates to the session's :meth:`_update_statusline` (fully best-effort,
claude_tg/bot.py:2117:            await self.streaming._maybe_update_statusline(
claude_tg/engine/engine.py:325:    async def context_percentage(self) -> Optional[int]:
claude_tg/engine/engine.py:328:        Delegates to the substrate's ``context_percentage`` (live ``get_context_usage()`` →
claude_tg/engine/engine.py:336:        ⭐ **ASYNC (B1 fix):** the substrate awaits the SDK's coroutine ``get_context_usage()``,
claude_tg/engine/engine.py:341:        getter = getattr(self._substrate, "context_percentage", None)
claude_tg/engine/engine.py:349:            log.debug("context_percentage() failed (ignored)", exc_info=True)
tests/test_stream_session.py:96:        # STATUSLINE T-SL-CORE: the ctx % the statusline reads via engine.context_percentage().
tests/test_stream_session.py:146:    async def context_percentage(self):
tests/test_stream_session.py:148:        # (None → "ctx —"). ASYNC to mirror the real Engine.context_percentage(), which awaits
tests/test_stream_session.py:149:        # the SDK's coroutine get_context_usage() — so the live awaited path is exercised (a
tests/test_stream_session.py:7301:# _update_statusline builds the foreground statusline body from CURRENT state and reconciles
tests/test_stream_session.py:7308:# call _update_statusline from the live turn path; this unit is machinery-only).
tests/test_stream_session.py:7313:    """Captures the send/edit/pin/unpin calls _update_statusline performs (with fault injection).
tests/test_stream_session.py:7360:    _update_statusline reads the FOREGROUND project's live state. With no store this auto-creates
tests/test_stream_session.py:7371:async def test_update_statusline_first_use_sends_then_pins_silently():
tests/test_stream_session.py:7377:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7399:async def test_update_statusline_second_changed_edits_in_place_no_repin():
tests/test_stream_session.py:7406:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7411:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7425:async def test_update_statusline_identical_state_is_no_io():
tests/test_stream_session.py:7432:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7434:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7441:async def test_update_statusline_edit_failure_resends_and_repins_orphan_recovery():
tests/test_stream_session.py:7449:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7453:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7465:async def test_update_statusline_pin_failure_is_swallowed_turn_unaffected():
tests/test_stream_session.py:7466:    # ⭐ RB1: a PIN that raises must NEVER escape — _update_statusline returns normally, the
tests/test_stream_session.py:7473:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7480:async def test_update_statusline_send_failure_is_swallowed_turn_unaffected():
tests/test_stream_session.py:7481:    # ⭐ RB1: a SEND that raises must NEVER escape — _update_statusline returns normally and no
tests/test_stream_session.py:7488:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7495:async def test_update_statusline_only_one_id_ever_held_across_many_updates():
tests/test_stream_session.py:7504:        await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7514:async def test_update_statusline_no_foreground_project_is_noop(tmp_path):
tests/test_stream_session.py:7523:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7529:async def test_update_statusline_yolo_mode_shows_in_line():
tests/test_stream_session.py:7536:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7540:async def test_update_statusline_plan_armed_shows_in_line():
tests/test_stream_session.py:7541:    # An armed /plan (plan_next) → "🔒 plan" (when not yolo).
tests/test_stream_session.py:7545:    rt.plan_next = True
tests/test_stream_session.py:7547:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7551:async def test_update_statusline_ctx_none_when_no_engine_shows_em_dash():
tests/test_stream_session.py:7556:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7561:async def test_update_statusline_engine_ctx_raises_is_swallowed_shows_dash():
tests/test_stream_session.py:7562:    # ⭐ RB1: a context_percentage() that raises is swallowed (the line still renders, ctx —).
tests/test_stream_session.py:7564:        async def context_percentage(self):
tests/test_stream_session.py:7571:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7576:async def test_update_statusline_edit_and_resend_both_raise_still_swallowed():
tests/test_stream_session.py:7578:    # ALSO raises (the chat is fully wedged at the Telegram layer). _update_statusline must STILL
tests/test_stream_session.py:7587:    await session._update_statusline(1, send=good.send, edit=good.edit, pin=good.pin, unpin=good.unpin)
tests/test_stream_session.py:7602:    await session._update_statusline(1, send=boom_send, edit=boom_edit, pin=good.pin, unpin=boom_unpin)
tests/test_stream_session.py:7613:#   * /switch (the session-level _maybe_update_statusline, for_project=None) rewrites the line
tests/test_stream_session.py:7653:    # turn-lifecycle wiring (T7): _drive_turn calls _update_statusline at start + end.
tests/test_stream_session.py:7662:    # Prime the active project's runtime with the SAME engine so _statusline_text reads ctx 12%.
tests/test_stream_session.py:7696:    # unaffected. (_maybe_update_statusline no-ops when any closure is missing.)
tests/test_stream_session.py:7779:    # /switch's session-level refresh (_maybe_update_statusline with for_project=None — the
tests/test_stream_session.py:7794:    await session._maybe_update_statusline(
tests/test_stream_session.py:7800:    await session._maybe_update_statusline(
tests/test_stream_session.py:7811:    # _maybe_update_statusline for_project=None).
tests/test_stream_session.py:7818:    await session._maybe_update_statusline(
tests/test_stream_session.py:7824:    await session._maybe_update_statusline(
tests/test_stream_session.py:7844:    await session._maybe_update_statusline(
tests/test_stream_session.py:7850:    await session._maybe_update_statusline(
tests/test_stream_session.py:7868:    await session._maybe_update_statusline(
tests/test_stream_session.py:7873:    await session._maybe_update_statusline(
tests/test_stream_session.py:7879:async def test_maybe_update_statusline_missing_closures_is_noop():
tests/test_stream_session.py:7881:    # wire the statusline), _maybe_update_statusline is a pure no-op (the turn is unaffected).
tests/test_stream_session.py:7888:    await session._maybe_update_statusline(
tests/test_stream_session.py:7894:async def test_maybe_update_statusline_background_gate_is_noop(tmp_path):
tests/test_stream_session.py:7895:    # The foreground gate at the helper level: _maybe_update_statusline with a for_project that
tests/test_stream_session.py:7901:    await session._maybe_update_statusline(
tests/test_stream_session.py:7914:    # engine.context_percentage(). FakeEngine.context_percentage is now async; if the session
tests/test_stream_session.py:7921:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7925:async def test_statusline_text_is_async_and_awaits_ctx():
tests/test_stream_session.py:7926:    # B1 at the builder level: _statusline_text is a coroutine that awaits the async ctx source.
tests/test_stream_session.py:7930:    body = await session._statusline_text(1)
tests/test_stream_session.py:7956:    # ⭐⭐ B2 (the foreground-switch race): _update_statusline snapshots the body, THEN the gated
tests/test_stream_session.py:7958:    # fix REBUILDS the body from current state right before the write. We wrap _statusline_text
tests/test_stream_session.py:7965:    real_text = session._statusline_text
tests/test_stream_session.py:7976:    session._statusline_text = racing_text
tests/test_stream_session.py:7979:    await session._update_statusline(
tests/test_stream_session.py:7999:    await session._update_statusline(
tests/test_stream_session.py:8003:    # Now wrap _statusline_text so a /switch lands between the edit's snapshot and its rebuild.
tests/test_stream_session.py:8004:    real_text = session._statusline_text
tests/test_stream_session.py:8014:    session._statusline_text = racing_text
tests/test_stream_session.py:8016:    await session._update_statusline(
tests/test_stream_session.py:8025:async def test_plan_turn_shows_plan_mode_while_running_then_gate(tmp_path):
tests/test_stream_session.py:8026:    # ⭐ B3: during an ACTUAL plan-mode turn the line shows 🔒 plan (not 🔒 gate). plan_next is
tests/test_stream_session.py:8027:    # consumed by handle_message BEFORE _drive_turn, so the live flag is in_plan_turn (set at
tests/test_stream_session.py:8028:    # turn start from the consumed plan_turn, cleared at turn end). Turn start → plan; end → gate.
tests/test_stream_session.py:8030:    # MUTATION PROBE: if _statusline_text still read only plan_next (consumed → False), the turn
tests/test_stream_session.py:8042:    # Drive a PLAN turn (plan_turn=True — the value handle_message would pass after consuming
tests/test_stream_session.py:8043:    # the one-shot plan_next).
tests/test_stream_session.py:8048:            pin=pins.pin, unpin=pins.unpin, target=(name, rt), plan_turn=True,
tests/test_stream_session.py:8056:    # Turn END line → back to 🔒 gate (in_plan_turn cleared; plan_next was already consumed).
tests/test_stream_session.py:8059:    assert rt.in_plan_turn is False
tests/test_stream_session.py:8062:async def test_non_plan_turn_does_not_show_plan_mode():
tests/test_stream_session.py:8063:    # B3 complement: a NORMAL turn (plan_turn=False) never shows 🔒 plan — it shows 🔒 gate.
tests/test_stream_session.py:8078:            pin=pins.pin, unpin=pins.unpin, target=(name, rt),  # plan_turn defaults False
tests/test_stream_session.py:8085:    assert rt.in_plan_turn is False
tests/test_stream_session.py:8090:    # + tracked but UNPINNED (statusline_pinned False). A later update RETRIES the pin even when
tests/test_stream_session.py:8100:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8105:    assert state.statusline_pinned is False, "a failed pin leaves the line UNPINNED"
tests/test_stream_session.py:8107:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8111:    assert state.statusline_pinned is True, "the retry succeeded → now pinned"
tests/test_stream_session.py:8113:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8118:    # The happy path of the pin-retry bookkeeping: a successful first pin sets statusline_pinned
tests/test_stream_session.py:8124:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8125:    assert session._chat(1).statusline_pinned is True

exec
/bin/zsh -lc 'rg -n "StatuslineRecorder|test_ctx_percentage|test_switch_after_snapshot|test_plan_turn|test_pin_fails|statusline failures|foreground|body-free|default turn|RB1|SB3" tests' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
tests/test_session_store.py:582:    """NaN / inf / negative deltas are ignored (RB1) — the running total never corrupts."""
tests/test_session_store.py:676:#   case-insensitive, unknown→None (RB1), unknown-project-raises, sparse-safe, 0600.
tests/test_session_store.py:728:    # RB1: a hand-edited / sparse record never crashes get_effort — a non-string or
tests/test_session_store.py:875:    """get_fork_pending on an unknown project / chat reads False (RB1 — ordinary continue)."""
tests/test_session_store.py:933:    # Removing a now-absent schedule is a clean False (RB1), never a raise.
tests/test_session_store.py:954:    # Pausing an unknown schedule is a clean False (RB1).
tests/test_session_store.py:1017:    # A hand-edited record with no interval_seconds is unrenderable → skipped by list (RB1).
tests/test_session_mirror.py:1:"""P11 T3 — live-mirror (`/watch <id>`): the tailer, the body-free normalizer (⭐ SB3),
tests/test_session_mirror.py:14:  resets cleanly; a vanished file stops without crashing (RB1-total);
tests/test_session_mirror.py:16:  message; tool_use → the body-free ``safe_input_summary`` line; tool_result → a body-free
tests/test_session_mirror.py:18:* **⭐ SB3** — a ``tool_result`` with a fat body and a ``tool_use``/Write with a huge input
tests/test_session_mirror.py:19:  body render to a body-free summary, and the raw body NEVER appears in any captured send;
tests/test_session_mirror.py:20:* the **SB3 mutation-probe**: passing ``tool_result.content`` through RAW must make the
tests/test_session_mirror.py:56:# 1. The tailer — offset / last-`\n` / RB1-total
tests/test_session_mirror.py:100:    # Rotate/replace with a SHORTER file — the tailer must reset to 0 and re-read (RB1).
tests/test_session_mirror.py:130:# 2. The dict→Event normalizer (reuses the bot's events) + 3. ⭐ SB3 scrub
tests/test_session_mirror.py:216:    """RB1: a non-JSON / truncated / unknown / odd line yields no events, never raises."""
tests/test_session_mirror.py:220:# -- ⭐ SB3: raw bodies NEVER appear in the rendered output ------------------
tests/test_session_mirror.py:224:    """A tool_result carrying a fat body + a secret renders to a body-free count only."""
tests/test_session_mirror.py:234:    assert "result (" in rendered  # the body-free indicator IS shown
tests/test_session_mirror.py:270:    """Mutation-probe: if the normalizer surfaced ``tool_result.content`` RAW, the body-free
tests/test_session_mirror.py:271:    assertion MUST fail — proving the SB3 test has teeth.
tests/test_render.py:164:    # The tool name + the (body-free) summary are shown verbatim in the prompt body.
tests/test_render.py:187:    # SB3: the summary is already lengths-not-bodies; render.py must NOT expand it.
tests/test_render.py:199:    assert raw_body not in action.text  # the raw body is absent (SB3)
tests/test_render.py:214:    # P6/R3 (SB3/H1): a tool_error wraps RAW tool output, so it now renders BODY-FREE — the
tests/test_render.py:221:    assert "boom" not in action.text  # body-free: the raw body is gone
tests/test_render.py:278:# ---- P12 T-THINK-2: thinking renders as a capped, collapsed 🧠 status line (SB3) ----
tests/test_render.py:291:    # A long chain-of-thought must be CAPPED to the recent tail (SB3 — never flood Telegram).
tests/test_render.py:309:    # SB3: a redacted thinking event renders ONLY the fixed opaque line — never any body, even
tests/test_render.py:356:    # SB3: the one-liner uses the event's tool_input_summary (lengths-not-bodies),
tests/test_render.py:579:    # The trust boundary that feeds SB1/RB1 at T5: a tampered/stale permission tap with
tests/test_render.py:724:    # RB1: a pending kind the relay did not expect degrades to a generic, still
tests/test_render.py:725:    # body-free "needs attention" ping rather than raising / leaking the raw kind.
tests/test_render.py:744:    # RB1: a blank/whitespace short_error never leaves an empty tail.
tests/test_render.py:750:    # SB3 mutation-probe: a held PermissionEvent whose summary carried a secret-bearing
tests/test_render.py:765:    # SB3: only the SHORT, body-free label the relay supplies appears — never a raw body.
tests/test_render.py:767:    # but the relay supplies the body-free ErrorKind; we assert a secret-bearing body
tests/test_render.py:771:    # The relay passes the body-free kind, NOT the raw body:
tests/test_render.py:799:    # event body (SB3): there is nothing here from which a question/plan/tool body could leak.
tests/test_render.py:840:# "🔐 Permission needed …" prompt body. Both now carry the SB3-safe summary inside
tests/test_render.py:925:    # The "🔐 Permission needed …" body shows the (body-free) summary inside <code> and is
tests/test_render.py:972:    # SB3 regression: wrapping in <code> is a RENDERING change only — it must NOT expand the
tests/test_render.py:982:    # And the plain fallback is likewise body-free.
tests/test_render.py:1007:    # RB1: an unexpected enum / a stray string / None (a project with no runtime) reads
tests/test_render.py:1090:    # P6/R3: a turn_error renders body-free now (raw "bad" is gone) — the ordering/flush
tests/test_render.py:1300:    # prove no HTML escaping. A tool_error would render body-free (covered above) — the
tests/test_render.py:1568:    # SB3 / the "no dollars anywhere routine" invariant: sweep the common result renders and
tests/test_render.py:1753:    # SB3: even with the queued counter, the ping carries ONLY the name + a fixed phrase + a
tests/test_render.py:1848:    # SB3: a long, HTML-bearing first-prompt is clipped AND escaped (no raw markup, no flood).
tests/test_render.py:1886:    # RB1: a non-numeric value → "unknown"; a future timestamp (skew) → "just now", not negative.
tests/test_render.py:2011:# P14 T-SCHED — /schedules listing (body-free, HTML-escaped, injected clock)
tests/test_render.py:2053:    # ⭐ SB3 / Codex-QA BLOCKER: the listing NEVER renders the prompt text (a truncated prompt
tests/test_render.py:2067:    # RB1: a non-Schedule object missing attributes is skipped, never crashes the listing.
tests/test_render.py:2085:# are exercised directly. SB3 is the binding constraint: every field is escaped once, and a
tests/test_render.py:2162:# --- SB3 (the binding constraint): escape-once + no path-as-fake-link --------
tests/test_render.py:2166:    # SB3: a name carrying < / > / & is HTML-escaped exactly once so it can't break the HTML
tests/test_render.py:2177:    # SB3 / P8: a path-shaped value (one containing "/") is wrapped in <code>…</code> so
tests/test_render.py:2222:    # RB1: an id matching no known family is shown VERBATIM (never mislabelled / crashed).
tests/test_render.py:2234:    # SB3 structural: the line is bot-derived STATE — no dollar amount, no body. The fields are
tests/test_security_reliability.py:5:requirement it covers** so the RB7 rule ("each of SB1/SB2/SB3/SB4/RB1/RB2/RB5 has a
tests/test_security_reliability.py:25:* SB3 — :func:`test_sb3_bot_token_never_appears_in_logs`
tests/test_security_reliability.py:29:* RB1 — :func:`test_rb1_garbage_callback_data_does_not_raise`
tests/test_security_reliability.py:254:    # exercise the turn loop / callback / error plumbing (SB4/RB1/RB2…), not path
tests/test_security_reliability.py:445:# SB3 — secret hygiene: the bot token is never written to logs.
tests/test_security_reliability.py:450:    """SB3: a representative flow logs nothing containing the bot token.
tests/test_security_reliability.py:455:    crown-jewel secret (SB3); it must stay out of logs entirely.
tests/test_security_reliability.py:473:    """SB3: the persisted session-state file is mode 0600 (holds ids/cwds, owner-only)."""
tests/test_security_reliability.py:480:# --- SB3/H1 (P6/R3): body-free rendering of RAW EXTERNAL error bodies -------
tests/test_security_reliability.py:483:# chat, this string would appear in a send — the body-free assertions below pin that it
tests/test_security_reliability.py:485:SB3_SECRET_BODY = "AKIA-SECRETKEY-do-not-send-to-chat-1234567890"
tests/test_security_reliability.py:489:    """SB3/H1: a streaming ``tool_error`` carrying a secret-like body renders BODY-FREE.
tests/test_security_reliability.py:492:    KIND + the fixed body-free line) is present instead. The raw detail still reaches the
tests/test_security_reliability.py:494:    (tool_error = raw external → body-free).
tests/test_security_reliability.py:499:            ErrorEvent(kind_of_error="tool_error", message=SB3_SECRET_BODY, is_error=True),
tests/test_security_reliability.py:509:    assert all(SB3_SECRET_BODY not in s["text"] for s in rec.sends)
tests/test_security_reliability.py:514:    assert SB3_SECRET_BODY in caplog.text
tests/test_security_reliability.py:518:    """SB3/H1 (classification, no over-redaction): a BOT-AUTHORED ``driver_error`` (a
tests/test_security_reliability.py:537:    """SB3/H1: the ONE-SHOT reply path renders a ``raw_external`` error BODY-FREE too.
tests/test_security_reliability.py:540:    the fixed body-free line, not the raw body; the raw detail goes to the local debug log.
tests/test_security_reliability.py:544:    runner = FakeRunner(ClaudeResult(ok=False, text="", error=SB3_SECRET_BODY, raw_external=True))
tests/test_security_reliability.py:550:    assert SB3_SECRET_BODY not in sent  # body-free reply
tests/test_security_reliability.py:552:    assert SB3_SECRET_BODY in caplog.text  # raw detail still in the local log
tests/test_security_reliability.py:556:    """SB3/H1 (classification): a bot-AUTHORED one-shot error (raw_external False — e.g. a
tests/test_security_reliability.py:570:    """SB3/H1 (the flag is really SET, not just wired): a real ``ClaudeRunner`` turn whose
tests/test_security_reliability.py:572:    it body-free). Proves the classification fires on the actual error-construction site,
tests/test_security_reliability.py:579:        return (1, "", SB3_SECRET_BODY)  # non-zero exit, secret on stderr, no parseable JSON
tests/test_security_reliability.py:585:    assert SB3_SECRET_BODY in (result.error or "")  # .error still carries it (for heuristics)
tests/test_security_reliability.py:597:    action = render_event(ErrorEvent(kind_of_error="tool_error", message=SB3_SECRET_BODY))
tests/test_security_reliability.py:598:    assert SB3_SECRET_BODY not in action.text  # the render layer strips the raw body
tests/test_security_reliability.py:652:# RB1 — never crash on bad input.
tests/test_security_reliability.py:657:    """RB1: garbage callback_data -> resolve_callback returns handled=False, no raise.
tests/test_security_reliability.py:672:    """RB1: a pathological /cd arg does not crash the handler (it replies, cleanly)."""
tests/test_answer_hold.py:8:* :class:`PendingRegistry` directly — routing, backstop, cancel, RB1 (unknown id).
tests/test_answer_hold.py:70:    # A resolve for an id that isn't pending is a no-op (RB1) and does NOT resolve ours.
tests/test_answer_hold.py:153:    # No pending at all: resolve / cancel are clean no-ops, never raise (RB1).
tests/test_answer_hold.py:164:    # The id is gone after the first resolve; a second is a no-op (RB1), no crash.
tests/test_answer_hold.py:529:# ---- risky tool HOLDS, emits a body-free PermissionEvent, allow-once allows -
tests/test_answer_hold.py:558:    # SB3: the emitted PermissionEvent summary carries a LENGTH for a body field, never
tests/test_audit.py:8:* SB3 (structural): ``AuditEvent`` has NO body-bearing field — a leak is impossible by
tests/test_audit.py:11:* RB1-total: an unwritable / raising sink never breaks a turn; a write failure is swallowed.
tests/test_audit.py:13:  allow_session / deny / backstop_deny / cancel + plan approve/reject) with body-free fields.
tests/test_audit.py:15:* ``/audit`` is SB1-gated, body-free, HTML-escaped, bounded, and per-chat filtered.
tests/test_audit.py:19:SB3 test still proves the value never reaches the log.
tests/test_audit.py:44:# body / secret (SB3). It is only ever passed as tool-input CONTENT, never written to a file.
tests/test_audit.py:49:# SB3 (structural) — AuditEvent has NO body-bearing field; a leak is impossible.
tests/test_audit.py:54:    """SB3 (structural): ``AuditEvent`` carries ONLY non-body fields.
tests/test_audit.py:59:    the only free-ish field and its contract is "an already-body-free string"
tests/test_audit.py:101:    """SB3: a record built from a ``Write`` with secret ``content`` carries NO content.
tests/test_audit.py:118:    """SB3 (BLOCKER 1): the AUDIT summary collapses IDENT fields too — a secret early in a
tests/test_audit.py:192:    """A pre-existing file with loose perms is tightened to ``0600`` on the next append (SB3)."""
tests/test_audit.py:226:    """A malformed line is skipped (RB1) — the rest of the tail still parses; never raises."""
tests/test_audit.py:236:    """A never-written log reads as ``[]`` (RB1 — no error)."""
tests/test_audit.py:241:# RB1-total — a write failure NEVER raises out of append (turn safety).
tests/test_audit.py:246:    """An unwritable path → ``append`` swallows the error and does NOT raise (RB1)."""
tests/test_audit.py:257:    """``FileAuditSink.record`` swallows even a raising ``append`` (defense-in-depth, RB1)."""
tests/test_audit.py:269:# ChatBoundSink — stamps chat_id + re-redacts a raw session id (SB3/H1).
tests/test_audit.py:289:    """SB3/H1: a raw UUID-shaped session id in ``session_tag`` is re-redacted before the write."""
tests/test_audit.py:299:# The engine hook (on_tool_request) — every outcome recorded, body-free.
tests/test_audit.py:316:    """A sink that RAISES — to prove an audit failure never breaks a turn (RB1 mutation)."""
tests/test_audit.py:417:    """SB3 (BLOCKER 1) end-to-end: a secret in a Bash command auto-allowed under /yolo is
tests/test_audit.py:418:    recorded body-free — neither the recorded event NOR the on-disk JSONL contains the secret.
tests/test_audit.py:421:    proves the durable file is strongly body-free. Mutation-probe: revert ``_record_tool`` to
tests/test_audit.py:439:    assert "command=<" in on_disk  # but it DID record a body-free shape (useful for review)
tests/test_audit.py:440:    # The recorded event is a body-free auto_allow tool_decision for Bash.
tests/test_audit.py:546:    """An ExitPlanMode approve/reject records a body-free ``plan_decision`` (NO plan text)."""
tests/test_audit.py:595:# RB1 mutation — a RAISING sink does not break the turn; the no-op default is identical.
tests/test_audit.py:600:    """RB1 mutation: a sink that RAISES on record must not break the turn — the turn's events
tests/test_multi_project.py:392:    # though GAMMA is now the active/foreground project. The held turn must unblock and
tests/test_sessions_discovery.py:7:* the SDK adapter maps ``SDKSessionInfo`` → ``_RawSession`` and survives a missing/odd SDK (RB1);
tests/test_sessions_discovery.py:74:# 1. The SDK adapter (the only SDK touch point) — mapping + RB1
tests/test_sessions_discovery.py:109:    assert sdk_list_sessions() == []  # RB1: a raising SDK → empty, not a crash
tests/test_sessions_discovery.py:132:    assert sdk_list_sessions() == []  # RB1 / SB pin: a missing/renamed SDK degrades cleanly
tests/test_sessions_discovery.py:145:    # Defensive (RB1): missing / non-numeric / boolean → None (never crash, never mis-sort).
tests/test_sessions_discovery.py:232:    assert scan_claude_processes(runner=boom) == []  # RB1
tests/test_sessions_discovery.py:325:# 6. discover_sessions glue + RB1 across seams
tests/test_sessions_discovery.py:350:    assert disc.discover() == []  # RB1: a broken lister → empty, never raises
tests/test_multimodal.py:87:    # SB3: ImageInput.repr must NEVER dump the base64 (an accidental log(image) is a leak).
tests/test_claude_runner.py:106:    # No override + no configured model → --model is omitted (CLI default). RB1: a store
tests/test_util.py:61:# _redact_sid (P6/R3 / H1 / SB3): session ids must never appear raw in logs.
tests/test_scheduler.py:83:    # RB1: a non-positive / odd value floors at "0s" rather than raising (a listing line
tests/test_scheduler_driver.py:9:* ⭐ RB1-TOTAL: a fire that RAISES is caught — the loop survives + continues to the next
tests/test_scheduler_driver.py:14:* a store-read error degrades to "nothing due" (RB1) — never raises out of the loop.
tests/test_scheduler_driver.py:140:    """⭐ RB1: a fire that RAISES is caught — the OTHER due schedules still fire, and the
tests/test_scheduler_driver.py:156:    # The tick itself must NOT raise despite the fire raising (RB1).
tests/test_scheduler_driver.py:255:    fires it on every iteration) — lets the RB1-survival test count repeated fires
tests/test_scheduler_driver.py:263:    """⭐ RB1 end-to-end: with the REAL loop body, a fire that raises EVERY time does NOT kill
tests/test_concurrency_matrix.py:35:3.   Foreground→background mid-stream flip (inline→ping) — ``test_foreground_to_background_*``
tests/test_concurrency_matrix.py:39:7.   RB1/RB2 isolation (one turn raising) — ``test_rb1_*``
tests/test_concurrency_matrix.py:93:    so a turn that errors while another runs concurrently can be driven (RB1/RB2 isolation).
tests/test_concurrency_matrix.py:150:        Used to advance an engine past a *foreground* HOLD (so the test can flip the
tests/test_concurrency_matrix.py:151:        foreground / start a concurrent turn before the next event renders) without
tests/test_concurrency_matrix.py:246:#    Project A parked at a hold while B is foreground + running; a button tap carrying A's
tests/test_concurrency_matrix.py:255:    """Set up: A holds a permission, B holds a permission, B is foreground. Returns the
tests/test_concurrency_matrix.py:272:    # lock) — two engines live at once. Switch foreground to beta so A is BACKGROUND.
tests/test_concurrency_matrix.py:278:    assert store.get_active(1) == "beta"  # beta is now the foreground
tests/test_concurrency_matrix.py:284:async def test_cross_project_tap_for_A_resolves_A_never_B_while_B_foreground(tmp_path):
tests/test_concurrency_matrix.py:285:    """⭐ THE headline. A held permission in A (background) + B foreground & running: a REAL
tests/test_concurrency_matrix.py:315:    so the property is symmetric (id routes to the owner, regardless of foreground)."""
tests/test_concurrency_matrix.py:332:    benign no-op — it resolves nothing in A or B (RB1 / SB6: a stale button never re-fires)."""
tests/test_concurrency_matrix.py:432:#    its terminal as a ✅ ping. Pins the per-event re-read of foreground in _drive_turn.
tests/test_concurrency_matrix.py:436:async def test_foreground_to_background_flip_attention_becomes_bell_ping(tmp_path):
tests/test_concurrency_matrix.py:437:    """alpha starts foreground (its status line renders inline), then /switch beta makes it
tests/test_concurrency_matrix.py:441:    Non-vacuous: if _drive_turn read foreground ONCE at turn start (instead of per event),
tests/test_concurrency_matrix.py:446:    # alpha: a bit of foreground status, HOLD #1 (we /switch during it), then a permission
tests/test_concurrency_matrix.py:450:            TextEvent(text="thinking", incremental=True),  # inline status while foreground
tests/test_concurrency_matrix.py:465:    # Let the inline status render while foreground (park #1). While foreground, NO 🔔 ping
tests/test_concurrency_matrix.py:469:        "while foreground, status renders inline — no bell ping yet"
tests/test_concurrency_matrix.py:472:    # Switch foreground to beta MID-STREAM, then release park #1 so alpha continues backgrounded.
tests/test_concurrency_matrix.py:488:    # The terminal arrived as a ✅ background ping (alpha is not foreground), not inline.
tests/test_concurrency_matrix.py:507:    whole ask with both answers — all while the project is NOT foreground.
tests/test_concurrency_matrix.py:521:    # A foreground HOLD #1 first (so we can /switch alpha to BACKGROUND while it is parked),
tests/test_concurrency_matrix.py:530:    # Start alpha's turn; it parks at HOLD #1 while still foreground.
tests/test_concurrency_matrix.py:533:    # Switch foreground to beta, then release HOLD #1 so the ask is emitted while alpha is BG.
tests/test_concurrency_matrix.py:684:# 7. RB1/RB2 isolation: one project's turn raising/erroring does NOT break a concurrent
tests/test_concurrency_matrix.py:742:    """A background project's ErrorEvent renders a body-free ``⚠️`` ping (not a crash, no raw
tests/test_concurrency_matrix.py:743:    body) while a concurrent foreground project keeps working — RB2 + SB3 under concurrency.
tests/test_concurrency_matrix.py:746:    the body-free ``kind_of_error``, the stand-in secret below WOULD appear in a send → the
tests/test_concurrency_matrix.py:747:    SB3 assert fails. And the ⚠️ ping presence pins that a background error still surfaces.
tests/test_concurrency_matrix.py:751:    # A foreground HOLD first (so we switch alpha to BACKGROUND while parked), THEN the error +
tests/test_concurrency_matrix.py:765:    # foreground to beta, release alpha's HOLD so its error renders backgrounded.
tests/test_concurrency_matrix.py:774:    # alpha's background error surfaced as a body-free ⚠️ ping — and the raw message (a
tests/test_concurrency_matrix.py:775:    # stand-in secret) NEVER appears in any send (SB3 body-free).
tests/test_concurrency_matrix.py:779:    # beta (the concurrent foreground run) is unaffected — still parked + usable; release it.
tests/test_concurrency_matrix.py:889:    # the inline result text (p3 — it ended up the foreground project). The ORDER of p2's then
tests/test_concurrency_matrix.py:1201:    # alpha: a status burst, a foreground HOLD (we start beta concurrently during it), then its
tests/test_concurrency_matrix.py:1202:    # verbatim result. beta: its own status burst + verbatim result. Each project is foreground
tests/test_concurrency_matrix.py:1231:    # Start alpha (foreground; renders inline status, then parks at HOLD). Switch active to beta
tests/test_bash_policy.py:14:* the render half — a flagged :class:`PermissionEvent` shows ⚠️ + the body-free label and
tests/test_bash_policy.py:144:    """A match carries only a body-free label + severity — never the raw command (SB3)."""
tests/test_bash_policy.py:301:    # The audit shows the policy flag + the resolved allow_once (both body-free). The policy
tests/test_bash_policy.py:302:    # event's summary is the action token + the body-free pattern label ("bash_policy_flag
tests/test_bash_policy.py:307:    assert any("force-delete" in (e.summary or "") for e in policy_events)  # body-free pattern label
tests/test_bash_policy.py:352:    """The held PermissionEvent for a flagged command carries bash_flag=True + the body-free
tests/test_bash_policy.py:374:    # The label is body-free — the raw command never rides the label.
tests/test_bash_policy.py:544:# --- audit body-free guard --------------------------------------------------------------
tests/test_bash_policy.py:548:    """SB3 (BLOCKER 1): a secret EARLY in a flagged Bash command appears in NO audit record.
tests/test_bash_policy.py:551:    only the action token + the body-free pattern LABEL (no command); the tool_decision's
tests/test_bash_policy.py:577:    # The tool_decision DOES record a body-free shape (argv[0] + length), proving it's useful.
tests/test_bash_policy.py:643:    """SB3: the flagged render shows the (already body-free) summary + the label — never a body."""
tests/test_bash_policy.py:656:    # The summary (body-free already) is shown; the label adds no body.
tests/test_engine.py:181:    """SB3/H1: the engine's start/resume DEBUG logs carry a REDACTED tag, never the raw id.
tests/test_engine.py:312:    # incremental ThinkingEvent (parallel to text_delta). SB3: it carries ONLY the text.
tests/test_engine.py:327:    # SB3: there is structurally no signature field on a ThinkingEvent.
tests/test_engine.py:343:    # SB3 — the signature must NOT appear anywhere on the emitted event (no field, not in repr).
tests/test_engine.py:349:    # SB3: the opaque signature arrives as a SEPARATE signature_delta — it must NEVER become an
tests/test_engine.py:364:    # Defensive/forward-compat (RB1/SB3): a redacted_thinking stream delta -> a ThinkingEvent
tests/test_engine.py:384:    # RB1: a thinking_delta with no/empty "thinking" -> None (nothing to show), never a crash.
tests/test_engine.py:436:    # SB3: the raw body is NOT in the summary; only a length is.
tests/test_engine.py:1397:    # A ResultMessage with no usage/window leaves a previously-captured good figure intact (RB1).
tests/test_engine.py:1462:    # A raising substrate method → None (RB1; an observer off the critical path never raises).
tests/test_stream_session.py:639:    # it). A stale/forged id is a benign no-op (RB1).
tests/test_stream_session.py:724:# SB1 / RB1: malformed / stale / no-session callbacks NEVER resolve.
tests/test_stream_session.py:744:    # (P5 / ADR-005 D3: an absent id resolves nothing — handled=False, RB1).
tests/test_stream_session.py:807:    # P6/R3 (SB3/H1): a tool_error wraps RAW tool output → it renders BODY-FREE to the chat
tests/test_stream_session.py:810:    # body appeared in the chat — the new body-free behavior is the SB3 fix.
tests/test_stream_session.py:1059:    # RB1: a failing delete (message gone / too old) must never kill the turn — the real
tests/test_stream_session.py:1299:    # #3 (R5 dedup, preserved under R3 body-free): a failing tool renders a tool_error
tests/test_stream_session.py:1304:    # count the ⚠️ error blocks (the raw msg is absent from the chat — SB3).
tests/test_stream_session.py:1317:    assert all(msg not in t for t in _send_texts(rec))  # body-free: raw body never sent
tests/test_stream_session.py:1325:    # #3 guard (no over-suppression), preserved under R3 body-free: a tool_error then a
tests/test_stream_session.py:1327:    # .messages differ), so BOTH error blocks render. Both render body-free now, so we
tests/test_stream_session.py:1328:    # distinguish them by KIND (the raw bodies are absent from the chat — SB3).
tests/test_stream_session.py:1341:    # Raw bodies never reach the chat (body-free); the two DISTINCT errors both still render,
tests/test_stream_session.py:1584:    # anything (RB1).
tests/test_stream_session.py:1818:    re-validation actually confines (unlike the default turn fixtures which no-op it)."""
tests/test_stream_session.py:1827:    # built/started (no factory call, no hang). The lock is released (RB1/SB6 fail-closed).
tests/test_stream_session.py:2180:# to the owning project (T2) regardless of which is foreground.
tests/test_stream_session.py:2267:    # (the foreground beta still rendered its own result inline above). This is the headline
tests/test_stream_session.py:2317:    assert session.is_busy(1, "missing") is False  # unknown name → never busy (RB1)
tests/test_stream_session.py:3820:# A even while B is the active/foreground project. These wire TWO live engines
tests/test_stream_session.py:3860:    # A held ask belongs to ALPHA, but BETA is the active/foreground project. A tap on
tests/test_stream_session.py:3925:    # though beta is the active/foreground project — not a chat-global slot.
tests/test_stream_session.py:3938:    # Defense-in-depth (RB1/SB6): a forged permission tap (m|…) whose id maps to an ASK
tests/test_stream_session.py:4288:    # turn this process) reads as idle — read-only, creates nothing (RB1).
tests/test_stream_session.py:4312:# (RB7, ADR-005 D8). A BACKGROUND (non-foreground) project's hold/terminal
tests/test_stream_session.py:4320:    """Drive ONE turn for a SPECIFIC project (background or foreground) to completion.
tests/test_stream_session.py:4323:    pinned ``target`` so ``_drive_turn`` acts on THAT project (its engine + foreground
tests/test_stream_session.py:4338:    # A BACKGROUND project (alpha) hits a permission hold while beta is foreground → a
tests/test_stream_session.py:4341:    # foreground (beta) is undisturbed.
tests/test_stream_session.py:4373:async def test_foreground_permission_hold_renders_inline_no_ping(tmp_path):
tests/test_stream_session.py:4391:        raise AssertionError("foreground permission prompt was never rendered inline")
tests/test_stream_session.py:4439:async def test_foreground_permission_body_is_sent_html_code_path_buttons_intact(tmp_path):
tests/test_stream_session.py:4510:    # verbatim ask body for the foreground.
tests/test_stream_session.py:4527:    # The bell ping is body-free; the question keyboard rides its own (safe) message.
tests/test_stream_session.py:4545:    # The result TEXT is NOT sent inline for a background project (SB3-adjacent: only the
tests/test_stream_session.py:4551:    # ⭐ The load-bearing SB3 check (T3-review SB3): a BACKGROUND error pings the body-free
tests/test_stream_session.py:4564:    # The error ping is "⚠️ alpha — tool_error" — the body-free ErrorKind, never the message.
tests/test_stream_session.py:4566:    # The secret-bearing body (and the raw message) appears in NO send (SB3).
tests/test_stream_session.py:4575:    # foreground project keeps its inline status line — covered by the existing turn tests.)
tests/test_stream_session.py:4955:    # no store → a single implicit foreground, so BOTH render inline and their combined
tests/test_stream_session.py:4969:    # No store → a single implicit "default" project (always foreground), but we exercise
tests/test_stream_session.py:5265:    # /to an unknown project name → a clear no-op message (RB1), never a crash / misroute.
tests/test_stream_session.py:5332:    # /cancel <unknown> → no-op (RB1): nothing cancelled, no crash.
tests/test_stream_session.py:5543:    store.switch(1, "beta")  # beta is now the ACTIVE (foreground) project…
tests/test_stream_session.py:6114:    # active one". A BACKGROUND turn runs on alpha while BETA is the active/foreground
tests/test_stream_session.py:6135:    # … and NOT on the active/foreground project (beta) — the mutation probe.
tests/test_stream_session.py:6424:    # a path/URL in the (body-free) ping never balloons into a Telegram preview card.
tests/test_stream_session.py:6534:    # RB1: a switch tap with no registry is a benign no-op (nothing to switch within).
tests/test_stream_session.py:6804:    """RB1: with no store there is no registry to attach into — a clean notice, no deref."""
tests/test_stream_session.py:7070:    """A StreamingSession with a live, body-free AuditLog wired (for proactive_fire/skip)."""
tests/test_stream_session.py:7088:    """fire_schedule sends the body-free ⏰ header, audits a ``proactive_fire`` session_event,
tests/test_stream_session.py:7102:    # A body-free ⏰ <name> (scheduled) header was sent (the name only, never the prompt).
tests/test_stream_session.py:7105:    # The fire was audited body-free: a proactive_fire session_event with the task name.
tests/test_stream_session.py:7110:    assert "run the tests" not in (fires[0].summary or "")  # SB3: no prompt in the record
tests/test_stream_session.py:7115:    a clean skip (StreamingBusy), with a body-free ⏰ skipped notice + a ``proactive_skip``
tests/test_stream_session.py:7142:    """⭐ RB1-total: if the driven turn raises a NON-busy error, fire_schedule CATCHES it,
tests/test_stream_session.py:7301:# _update_statusline builds the foreground statusline body from CURRENT state and reconciles
tests/test_stream_session.py:7304:# (orphan recovery); and a pin/edit/send raising is SWALLOWED (RB1 — never breaks a turn). All
tests/test_stream_session.py:7312:class StatuslineRecorder:
tests/test_stream_session.py:7316:    unpinned/deleted the line). ``fail_pin`` / ``fail_send`` make pin / send raise (the RB1
tests/test_stream_session.py:7376:    rec = StatuslineRecorder()
tests/test_stream_session.py:7404:    rec = StatuslineRecorder()
tests/test_stream_session.py:7431:    rec = StatuslineRecorder()
tests/test_stream_session.py:7447:    rec = StatuslineRecorder(fail_edit=True)  # the first edit will raise
tests/test_stream_session.py:7466:    # ⭐ RB1: a PIN that raises must NEVER escape — _update_statusline returns normally, the
tests/test_stream_session.py:7471:    rec = StatuslineRecorder(fail_pin=True)
tests/test_stream_session.py:7481:    # ⭐ RB1: a SEND that raises must NEVER escape — _update_statusline returns normally and no
tests/test_stream_session.py:7486:    rec = StatuslineRecorder(fail_send=True)
tests/test_stream_session.py:7501:    rec = StatuslineRecorder()
tests/test_stream_session.py:7514:async def test_update_statusline_no_foreground_project_is_noop(tmp_path):
tests/test_stream_session.py:7522:    rec = StatuslineRecorder()
tests/test_stream_session.py:7535:    rec = StatuslineRecorder()
tests/test_stream_session.py:7546:    rec = StatuslineRecorder()
tests/test_stream_session.py:7555:    rec = StatuslineRecorder()
tests/test_stream_session.py:7562:    # ⭐ RB1: a context_percentage() that raises is swallowed (the line still renders, ctx —).
tests/test_stream_session.py:7569:    rec = StatuslineRecorder()
tests/test_stream_session.py:7577:    # ⭐ The make-or-break RB1 mutation probe: the in-place edit raises AND the recovery re-send
tests/test_stream_session.py:7586:    good = StatuslineRecorder()
tests/test_stream_session.py:7615:#   * ⭐ a BACKGROUND turn (a non-active project running) does NOT rewrite the foreground line
tests/test_stream_session.py:7616:#     (the make-or-break foreground-only invariant — mutation probe).
tests/test_stream_session.py:7650:async def test_foreground_turn_pins_at_start_then_refreshes_at_end():
tests/test_stream_session.py:7712:async def test_background_turn_does_not_rewrite_foreground_statusline(tmp_path):
tests/test_stream_session.py:7714:    # BETA is the active/foreground project) must NEVER touch the pinned statusline — the line
tests/test_stream_session.py:7716:    # on _is_foreground(turn_name); a background turn skips them.
tests/test_stream_session.py:7718:    # MUTATION PROBE: if the turn-start/turn-end triggers were NOT foreground-gated (i.e. a
tests/test_stream_session.py:7741:    # branch, the exact scenario the foreground gate must cover) …
tests/test_stream_session.py:7743:    # … but the foreground statusline was NEVER written — no 📁 send/edit, no pin.
tests/test_stream_session.py:7744:    assert _statusline_sends(rec) == [], "a BACKGROUND turn must NOT pin/send the foreground line"
tests/test_stream_session.py:7745:    assert _statusline_edits(rec) == [], "a BACKGROUND turn must NOT edit the foreground line"
tests/test_stream_session.py:7746:    assert pins.pins == [], "a BACKGROUND turn must NOT pin the foreground line"
tests/test_stream_session.py:7751:async def test_foreground_turn_among_two_projects_updates_line(tmp_path):
tests/test_stream_session.py:7752:    # The complement of the background probe: when the RUNNING project IS the foreground (alpha
tests/test_stream_session.py:7753:    # active), its turn DOES pin/refresh the line — so the gate keys on foreground, not on
tests/test_stream_session.py:7774:    assert "📁 alpha" in sl_sends[0]["text"], "the line names the foreground project (alpha)"
tests/test_stream_session.py:7780:    # command path is foreground by definition) REWRITES the pinned line for the NOW-active
tests/test_stream_session.py:7798:    # /switch → beta is now the active/foreground project; refresh rewrites the SAME line.
tests/test_stream_session.py:7895:    # The foreground gate at the helper level: _maybe_update_statusline with a for_project that
tests/test_stream_session.py:7896:    # is NOT the chat's foreground is a no-op (this is what the turn-start/end triggers rely on).
tests/test_stream_session.py:7900:    # alpha is NOT foreground (beta is active) → the helper skips.
tests/test_stream_session.py:7912:async def test_ctx_percentage_is_awaited_end_to_end_via_async_engine():
tests/test_stream_session.py:7920:    rec = StatuslineRecorder()
tests/test_stream_session.py:7955:async def test_switch_after_snapshot_writes_current_line_not_stale(tmp_path):
tests/test_stream_session.py:7956:    # ⭐⭐ B2 (the foreground-switch race): _update_statusline snapshots the body, THEN the gated
tests/test_stream_session.py:7963:    # this FAILS (it requires beta, the post-switch foreground).
tests/test_stream_session.py:7985:    assert "📁 beta" in sl_sends[0]["text"], "B2: the line names the POST-switch foreground (beta)"
tests/test_stream_session.py:7989:async def test_switch_after_snapshot_on_edit_writes_current_line(tmp_path):
tests/test_stream_session.py:8025:async def test_plan_turn_shows_plan_mode_while_running_then_gate(tmp_path):
tests/test_stream_session.py:8088:async def test_pin_fails_then_retried_on_next_update():
tests/test_stream_session.py:8098:    rec = StatuslineRecorder(fail_pin_times=1)  # the FIRST pin raises, later pins succeed
tests/test_stream_session.py:8123:    rec = StatuslineRecorder()
tests/test_voice.py:158:    # Codex B2 (SB3): the raw stderr can carry partial transcripts / paths / secrets — it must
tests/test_voice.py:159:    # NOT be logged either. We log only a body-free summary (exit code), never the stderr body.
tests/test_voice.py:163:    # But a body-free summary IS logged (so an operator sees the transcriber failed).
tests/test_skill_launch.py:15:* RB1: empty / whitespace / ``/``-only / unicode-garbage / missing-message text never
tests/test_skill_launch.py:288:# (d) RB1 — malformed / empty command never raises; session stays usable.
tests/test_skill_launch.py:293:    """RB1: empty message text no-ops (after strip) — no turn, no raise."""
tests/test_skill_launch.py:302:    """RB1: whitespace-only text no-ops (after strip)."""
tests/test_skill_launch.py:311:    """RB1: a bare '/' is non-empty after strip -> forwarded verbatim as an ordinary turn
tests/test_skill_launch.py:321:    """RB1: unicode garbage after the slash is just forwarded as a turn — never raises."""
tests/test_skill_launch.py:330:    """RB1: a missing message (update.message is None) no-ops — never raises."""
tests/test_skill_launch.py:341:    """RB1: update.message.text is None -> treated as empty, no-ops, never raises."""
tests/test_skill_launch.py:351:    """RB1: after a no-op/garbage command, a normal command still launches (session usable)."""
tests/test_bot_streaming.py:463:    # RB1: even if resolve_callback raises, the handler answers and does not crash.
tests/test_bot_streaming.py:672:    # RB1: a bare /thinking (no on|off) shows usage and toggles NOTHING.
tests/test_bot_streaming.py:733:    # RB1: an unrecognized level is a clean error listing the valid levels — the override is
tests/test_bot_streaming.py:1173:    # regardless of which project is foreground).
tests/test_bot_streaming.py:1475:# ---- RB1: streaming + no STATE_FILE (store is None) must not crash ---------
tests/test_bot_streaming.py:1479:    """RB1 (T5 review): ENGINE_MODE=streaming with STATE_FILE unset → store is None.
tests/test_bot_streaming.py:1490:    """RB1 (T5 review): /rm with a None store replies gracefully, never crashes."""
tests/test_bot_streaming.py:1707:    """RB1: a hand-edited/sparse on-disk doc (record missing cwd; active pointing at a
tests/test_bot_streaming.py:1986:    # RB1: ENGINE_MODE=streaming with STATE_FILE unset → store is None. /new must reply
tests/test_bot_streaming.py:2023:    # RB1: only a name, no path → usage (handles the 1-arg case).
tests/test_bot_streaming.py:2038:    # RB1: zero args → usage (handles the 0-arg case).
tests/test_bot_streaming.py:2440:    # /to with a name but no text → usage (RB1), no routing.
tests/test_bot_streaming.py:2637:    # RB1: a Telegram API failure registering the menu must not crash startup.
tests/test_bot_streaming.py:2671:    # RB1: a welcome send failure must not stop the turn from running.
tests/test_bot_streaming.py:2729:    # SB3: /status carries only health values — never tool input/output or file content. We
tests/test_bot_streaming.py:2746:    # wording shows and stays body-free + HTML-escaped.
tests/test_bot_streaming.py:2820:    assert _format_uptime(-5) == "0s"  # RB1: defensive floor
tests/test_bot_streaming.py:3480:    # SB3: neither the raw bytes nor the base64 may appear anywhere in the logs.
tests/test_bot_streaming.py:3729:    # RB1: a download exception → a clean message, no crash, no write, no turn.
tests/test_bot_streaming.py:4160:    # No turn fired; a clean (body-free) error message was sent.
tests/test_bot_streaming.py:4261:# ---- SB3: never log the audio bytes or the raw transcript -------------------
tests/test_bot_streaming.py:4285:# ---- temp files cleaned (RB1) -----------------------------------------------
tests/test_bot_streaming.py:4383:# discover_sessions is patched (no real SDK / ps / ~/.claude); we assert SB1, body-free,
tests/test_bot_streaming.py:4384:# <code>-paths, the bot-project merge/dedup, and the RB1 empty-reply path.
tests/test_bot_streaming.py:4447:    await bot.cmd_sessions(upd, make_cmd_ctx())  # must NOT raise (RB1)
tests/test_bot_streaming.py:4814:    # RB1: a failure during shutdown must not block the bot from stopping.
tests/test_bot_streaming.py:4945:async def test_plan_turn_rebuild_resumes_persisted_session_for_continuity(tmp_path):
tests/test_bot_streaming.py:5116:# P13 T-AUDIT — /audit command (SB1, body-free, per-chat filtered, bounded).
tests/test_bot_streaming.py:5138:    """``/audit`` renders this chat's recent records, HTML-escaped + body-free.
tests/test_bot_streaming.py:5141:    string to prove the render escapes the markup and that nothing beyond the already-body-free
tests/test_bot_streaming.py:5146:    # A body-free summary (as safe_input_summary would produce) carrying angle-brackets.
tests/test_bot_streaming.py:5203:    """With audit disabled (no log), ``/audit`` replies a clean 'not enabled' notice (RB1)."""
tests/test_bot_streaming.py:5224:    """SB3 at the /audit surface: a record built from a secret-laden summary renders body-free.
tests/test_bot_streaming.py:5249:    """``/yolo`` (set_yolo True) writes a body-free ``yolo_on`` policy_event; /unyolo → yolo_off."""
tests/test_bot_streaming.py:5275:    """``reset`` on an active project writes a ``reset`` session_event (body-free)."""
tests/test_bot_streaming.py:5288:# listing is body-free (SB3). This task does NOT fire — a created schedule is
tests/test_bot_streaming.py:5413:    # ⭐ SB3 / Codex-QA BLOCKER: /schedules is BODY-FREE — it shows the name + interval + next
tests/test_bot_streaming.py:5436:    # ⭐ SB3: a prompt containing </>& cannot break the HTML or inject markup BECAUSE it is
tests/test_bot_streaming.py:5437:    # not rendered at all (body-free). Neither the raw nor an escaped form of the prompt
tests/test_bot_streaming.py:5445:    # The prompt is body-free: neither its raw markup nor an escaped copy appears, so it can

codex
The searches show the claimed tests exist and the code paths are localized in `adapter_sdk.py`, `engine.py`, and `stream_session.py`. I’m reading those exact sections now to check for logic gaps the tests might miss, especially await windows and best-effort behavior.
exec
/bin/zsh -lc "nl -ba claude_tg/engine/adapter_sdk.py | sed -n '800,875p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
   800	
   801	        if not isinstance(msg, ResultMessage):
   802	            return
   803	        try:
   804	            usage = getattr(msg, "usage", None)
   805	            tokens = _usage_tokens(usage)
   806	            window = _context_window_of(getattr(msg, "model_usage", None))
   807	            if tokens is not None:
   808	                self._last_usage_tokens = tokens
   809	            if window is not None and window > 0:
   810	                self._last_context_window = window
   811	        except Exception:  # pragma: no cover - defensive; never break the receive loop (RB1)
   812	            log.debug("usage capture failed (ignored)", exc_info=True)
   813	
   814	    async def context_percentage(self) -> Optional[int]:
   815	        """Best-effort % of the context window currently used — the honest ctx figure (§2.1).
   816	
   817	        **Primary path:** call the LIVE client's ``get_context_usage()`` and return
   818	        ``round(resp["percentage"])`` — the same number the CLI ``/context`` shows (spike-proven,
   819	        design §2.1). **Fallback:** if there is no live client, the method is absent, or it
   820	        raises, derive ``round(100 * tokens / window)`` from the LAST ``ResultMessage``'s usage
   821	        captured by :meth:`_capture_usage` (an honest ratio, not a fabricated number). If neither
   822	        is available (no client AND no completed turn yet) → ``None`` (the caller shows ``ctx —``,
   823	        NEVER a fake 0%).
   824	
   825	        ⭐ **ASYNC — the installed SDK's ``ClaudeSDKClient.get_context_usage()`` is a COROUTINE**
   826	        (verified: ``inspect.iscoroutinefunction`` is True), so it MUST be awaited or the headline
   827	        percentage is never read (it would return an un-awaited coroutine that the dict-extractor
   828	        rejects, silently degrading to the usage fallback). We await it when it returns an
   829	        awaitable, and still accept a plain dict (defensive — a future/sync build keeps working).
   830	
   831	        Fully best-effort (RB1): this is an observer off the turn's critical path — it NEVER
   832	        raises (any error / no client → usage fallback → ``None``).
   833	        """
   834	        client = self._client
   835	        if client is not None:
   836	            try:
   837	                getter = getattr(client, "get_context_usage", None)
   838	                if getter is not None:
   839	                    resp = getter()
   840	                    if inspect.isawaitable(resp):
   841	                        resp = await resp  # ⭐ the SDK call is a coroutine — AWAIT it (B1 fix).
   842	                    pct = _percentage_of(resp)
   843	                    if pct is not None:
   844	                        return pct
   845	            except Exception:
   846	                # The live call is best-effort; fall through to the usage-derived fallback.
   847	                log.debug("get_context_usage() failed; using usage fallback", exc_info=True)
   848	        # Fallback: the honest ratio from the last completed turn's usage (§2.1).
   849	        tokens = self._last_usage_tokens
   850	        window = self._last_context_window
   851	        if tokens is not None and window is not None and window > 0:
   852	            try:
   853	                return round(100 * tokens / window)
   854	            except Exception:  # pragma: no cover - arithmetic guard (RB1)
   855	                return None
   856	        return None
   857	
   858	    async def stop(self) -> None:
   859	        if self._client is None:
   860	            return
   861	        try:
   862	            await self._client.disconnect()
   863	        finally:
   864	            self._client = None
   865	            # STATUSLINE T-SL-CORE: drop the ctx-% fallback cache with the session — it
   866	            # described THAT session's context; a fresh session starts with no figure (→ ctx —
   867	            # until its first turn completes), never a stale carryover. RB3 (in-memory only).
   868	            self._last_usage_tokens = None
   869	            self._last_context_window = None
   870	
   871	
   872	__all__ = [
   873	    "SdkSubstrate",
   874	    "normalize",
   875	    "INCREMENTAL_EVENT_TYPES",

exec
/bin/zsh -lc "nl -ba claude_tg/engine/engine.py | sed -n '315,360p'" in /Users/ray/dev/claude-telegram-bot-statusline
exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '420,470p' && nl -ba claude_tg/stream_session.py | sed -n '720,740p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
   315	
   316	    # -- session id ----------------------------------------------------------
   317	
   318	    @property
   319	    def session_id(self) -> Optional[str]:
   320	        """The current Claude session id (None before the substrate reports one)."""
   321	        return self._substrate.session_id
   322	
   323	    # -- ctx % for the statusline (STATUSLINE T-SL-CORE) ---------------------
   324	
   325	    async def context_percentage(self) -> Optional[int]:
   326	        """Best-effort % of the context window currently used, or ``None`` (design §2.1/§5 T5).
   327	
   328	        Delegates to the substrate's ``context_percentage`` (live ``get_context_usage()`` →
   329	        honest usage-derived fallback). The statusline shows ``🧠 ctx <X>%`` when this is an
   330	        int and ``🧠 ctx —`` when it is ``None`` — NEVER a fabricated number. Read defensively
   331	        via ``getattr`` so a substrate that predates this method (or a fake in a test) simply
   332	        yields ``None`` (the additive-seam discipline, mirroring the optional ``fork`` keyword);
   333	        the call is fully best-effort and NEVER raises — it is an observer off the turn's
   334	        critical path (RB1).
   335	
   336	        ⭐ **ASYNC (B1 fix):** the substrate awaits the SDK's coroutine ``get_context_usage()``,
   337	        so this is async too. We accept either a coroutine (await it — the real path) or a plain
   338	        ``int``/``None`` (a sync fake / a predating substrate), so every existing seam keeps
   339	        working while the real awaited SDK percentage is actually read.
   340	        """
   341	        getter = getattr(self._substrate, "context_percentage", None)
   342	        if getter is None:
   343	            return None
   344	        try:
   345	            value = getter()
   346	            if inspect.isawaitable(value):
   347	                value = await value
   348	        except Exception:  # pragma: no cover - the substrate is already best-effort
   349	            log.debug("context_percentage() failed (ignored)", exc_info=True)
   350	            return None
   351	        return value if isinstance(value, int) and not isinstance(value, bool) else None
   352	
   353	    # -- the decision seam (the async answer-hold) ---------------------------
   354	
   355	    async def on_tool_request(
   356	        self,
   357	        tool_name: str,
   358	        tool_input: dict[str, Any],
   359	        tool_use_id: Optional[str],
   360	    ) -> SubstrateDecision:

 succeeded in 0ms:
   420	    # slot future resolves — BEFORE acquiring the lock / starting the engine / entering
   421	    # ``_drive_turn`` — and if set aborts CLEANLY (releases the slot via the inner finally,
   422	    # clears ``inflight`` via the outer finally, persists NOTHING, never runs). This closes the
   423	    # gap where a turn in the pop→lock window is in NEITHER the run queue (``_drain_queued``
   424	    # already popped it) NOR holding a live engine (``engine.cancel`` finds none) — so a
   425	    # ``/cancel``|``/reset``|``/rm`` in that window used to miss it entirely and it ZOMBIE-RAN.
   426	    # CLEARED at the start of each accepted turn (alongside ``inflight = True``) so a stale
   427	    # abort from a previously-cancelled turn never kills a fresh one. Built lazily in
   428	    # __post_init__ (like ``lock``); transient in-memory (RB3).
   429	    abort: asyncio.Event = None  # type: ignore[assignment]
   430	    # QF3 (B3/RB3): True from the moment ``engine.resume()`` SUCCEEDS until the first
   431	    # turn on that resumed session completes WITHOUT a resume-failure-shaped error. A
   432	    # stale/aged/torn session can resume "successfully" (connect) and then error on the
   433	    # FIRST ``send`` — this flag tells :meth:`_drive_turn` the current turn is that first,
   434	    # unconfirmed use of a resumed session, so it (and ONLY it) applies the
   435	    # ``_is_resume_failure`` heuristic. A FRESH-started session never sets this, so a fresh
   436	    # session erroring is never mistaken for a resume failure. Reset on the in-memory
   437	    # runtime only (never persisted).
   438	    resumed_unverified: bool = False
   439	    # P12 T-PLAN-2 (/plan): a per-project, ONE-SHOT, in-memory marker — True from the moment
   440	    # ``/plan`` arms this project until the NEXT turn for it consumes it. ``_ensure_engine``
   441	    # reads + CLEARS it and builds that one turn's session in ``permission_mode="plan"`` (a
   442	    # FRESH plan-mode session — mechanism (a), mirroring how ``model`` is baked at session
   443	    # creation), so Claude reasons + proposes a plan and surfaces ``ExitPlanMode`` through the
   444	    # SHIPPED P6 hold/keyboard. The turn AFTER is a normal ``"default"`` session again (the
   445	    # marker is one-shot). Transient in-memory like the rest of the runtime (RB3): a process
   446	    # restart drops it — the supervision posture NEVER silently survives a restart, and it is
   447	    # never persisted to the registry. Set by :meth:`arm_plan`; consumed (read + cleared) in
   448	    # :meth:`_ensure_engine`. ADR-001 C4: arming plan mode greenlights NOTHING about tools —
   449	    # an approved plan's later risky tools still hit the permission gate independently.
   450	    plan_next: bool = False
   451	    # STATUSLINE T-SL-WIRE (B3 fix): True WHILE a plan-mode turn is actually running on this
   452	    # project, so the statusline shows ``🔒 plan`` for the live plan turn's duration. The
   453	    # one-shot ``plan_next`` above is CONSUMED (read + cleared) in ``handle_message`` BEFORE
   454	    # ``_drive_turn`` runs, so by the time the plan turn is streaming ``plan_next`` is already
   455	    # False — reading it in :meth:`_statusline_text` would wrongly show ``gate`` DURING the plan
   456	    # turn. So ``_drive_turn`` sets this from the consumed ``plan_turn`` local at turn start and
   457	    # CLEARS it in its finally (turn end) — the line reads THIS for the live mode. Transient
   458	    # in-memory (RB3); a restart drops it (no turn is running across a restart anyway).
   459	    in_plan_turn: bool = False
   460	    # P12 T-PLAN: the SDK ``permission_mode`` the CURRENT live engine (``engine``) was built
   461	    # with — ``"default"`` for an ordinary session, ``"plan"`` for the fresh session built for
   462	    # an armed ``/plan`` turn. ``_ensure_engine`` records it at build time and consults it in
   463	    # the warm fast-path: a warm engine is reused ONLY when its mode matches the turn's
   464	    # requested mode, so (a) a normal turn after a plan turn rebuilds back to ``"default"``
   465	    # (the plan-mode session is one-shot — it never silently lingers onto the next turn), and
   466	    # (b) a plan turn never reuses a ``"default"`` warm engine (mechanism (a) is session-
   467	    # creation — the mode can't be hot-switched). Transient in-memory (RB3); a restart rebuilds
   468	    # the engine from the persisted id in ``"default"`` (the arming never persists).
   469	    engine_permission_mode: str = "default"
   470	    # P12 T-THINK (/thinking): this project's LIVE-REASONING toggle — False by default (cost +
   720	    # slot — mirrors the transient status line's ``status_text``). EXACTLY ONE id is ever held
   721	    # (we only edit it; on recovery we re-point it). Transient/in-memory only (RB3): a restart
   722	    # drops the reference (the bot re-creates the line on the first post-restart update) — like
   723	    # ``send_gate``/``status_message_id``, the live pin id is never persisted.
   724	    statusline_message_id: Optional[int] = None
   725	    statusline_text: Optional[str] = None
   726	    # STATUSLINE T-SL-WIRE (pin-retry fix): whether the held ``statusline_message_id`` is
   727	    # actually PINNED. The send and the pin are separate Telegram calls — a send can succeed
   728	    # (id stored) while the pin RAISES (rate-limit, perms, hiccup), leaving the line sent but
   729	    # UNPINNED. Without this flag the identical-text skip would short-circuit every later update
   730	    # and the line would stay unpinned forever. So on a failed pin we leave this False and RETRY
   731	    # the pin on the next update even when the text is unchanged. Transient in-memory (RB3).
   732	    statusline_pinned: bool = False
   733	
   734	
   735	class StreamingBusy(Exception):
   736	    """Raised when a chat already has a streaming turn in flight (harvested ClaudeBusy)."""
   737	
   738	
   739	class StreamingSession:
   740	    """Drives the streaming engine for every chat (the bot delegates here in streaming mode).

exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '4148,4442p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
  4148	                except Exception:
  4149	                    log.debug("orphaned status-line delete failed (ignored)", exc_info=True)
  4150	            mid = await self._gated_send(
  4151	                state, send, verbatim=False,
  4152	                text=body, reply_markup=None, parse_mode=action.parse_mode,
  4153	            )
  4154	            rt.status_message_id = mid
  4155	            rt.status_text = body
  4156	
  4157	    # -- the pinned mobile statusline (STATUSLINE T-SL-CORE, design §3.1/§4) --
  4158	
  4159	    async def _maybe_update_statusline(
  4160	        self,
  4161	        chat_id: int,
  4162	        *,
  4163	        send: Optional[SendFn],
  4164	        edit: Optional[EditFn],
  4165	        pin: Optional[PinFn],
  4166	        unpin: Optional[UnpinFn],
  4167	        for_project: Optional[str] = None,
  4168	    ) -> None:
  4169	        """Refresh the pinned statusline IFF this is the chat's FOREGROUND project (T-SL-WIRE).
  4170	
  4171	        ⭐ **The make-or-break wiring invariant (design §3.1).** The pinned line reflects the
  4172	        chat's ACTIVE (foreground) project — the one the operator is watching. A BACKGROUND
  4173	        concurrent turn (a non-active project running under P5 concurrency) must NEVER rewrite
  4174	        the line, or two concurrent turns would stomp each other's state and the single pinned
  4175	        line would stop describing "what you're looking at". So the turn-start / turn-end
  4176	        triggers route through HERE, which:
  4177	
  4178	        * **skips** when ``for_project`` is not the chat's foreground (:meth:`_is_foreground`) —
  4179	          a background turn leaves the foreground line untouched;
  4180	        * **skips** when any closure is missing (a caller/test that didn't inject pin/unpin —
  4181	          back-compat: the statusline simply isn't driven, the turn is unaffected);
  4182	        * otherwise delegates to :meth:`_update_statusline` (itself fully best-effort, RB1).
  4183	
  4184	        ``for_project=None`` means "the caller already knows this is foreground" (the command
  4185	        paths: ``/switch`` + the knob setters always act on the active project), so the
  4186	        foreground gate is bypassed but the closure-presence gate still applies. The whole call
  4187	        is wrapped so a foreground-check / build error can never escape to the turn (RB1) — the
  4188	        statusline is an observer off the turn's critical path.
  4189	        """
  4190	        if send is None or edit is None or pin is None or unpin is None:
  4191	            return  # no closures injected (a test / a caller that didn't wire them) → no-op.
  4192	        try:
  4193	            if for_project is not None and not self._is_foreground(chat_id, for_project):
  4194	                # ⭐ Foreground-only: a BACKGROUND turn never rewrites the foreground line.
  4195	                return
  4196	            await self._update_statusline(
  4197	                chat_id, send=send, edit=edit, pin=pin, unpin=unpin
  4198	            )
  4199	        except Exception:
  4200	            # RB1: a foreground-check / dispatch error must never break the turn (the inner
  4201	            # _update_statusline already swallows its own I/O; this guards the gate itself).
  4202	            log.debug("statusline trigger failed for chat (ignored)", exc_info=True)
  4203	
  4204	    async def _statusline_text(self, chat_id: int) -> Optional[str]:
  4205	        """Build the CURRENT statusline body for ``chat_id``'s foreground project (live read).
  4206	
  4207	        Reads the chat's ACTIVE (foreground) project's live state — the worktree NAME, the
  4208	        effective model + effort, the permission mode, the working/idle marker, and the ctx %
  4209	        — and renders it through :func:`~claude_tg.render.format_statusline`. Foreground-only
  4210	        (design §3.1): a background project's turn never rewrites the line, so the single pinned
  4211	        line always describes "what you're looking at".
  4212	
  4213	        **Read-only / fail-safe (RB1):** resolves the active runtime with ``create_default=
  4214	        False`` so a statusline refresh NEVER creates a project as a side effect; with no active
  4215	        project (nothing run yet) returns ``None`` (nothing to show). Each field read is
  4216	        defensive — a missing store / odd record / ctx call that raises degrades to a safe
  4217	        default (``ctx —``, ``gate``) rather than raising. Returns the formatted body, or
  4218	        ``None`` when there is no foreground project to describe.
  4219	
  4220	        ⭐ **ASYNC (B1 fix):** the ctx % comes from ``Engine.context_percentage()`` which AWAITS
  4221	        the SDK's coroutine ``get_context_usage()`` — so this method is async and awaits it. The
  4222	        await is still fully best-effort (any raise → ``ctx —``, never a fabricated number); it
  4223	        is the only await here (every other field is a pure in-memory read).
  4224	
  4225	        * ``worktree`` — the active project NAME (SB4-validated charset, so inert — SB3).
  4226	        * ``model`` — :meth:`_resolve_project_model` reduced by :func:`model_short_label`.
  4227	        * ``effort`` — :meth:`_resolve_project_effort` (``None`` → model-only).
  4228	        * ``mode`` — ``yolo`` if the project's policy is allow-all, else ``plan`` if a plan turn
  4229	          is RUNNING (``in_plan_turn`` — B3) OR a ``/plan`` is armed for the next turn
  4230	          (``plan_next``), else ``gate`` (the fail-closed default).
  4231	        * ``working`` — the per-project status enum is a working state (``running`` /
  4232	          ``awaiting_*`` / ``queued``) vs ``idle``.
  4233	        * ``ctx_pct`` — the live engine's :meth:`~claude_tg.engine.engine.Engine.context_percentage`
  4234	          (``None`` → ``ctx —``, never a fabricated number).
  4235	        """
  4236	        name, rt = self._active_runtime(chat_id, create_default=False)
  4237	        if name is None or rt is None:
  4238	            return None
  4239	        worktree = name  # the SB4-validated project name (no path; SB3-inert).
  4240	        model_label = model_short_label(self._resolve_project_model(chat_id, name))
  4241	        effort = self._resolve_project_effort(chat_id, name)
  4242	        # mode: yolo (allow-all) wins; else plan — either a plan turn is RUNNING NOW
  4243	        # (``in_plan_turn``, B3 — ``plan_next`` is already consumed by the time the turn streams)
  4244	        # OR a ``/plan`` is armed for the NEXT turn (``plan_next``); else the fail-closed gate.
  4245	        if bool(getattr(rt.policy, "yolo", False)):
  4246	            mode = "yolo"
  4247	        elif bool(getattr(rt, "in_plan_turn", False)) or bool(getattr(rt, "plan_next", False)):
  4248	            mode = "plan"
  4249	        else:
  4250	            mode = "gate"
  4251	        working = rt.status in ("running", "awaiting_approval", "awaiting_answer", "awaiting_plan", "queued")
  4252	        ctx_pct: Optional[int] = None
  4253	        engine = rt.engine
  4254	        if engine is not None:
  4255	            try:
  4256	                ctx_pct = await engine.context_percentage()
  4257	            except Exception:  # pragma: no cover - the engine call is already best-effort (RB1)
  4258	                ctx_pct = None
  4259	        return format_statusline(
  4260	            worktree=worktree,
  4261	            model_label=model_label,
  4262	            effort=effort,
  4263	            ctx_pct=ctx_pct,
  4264	            mode=mode,
  4265	            working=working,
  4266	        )
  4267	
  4268	    async def _update_statusline(
  4269	        self,
  4270	        chat_id: int,
  4271	        *,
  4272	        send: SendFn,
  4273	        edit: EditFn,
  4274	        pin: PinFn,
  4275	        unpin: UnpinFn,
  4276	    ) -> None:
  4277	        """Refresh the chat's ONE pinned statusline — send+pin on first use, edit thereafter.
  4278	
  4279	        STATUSLINE T-SL-CORE (design §3.1/§4). Builds the current foreground statusline body
  4280	        (:meth:`_statusline_text`) and reconciles it with the chat's pinned line:
  4281	
  4282	        * **identical text** → skip entirely (no I/O — a no-op edit raises "message is not
  4283	          modified" AND wastes a send slot; mirrors :meth:`_edit_status`).
  4284	        * **first update** (no id held) → SEND the body then PIN it with the notification
  4285	          DISABLED (a silent pin — design §3.1); store the id + text.
  4286	        * **subsequent update** → EDIT in place only (no re-pin, no re-send; a pinned message
  4287	          edited in place stays pinned and silent).
  4288	        * **edit FAILURE** (the operator unpinned/deleted it → "message to edit not found", an
  4289	          API hiccup, too old) → ORPHAN RECOVERY: clear the stored id, best-effort UNPIN the
  4290	          stale one (the "one pinned message" invariant — Telegram's current pin is the newest,
  4291	          so the bar self-corrects), then re-SEND + re-PIN a fresh line (mirrors the orphaned
  4292	          status-line recovery in :meth:`_edit_status`).
  4293	
  4294	        **⭐ RB1 — a pin/edit/send failure NEVER breaks or wedges a turn.** This is an observer
  4295	        OFF the turn's critical path: the WHOLE body is wrapped so ANY exception (a raising
  4296	        ``send``/``edit``/``pin``/``unpin``, a build error) is logged at debug and swallowed —
  4297	        the caller (the turn loop / a command) is unaffected. **RB5** — every send/edit funnels
  4298	        through the per-chat gate as the **non-verbatim** kind (:meth:`_gated_send`/
  4299	        :meth:`_gated_edit`), so the statusline can never flood and never starves a real
  4300	        answer/prompt. **One id invariant** — exactly one ``statusline_message_id`` is ever held
  4301	        per chat; we only ever edit it, and on recovery re-point it.
  4302	
  4303	        ``send``/``edit``/``pin``/``unpin`` are injected by ``bot.py`` (the same pattern as the
  4304	        existing send/edit/delete closures) targeting THIS chat — so the line is SB1-confined to
  4305	        the operator's allowlisted chat (no new outbound surface).
  4306	
  4307	        **⭐ B2 fix — no stale line across a ``/switch``.** The body is built from the FOREGROUND
  4308	        project's state, but the gated send/edit ``await``s the gate's wait — a ``/switch`` in
  4309	        that window would change the foreground. So the body is REBUILT from CURRENT state right
  4310	        before the actual edit/send (inside the gated helpers, AFTER the gate wait); whatever the
  4311	        foreground is at write time, the line that lands describes IT, never a pre-switch
  4312	        snapshot. **Pin-retry** — a send that succeeded while its pin RAISED leaves the line
  4313	        UNPINNED (``statusline_pinned`` False); a later update RETRIES the pin even if the text is
  4314	        unchanged, so a transient pin failure self-heals instead of sticking unpinned forever.
  4315	        """
  4316	        try:
  4317	            body = await self._statusline_text(chat_id)  # async (B1: awaits the SDK ctx %)
  4318	            if not body:
  4319	                return  # no foreground project to describe — nothing to pin/edit.
  4320	            state = self._chat(chat_id)
  4321	            # Pin-retry: if we hold a sent id whose pin FAILED, retry the pin even on identical
  4322	            # text (the identical-text skip below would otherwise leave it unpinned forever).
  4323	            if (
  4324	                state.statusline_message_id is not None
  4325	                and not state.statusline_pinned
  4326	                and body == state.statusline_text
  4327	            ):
  4328	                await self._statusline_pin(state, state.statusline_message_id, pin=pin)
  4329	                return
  4330	            if body == state.statusline_text:
  4331	                # Identical to what's pinned — skip BEFORE the gate so an unchanged refresh
  4332	                # never consumes a send slot and never triggers a no-op "not modified" edit.
  4333	                return
  4334	            if state.statusline_message_id is None:
  4335	                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
  4336	                return
  4337	            try:
  4338	                # B2: rebuild the body AFTER the gate wait (inside _statusline_gated_edit) so a
  4339	                # /switch during the wait writes the now-current line, never the stale snapshot.
  4340	                await self._statusline_gated_edit(
  4341	                    chat_id, state, state.statusline_message_id, edit=edit
  4342	                )
  4343	            except Exception:
  4344	                # Orphan recovery (design §4 RB1): the pinned line is gone (unpinned/deleted by
  4345	                # the operator) / too old / an API hiccup. Clear the dead id, best-effort UNPIN
  4346	                # the stale one (one-pin invariant), then re-send + re-pin a fresh line. The
  4347	                # turn is unaffected either way (this whole method is best-effort).
  4348	                log.debug("statusline edit failed for chat; re-sending + re-pinning", exc_info=True)
  4349	                stale_id = state.statusline_message_id
  4350	                state.statusline_message_id = None
  4351	                state.statusline_text = None
  4352	                state.statusline_pinned = False
  4353	                try:
  4354	                    await unpin(message_id=stale_id)
  4355	                except Exception:
  4356	                    log.debug("stale statusline unpin failed (ignored)", exc_info=True)
  4357	                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
  4358	        except Exception:
  4359	            # ⭐ The make-or-break swallow (RB1): NOTHING the statusline does may escape to the
  4360	            # turn. A build/gate/closure failure is logged at debug and dropped — the next state
  4361	            # change re-creates the line.
  4362	            log.debug("statusline update failed for chat (ignored)", exc_info=True)
  4363	
  4364	    async def _statusline_gated_edit(
  4365	        self, chat_id: int, state: _ChatState, message_id: int, *, edit: EditFn
  4366	    ) -> None:
  4367	        """Edit the pinned line through the gate, REBUILDING the body AFTER the gate wait (B2).
  4368	
  4369	        Reserves the per-chat gate slot and awaits its wait (non-verbatim — RB5), THEN re-derives
  4370	        the statusline body from CURRENT state and performs the raw edit. Rebuilding after the
  4371	        wait closes the ``/switch``-during-wait race: the line that lands always describes the
  4372	        foreground project AT WRITE TIME, never the pre-wait snapshot. If the rebuilt body is
  4373	        empty (the foreground project vanished mid-wait — e.g. ``/rm``) or identical to what is
  4374	        already shown, the edit is SKIPPED (no stale write, no no-op "not modified"). A raise
  4375	        propagates to the caller's orphan-recovery (the message may be gone).
  4376	        """
  4377	        wait = self._gate(state).reserve(verbatim=False)
  4378	        if wait > 0:
  4379	            await self._sleep(wait)
  4380	        body = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
  4381	        if not body or body == state.statusline_text:
  4382	            return  # foreground vanished mid-wait, or nothing changed → no stale/no-op write.
  4383	        await edit(message_id=message_id, text=body, parse_mode="HTML")
  4384	        state.statusline_text = body
  4385	
  4386	    async def _statusline_send_and_pin(
  4387	        self,
  4388	        chat_id: int,
  4389	        state: _ChatState,
  4390	        *,
  4391	        send: SendFn,
  4392	        pin: PinFn,
  4393	    ) -> None:
  4394	        """Send the statusline body (gated, non-verbatim) then PIN it silently (design §3.1).
  4395	
  4396	        The first-use + orphan-recovery primitive: reserve the gate slot, await its wait, THEN
  4397	        rebuild the body from CURRENT state (B2 — a ``/switch`` during the wait sends the
  4398	        now-current line, never the pre-wait snapshot) and send it; a best-effort silent pin
  4399	        follows (``disable_notification=True`` — a pin must never re-ping the operator). The
  4400	        id/text are stored on the chat ONLY when the send returns an id (so a send that yields
  4401	        ``None`` does not leave a half-set state). A PIN failure is swallowed (RB1) AND records
  4402	        ``statusline_pinned=False`` so the next update retries the pin (the line is still sent +
  4403	        tracked; only the bar placement is deferred, never the turn). Called from
  4404	        :meth:`_update_statusline` inside its best-effort guard, so a raising ``send`` propagates
  4405	        to that guard's swallow.
  4406	        """
  4407	        wait = self._gate(state).reserve(verbatim=False)
  4408	        if wait > 0:
  4409	            await self._sleep(wait)
  4410	        body = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
  4411	        if not body:
  4412	            return  # foreground vanished mid-wait — nothing to send.
  4413	        mid = await send(text=body, reply_markup=None, parse_mode="HTML")
  4414	        if mid is None:
  4415	            # The send produced no id (a closure that returns None) — don't store a half state;
  4416	            # the next update will try a fresh send.
  4417	            return
  4418	        state.statusline_message_id = mid
  4419	        state.statusline_text = body
  4420	        state.statusline_pinned = False  # not pinned until the pin call below succeeds.
  4421	        await self._statusline_pin(state, mid, pin=pin)
  4422	
  4423	    async def _statusline_pin(self, state: _ChatState, message_id: int, *, pin: PinFn) -> None:
  4424	        """Best-effort SILENT pin of the statusline message; record whether it stuck (pin-retry).
  4425	
  4426	        A pin must never re-ping (``disable_notification=True``) and never break the turn (RB1).
  4427	        On success ``statusline_pinned`` is set True; on failure it stays/!becomes False and is
  4428	        swallowed — :meth:`_update_statusline` then RETRIES the pin on the next update (even with
  4429	        unchanged text) so a transient pin failure self-heals instead of leaving the line unpinned
  4430	        forever. Only the pinned-bar placement is ever at stake here, never the turn.
  4431	        """
  4432	        try:
  4433	            await pin(message_id=message_id, disable_notification=True)
  4434	            state.statusline_pinned = True
  4435	        except Exception:
  4436	            state.statusline_pinned = False
  4437	            log.debug("statusline pin failed (will retry on next update)", exc_info=True)
  4438	
  4439	    # -- the callback resolve path (LOCK-FREE: SB1 enforced at the bot) ------
  4440	
  4441	    def resolve_callback(self, chat_id: int, data: object) -> "CallbackOutcome":
  4442	        """Route a decoded inline-keyboard tap to its OWNING project's pending request.

codex
One important thing surfaced: the body is rebuilt after the send-gate wait, but `_statusline_text()` itself awaits the async ctx read after it has already captured the foreground project. I’m checking whether that leaves a real stale-write race against `/switch`, because that would keep B2 open despite the new tests.
exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '3220,3370p'" in /Users/ray/dev/claude-telegram-bot-statusline
exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '3540,3860p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
  3220	        # project never consumes a queue entry. "Busy" is the project's IN-FLIGHT marker
  3221	        # (``inflight``) — True continuously from the instant a turn is accepted (just below)
  3222	        # through queued / the pop→lock transfer window / running, until the end-of-turn
  3223	        # finally. The round-2 cross-model-QA RACE: a lock+queue guard (``lock.locked() or
  3224	        # _is_queued``) had a TOCTOU — after ``_pop_next_waiter`` pops a queued project's
  3225	        # waiter (so ``_is_queued`` is False) but before the woken turn acquires its lock (so
  3226	        # ``lock.locked()`` is False), a same-project 2nd message slipped through and appended a
  3227	        # SECOND _QueuedTurn (two turns for one project). ``inflight`` has no such gap. (It
  3228	        # subsumes the BLOCKER-2 ``_is_queued`` guard — a queued turn is in-flight — and the
  3229	        # lock guard; both are kept as belt-and-braces but ``inflight`` alone is sufficient.)
  3230	        if target_rt.inflight or target_rt.lock.locked() or self._is_queued(state, target_rt):
  3231	            raise StreamingBusy()
  3232	
  3233	        # Accept the turn for THIS project: mark it in-flight BEFORE acquiring a slot, so the
  3234	        # busy-guard above rejects any same-project 2nd message at EVERY subsequent point
  3235	        # (queued, the slot-transfer window, running). Cleared ONLY in the outer finally below,
  3236	        # on every exit path — including a DRAIN-cancel of a queued waiter (which raises
  3237	        # CancelledError out of _acquire_slot, BEFORE the slot-release try) — so a cancelled /
  3238	        # drained / failed turn never leaves the project wedged as in-flight.
  3239	        target_rt.inflight = True
  3240	        # ADR-005 D9 (round-3 BLOCKERS 1+2): a FRESH turn starts un-aborted. Clear any stale
  3241	        # abort left set by a PREVIOUS turn's /cancel|/reset|/rm so it can't kill this one. Done
  3242	        # under inflight=True (after the busy-guard) — no other turn for this project can run
  3243	        # concurrently to observe a transient clear.
  3244	        target_rt.abort.clear()
  3245	        # P12 T-PLAN-2 (/plan), round-2 QA BLOCKER — CONSUME the one-shot plan marker HERE, at
  3246	        # the EARLIEST point this PROMPT message is committed to being driven as a turn: after
  3247	        # the busy-guard passed (so we won't reject + leave it armed) and BEFORE _acquire_slot,
  3248	        # the two pre-engine abort guards (the slot-transfer + lock-wait windows below), and
  3249	        # _ensure_engine's SB2 PathNotAllowed check. Read + CLEAR atomically into a local
  3250	        # ``plan_turn`` that is threaded DOWN to _ensure_engine (which no longer reads/clears
  3251	        # the marker). Consuming it this early makes the one-shot contract hold on EVERY exit:
  3252	        # a turn that aborts (abort.is_set → return) or is refused fail-closed (SB2 raise) STILL
  3253	        # consumed the marker, so the NEXT message is ALWAYS a normal turn — never a surprise
  3254	        # plan prompt (the bug: the late read/clear in _ensure_engine was skipped by both the
  3255	        # pre-engine ``return``s and the SB2 raise). PROMPT-turn-scoped: commands (/status,
  3256	        # /plan itself) go through their cmd_* handlers, never handle_message, so they never
  3257	        # reach here and never consume the marker (/plan → /status → a prompt = the PROMPT runs
  3258	        # in plan mode); a free-text "Other"/reject reply returned above (it resolves a hold,
  3259	        # not a new turn) so it doesn't consume either. RB3: ``plan_turn`` is a local; the
  3260	        # marker stays in-memory + un-persisted.
  3261	        plan_turn = target_rt.plan_next
  3262	        target_rt.plan_next = False
  3263	        try:
  3264	            # P5 / ADR-005 D6 (T6): acquire a run SLOT before driving. Under the cap → run now
  3265	            # (the counter is incremented). At the cap → enqueue (per-chat FIFO), set this
  3266	            # project's status to "queued", send a one-time "queued behind N run(s)" notice, and
  3267	            # park until a finishing run hands this turn the freed slot (SB6: queue, never drop /
  3268	            # refuse). After this returns a slot is held and MUST be released exactly once below.
  3269	            # A DRAIN-cancel of this project's waiter raises CancelledError here (its own handler
  3270	            # in _acquire_slot does the slot bookkeeping); the outer finally still clears inflight.
  3271	            await self._acquire_slot(state, target_rt, send=send)
  3272	            # SLOT-LEAK SAFETY (the flagged D6 hazard): from here the slot is HELD. The whole
  3273	            # remainder — _ensure_engine, the SB2 refusal, the resume notice, AND _drive_turn —
  3274	            # runs inside this try so the finally's _release_slot fires on EVERY exit path
  3275	            # (normal end, mid-stream raise, cancel, resume-failure return, StreamingBusy below).
  3276	            # _release_slot decrements the global counter and pops the next queued waiter exactly
  3277	            # once, so a raised turn can never leak a slot (which would permanently shrink
  3278	            # capacity) and a slot is never double-released. Mirrors T5's end-of-turn finally.
  3279	            try:
  3280	                # ADR-005 D9 (round-3 BLOCKERS 1+2): THE SLOT-TRANSFER WINDOW abort check.
  3281	                # We have just resumed from _acquire_slot holding a slot. If this turn was
  3282	                # QUEUED, it spent the pop→here window in NEITHER the run queue (a transferring
  3283	                # _release_slot already popped it — _drain_queued can't see it) NOR holding a
  3284	                # live engine (none is started yet — engine.cancel finds nothing). So a
  3285	                # /cancel|/reset|/rm landing in that window can't reach this turn via the
  3286	                # queue-drain or the engine-cancel paths — it can only SET this project's abort.
  3287	                # Honor it HERE, before acquiring the lock / starting the engine / entering
  3288	                # _drive_turn: abort CLEANLY — the inner finally releases the slot we hold (no
  3289	                # leak), the outer finally clears inflight, and we persist NOTHING and never run.
  3290	                # This is the net invariant: a control command in the transfer window → the turn
  3291	                # NEVER starts; _running returns to 0; no session is persisted.
  3292	                if target_rt.abort.is_set():
  3293	                    return False
  3294	                # While this turn was parked in the queue, another message to the SAME project
  3295	                # could have started running it (its lock would now be held). Re-check after the
  3296	                # slot is granted so the per-project one-run invariant holds even across a queue
  3297	                # wait; the finally still releases the slot this turn acquired.
  3298	                if target_rt.lock.locked():
  3299	                    raise StreamingBusy()
  3300	                async with target_rt.lock:
  3301	                    # ADR-005 D9: re-check the abort AFTER taking the lock and BEFORE starting
  3302	                    # the engine — a /cancel|/reset|/rm could have set it during the (awaited)
  3303	                    # lock acquisition above (the lock-wait sub-window). Bailing here means no
  3304	                    # engine is ever started/resumed for an aborted turn (no connected-but-
  3305	                    # undriven client, no _drive_turn, no persist). The finally still releases
  3306	                    # the slot. Together with the window check above, the abort covers EVERY
  3307	                    # pre-run await boundary; once _drive_turn starts streaming, a live engine
  3308	                    # exists and the command's engine.cancel() unblocks it instead.
  3309	                    if target_rt.abort.is_set():
  3310	                        return False
  3311	                    try:
  3312	                        engine, resume_failed = await self._ensure_engine(
  3313	                            chat_id, target=target, plan_turn=plan_turn
  3314	                        )
  3315	                    except PathNotAllowed:
  3316	                        # SB2 (T7): the active project's stored cwd drifted out of the permitted
  3317	                        # roots (config narrowed, or a path component became an out-of-root
  3318	                        # symlink). Refuse the turn fail-closed WITHOUT starting the engine; the
  3319	                        # lock releases on return AND the finally releases the slot (no leak).
  3320	                        # Operator-facing refusal → verbatim priority through the D8 gate.
  3321	                        # R6: wrap the cwd in <code> (HTML) so Telegram renders the path as
  3322	                        # inert monospace, not a row of tappable fake /segment command-links;
  3323	                        # code_path HTML-escapes it so a stray &/</> can't break the message.
  3324	                        # The literal "<name> <path>" placeholders are written ESCAPED
  3325	                        # (&lt;…&gt;) because this is now an HTML message — unescaped "<name>"
  3326	                        # would be parsed as a (broken) tag and Telegram would reject the send.
  3327	                        # The send closure passes parse_mode straight through; with the path
  3328	                        # escaped + the placeholders escaped, the content is always valid HTML.
  3329	                        await self._gated_send(
  3330	                            state, send, verbatim=True,
  3331	                            text=(
  3332	                                f"❌ This project's directory {code_path(self.get_cwd(chat_id))} "
  3333	                                "is no longer within the permitted roots — use "
  3334	                                "/new &lt;name&gt; &lt;path&gt; to create one inside them."
  3335	                            ),
  3336	                            reply_markup=None,
  3337	                            parse_mode="HTML",
  3338	                        )
  3339	                        return False
  3340	                    if resume_failed:
  3341	                        # RB3: the persisted session could not be resumed; a fresh one was
  3342	                        # started. Tell the operator BEFORE driving the turn (it still completes).
  3343	                        await self._gated_send(
  3344	                            state, send, verbatim=True,
  3345	                            text="⚠️ Couldn't resume this project's previous session; started a fresh one.",
  3346	                            reply_markup=None,
  3347	                            parse_mode=None,
  3348	                        )
  3349	                    await self._drive_turn(
  3350	                        state, chat_id, engine, text,
  3351	                        send=send, edit=edit, delete=delete,
  3352	                        pin=pin, unpin=unpin, target=target,
  3353	                        images=images, proactive=proactive, plan_turn=plan_turn,
  3354	                    )
  3355	            finally:
  3356	                # SLOT-LEAK SAFETY: release the slot this turn held — exactly once, on every
  3357	                # exit path. _release_slot decrements the global counter and, if a turn is
  3358	                # queued (this chat first, then any chat — FIFO), TRANSFERS the freed slot to
  3359	                # the oldest waiter (re-incrementing + waking it) so the dequeue fires on every
  3360	                # turn-exit too (normal / error / cancel / resume-failure). Pure bookkeeping +
  3361	                # a Future.set_result — it never awaits and never raises, so it cannot itself
  3362	                # leak or mask the turn's own exception (which propagates after the finally).
  3363	                self._release_slot(state)
  3364	        finally:
  3365	            # RACE fix (D6 / round-2 cross-model QA): clear the in-flight marker on EVERY exit
  3366	            # path of this turn — normal end, mid-stream raise, /cancel of a running turn, a
  3367	            # DRAIN-cancel of a queued waiter (CancelledError from _acquire_slot, which the
  3368	            # inner slot-release try does NOT cover), resume-failure, and the post-wait
  3369	            # StreamingBusy re-raise. This OUTER finally wraps _acquire_slot too, so inflight is
  3370	            # balanced even when the slot-release try is never entered (the drain-cancel path).

 succeeded in 0ms:
  3540	        send: SendFn,
  3541	        edit: EditFn,
  3542	        delete: Optional[DeleteFn] = None,
  3543	        pin: Optional[PinFn] = None,
  3544	        unpin: Optional[UnpinFn] = None,
  3545	        target: Optional[tuple[str, _ProjectRuntime]] = None,
  3546	        images: Optional[Sequence[ImageInput]] = None,
  3547	        proactive: bool = False,
  3548	        plan_turn: bool = False,
  3549	    ) -> None:
  3550	        """Iterate ``engine.send`` → render → Telegram send/edit (coalesced).
  3551	
  3552	        **P10 T1:** ``images`` (default ``None`` → text turn) is forwarded to
  3553	        ``engine.send`` so a multimodal turn streams the prompt + pixels; the render /
  3554	        coalesce / QF3-recovery machinery below is identical for both.
  3555	
  3556	        D6 "loud throughout": if the active project's policy has ``/yolo`` on, lead the
  3557	        turn with a persistent ``⚠️`` marker (its OWN message, before any event renders)
  3558	        so an in-progress allow-all session is never silent — the bypass shows on every
  3559	        turn, not just at the ``/yolo`` toggle. A plain ``send`` (no coalescer / no
  3560	        status-line edit) so it cannot be overwritten by the in-place status edits that
  3561	        follow.
  3562	
  3563	        **QF3 (B3/RB3): recover from a resume that connects then errors on first use.**
  3564	        If this is the FIRST turn on a freshly-resumed session (the runtime's
  3565	        ``resumed_unverified`` flag), every ``error``/``result`` event is checked with the
  3566	        ported ``_is_resume_failure`` heuristic. On a resume-failure-shaped event the dead
  3567	        ``session_id`` is NOT persisted; instead, AFTER the stream drains, the persisted id
  3568	        is cleared, the engine is dropped (so the next turn starts fresh — never re-resumes
  3569	        the dead id), and the operator is told to resend. If the turn instead completes
  3570	        cleanly, the flag is cleared (the resume is confirmed good). A FRESH session is
  3571	        never ``resumed_unverified``, so an unrelated fresh-turn error is never mistaken for
  3572	        a resume failure. The check happens INLINE while iterating and recovery happens
  3573	        AFTER the loop ends naturally (the substrate stream always terminates — RB2), so we
  3574	        never re-drive a turn mid-stream (no double-render / re-entrancy).
  3575	        """
  3576	        # The project this turn is running on. ``handle_message`` passes the project it
  3577	        # captured at message time (``target``) so the result-persist, any QF3 recovery, AND
  3578	        # the pending-index registration (ADR-005 D3 — id -> THIS turn's project) act on THIS
  3579	        # turn's project — critically for a turn that QUEUED behind the cap (D6/T6) and so
  3580	        # parked while the active project may have moved. Falling back to the active project
  3581	        # (no target) preserves the prior behavior for any direct caller.
  3582	        turn_name: Optional[str]
  3583	        turn_rt: Optional[_ProjectRuntime]
  3584	        if target is not None:
  3585	            turn_name, turn_rt = target
  3586	        else:
  3587	            turn_name, turn_rt = self._active_runtime(chat_id, create_default=True)
  3588	        assert turn_name is not None  # create_default=True always yields a project name
  3589	        assert turn_rt is not None  # create_default=True always yields a runtime too
  3590	        # This first turn applies the resume-failure heuristic iff the session was resumed
  3591	        # (not freshly started) and is not yet confirmed good.
  3592	        check_resume = turn_rt.resumed_unverified
  3593	        resume_failure_detected = False
  3594	        # P6/H2/RB2: latch a transport/liveness ``driver_error`` on this turn. A
  3595	        # driver_error means the SDK client is dead/wedged (a 120s liveness timeout that
  3596	        # was NOT a held human-approval — that case is suppressed in the adapter now — or a
  3597	        # transport failure). On an already-VERIFIED session (not the resume-failure case,
  3598	        # which has its OWN rebuild via _recover_failed_resume) the engine must be torn down
  3599	        # + rebuilt so the NEXT turn starts a fresh client, instead of every later turn
  3600	        # re-timing-out against the same dead client (the wedge-until-restart finding, RB2).
  3601	        # Latched here (body-free — only the kind_of_error is read, never the message, SB3)
  3602	        # and acted on AFTER the stream drains so we never re-enter the render loop mid-turn.
  3603	        driver_error_detected = False
  3604	
  3605	        coalescer = Coalescer(now=self._clock, min_interval=self._min_edit_interval)
  3606	        # P6/R5: per-turn duplicate-render dedup (the single foreground policy point for the
  3607	        # twin-render paths, alongside the ask/plan dedup the engine does in _drain_substrate).
  3608	        # Remembers verbatim bodies emitted THIS turn so the terminal frame doesn't re-send the
  3609	        # assistant prose (#1) or re-render a tool_error as a near-identical turn_error (#3).
  3610	        # Foreground-only: the background branch pings ✅/🔔 and continues before the render
  3611	        # section, so this never touches a backgrounded run.
  3612	        dedup = _TurnDedup()
  3613	        # P5 / ADR-005 D7: THIS project's status line + status enum (per-project, not a
  3614	        # chat-global slot). Status line starts unset (create on first edit_status); the
  3615	        # status enum goes idle -> running at turn start, awaiting_<kind> on a hold, back to
  3616	        # running on resolve, idle at turn end. Two concurrent turns each drive their OWN
  3617	        # runtime's line + status, so they never clash.
  3618	        turn_rt.status_message_id = None
  3619	        turn_rt.status_text = None
  3620	        turn_rt.status = "running"
  3621	        # STATUSLINE T-SL-WIRE (B3 fix): mark the LIVE plan-mode flag for the statusline's
  3622	        # duration so the line shows 🔒 plan WHILE the plan turn runs. ``plan_turn`` is the value
  3623	        # ``handle_message`` consumed from the one-shot ``plan_next`` (already cleared there), so
  3624	        # this transient flag is the only honest "this turn is a plan turn" signal at render
  3625	        # time. Cleared in the finally (turn end → back to gate/yolo). Set BEFORE the turn-start
  3626	        # statusline trigger so that first render already reads ``plan``.
  3627	        turn_rt.in_plan_turn = plan_turn
  3628	        # STATUSLINE T-SL-WIRE (design §3.1): turn START → flip the working ⚙️ marker ON (and
  3629	        # refresh model/effort/mode/worktree). FOREGROUND-ONLY — gated on ``turn_name`` so a
  3630	        # BACKGROUND concurrent turn never stomps the foreground line (the make-or-break
  3631	        # invariant). Best-effort (RB1): pins/edits can't break the turn (the helper swallows).
  3632	        await self._maybe_update_statusline(
  3633	            chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=turn_name,
  3634	        )
  3635	        # D6 "loud throughout" — but only inline for a FOREGROUND turn (a backgrounded run is
  3636	        # silent inline, D4; its yolo posture still shows on each foreground turn + via
  3637	        # /projects is not yolo-aware, so this is the loud surface when watched). Verbatim
  3638	        # priority through the D8 gate so the marker is never starved by status churn.
  3639	        if turn_rt.policy.yolo and self._is_foreground(chat_id, turn_name):
  3640	            await self._gated_send(
  3641	                state, send, verbatim=True,
  3642	                text=yolo_indicator(), reply_markup=None, parse_mode=None,
  3643	            )
  3644	        # P5 / ADR-005 D1 + T4-review: now runs are CONCURRENT and per-project ``status``
  3645	        # feeds /projects, a mid-stream exception in the loop below must NOT leave this
  3646	        # project stuck at running/awaiting_* (a stale status would mislead /projects and a
  3647	        # lingering "💭 thinking…" line would never clear). So the turn body is wrapped in
  3648	        # try/finally: the finally forces this project's status back to ``idle`` and clears
  3649	        # its transient status line (best-effort delete) no matter how the loop exits. The
  3650	        # per-project lock is released by handle_message's ``async with`` regardless, so a
  3651	        # raised turn frees its lock and leaves OTHER concurrent runs untouched (RB1/RB2).
  3652	        # P10 T1: pass ``images`` to ``engine.send`` ONLY when present, so a pure TEXT turn
  3653	        # calls ``engine.send(prompt)`` with the EXACT pre-P10 signature — every existing
  3654	        # injected fake engine (whose ``send`` has no ``images`` kwarg) keeps working
  3655	        # verbatim. The image path supplies the kwarg to the real Engine (which accepts it).
  3656	        # P14 T-FIRE: ⭐ pass ``proactive=True`` to ``engine.send`` ONLY for a proactive turn
  3657	        # (the same additive-kwarg discipline), so the engine FORCES the gate on for it; a
  3658	        # normal turn omits it entirely (the pre-P14 signature is preserved for every fake).
  3659	        send_kwargs: dict[str, Any] = {}
  3660	        if images:
  3661	            send_kwargs["images"] = images
  3662	        if proactive:
  3663	            send_kwargs["proactive"] = True
  3664	        try:
  3665	            async for event in engine.send(prompt, **send_kwargs):
  3666	                # QF3: on the first turn of a resumed session, flag a resume-failure-shaped
  3667	                # error/result. Latch on the first hit (the dead id is the same all turn).
  3668	                if check_resume and not resume_failure_detected and _is_resume_failure_event(event):
  3669	                    resume_failure_detected = True
  3670	                # P6/H2/RB2: latch a transport/liveness driver_error (body-free — kind only,
  3671	                # never event.message, SB3) so the verified-session engine is rebuilt after
  3672	                # the stream drains. Independent of the resume-failure check above: a fresh
  3673	                # OR resume-confirmed session can still driver_error mid-life, and that is the
  3674	                # wedge this guards. (A resume-failure-shaped driver_error on an UNVERIFIED
  3675	                # resumed session is handled by _recover_failed_resume instead — see below.)
  3676	                if (
  3677	                    not driver_error_detected
  3678	                    and isinstance(event, ErrorEvent)
  3679	                    and event.kind_of_error == "driver_error"
  3680	                ):
  3681	                    driver_error_detected = True
  3682	                # ADR-005 D3: register an injected ask/plan/permission in the pending index,
  3683	                # keyed by tool_use_id -> THIS turn's project, so a later tap / free-text
  3684	                # reply routes to THIS project's engine (not _active_engine). Cleared on
  3685	                # resolve / cancel / turn-end. Permission is registered too (P4 routed it
  3686	                # id-only, but the index must own every held request so the
  3687	                # foreground-vs-notify decision (T3) and the cross-project routing cover it).
  3688	                self._register_pending(state, turn_name, event)
  3689	                # ADR-005 D7: a held request flips THIS project's status to the matching
  3690	                # awaiting_<kind> for /projects; it returns to running when the resolve path
  3691	                # unblocks the held turn (set in the resolve/cancel methods, which own ref).
  3692	                held_kind = _pending_kind_of(event)
  3693	                if held_kind is not None:
  3694	                    turn_rt.status = _AWAITING_STATUS[held_kind]
  3695	                if isinstance(event, ResultEvent):
  3696	                    # QF3: do NOT re-persist the dead session_id on a resume-failure result
  3697	                    # — it would just re-arm the same broken resume. Recovery below clears it.
  3698	                    # (Foreground-INDEPENDENT — the session_id must persist whether the turn
  3699	                    # rendered inline or pinged in the background.)
  3700	                    if not resume_failure_detected:
  3701	                        # ADR-005 D2: persist to THIS turn's CAPTURED project (turn_name), not
  3702	                        # the active one — once /switch is free the active project can change
  3703	                        # mid-turn, so writing to "active" would clobber a different project's
  3704	                        # session_id (the lock-P-drive-Q / persist-drift hazard). turn_name is
  3705	                        # the project handle_message pinned at message time.
  3706	                        self._persist(
  3707	                            chat_id,
  3708	                            session_id=event.session_id or engine.session_id,
  3709	                            name=turn_name,
  3710	                        )
  3711	                        # T3 (P9): accumulate this turn's SDK-reported cost into the
  3712	                        # project's durable cumulative total (shown by /status). Only when
  3713	                        # the SDK gave a cost (oneshot / a partial result may not) and a
  3714	                        # store + named project exist; swallowed like _persist (RB1 — never
  3715	                        # crash a turn over a write). Persisted to THIS turn's CAPTURED
  3716	                        # project (turn_name), same per-project discipline as the session_id.
  3717	                        if event.total_cost_usd is not None and self.store is not None:
  3718	                            try:
  3719	                                self.store.add_cost(
  3720	                                    chat_id, turn_name, event.total_cost_usd
  3721	                                )
  3722	                            except Exception:
  3723	                                log.exception(
  3724	                                    "failed to accumulate project cost for chat %s", chat_id
  3725	                                )
  3726	                # ADR-005 D4: the inline-vs-notify send-decision. Re-read foreground PER
  3727	                # EVENT — /switch is free (T7), so the foreground can change mid-turn; an
  3728	                # event for the foreground project renders inline (as P4), an event for a
  3729	                # BACKGROUND project becomes a name-prefixed 🔔/✅/⚠️ ping (the operator is
  3730	                # not watching that project). A backgrounded run does NOT spam its verbose
  3731	                # status inline — its progress is summarized by the ping + the /projects
  3732	                # status column (D4) — so non-hold, non-terminal events are dropped for a
  3733	                # background turn (they never reach the coalescer/status line).
  3734	                if not self._is_foreground(chat_id, turn_name):
  3735	                    if held_kind is not None:
  3736	                        await self._notify_background(
  3737	                            state, chat_id, turn_name, event, held_kind, send=send
  3738	                        )
  3739	                    elif isinstance(event, (ResultEvent, ErrorEvent)):
  3740	                        await self._notify_terminal(state, turn_name, event, send=send)
  3741	                    # else (text/tool_use/status/incremental): a background run is silent —
  3742	                    # no inline status spam (D4). Skip the inline render entirely.
  3743	                    continue
  3744	                # --- foreground: render inline exactly as P4 (through the D8 send gate) ---
  3745	                if isinstance(event, AskEvent):
  3746	                    # Render each question as its OWN message + option keyboard so a
  3747	                    # question's choices sit directly beneath it. A single stacked keyboard
  3748	                    # for a multi-question ask is an unreadable wall of buttons (the operator
  3749	                    # can't tell which buttons belong to which question). Flush any buffered
  3750	                    # status first so the questions appear after it, in order.
  3751	                    for action in coalescer.flush().actions:
  3752	                        await self._perform(
  3753	                            state, turn_rt, action, send=send, edit=edit, delete=delete
  3754	                        )
  3755	                    for q_idx in range(len(event.questions)):
  3756	                        keyboard = ask_question_keyboard(event, q_idx)
  3757	                        # The question text is Claude-authored CommonMark -> render as HTML
  3758	                        # so **bold** etc. show and a stray < / & can't break the message; on
  3759	                        # a Telegram HTML rejection, resend the plain body (raw fallback —
  3760	                        # never a dropped question). Verbatim priority in the D8 gate.
  3761	                        try:
  3762	                            await self._gated_send(
  3763	                                state, send, verbatim=True,
  3764	                                text=ask_question_body_html(event, q_idx),
  3765	                                reply_markup=keyboard,
  3766	                                parse_mode="HTML",
  3767	                            )
  3768	                        except Exception:
  3769	                            await self._gated_send(
  3770	                                state, send, verbatim=True,
  3771	                                text=ask_question_body(event, q_idx),
  3772	                                reply_markup=keyboard,
  3773	                                parse_mode=None,
  3774	                            )
  3775	                    continue
  3776	                # P6/R5 #3: a terminal turn_error that merely repeats a tool_error already
  3777	                # shown this turn is a duplicate error block — drop it (the tool_error already
  3778	                # rendered the failure verbatim). Done BEFORE record so we never compare an
  3779	                # event against itself.
  3780	                if dedup.suppresses(event):
  3781	                    continue
  3782	                # P6/R5 #1: when the terminal ResultEvent.result_text just repeats assistant
  3783	                # prose already emitted this turn, render only the compact ✅ done footer rather
  3784	                # than re-sending the identical answer. Swap in a footer-only result (keeps
  3785	                # num_turns/cost) — the done indicator still appears, the prose is sent once.
  3786	                render_event_ = event
  3787	                if isinstance(event, ResultEvent) and dedup.result_is_duplicate_prose(event):
  3788	                    render_event_ = _footer_only_result(event)
  3789	                # Remember this turn's verbatim bodies (assistant prose + tool_error messages)
  3790	                # so a later twin (the result_text / terminal turn_error) can dedup against it.
  3791	                dedup.record(event)
  3792	                # SB3/H1 (body-free): a RAW EXTERNAL error (tool/SDK stderr) renders as a
  3793	                # body-free summary to the chat (see render._render_error); its raw detail
  3794	                # goes ONLY to the LOCAL debug log, SCRUBBED through _redact_sid (the body can
  3795	                # carry a session id — the bot token is never logged anywhere). This is the
  3796	                # single place the raw body is persisted, and only at DEBUG.
  3797	                if isinstance(render_event_, ErrorEvent) and error_is_raw_external(render_event_):
  3798	                    log.debug(
  3799	                        "raw external error (%s) for chat %s project %s [%s]: %s",
  3800	                        render_event_.kind_of_error,
  3801	                        chat_id,
  3802	                        turn_name,
  3803	                        _redact_sid(render_event_.session_id),
  3804	                        _redact_sid_in_text(render_event_.message),
  3805	                    )
  3806	                for action in coalescer.offer(render_event_).actions:
  3807	                    await self._perform(
  3808	                        state, turn_rt, action, send=send, edit=edit, delete=delete
  3809	                    )
  3810	            # End of turn: flush any trailing coalesced status line, then DELETE the
  3811	            # transient status message ("💭 Claude is thinking…") so a stale thinking-line
  3812	            # never lingers after the turn's real content. Best-effort (RB1): a failed delete
  3813	            # must never kill the turn — the content is already sent. Optional `delete` so
  3814	            # existing callers that don't pass one keep working (the status line just stays).
  3815	            for action in coalescer.flush().actions:
  3816	                await self._perform(
  3817	                    state, turn_rt, action, send=send, edit=edit, delete=delete
  3818	                )
  3819	        finally:
  3820	            # T4-review: ALWAYS clear this project's transient status line + set status idle,
  3821	            # even if the loop above raised mid-stream — so a concurrent project is never
  3822	            # left reading a stale running/awaiting_* status and the "💭 thinking…" line is
  3823	            # never orphaned. On the clean path this is the same cleanup that used to follow
  3824	            # the loop; on the exception path it is the safety net (then the exception
  3825	            # propagates to handle_message, whose ``async with`` releases the per-project
  3826	            # lock — the chat stays usable, RB1).
  3827	            if delete is not None and turn_rt.status_message_id is not None:
  3828	                try:
  3829	                    await delete(message_id=turn_rt.status_message_id)
  3830	                except Exception:
  3831	                    log.debug("status-line delete failed at turn end", exc_info=True)
  3832	            turn_rt.status_message_id = None
  3833	            turn_rt.status_text = None
  3834	            # ADR-005 D7: the turn is over → this project is idle again (no runtime → idle is
  3835	            # the /projects default; a running/awaiting project that just ended → idle).
  3836	            turn_rt.status = "idle"
  3837	            # STATUSLINE T-SL-WIRE (B3 fix): the plan turn is over → clear the live plan flag so
  3838	            # the turn-end render (below) and every idle refresh show 🔒 gate/yolo again, not a
  3839	            # lingering 🔒 plan. Cleared BEFORE the turn-end statusline trigger. (A freshly-armed
  3840	            # /plan for the NEXT turn re-shows 🔒 plan via the command refresh's ``plan_next``.)
  3841	            turn_rt.in_plan_turn = False
  3842	            # STATUSLINE T-SL-WIRE (design §3.1): turn END → flip the working ⚙️ marker OFF and
  3843	            # refresh ctx % (the context just grew, and the engine is still alive here — its
  3844	            # teardown for a driver_error/resume-failure happens AFTER this finally — so
  3845	            # _statusline_text's engine.context_percentage() reads the fresh figure). FOREGROUND-
  3846	            # ONLY (``turn_name``) so a background turn's end never stomps the foreground line.
  3847	            # In the finally + fully best-effort (RB1), so it fires on EVERY exit path (clean
  3848	            # end, mid-stream raise, cancel) and can never mask the turn's own exception.
  3849	            await self._maybe_update_statusline(
  3850	                chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=turn_name,
  3851	            )
  3852	            # ADR-005 D3: drop any pending-index entries this turn's project left open (an
  3853	            # ask/plan/permission the operator never answered — the engine has stopped
  3854	            # awaiting it now the stream drained / the turn died, so a late tap on it is a
  3855	            # stale-id no-op). In the finally so a mid-stream raise can't leak a project's
  3856	            # index entries either. Scoped to THIS turn's project so a concurrent project's
  3857	            # still-open holds survive (T5); an in-flight free-text capture aimed at one of
  3858	            # them is cleared with it. Pure + no await, so it can't itself raise here.
  3859	            self._clear_project_pending(state, turn_name)
  3860	

exec
/bin/zsh -lc "nl -ba tests/test_stream_session.py | sed -n '7910,8130p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
  7910	
  7911	
  7912	async def test_ctx_percentage_is_awaited_end_to_end_via_async_engine():
  7913	    # ⭐ B1 (make-or-break): the statusline body reads the ctx % via an AWAITED async
  7914	    # engine.context_percentage(). FakeEngine.context_percentage is now async; if the session
  7915	    # ever stopped awaiting it, the line would show "ctx —" and this FAILS. Proves the headline
  7916	    # SDK percentage actually reaches the rendered line through the awaited chain.
  7917	    session = make_session(FakeEngine([]))
  7918	    eng = FakeEngine([], ctx_pct=37)
  7919	    _prime_statusline_project(session, engine=eng, status="running")
  7920	    rec = StatuslineRecorder()
  7921	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  7922	    assert "🧠 ctx 37%" in rec.sends[0]["text"], "the awaited async ctx % must reach the line (B1)"
  7923	
  7924	
  7925	async def test_statusline_text_is_async_and_awaits_ctx():
  7926	    # B1 at the builder level: _statusline_text is a coroutine that awaits the async ctx source.
  7927	    session = make_session(FakeEngine([]))
  7928	    eng = FakeEngine([], ctx_pct=21)
  7929	    _prime_statusline_project(session, engine=eng, status="idle")
  7930	    body = await session._statusline_text(1)
  7931	    assert body is not None and "🧠 ctx 21%" in body
  7932	
  7933	
  7934	def _two_project_statusline_session(tmp_path):
  7935	    """A real-store session with alpha (active) + beta, each engine ready for a statusline read."""
  7936	    from claude_tg.session_store import JsonSessionStore
  7937	
  7938	    store = JsonSessionStore(tmp_path / "state.json")
  7939	    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
  7940	    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
  7941	    (tmp_path / "a").mkdir()
  7942	    (tmp_path / "b").mkdir()
  7943	    eng = FakeEngine([], ctx_pct=5)
  7944	    cfg = make_config(allowed_roots=(str(tmp_path),), allow_any_path=False)
  7945	    session = StreamingSession(
  7946	        cfg, session_store=store,
  7947	        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
  7948	        clock=lambda: 0.0,
  7949	    )
  7950	    session._runtime(1, "alpha", str(tmp_path / "a")).engine = eng
  7951	    session._runtime(1, "beta", str(tmp_path / "b")).engine = eng
  7952	    return session, store
  7953	
  7954	
  7955	async def test_switch_after_snapshot_writes_current_line_not_stale(tmp_path):
  7956	    # ⭐⭐ B2 (the foreground-switch race): _update_statusline snapshots the body, THEN the gated
  7957	    # send awaits — a /switch in that window must NOT write the stale previous-project line. The
  7958	    # fix REBUILDS the body from current state right before the write. We wrap _statusline_text
  7959	    # so the SWITCH lands between the snapshot (1st call) and the rebuild (2nd call) — exactly the
  7960	    # race window — and assert the line that LANDS names the NEW project (beta), not alpha.
  7961	    #
  7962	    # MUTATION PROBE: revert the rebuild-after-wait and the SEND carries alpha (the snapshot) →
  7963	    # this FAILS (it requires beta, the post-switch foreground).
  7964	    session, store = _two_project_statusline_session(tmp_path)
  7965	    real_text = session._statusline_text
  7966	    calls = {"n": 0}
  7967	
  7968	    async def racing_text(chat_id):
  7969	        calls["n"] += 1
  7970	        body = await real_text(chat_id)  # 1st call → alpha (the snapshot); 2nd → beta (rebuild)
  7971	        if calls["n"] == 1:
  7972	            # The snapshot read just returned alpha; a /switch lands BEFORE the rebuild read.
  7973	            store.switch(1, "beta")
  7974	        return body
  7975	
  7976	    session._statusline_text = racing_text
  7977	    rec = Recorder()
  7978	    pins = PinRecorder()
  7979	    await session._update_statusline(
  7980	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  7981	    )
  7982	    assert calls["n"] >= 2, "the body must be REBUILT after the snapshot (B2)"
  7983	    sl_sends = _statusline_sends(rec)
  7984	    assert sl_sends, "the line was sent"
  7985	    assert "📁 beta" in sl_sends[0]["text"], "B2: the line names the POST-switch foreground (beta)"
  7986	    assert "📁 alpha" not in sl_sends[0]["text"], "B2: never the stale pre-switch project (alpha)"
  7987	
  7988	
  7989	async def test_switch_after_snapshot_on_edit_writes_current_line(tmp_path):
  7990	    # B2 on the EDIT path: an established line, then a /switch between the edit's snapshot and its
  7991	    # rebuild → the now-current project (beta) is edited in, never the stale snapshot (alpha).
  7992	    session, store = _two_project_statusline_session(tmp_path)
  7993	    # alpha starts RUNNING so its first pinned line differs from the idle line the 2nd update
  7994	    # builds → the 2nd update reaches the EDIT path (not the identical-text skip).
  7995	    session._runtime(1, "alpha", str(tmp_path / "a")).status = "running"
  7996	    rec = Recorder()
  7997	    pins = PinRecorder()
  7998	    # Establish a pinned line on alpha first (no racing wrapper yet).
  7999	    await session._update_statusline(
  8000	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  8001	    )
  8002	    assert "📁 alpha" in _statusline_sends(rec)[0]["text"]
  8003	    # Now wrap _statusline_text so a /switch lands between the edit's snapshot and its rebuild.
  8004	    real_text = session._statusline_text
  8005	    calls = {"n": 0}
  8006	
  8007	    async def racing_text(chat_id):
  8008	        calls["n"] += 1
  8009	        body = await real_text(chat_id)
  8010	        if calls["n"] == 1:
  8011	            store.switch(1, "beta")  # switch AFTER the snapshot read, BEFORE the rebuild
  8012	        return body
  8013	
  8014	    session._statusline_text = racing_text
  8015	    session._runtime(1, "alpha", str(tmp_path / "a")).status = "idle"  # alpha line now differs
  8016	    await session._update_statusline(
  8017	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  8018	    )
  8019	    sl_edits = _statusline_edits(rec)
  8020	    assert sl_edits, "an edit happened"
  8021	    assert "📁 beta" in sl_edits[-1]["text"], "B2 (edit): the now-current project is written"
  8022	    assert "📁 alpha" not in sl_edits[-1]["text"]
  8023	
  8024	
  8025	async def test_plan_turn_shows_plan_mode_while_running_then_gate(tmp_path):
  8026	    # ⭐ B3: during an ACTUAL plan-mode turn the line shows 🔒 plan (not 🔒 gate). plan_next is
  8027	    # consumed by handle_message BEFORE _drive_turn, so the live flag is in_plan_turn (set at
  8028	    # turn start from the consumed plan_turn, cleared at turn end). Turn start → plan; end → gate.
  8029	    #
  8030	    # MUTATION PROBE: if _statusline_text still read only plan_next (consumed → False), the turn
  8031	    # would show 🔒 gate and this FAILS.
  8032	    engine = FakeEngine(
  8033	        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="planned")],
  8034	        ctx_pct=8,
  8035	    )
  8036	    session = make_session(engine)
  8037	    name, rt = session._active_runtime(1, create_default=True)
  8038	    rt.engine = engine
  8039	    rec = Recorder()
  8040	    pins = PinRecorder()
  8041	    state = session._chat(1)
  8042	    # Drive a PLAN turn (plan_turn=True — the value handle_message would pass after consuming
  8043	    # the one-shot plan_next).
  8044	    await asyncio.wait_for(
  8045	        session._drive_turn(
  8046	            state, 1, engine, "go",
  8047	            send=rec.send, edit=rec.edit, delete=rec.delete,
  8048	            pin=pins.pin, unpin=pins.unpin, target=(name, rt), plan_turn=True,
  8049	        ),
  8050	        timeout=2.0,
  8051	    )
  8052	    sl_sends = _statusline_sends(rec)
  8053	    sl_edits = _statusline_edits(rec)
  8054	    # Turn START line → 🔒 plan (the live plan turn).
  8055	    assert sl_sends and "🔒 plan" in sl_sends[0]["text"], "B3: a running plan turn shows 🔒 plan"
  8056	    # Turn END line → back to 🔒 gate (in_plan_turn cleared; plan_next was already consumed).
  8057	    assert sl_edits and "🔒 gate" in sl_edits[-1]["text"], "B3: after the plan turn → 🔒 gate"
  8058	    # The live flag is cleared after the turn (no lingering plan mode).
  8059	    assert rt.in_plan_turn is False
  8060	
  8061	
  8062	async def test_non_plan_turn_does_not_show_plan_mode():
  8063	    # B3 complement: a NORMAL turn (plan_turn=False) never shows 🔒 plan — it shows 🔒 gate.
  8064	    engine = FakeEngine(
  8065	        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
  8066	        ctx_pct=8,
  8067	    )
  8068	    session = make_session(engine)
  8069	    name, rt = session._active_runtime(1, create_default=True)
  8070	    rt.engine = engine
  8071	    rec = Recorder()
  8072	    pins = PinRecorder()
  8073	    state = session._chat(1)
  8074	    await asyncio.wait_for(
  8075	        session._drive_turn(
  8076	            state, 1, engine, "go",
  8077	            send=rec.send, edit=rec.edit, delete=rec.delete,
  8078	            pin=pins.pin, unpin=pins.unpin, target=(name, rt),  # plan_turn defaults False
  8079	        ),
  8080	        timeout=2.0,
  8081	    )
  8082	    for s in _statusline_sends(rec):
  8083	        assert "🔒 plan" not in s["text"]
  8084	    assert "🔒 gate" in _statusline_sends(rec)[0]["text"]
  8085	    assert rt.in_plan_turn is False
  8086	
  8087	
  8088	async def test_pin_fails_then_retried_on_next_update():
  8089	    # ⭐ Pin-retry (non-blocking): the SEND succeeds but the first PIN raises → the line is sent
  8090	    # + tracked but UNPINNED (statusline_pinned False). A later update RETRIES the pin even when
  8091	    # the text is unchanged — so a transient pin failure self-heals instead of sticking unpinned.
  8092	    #
  8093	    # MUTATION PROBE: without the retry, the identical-text skip would short-circuit and the
  8094	    # second update would NOT pin (pins stays length 1) → this FAILS.
  8095	    session = make_session(FakeEngine([]))
  8096	    eng = FakeEngine([], ctx_pct=6)
  8097	    _prime_statusline_project(session, engine=eng, status="running")
  8098	    rec = StatuslineRecorder(fail_pin_times=1)  # the FIRST pin raises, later pins succeed
  8099	    # First update: send ok, pin raises → tracked but not pinned.
  8100	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  8101	    assert len(rec.sends) == 1
  8102	    assert len(rec.pins) == 1, "the first pin was attempted (and raised)"
  8103	    state = session._chat(1)
  8104	    assert state.statusline_message_id is not None
  8105	    assert state.statusline_pinned is False, "a failed pin leaves the line UNPINNED"
  8106	    # Second update with IDENTICAL state: must RETRY the pin (not skip past the unpinned state).
  8107	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  8108	    assert len(rec.sends) == 1, "no re-send (the line is already sent)"
  8109	    assert len(rec.pins) == 2, "the pin was RETRIED on the next update (pin-retry fix)"
  8110	    assert rec.pins[-1]["message_id"] == state.statusline_message_id
  8111	    assert state.statusline_pinned is True, "the retry succeeded → now pinned"
  8112	    # A THIRD identical update is now a true no-op (pinned + identical → skip).
  8113	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  8114	    assert len(rec.pins) == 2, "once pinned, an identical update skips (no needless re-pin)"
  8115	
  8116	
  8117	async def test_successful_pin_sets_pinned_flag():
  8118	    # The happy path of the pin-retry bookkeeping: a successful first pin sets statusline_pinned
  8119	    # True so subsequent identical updates correctly skip.
  8120	    session = make_session(FakeEngine([]))
  8121	    eng = FakeEngine([], ctx_pct=6)
  8122	    _prime_statusline_project(session, engine=eng, status="running")
  8123	    rec = StatuslineRecorder()
  8124	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  8125	    assert session._chat(1).statusline_pinned is True
  8126	    assert len(rec.pins) == 1

codex
The plan-turn and pin-retry wiring matches the claimed shape in code: `plan_next` is consumed before `_drive_turn`, `in_plan_turn` is set before the start refresh and cleared before the end refresh; pin retry checks `statusline_pinned` before the identical-text skip. I’m running the focused tests now, but B2 still looks suspect because the ctx await happens inside the “rebuilt” body read.
exec
/bin/zsh -lc 'pytest -q tests/test_engine.py::test_context_percentage_live_client_returns_rounded_percentage tests/test_engine.py::test_context_percentage_uses_live_call_over_usage_fallback tests/test_engine.py::test_context_percentage_raising_client_falls_back_to_none_without_usage tests/test_engine.py::test_context_percentage_raising_client_uses_usage_fallback tests/test_engine.py::test_engine_context_percentage_delegates_to_async_substrate tests/test_stream_session.py::test_ctx_percentage_is_awaited_end_to_end_via_async_engine tests/test_stream_session.py::test_switch_after_snapshot_writes_current_line_not_stale tests/test_stream_session.py::test_switch_after_snapshot_on_edit_writes_current_line tests/test_stream_session.py::test_plan_turn_shows_plan_mode_while_running_then_gate tests/test_stream_session.py::test_non_plan_turn_does_not_show_plan_mode tests/test_stream_session.py::test_pin_fails_then_retried_on_next_update tests/test_stream_session.py::test_update_statusline_pin_failure_is_swallowed_turn_unaffected tests/test_stream_session.py::test_background_turn_does_not_rewrite_foreground_statusline tests/test_stream_session.py::test_foreground_turn_among_two_projects_updates_line' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 679ms:
..............                                                           [100%]

codex
The focused regression suite passes: 14 targeted tests covering B1, the added B2 snapshot tests, B3, pin retry, RB1 pin failure, and foreground-only behavior. I’m going one step further with a minimal race probe for the remaining ctx-await window, since passing tests do not exercise a switch during the rebuilt body’s own async ctx read.
exec
/bin/zsh -lc "nl -ba tests/test_stream_session.py | sed -n '80,170p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
    80	        allow_any_path=allow_any_path,
    81	    )
    82	
    83	
    84	# ---------------------------------------------------------------------------
    85	# A scripted fake Engine. resolve()/cancel() record calls; send() yields the
    86	# scripted events and (optionally) PARKS on a "hold" sentinel until resolve fires.
    87	# ---------------------------------------------------------------------------
    88	
    89	HOLD = object()  # sentinel in a script: park send() here until a resolve/cancel arrives
    90	
    91	
    92	class FakeEngine:
    93	    def __init__(self, script: list, *, session_id="sess-1", resolve_result=True, ctx_pct=None):
    94	        self._script = script
    95	        self.session_id = session_id
    96	        # STATUSLINE T-SL-CORE: the ctx % the statusline reads via engine.context_percentage().
    97	        # Default None (→ "ctx —"); a test sets it to assert the figure flows into the line.
    98	        self._ctx_pct = ctx_pct
    99	        self.resolve_calls: list[tuple[str, object]] = []
   100	        self.cancel_calls: list = []
   101	        # P14 T-FIRE: records the ``proactive`` flag passed to each send() (the force-gate
   102	        # signal threaded by _drive_turn) so a fire test can assert it was set.
   103	        self.proactive_calls: list[bool] = []
   104	        self.started = False
   105	        self.resumed: str | None = None
   106	        self.stopped = False
   107	        # What resolve() returns — True = a pending request was resolved (the live
   108	        # path); False simulates a stale/already-decided id (nothing pending).
   109	        self._resolve_result = resolve_result
   110	        # Set when send() parks on a HOLD; resolve()/cancel() set it to release.
   111	        self._gate = asyncio.Event()
   112	
   113	    async def start(self) -> None:
   114	        self.started = True
   115	
   116	    async def resume(self, session_id: str) -> None:
   117	        self.resumed = session_id
   118	        self.started = True
   119	
   120	    async def stop(self) -> None:
   121	        self.stopped = True
   122	
   123	    async def send(self, prompt: str, *, timeout=None, proactive=False, **_kwargs):
   124	        # P14 T-FIRE: ``proactive`` is recorded so a fire test can assert the force-gate flag
   125	        # was threaded into engine.send; ignored otherwise (the FakeEngine doesn't gate). The
   126	        # ``**_kwargs`` absorbs ``images`` (P10) so the fake stays signature-compatible.
   127	        self.proactive_calls.append(proactive)
   128	        for item in self._script:
   129	            if item is HOLD:
   130	                # Park until the operator resolves (mirrors the held can_use_tool).
   131	                await self._gate.wait()
   132	                self._gate.clear()
   133	                continue
   134	            yield item
   135	
   136	    def resolve(self, tool_use_id: str, decision) -> bool:
   137	        self.resolve_calls.append((tool_use_id, decision))
   138	        self._gate.set()
   139	        return self._resolve_result
   140	
   141	    def cancel(self, tool_use_id=None) -> int:
   142	        self.cancel_calls.append(tool_use_id)
   143	        self._gate.set()
   144	        return 1
   145	
   146	    async def context_percentage(self):
   147	        # STATUSLINE T-SL-CORE / T-SL-WIRE (B1): the best-effort ctx % the statusline reads
   148	        # (None → "ctx —"). ASYNC to mirror the real Engine.context_percentage(), which awaits
   149	        # the SDK's coroutine get_context_usage() — so the live awaited path is exercised (a
   150	        # non-awaited regression would fail: awaiting a sync int raises).
   151	        return self._ctx_pct
   152	
   153	
   154	class Recorder:
   155	    """Captures the send/edit/delete calls the driver performs.
   156	
   157	    ``fail_html`` (default off) makes ``send`` raise on a ``parse_mode=="HTML"`` call —
   158	    simulating Telegram rejecting a bad HTML entity — so the driver's plain-text fallback
   159	    can be exercised. The raising send is still recorded (so the attempt is observable).
   160	    """
   161	
   162	    def __init__(self, *, fail_html: bool = False):
   163	        self.sends: list[dict] = []
   164	        self.edits: list[dict] = []
   165	        self.deletes: list[dict] = []
   166	        self._next_id = 100
   167	        self._fail_html = fail_html
   168	
   169	    async def send(self, *, text, reply_markup=None, parse_mode=None, **kwargs) -> int:
   170	        # T6/P9: notification sends pass link_preview_options=LinkPreviewOptions(is_disabled=

exec
/bin/zsh -lc "nl -ba tests/test_stream_session.py | sed -n '7310,7355p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
  7310	
  7311	
  7312	class StatuslineRecorder:
  7313	    """Captures the send/edit/pin/unpin calls _update_statusline performs (with fault injection).
  7314	
  7315	    ``fail_edit`` makes the FIRST edit raise (the orphan-recovery trigger — the operator
  7316	    unpinned/deleted the line). ``fail_pin`` / ``fail_send`` make pin / send raise (the RB1
  7317	    swallow probe). ``fail_pin_times=N`` makes only the first N pins raise (then succeed — the
  7318	    pin-retry probe). Each call is recorded so the sequence + the silent-pin flag are assertable.
  7319	    """
  7320	
  7321	    def __init__(self, *, fail_edit=False, fail_pin=False, fail_send=False, fail_pin_times=0):
  7322	        self.sends: list[dict] = []
  7323	        self.edits: list[dict] = []
  7324	        self.pins: list[dict] = []
  7325	        self.unpins: list[dict] = []
  7326	        self._next_id = 500
  7327	        self._fail_edit_first = fail_edit
  7328	        self._fail_pin = fail_pin
  7329	        self._fail_send = fail_send
  7330	        self._fail_pin_remaining = fail_pin_times
  7331	
  7332	    async def send(self, *, text, reply_markup=None, parse_mode=None, **kwargs) -> int:
  7333	        self.sends.append({"text": text, "parse_mode": parse_mode})
  7334	        if self._fail_send:
  7335	            raise RuntimeError("Telegram error: send failed")
  7336	        self._next_id += 1
  7337	        return self._next_id
  7338	
  7339	    async def edit(self, *, message_id, text, parse_mode=None) -> None:
  7340	        if self._fail_edit_first:
  7341	            self._fail_edit_first = False
  7342	            raise RuntimeError("Telegram BadRequest: message to edit not found")
  7343	        self.edits.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})
  7344	
  7345	    async def pin(self, *, message_id, disable_notification=None) -> None:
  7346	        self.pins.append({"message_id": message_id, "disable_notification": disable_notification})
  7347	        if self._fail_pin:
  7348	            raise RuntimeError("Telegram error: pin failed")
  7349	        if self._fail_pin_remaining > 0:
  7350	            self._fail_pin_remaining -= 1
  7351	            raise RuntimeError("Telegram error: pin failed (transient)")
  7352	
  7353	    async def unpin(self, *, message_id) -> None:
  7354	        self.unpins.append({"message_id": message_id})
  7355	

exec
/bin/zsh -lc "python - <<'PY'
import asyncio, tempfile
from pathlib import Path
from claude_tg.stream_session import StreamingSession
from claude_tg.session_store import JsonSessionStore
from tests.test_stream_session import make_config, StatuslineRecorder

async def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        store = JsonSessionStore(tmp / 'state.json')
        (tmp / 'a').mkdir(); (tmp / 'b').mkdir()
        store.create(1, 'alpha', str(tmp / 'a'), make_active=True)
        store.create(1, 'beta', str(tmp / 'b'), make_active=False)
        class SlowSwitchEngine:
            session_id = 's'
            async def context_percentage(self):
                # This lands after _statusline_text has captured alpha, but before send/edit.
                store.switch(1, 'beta')
                await asyncio.sleep(0)
                return 5
        eng = SlowSwitchEngine()
        session = StreamingSession(
            make_config(allowed_roots=(str(tmp),), allow_any_path=False),
            session_store=store,
            engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
            clock=lambda: 0.0,
        )
        session._runtime(1, 'alpha', str(tmp / 'a')).engine = eng
        session._runtime(1, 'beta', str(tmp / 'b')).engine = eng
        rec = StatuslineRecorder()
        await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
        print(rec.sends[0]['text'] if rec.sends else 'NO_SEND')
        print('active', store.get_active(1))
asyncio.run(main())
PY" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 191ms:
📁 beta · 🤖  · 🧠 ctx 5% · 🔒 gate
active beta

codex
The first probe only switched during the initial snapshot; the new rebuild then naturally saw the switched project, so it did not prove the remaining window. I’m adjusting it to switch during the second `_statusline_text()` call, which is the actual post-gate rebuild used for the send.
exec
/bin/zsh -lc "python - <<'PY'
import asyncio, tempfile
from pathlib import Path
from claude_tg.stream_session import StreamingSession
from claude_tg.session_store import JsonSessionStore
from tests.test_stream_session import make_config, StatuslineRecorder

async def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        store = JsonSessionStore(tmp / 'state.json')
        (tmp / 'a').mkdir(); (tmp / 'b').mkdir()
        store.create(1, 'alpha', str(tmp / 'a'), make_active=True)
        store.create(1, 'beta', str(tmp / 'b'), make_active=False)
        class SlowSwitchEngine:
            session_id = 's'
            def __init__(self): self.calls = 0
            async def context_percentage(self):
                self.calls += 1
                if self.calls == 2:
                    # During the rebuilt body read, after it captured alpha, before send.
                    store.switch(1, 'beta')
                    await asyncio.sleep(0)
                return 5
        eng = SlowSwitchEngine()
        session = StreamingSession(
            make_config(allowed_roots=(str(tmp),), allow_any_path=False),
            session_store=store,
            engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
            clock=lambda: 0.0,
        )
        session._runtime(1, 'alpha', str(tmp / 'a')).engine = eng
        session._runtime(1, 'beta', str(tmp / 'b')).engine = eng
        rec = StatuslineRecorder()
        await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
        print(rec.sends[0]['text'] if rec.sends else 'NO_SEND')
        print('active', store.get_active(1), 'ctx_calls', eng.calls)
asyncio.run(main())
PY" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 136ms:
📁 alpha · 🤖  · 🧠 ctx 5% · 🔒 gate
active beta ctx_calls 2

codex
The adjusted probe confirms a real stale write: during the second, post-gate body read, a switch inside `engine.context_percentage()` leaves the active project as beta while the sent statusline still says alpha. I’m doing one broader pass over the affected test files to make sure the other invariants are not hiding obvious regressions.
exec
/bin/zsh -lc 'pytest -q tests/test_engine.py tests/test_stream_session.py' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 1364ms:
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 65%]
........................................................................ [ 87%]
........................................                                 [100%]

codex
`tests/test_engine.py` and `tests/test_stream_session.py` both pass completely, including the existing RB1, foreground-only, and statusline-specific regression coverage. I’m running the bot-facing streaming tests as a final check for command/default-turn wiring regressions.
exec
/bin/zsh -lc 'pytest -q tests/test_bot_streaming.py' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 801ms:
........................................................................ [ 24%]
........................................................................ [ 49%]
........................................................................ [ 74%]
........................................................................ [ 98%]
...                                                                      [100%]

codex
The bot streaming tests also pass. I’m checking the dedicated concurrency matrix and render/body-free tests now, since those are the closest coverage for foreground-only behavior and SB3.
exec
/bin/zsh -lc 'pytest -q tests/test_concurrency_matrix.py' in /Users/ray/dev/claude-telegram-bot-statusline
exec
/bin/zsh -lc 'pytest -q tests/test_render.py tests/test_session_mirror.py tests/test_security_reliability.py' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 304ms:
......................                                                   [100%]

 succeeded in 394ms:
........................................................................ [ 25%]
........................................................................ [ 51%]
........................................................................ [ 77%]
...............................................................          [100%]

codex
## Blocker status
- B1 (ctx await): CLOSED — SDK `get_context_usage()` is awaited; live percentage wins over usage fallback; errors/no-client fall back to usage or `None`.
- B2 (foreground race): STILL-OPEN — body is rebuilt after the gate wait, but `_statusline_text()` captures the foreground before awaiting async ctx; a `/switch` during that await can still send/edit the stale previous-project line.
- B3 (/plan mode): CLOSED — `in_plan_turn` is set before the turn-start statusline refresh and cleared before the turn-end refresh, so `🔒 plan` shows only during the plan turn.
- NB (pin retry): CLOSED — failed pin leaves `statusline_pinned=False`, and the next identical-text update retries pin before the no-op skip.
## Any new issues
- none beyond the remaining B2 await-straddle
## Verdict
NO_SHIP
## Reasoning
Targeted and broader regression tests pass, including engine/session/bot streaming/concurrency/render/security suites. But a direct race probe confirms the active project can switch during the rebuilt body’s ctx await, leaving active `beta` while the written statusline still says `alpha`; that is the same class of stale foreground write B2 was meant to close.
tokens used
139,235
## Blocker status
- B1 (ctx await): CLOSED — SDK `get_context_usage()` is awaited; live percentage wins over usage fallback; errors/no-client fall back to usage or `None`.
- B2 (foreground race): STILL-OPEN — body is rebuilt after the gate wait, but `_statusline_text()` captures the foreground before awaiting async ctx; a `/switch` during that await can still send/edit the stale previous-project line.
- B3 (/plan mode): CLOSED — `in_plan_turn` is set before the turn-start statusline refresh and cleared before the turn-end refresh, so `🔒 plan` shows only during the plan turn.
- NB (pin retry): CLOSED — failed pin leaves `statusline_pinned=False`, and the next identical-text update retries pin before the no-op skip.
## Any new issues
- none beyond the remaining B2 await-straddle
## Verdict
NO_SHIP
## Reasoning
Targeted and broader regression tests pass, including engine/session/bot streaming/concurrency/render/security suites. But a direct race probe confirms the active project can switch during the rebuilt body’s ctx await, leaving active `beta` while the written statusline still says `alpha`; that is the same class of stale foreground write B2 was meant to close.
