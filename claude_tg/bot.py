"""Telegram transport: routes allowlisted messages to Claude and replies."""

from __future__ import annotations

import asyncio
import html
import logging
import time

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .claude_runner import ClaudeBusy, ClaudeRunner
from .config import Config
from .paths import PathNotAllowed, resolve_within_roots
from .render import (
    BODY_FREE_ERROR_LINE,
    code_path,
    free_text_prompt,
    project_status_label,
    yolo_banner,
)
from .session_store import (
    DuplicateProject,
    InvalidProjectName,
    UnknownProject,
    validate_project_name,
)
from .stream_session import StreamingBusy, StreamingSession
from .util import _redact_sid_in_text, expand_macro, split_message

log = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 *Claude Code remote*\n\n"
    "Just send me a message and I'll run it through Claude Code on the Mac and reply.\n\n"
    "Commands:\n"
    "/help — this help\n"
    "/reset — start a fresh Claude session (forget context)\n"
    "/cancel [name|all] — abort the in-flight run: the active project, a named project, "
    "or every running/queued project (streaming mode)\n"
    "/to <name> <text> — send a free-text answer/feedback to a named project's pending "
    "“Other”/“Reject” prompt (streaming mode; or just reply to the prompt)\n"
    "/yolo — run every tool with NO approval prompt this session (streaming mode)\n"
    "/unyolo — restore the per-tool permission gate (streaming mode)\n"
    "/fast — use the fast model (Haiku) for this project's next turn (streaming mode)\n"
    "/deep — use the deep model (Opus) for this project's next turn (streaming mode)\n"
    "/auto — clear the model override, back to the default (streaming mode)\n"
    "/projects — list your projects and which one is active (streaming mode)\n"
    "/new <name> <path> — create a project at <path> and switch to it; <path> must be an "
    "existing directory inside the permitted roots (streaming mode)\n"
    "/switch <name> — switch the active project; the next message resumes it (streaming mode)\n"
    "/rm <name> — drop a project from the registry, leaving its transcript on disk (streaming mode)\n"
    "/pwd — show the active project's working directory\n"
    "/cd <path> — change the working directory (one-shot mode only; in streaming mode "
    "the cwd is fixed per project — use /new to work elsewhere)\n"
    "/save <name> <text> — save a reusable prompt template (macro)\n"
    "/run <name> [args…] — run a saved macro (expands $1 $2 … and $* = all args)\n"
    "/macros — list your saved macros\n"
    "/unsave <name> — remove a saved macro\n"
    "\nAny *other* slash-command (e.g. /grill, /pipeline, /scaffold) is forwarded "
    "verbatim and runs as a skill in the Claude session.\n"
)

#: T1 (P9): the native Telegram ``/`` command menu — ``set_my_commands`` is called with
#: this list at startup so the commands are discoverable without the wall-of-text /help.
#: Each entry is ``(command, one-line description)``. **Invariant (tested):** this list
#: MUST stay in lock-step with the CommandHandlers registered in :meth:`build_application`
#: — no documented-but-unregistered command, no registered command missing here. The
#: ``start`` alias of ``/help`` is intentionally NOT listed (Telegram treats /start
#: specially and a duplicate menu row is noise). Descriptions are concise (Telegram
#: truncates long ones) and secret-free.
COMMAND_MENU: tuple[tuple[str, str], ...] = (
    ("help", "Show the help text"),
    ("status", "Health: uptime, mode, runs, per-project status + cost"),
    ("reset", "Start a fresh Claude session (forget context)"),
    ("cancel", "Abort the in-flight run (active / a name / all)"),
    ("to", "Send a free-text answer to a named project's prompt"),
    ("yolo", "Run every tool with NO approval prompt this session"),
    ("unyolo", "Restore the per-tool permission gate"),
    ("fast", "Use the fast model (Haiku) for this project's next turn"),
    ("deep", "Use the deep model (Opus) for this project's next turn"),
    ("auto", "Clear the model override (back to the default)"),
    ("model", "Clear the model override (alias of /auto)"),
    ("projects", "List your projects and which one is active"),
    ("new", "Create a project at a path and switch to it"),
    ("switch", "Switch the active project"),
    ("rm", "Drop a project from the registry"),
    ("pwd", "Show the active project's working directory"),
    ("cd", "Change the working directory (one-shot mode only)"),
    ("save", "Save a reusable prompt template: /save <name> <text>"),
    ("run", "Run a saved macro: /run <name> [args…]"),
    ("macros", "List your saved macros"),
    ("unsave", "Remove a saved macro: /unsave <name>"),
)


def _format_uptime(seconds: float) -> str:
    """A compact human uptime like ``3d 4h 5m`` / ``5m 12s`` / ``8s`` (T2 /status).

    Pure + defensive (RB1): a negative/odd value floors at ``0s``. Shows the two most
    significant non-zero units (days→hours→minutes→seconds) so the line stays short; under
    a minute it shows whole seconds. No I/O.
    """
    total = int(seconds) if seconds and seconds > 0 else 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class TelegramClaudeBot:
    def __init__(
        self,
        config: Config,
        runner: ClaudeRunner,
        *,
        streaming: StreamingSession | None = None,
    ):
        self.config = config
        self.runner = runner
        # S4 switch: the streaming collaborator is constructed (by main.py) ONLY when
        # ENGINE_MODE=streaming. In oneshot mode it is None and EVERY path below behaves
        # exactly as before — the live one-shot bot is untouched until the owner flips
        # the flag. Streaming-mode methods delegate to this driver.
        self.streaming = streaming if config.engine_mode == "streaming" else None
        # T2 (P9): process start time for the /status uptime line (monotonic-independent
        # wall reference; uptime is a human-facing duration so wall time is fine here).
        self._start_time = time.time()
        # T1 (P9): one-time first-run onboarding. A chat whose first-ever message fires the
        # welcome once; the chat id is recorded here so subsequent messages do NOT re-welcome.
        # CAVEAT: in-memory only (not persisted in the session store) — the welcome re-fires
        # once after a bot restart. That is an accepted trade-off (a single extra welcome is
        # harmless and the registry's per-chat shape is project-scoped, not a natural home for
        # a UX seen-flag); persisting it is a possible later refinement.
        self._welcomed: set[int] = set()

    # ---- auth ---------------------------------------------------------------
    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id in self.config.allowed_chat_ids

    async def _ok(self, update: Update) -> bool:
        if self._authorized(update):
            return True
        chat = update.effective_chat
        log.warning("ignoring update from unauthorized chat %s", chat.id if chat else "?")
        return False

    # ---- commands -----------------------------------------------------------
    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

    async def _maybe_welcome(self, update: Update) -> None:
        """T1 (P9): send the one-time first-run welcome for a chat's FIRST-ever message.

        Gated on :meth:`_authorized` by the caller (a non-allowlisted chat never reaches
        here — SB1), this fires AT MOST ONCE per chat per process: the chat id is recorded
        in :attr:`_welcomed` so every subsequent message is a no-op. The welcome names the
        current ``ENGINE_MODE`` + active cwd and points at the ``/`` menu (a hint, not the
        wall-of-text /help). Best-effort (RB1): a failed send must never break the turn that
        follows it — the message is dispatched regardless. CAVEAT: the seen-set is in-memory,
        so the welcome re-fires once after a restart (accepted — see ``__init__``).
        """
        chat = update.effective_chat
        if chat is None or chat.id in self._welcomed:
            return
        # Mark BEFORE sending so a send failure (or a racing second message under
        # concurrent_updates) cannot double-welcome — at-most-once wins over at-least-once.
        self._welcomed.add(chat.id)
        if update.message is None:
            return
        try:
            # The active cwd: the streaming session / runner both expose get_cwd (read-only;
            # never creates a project). Wrapped in <code> (R6) so its /segments are inert.
            cwd = (
                self.streaming.get_cwd(chat.id)
                if self.streaming is not None
                else self.runner.get_cwd(chat.id)
            )
            mode = html.escape(self.config.engine_mode, quote=False)
            await update.message.reply_text(
                f"👋 <b>Claude Code remote</b> — engine mode <b>{mode}</b>\n"
                f"Working in {code_path(cwd)}\n\n"
                "Send a message, or tap a command — try /projects or /status.",
                parse_mode="HTML",
            )
        except Exception:  # RB1: a failed welcome must never break the message dispatch.
            log.debug("first-run welcome send failed for chat %s", chat.id, exc_info=True)

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """T2 (P9): an at-a-glance health view — uptime, mode, gate/yolo, runs, projects.

        SB1: allowlist-gated like every command (the ``_ok`` recheck). **Body-free (SB3):**
        the reply carries only bot-derived health values — uptime, ``ENGINE_MODE``, the
        permission-gate posture, active-vs-cap run counts, and a per-project line (name +
        :data:`~claude_tg.render.ProjectStatus` label + cwd in ``<code>`` + cumulative
        cost) — never tool input/output or file content. Every interpolated value is
        HTML-escaped (or a fixed/numeric bot value); paths are ``code_path``-wrapped (R6).
        Reuses the ``/projects`` internals (``store.list_projects`` / ``get_active`` +
        ``streaming.project_status``) so it never diverges from that surface. RB1: read-only,
        never crashes on a sparse/odd record (mirrors ``/projects``).
        """
        if not await self._ok(update) or update.message is None:
            return
        chat_id = update.effective_chat.id
        lines = ["📊 <b>Status</b>", f"Uptime: {html.escape(_format_uptime(time.time() - self._start_time))}"]
        lines.append(f"Engine mode: <b>{html.escape(self.config.engine_mode, quote=False)}</b>")
        if self.streaming is not None:
            # Gate / yolo posture for the ACTIVE project (the policy is per-project; read it
            # without mutating — get_yolo is read-only). The gate is ON unless yolo bypasses
            # it; skip_permissions (one-shot bypass) is reported too for completeness.
            yolo = self.streaming.get_yolo(chat_id)
            gate = "OFF (/yolo — all tools auto-allowed)" if yolo else "ON (per-tool approval)"
            lines.append(f"Permission gate: {gate}")
            active_runs = self.streaming.active_run_count()
            cap = self.config.max_concurrent_runs
            lines.append(f"Runs: {active_runs} active / {cap} max concurrent")
            projects = self.streaming.store.list_projects(chat_id) if self.streaming.store else {}
            active = self.streaming.store.get_active(chat_id) if self.streaming.store else None
            if projects:
                lines.append("Projects:")
                for name, record in projects.items():
                    rec = record if isinstance(record, dict) else {}
                    marker = "→" if name == active else "  "
                    cwd = rec.get("cwd")
                    cwd_html = code_path(cwd) if cwd else "(no path)"
                    status = project_status_label(self.streaming.project_status(chat_id, name))
                    # T4 (P9): surface the per-project model override on the status line. Only
                    # shown when an explicit /fast·/deep override is set (no override → the
                    # configured default, omitted to keep the line short). Read-only; a missing
                    # field reads as no override (RB1). The id is a config/SDK constant; escape
                    # it defensively for the HTML message.
                    model = self.streaming.store.get_model(chat_id, name) if self.streaming.store else None
                    model_html = f" · <code>{html.escape(model, quote=False)}</code>" if model else ""
                    cost = self.streaming.store.get_cost(chat_id, name) if self.streaming.store else 0.0
                    cost_html = f" · ${cost:.2f}" if cost > 0 else ""
                    lines.append(
                        f"{marker} <b>{html.escape(str(name), quote=False)}</b> — "
                        f"{cwd_html} ({status}){model_html}{cost_html}"
                    )
            else:
                lines.append("Projects: none yet — /new &lt;name&gt; &lt;path&gt;")
        else:
            # One-shot mode: the gate posture is the skip_permissions opt-out; no per-project
            # registry / concurrency (each message runs to completion against one session).
            gate = (
                "OFF (CLAUDE_SKIP_PERMISSIONS — all tools auto-allowed)"
                if self.config.skip_permissions
                else "ON (CLI approval prompt)"
            )
            lines.append(f"Permission gate: {gate}")
            lines.append(f"Working dir: {code_path(self.runner.get_cwd(chat_id))}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_reset(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        chat_id = update.effective_chat.id
        # D4/B1: in streaming mode reset ONLY the streaming session. Do NOT also call
        # runner.reset — the runner's _persist writes its flat-view cwd (seeded at
        # startup, never updated by /switch) onto the chat's *active* project, so after
        # restart→/switch→/reset it would clobber the active project's cwd with the
        # runner's stale value (corrupting D4/D5). Branch on streaming vs one-shot so each
        # mode resets only its own state; the reply is unchanged.
        if self.streaming is not None:
            # B5 / P5 (ADR-005 D2): refuse /reset only while the ACTIVE project's OWN turn
            # is in flight. reset() drops the active project's engine; doing that while that
            # project is mid-turn would ORPHAN its parked answer-hold — the engine reference
            # is gone, so neither a tap nor /cancel could reach it (wedging the turn until
            # the 60-min backstop). While the active project is busy its engine is still
            # live, so /cancel genuinely recovers — tell the operator to use it first.
            #
            # The guard is now PER-PROJECT (is_busy(chat_id, active)), not "any project
            # busy": once /switch is free a BACKGROUND run in another project must NOT block
            # resetting an IDLE active project (reset only touches the active project's
            # session, so a concurrent project's run is irrelevant). With no active project
            # there is nothing busy and nothing to reset — reset() is a clean no-op.
            active = self.streaming.store.get_active(chat_id) if self.streaming.store else None
            if active is not None and self.streaming.is_busy(chat_id, active):
                await update.message.reply_text(
                    "⏳ A turn is in flight — /cancel it first, then /reset."
                )
                return
            self.streaming.reset(chat_id)
        else:
            self.runner.reset(chat_id)
        await update.message.reply_text("🔄 Fresh Claude session started.")

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Abort a run cleanly (RB4/D9). Streaming mode only; oneshot is a no-op.

        **Concurrency-aware target (P5 / ADR-005 D9):**

        * ``/cancel`` (no arg) → the **active** project's run.
        * ``/cancel <name>`` → **that** project's run (case-insensitive, like the store).
        * ``/cancel all`` → **every** running/queued project for the chat.

        Delegates to ``streaming.handle_cancel(chat_id, name)`` which cancels a RUNNING
        engine AND drains a QUEUED-not-yet-running project's parked waiter (no zombie run —
        D9), returning the count of pending requests aborted. ``all`` is a reserved arg
        (a project can never be named ``all`` — SB4 allows it lexically, but the cancel-all
        intent wins; the help spells this out). SB1: allowlist-gated like every command.
        """
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is None:
            await update.message.reply_text(
                "Nothing to cancel — one-shot mode runs each message to completion."
            )
            return
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        # No arg → active project (name=None); "all" → every run; else the named project.
        name = arg or None
        # NB1: handle_cancel returns the count of CANCELLED UNITS — pending requests the
        # engine aborted PLUS any drained queued-not-yet-running turn. A queued-only cancel
        # therefore returns >= 1 (0 pending requests, but a turn WAS cancelled), so the
        # operator is no longer wrongly told "nothing was in flight" for a turn they killed.
        cancelled = self.streaming.handle_cancel(update.effective_chat.id, name)
        if cancelled:
            await update.message.reply_text(f"🛑 Cancelled ({cancelled} aborted).")
        elif name and name.casefold() != "all":
            # A named target with nothing to cancel — not running and not queued. A clear,
            # honest message (NB1: a drained queued turn would have counted above, so reaching
            # here means the project really had no in-flight or queued turn).
            await update.message.reply_text(
                f"Nothing in flight to cancel for {name} (it may have already finished)."
            )
        else:
            await update.message.reply_text("Nothing in flight to cancel.")

    async def cmd_yolo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Turn ON ``/yolo`` — every tool runs with NO approval prompt this session (P2, D6).

        Streaming mode only (the permission gate is a streaming-engine concept; one-shot
        has no per-tool gating). Mirrors :meth:`cmd_cancel`: the ``_ok`` allowlist guard
        first, then delegate to the session. The reply is the LOUD enable banner
        (``render.yolo_banner`` — carries the ``⚠️`` glyph) so allow-all is never silent
        at toggle time; the session keeps it loud throughout each turn (D6).
        """
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is None:
            await update.message.reply_text(
                "Permission gating (and /yolo) applies to streaming mode only — "
                "one-shot mode has no per-tool approval prompts."
            )
            return
        self.streaming.set_yolo(update.effective_chat.id, True)
        await update.message.reply_text(yolo_banner())

    async def cmd_unyolo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Turn OFF ``/yolo`` — restore the fail-closed per-tool permission gate (P2, D6).

        Streaming mode only (mirrors :meth:`cmd_yolo`). After this, risky tools are held
        for approval again. A clear confirmation so the operator knows gating is back on.
        """
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is None:
            await update.message.reply_text(
                "Permission gating (and /yolo) applies to streaming mode only — "
                "one-shot mode has no per-tool approval prompts."
            )
            return
        self.streaming.set_yolo(update.effective_chat.id, False)
        await update.message.reply_text(
            "✅ Gating restored — risky tools will ask for approval again (/yolo is off)."
        )

    async def _set_model(
        self, update: Update, label: str, model: str | None
    ) -> None:
        """Shared body for ``/fast`` · ``/deep`` · ``/auto`` (T4 / P9; streaming mode only).

        Sets (or clears, for ``/auto``) the ACTIVE project's per-project model override and
        confirms. SB1: the caller did the ``_ok`` recheck. The override is persisted on the
        active project (atomic + ``0600``, RB6) and **applies on the next turn/session** — a
        project with a live session keeps its current model until that session ends (model is
        a session-creation param; never hot-swapped mid-turn). One-shot mode has no per-project
        registry, so the model toggles apply to the streaming engine only. ``model`` is a fixed,
        operator-chosen id (a config/SDK constant), never interpolated into a shell command.
        """
        if self.streaming is None:
            await update.message.reply_text(
                "Model routing (/fast · /deep · /auto) applies to streaming mode only."
            )
            return
        chosen = self.streaming.set_model(update.effective_chat.id, model)
        if chosen is None:
            await update.message.reply_text(
                "🔧 Model set to default — your next turn uses the configured default. "
                "(Applies to the next session; a turn in flight keeps its current model.)"
            )
        else:
            # ``chosen`` is a fixed model id (config constant); escape defensively for HTML.
            await update.message.reply_text(
                f"🔧 Model set to <b>{label}</b> (<code>{html.escape(chosen, quote=False)}</code>) — "
                "applies to your next turn. A turn in flight keeps its current model.",
                parse_mode="HTML",
            )

    async def cmd_fast(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """``/fast`` — route the active project to the fast model (Haiku) on the next turn."""
        if not await self._ok(update) or update.message is None:
            return
        await self._set_model(update, "fast", self.config.fast_model)

    async def cmd_deep(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """``/deep`` — route the active project to the deep model (Opus) on the next turn."""
        if not await self._ok(update) or update.message is None:
            return
        await self._set_model(update, "deep", self.config.deep_model)

    async def cmd_auto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """``/auto`` (and ``/model default``) — clear the per-project model override.

        Clears the active project's override so the next turn falls back to the configured
        ``CLAUDE_MODEL`` (or the SDK default). ``/model default`` routes here too (registered
        as the ``model`` command); a bare ``/model`` or ``/model <anything-else>`` also clears
        (RB2: any unrecognized arg fails safe to the default rather than guessing a model id).
        """
        if not await self._ok(update) or update.message is None:
            return
        await self._set_model(update, "default", None)

    async def cmd_pwd(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is not None:
            # Streaming mode: cwd is per-project (D4). Report the ACTIVE project's name +
            # its fixed cwd, or a no-active-project hint. get_cwd is read-only (never
            # creates a project), so we check the store for the active name alongside it.
            chat_id = update.effective_chat.id
            active = self.streaming.store.get_active(chat_id) if self.streaming.store else None
            cwd = self.streaming.get_cwd(chat_id)
            if active is None:
                await update.message.reply_text(
                    "No active project yet. Send a message to start one, or /new <name> <path>."
                )
            else:
                # R6: wrap the cwd in <code> so Telegram renders it as monospace, not as a
                # row of tappable fake /segment command-links. The project name is
                # SB4-validated (safe) but bolded for readability; HTML parse mode required.
                await update.message.reply_text(
                    f"📁 <b>{html.escape(active, quote=False)}</b>\n{code_path(cwd)}",
                    parse_mode="HTML",
                )
            return
        await update.message.reply_text(
            f"📁 {code_path(self.runner.get_cwd(update.effective_chat.id))}",
            parse_mode="HTML",
        )

    async def cmd_cd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is not None:
            # D4: in streaming mode a project's cwd is bound to its session for the life of
            # that session (the ADR-001 (session_id, cwd) resume coupling), so it is fixed —
            # to work elsewhere, /new another project. Do NOT touch the runner/store here.
            await update.message.reply_text(
                "📁 The working directory is fixed per project in streaming mode. "
                "Use /new <name> <path> to work in a different directory."
            )
            return
        chat_id = update.effective_chat.id
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if not arg:
            await update.message.reply_text("Usage: /cd <path>")
            return
        # SB2: canonicalize (resolves symlinks AND ..) and confine to ALLOWED_ROOTS
        # BEFORE touching the runner. A path that escapes the permitted roots is
        # refused here and never reaches set_cwd. ALLOW_ANY_PATH=true is the opt-out.
        try:
            target = resolve_within_roots(
                arg,
                cwd=self.runner.get_cwd(chat_id),
                allowed_roots=self.config.allowed_roots,
                allow_any=self.config.allow_any_path,
            )
        except PathNotAllowed:
            # R6: wrap the (operator-supplied) path in <code> so it renders as monospace,
            # not as tappable fake command-links; escape it so a stray &/</> can't break
            # the HTML message or inject a tag. HTML parse mode required.
            await update.message.reply_text(
                f"❌ Path not allowed (outside the permitted roots): {code_path(arg)}",
                parse_mode="HTML",
            )
            return
        try:
            new_cwd = self.runner.set_cwd(chat_id, str(target))
        except NotADirectoryError:
            await update.message.reply_text(
                f"❌ Not a directory: {code_path(arg)}", parse_mode="HTML"
            )
            return
        await update.message.reply_text(
            f"📁 Working directory set to:\n{code_path(new_cwd)}", parse_mode="HTML"
        )

    # ---- multi-project navigation (streaming mode only, P4 / ADR-004) -------
    async def _require_streaming(self, update: Update) -> bool:
        """Reply the streaming-only notice and return False in one-shot mode.

        The multi-project surface (``/projects`` / ``/switch`` / ``/rm``) is a
        streaming-engine concept — one-shot keeps a single implicit session. Callers
        have already done the ``_ok`` recheck; this is the second guard (mirrors
        :meth:`cmd_yolo`'s one-shot notice). ``update.message`` is non-None here.
        """
        if self.streaming is None:
            await update.message.reply_text(
                "Projects apply to streaming mode only — one-shot mode runs each "
                "message against a single session."
            )
            return False
        return True

    async def cmd_projects(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """List the chat's projects with an active marker + run status (streaming mode only).

        Each line shows the active marker, name, cwd, per-project **run status** (P5 /
        ADR-005 D7), and last-active timestamp; the active project (from
        ``store.get_active``) is flagged. The status —
        ``running`` / ``awaiting approval`` / ``awaiting answer`` / ``awaiting plan`` /
        ``queued`` / ``idle`` — is read from the per-project runtime via
        ``streaming.project_status`` (a project with no runtime, e.g. just after restart,
        reads ``idle``) and mapped to its label by ``render.project_status_label`` (the T3
        label map). No projects → tell the operator to ``/new``. RB1: read-only, never
        crashes on a sparse/odd record or an unexpected status value (the label map falls
        back to ``idle``).
        """
        if not await self._ok(update) or update.message is None:
            return
        if not await self._require_streaming(update):
            return
        assert self.streaming is not None
        chat_id = update.effective_chat.id
        projects = self.streaming.store.list_projects(chat_id) if self.streaming.store else {}
        if not projects:
            await update.message.reply_text(
                "No projects yet. Create one with /new <name> <path>."
            )
            return
        active = self.streaming.store.get_active(chat_id) if self.streaming.store else None
        lines = ["📂 <b>Projects</b>:"]
        for name, record in projects.items():
            rec = record if isinstance(record, dict) else {}
            marker = "→" if name == active else "  "
            cwd = rec.get("cwd")
            # R6: the cwd column was the worst auto-linkify offender (every project's path
            # rendered as a row of fake /segment "commands"). Wrap it in <code> (escaped) so
            # it is inert monospace; the name is bolded + escaped (defensive — a hand-edited
            # registry record could carry an odd name). A missing cwd reads "(no path)".
            cwd_html = code_path(cwd) if cwd else "(no path)"
            last = rec.get("last_active") or "—"
            status = project_status_label(self.streaming.project_status(chat_id, name))
            lines.append(
                f"{marker} <b>{html.escape(str(name), quote=False)}</b> — "
                f"{cwd_html} ({status}) (last active {html.escape(str(last), quote=False)})"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_switch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch the chat's active project (streaming mode only).

        **No busy-guard (P5 / ADR-005 D2 — the headline relaxation).** P4 refused
        ``/switch`` while any turn was in flight *only because* the relay routed every
        inbound answer to ``_active_engine``, so flipping ``store.active`` mid-hold stranded
        the parked turn against the wrong engine (ADR-004 D2's deadlock). P5 routes every
        decision-in by its ``tool_use_id`` to the **owning** project (the pending index,
        ADR-005 D3), so switching away no longer strands anything — the prior run keeps
        running in the background and a tap on its prompt still resolves it. ``/switch`` is
        therefore **free while other projects (or this one) are mid-run** — that is the
        point of background concurrency.

        Everything else is unchanged: no arg → usage; unknown name → error listing the
        available names (RB1); before activating, the TARGET project's stored cwd is
        re-validated against the permitted roots (SB2/B2) — an out-of-root (or missing) cwd
        is refused and the active project is left unchanged. On success the active project
        changes and the next message resumes it.
        """
        if not await self._ok(update) or update.message is None:
            return
        if not await self._require_streaming(update):
            return
        assert self.streaming is not None
        chat_id = update.effective_chat.id
        name = " ".join(ctx.args).strip() if ctx.args else ""
        if not name:
            await update.message.reply_text("Usage: /switch <name>")
            return
        if self.streaming.store is None:
            # No STATE_FILE configured → no registry to switch within. RB1: never crash on
            # a streaming + no-persistence deployment (store is None). Mirror the empty
            # /projects notice rather than dereferencing a None store.
            await update.message.reply_text(
                "No projects yet. Create one with /new <name> <path>."
            )
            return
        # Resolve the target record FIRST (case-insensitive). Unknown name → error listing
        # the available names (replaces the old try/except UnknownProject).
        record = self.streaming.store.get_project(chat_id, name)
        if record is None:
            # R6: style the name like /projects (bold), not a Python repr. ``name`` is
            # operator input that FAILED the registry lookup (never SB4-validated), so
            # escape it (defense-in-depth — a hostile name can't break the HTML). The
            # available names are SB4-validated but escape them too, uniformly.
            available = (
                ", ".join(
                    html.escape(n, quote=False)
                    for n in self.streaming.store.list_projects(chat_id)
                )
                or "(none)"
            )
            await update.message.reply_text(
                f"❌ No project named <b>{html.escape(name, quote=False)}</b>. "
                f"Available: {available}",
                parse_mode="HTML",
            )
            return
        # SB2/B2: re-validate the TARGET project's stored cwd against the permitted roots
        # BEFORE activating (the design says re-validate "on switch/resume"; the resume
        # path is the authoritative gate, this closes the switch-time gap + improves UX).
        # Fail-closed: a missing/empty stored cwd is refused rather than crashing, and the
        # active project is left UNCHANGED (store.switch is never called) on any refusal.
        cwd = record.get("cwd")
        if not cwd:
            await update.message.reply_text(
                f"❌ {name} has no recorded directory — re-create it with /new <name> <path>."
            )
            return
        try:
            resolve_within_roots(
                cwd,
                cwd=cwd,
                allowed_roots=self.config.allowed_roots,
                allow_any=self.config.allow_any_path,
            )
        except PathNotAllowed:
            await update.message.reply_text(
                f"❌ {name}'s directory is no longer within the permitted roots — "
                "not switching. Use /new <name> <path> to point it somewhere allowed."
            )
            return
        self.streaming.store.switch(chat_id, name)
        await update.message.reply_text(
            f"✅ Switched to {name} — your next message resumes that project."
        )

    async def cmd_rm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove a project from the registry (streaming mode only).

        No arg → usage. **Refuse if ``name`` resolves to the ACTIVE project** (D5;
        compared case-insensitively against ``store.get_active``) — the operator must
        ``/switch`` away first. **Refuse a currently-RUNNING project too (P5 / ADR-005 D9):**
        a non-active project may have its own in-flight turn now that runs are concurrent;
        tearing down a live engine mid-turn would orphan its parked hold, so the operator
        must ``/cancel`` it first. Unknown name → error. On success the project is dropped
        from the registry (its Claude transcript left on disk) and its in-memory runtime is
        purged — a QUEUED-not-yet-running turn for it is **drained** first (no zombie run,
        D9 — handled inside ``forget_project``). Safe mid-turn for OTHER projects (the active
        + running cases are refused, so the purged runtime is never a live turn's).
        """
        if not await self._ok(update) or update.message is None:
            return
        if not await self._require_streaming(update):
            return
        assert self.streaming is not None
        chat_id = update.effective_chat.id
        name = " ".join(ctx.args).strip() if ctx.args else ""
        if not name:
            await update.message.reply_text("Usage: /rm <name>")
            return
        if self.streaming.store is None:
            # No STATE_FILE → no registry to remove from (RB1: never crash, store is None).
            await update.message.reply_text("No projects to remove.")
            return
        # D5: never remove the active project (case-insensitive — the store matches names
        # case-insensitively, so the guard must too) — switch away first.
        active = self.streaming.store.get_active(chat_id) if self.streaming.store else None
        if active is not None and name.casefold() == active.casefold():
            await update.message.reply_text(
                f"❌ {name} is the active project — /switch to another project first."
            )
            return
        # P5 / ADR-005 D9 (round-3 BLOCKERS 1+2): INFLIGHT-AWARE admission. request_remove
        # refuses ONLY a project running with a live engine (lock held — tearing it down would
        # orphan its parked hold; /cancel first, T9 unchanged). For a QUEUED or TRANSFER-WINDOW
        # turn (no lock, no started engine — nothing to orphan) it returns True AFTER setting
        # that project's ABORT and draining its waiter — so a turn caught in the pop→lock
        # transfer window aborts cleanly and never zombie-runs the project we are about to
        # remove. Crucially this sets the abort BEFORE store.remove below, closing the race
        # where a window turn would otherwise persist to an already-deleted record (the
        # lock-based is_busy missed the window turn entirely — it holds no lock).
        if not self.streaming.request_remove(chat_id, name):
            await update.message.reply_text(
                f"❌ {name} has a turn in flight — /cancel {name} first, then /rm it."
            )
            return
        try:
            self.streaming.store.remove(chat_id, name)
        except UnknownProject:
            # R6: bold the name like /projects, not a Python repr. ``name`` is operator
            # input that missed the registry, so escape it (defense-in-depth).
            await update.message.reply_text(
                f"❌ No project named <b>{html.escape(name, quote=False)}</b>.",
                parse_mode="HTML",
            )
            return
        # B4: purge the project's in-memory runtime too. store.remove only drops the
        # persisted record; the cached _ProjectRuntime (engine + fixed cwd + policy) would
        # otherwise survive and be REUSED if the same name is re-created via /new, running
        # the recreated project in the OLD cwd and inheriting the OLD /yolo + grants (the
        # SB5 bypass leak / D4 cwd leak). forget_project stops its engine (best-effort) and
        # drops it, so a later /new <name> builds a fresh runtime. The active project is
        # already refused above, so the purged runtime is never the live one.
        await self.streaming.forget_project(chat_id, name)
        await update.message.reply_text(
            f"🗑️ Removed {name} (its Claude transcript is left on disk)."
        )

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a new project confined to the permitted roots, then switch to it.

        **The one path-input command (SB2).** ``/new <name> <path>`` — streaming mode
        only. Order of checks is fail-fast and secure:

        1. ``_ok`` allowlist recheck (SB1) + ``_require_streaming`` one-shot notice.
        2. store-None guard (RB1): no STATE_FILE → no registry to create in; reply and
           return (never dereference a None store).
        3. Parse ``name`` (first arg) + ``path`` (the rest, so a path may contain
           spaces); missing either → usage (RB1: 0/1 args, whitespace).
        4. SB4 name validation **before** any filesystem touch.
        5. SB2 path confinement via ``resolve_within_roots`` (canonicalizes ``~``,
           ``..``, and symlinks; relative paths resolve against the active project's
           cwd) — an out-of-roots target is refused and the project is NOT created.
        6. Existing-directory check (a project's cwd must be runnable).
        7. ``store.create(..., make_active=True)`` — duplicate name → refuse. On success
           confirm with the RESOLVED cwd.

        **No busy-guard (P5 / ADR-005 D2).** P4 refused ``/new`` while a turn was in flight
        because it auto-switches the active project and the relay routed answers to
        ``_active_engine`` (a mid-hold switch deadlocked the parked turn). With id-routing
        (ADR-005 D3) the prior run keeps going in the background and its prompt still
        resolves, so ``/new`` runs freely mid-run — same relaxation as ``/switch``.
        """
        if not await self._ok(update) or update.message is None:
            return
        if not await self._require_streaming(update):
            return
        assert self.streaming is not None
        chat_id = update.effective_chat.id
        if self.streaming.store is None:
            # No STATE_FILE configured → no registry to create a project in. RB1: never
            # crash on a streaming + no-persistence deployment (store is None).
            await update.message.reply_text(
                "Projects need persistence — set CLAUDE_STATE_FILE to create one."
            )
            return
        name = ctx.args[0] if ctx.args else ""
        path = " ".join(ctx.args[1:]).strip() if ctx.args and len(ctx.args) > 1 else ""
        if not name or not path:
            await update.message.reply_text("Usage: /new <name> <path>")
            return
        # SB4: validate the name BEFORE touching the filesystem (a bad name never causes
        # a resolve/stat on operator-supplied input).
        try:
            validate_project_name(name)
        except InvalidProjectName:
            # R6: bold the name like /projects, not a Python repr. CRITICAL: this echoes
            # PRE-validation input — the name was just REJECTED by SB4, so it is arbitrary
            # operator input (may contain <, &, >) and MUST be HTML-escaped so it renders
            # as inert text, never a live tag.
            await update.message.reply_text(
                f"❌ Invalid project name <b>{html.escape(name, quote=False)}</b> — "
                "use letters, digits, _ or - (≤32 chars).",
                parse_mode="HTML",
            )
            return
        # SB2: canonicalize (resolves symlinks AND ..) and confine to ALLOWED_ROOTS
        # BEFORE creating the project. A relative path resolves against the active
        # project's cwd (get_cwd). A path that escapes the permitted roots is refused
        # here and the project is never created. ALLOW_ANY_PATH=true is the opt-out.
        try:
            target = resolve_within_roots(
                path,
                cwd=self.streaming.get_cwd(chat_id),
                allowed_roots=self.config.allowed_roots,
                allow_any=self.config.allow_any_path,
            )
        except PathNotAllowed:
            # R6: wrap the (operator-supplied) path in <code> — inert monospace, not fake
            # command-links — and escape it so a stray &/</> can't break the HTML message.
            await update.message.reply_text(
                f"❌ Path not allowed (outside the permitted roots): {code_path(path)}",
                parse_mode="HTML",
            )
            return
        # A project's cwd must be a runnable existing directory (the engine cds into it).
        if not target.is_dir():
            await update.message.reply_text(
                f"❌ Not a directory: {code_path(path)}", parse_mode="HTML"
            )
            return
        # Create + auto-switch. The resolved (contained) cwd is stored, never the raw arg.
        try:
            self.streaming.store.create(chat_id, name, str(target), make_active=True)
        except DuplicateProject:
            await update.message.reply_text(f"❌ A project named {name} already exists.")
            return
        # R6: the resolved cwd is wrapped in <code> (monospace, no auto-linkify); the name
        # is bolded + escaped. HTML parse mode required for the tags to render.
        await update.message.reply_text(
            f"✅ Created <b>{html.escape(name, quote=False)}</b> at {code_path(target)} "
            "and switched to it — your next message runs there.",
            parse_mode="HTML",
        )

    async def cmd_to(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Route a free-text answer/feedback to a NAMED project (``/to <name> <text>`` — D5).

        The explicit free-text escape hatch (P5 / ADR-005 D5): when several projects are
        awaiting an "Other" answer / plan-reject feedback, ``/to work use the staging URL``
        resolves **work**'s pending free-text request regardless of which is the
        most-recent default — disambiguation without needing a Telegram reply-to. Streaming
        mode only. Order:

        1. ``_ok`` allowlist recheck (SB1) + ``_require_streaming`` notice — **no new
           callback surface**; this rides the same ``allowed``-filtered command path.
        2. Parse ``name`` (first arg) + ``text`` (the rest, so the answer may contain
           spaces); missing either → usage (RB1).
        3. Delegate to ``streaming.resolve_to(chat_id, name, text)`` which resolves the
           named project's pending free-text request (lock-free; unblocks the held turn) or
           returns a clear no-op message if that project is not awaiting free text — it
           **never** silently routes to the wrong project (the D5 "never misroute" bar). The
           returned string is the operator-facing reply.

        SB4: ``text`` is the answer verbatim (the engine's free-text answer), never
        interpolated into a shell command/argument.
        """
        if not await self._ok(update) or update.message is None:
            return
        if not await self._require_streaming(update):
            return
        assert self.streaming is not None
        chat_id = update.effective_chat.id
        name = ctx.args[0] if ctx.args else ""
        text = " ".join(ctx.args[1:]).strip() if ctx.args and len(ctx.args) > 1 else ""
        if not name or not text:
            await update.message.reply_text("Usage: /to <name> <your answer>")
            return
        reply = self.streaming.resolve_to(chat_id, name, text)
        await update.message.reply_text(reply)

    # ---- macros (T5 / P9; both modes — a macro fires as a normal turn) ------
    def _macro_store(self):
        """The session store macros are persisted in, or ``None`` if there is no store.

        Macros are per-chat prompt templates that work in BOTH engine modes (they expand
        to a normal turn). The store is the streaming session's in streaming mode, else the
        one-shot runner's; either may be ``None`` if no ``CLAUDE_STATE_FILE`` is configured
        (RB1: the commands then reply a clean "needs persistence" notice instead of crashing).
        """
        if self.streaming is not None:
            return self.streaming.store
        return getattr(self.runner, "store", None)

    async def cmd_save(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """``/save <name> <prompt text…>`` — store a reusable prompt template (T5 / P9).

        SB1: allowlist-gated (the ``_ok`` recheck). The name is validated against the SB4
        rule (``^[A-Za-z0-9_-]{1,32}$``) — an invalid name (``../``, spaces, > 32 chars,
        empty) is refused with a clean message and HTML-escaped in the reply (it is
        pre-validation operator input). The body is the operator-authored template stored
        verbatim (fired later as a normal turn — no injection concern beyond ordinary turn
        handling). Persisted atomic + ``0600`` (RB6). RB1: never crashes.
        """
        if not await self._ok(update) or update.message is None:
            return
        store = self._macro_store()
        if store is None:
            await update.message.reply_text(
                "Macros need persistence — set CLAUDE_STATE_FILE to save one."
            )
            return
        chat_id = update.effective_chat.id
        name = ctx.args[0] if ctx.args else ""
        body = " ".join(ctx.args[1:]).strip() if ctx.args and len(ctx.args) > 1 else ""
        if not name or not body:
            await update.message.reply_text("Usage: /save <name> <prompt text…>")
            return
        try:
            store.save_macro(chat_id, name, body)
        except InvalidProjectName:
            # CRITICAL: echoes PRE-validation input (the name was just rejected by SB4), so it
            # is arbitrary operator input — HTML-escape it so it renders as inert text.
            await update.message.reply_text(
                f"❌ Invalid macro name <b>{html.escape(name, quote=False)}</b> — "
                "use letters, digits, _ or - (≤32 chars).",
                parse_mode="HTML",
            )
            return
        # The name passed SB4 (safe charset) but escape it uniformly for the HTML reply.
        await update.message.reply_text(
            f"💾 Saved macro <b>{html.escape(name, quote=False)}</b>. "
            "Run it with /run " + html.escape(name, quote=False) + " [args…].",
            parse_mode="HTML",
        )

    async def cmd_unsave(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """``/unsave <name>`` — remove a saved macro (T5 / P9). SB1-gated; RB1/RB2 clean."""
        if not await self._ok(update) or update.message is None:
            return
        store = self._macro_store()
        if store is None:
            await update.message.reply_text("No macros to remove.")
            return
        name = " ".join(ctx.args).strip() if ctx.args else ""
        if not name:
            await update.message.reply_text("Usage: /unsave <name>")
            return
        removed = store.remove_macro(update.effective_chat.id, name)
        if removed:
            await update.message.reply_text(
                f"🗑️ Removed macro <b>{html.escape(name, quote=False)}</b>.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"No macro named <b>{html.escape(name, quote=False)}</b>.",
                parse_mode="HTML",
            )

    async def cmd_macros(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """``/macros`` — list saved macro names with a short body preview (T5 / P9). SB1."""
        if not await self._ok(update) or update.message is None:
            return
        store = self._macro_store()
        macros = store.list_macros(update.effective_chat.id) if store is not None else {}
        if not macros:
            await update.message.reply_text(
                "No macros yet. Save one with /save <name> <prompt text…>."
            )
            return
        lines = ["📑 <b>Macros</b>:"]
        for name, body in macros.items():
            # Preview the body (truncated) so the list stays compact; escape both name + body
            # (the body is operator-authored but may contain </>& — render it inert).
            preview = body if len(body) <= 60 else body[:57] + "…"
            lines.append(
                f"• <b>{html.escape(str(name), quote=False)}</b> — "
                f"<code>{html.escape(preview, quote=False)}</code>"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """``/run <name> [args…]`` — expand a saved macro and fire it as a normal turn (T5).

        Looks up the macro (case-insensitive), expands ``$1``..``$9`` positionally and ``$*``
        (all args) via :func:`~claude_tg.util.expand_macro`, then runs the expanded text
        through the SAME dispatch a plain message uses (:meth:`_run_turn`) against the active
        project. SB1: allowlist-gated. Unknown name → clean reply (RB2), never crash (RB1). The
        macro body is operator-authored and fired as an ordinary turn (no injection concern
        beyond normal turn handling).
        """
        if not await self._ok(update) or update.message is None:
            return
        store = self._macro_store()
        if store is None:
            await update.message.reply_text(
                "Macros need persistence — set CLAUDE_STATE_FILE to use one."
            )
            return
        chat_id = update.effective_chat.id
        name = ctx.args[0] if ctx.args else ""
        if not name:
            await update.message.reply_text("Usage: /run <name> [args…]")
            return
        body = store.get_macro(chat_id, name)
        if body is None:
            await update.message.reply_text(
                f"No macro named <b>{html.escape(name, quote=False)}</b>. "
                "List them with /macros.",
                parse_mode="HTML",
            )
            return
        macro_args = list(ctx.args[1:]) if ctx.args and len(ctx.args) > 1 else []
        expanded = expand_macro(body, macro_args)
        if not expanded.strip():
            # A macro that expands to nothing (empty body, or all-placeholder with no args) —
            # don't fire an empty turn (RB1/RB2). Tell the operator cleanly.
            await update.message.reply_text(
                "That macro expanded to an empty prompt — give it some args, or /macros to review."
            )
            return
        # Fire as a normal turn (same path a plain message takes) against the active project.
        await self._run_turn(update, ctx, chat_id, expanded)

    # ---- messages -----------------------------------------------------------
    @staticmethod
    def _reply_to_id(update: Update) -> int | None:
        """The message_id this message is a reply-to, or ``None`` (D5 free-text routing).

        A plain message that is a Telegram reply-to carries the replied-to message under
        ``update.message.reply_to_message``; its ``message_id`` lets the streaming session
        route a free-text answer to the project that owns the replied-to prompt (the D5
        reply-to escape hatch). ``None`` when the message is not a reply (the common case).
        Defensive (RB1): any missing attribute → ``None``.
        """
        msg = getattr(update, "message", None)
        replied = getattr(msg, "reply_to_message", None) if msg is not None else None
        return getattr(replied, "message_id", None) if replied is not None else None

    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        # T1: first-ever message for this chat → one-time welcome (SB1: only reached for an
        # authorized chat). At-most-once; subsequent messages are a no-op. Sent before the
        # turn runs so the welcome appears first.
        await self._maybe_welcome(update)
        await self._run_turn(
            update, ctx, update.effective_chat.id, text,
            reply_to_message_id=self._reply_to_id(update),
        )

    async def on_skill_command(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward any *unregistered* slash-command verbatim to the active session (P3, D1).

        Registered as ``MessageHandler(allowed & filters.COMMAND, …)`` **after** the
        specific ``CommandHandler``s, so PTB's first-match-wins routing means a real bot
        command (``/reset``, ``/cd``, …) is consumed by its own handler and only an
        *unregistered* command (``/grill``, ``/pipeline``, …) falls through to here. The
        text is then run as an ordinary turn — the same dispatch path :meth:`on_message`
        uses — so the slash-command launches that skill in the live session.

        SB1: this is a new inbound surface, so it carries the SAME guards as every other
        handler — the ``allowed`` filter on the registration AND the ``_ok`` recheck below
        (defense in depth). A non-allowlisted chat reaches neither the runner nor the
        streaming session. RB1: a missing/empty/whitespace command no-ops (after strip),
        and ``/`` only / unicode garbage is just forwarded as a turn — the handler never
        raises and the session stays usable. The command is forwarded VERBATIM (leading
        ``/`` and args intact); we do not validate, rewrite, or allowlist skill names (D1/D2).
        """
        if not await self._ok(update) or update.message is None:
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        # T1: a skill-launch slash-command can also be a chat's first-ever message — welcome
        # once here too (at-most-once shared with on_message via the same seen-set).
        await self._maybe_welcome(update)
        await self._run_turn(update, ctx, update.effective_chat.id, text)

    async def _run_turn(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Run ``text`` as one turn for ``chat_id`` — the shared dispatch both the message
        handler and the skill-launch passthrough route through (one path, no duplication).

        Streaming mode hands the turn to the :class:`StreamingSession`; one-shot mode runs
        it through the runner and replies (chunked). Callers MUST have already done the
        ``_ok`` allowlist recheck and the empty-text guard. ``reply_to_message_id`` (D5) is
        forwarded to the streaming session for free-text reply-to routing; it is ignored in
        one-shot mode (no interactive holds there).
        """
        if self.streaming is not None:
            await self._on_message_streaming(
                update, ctx, chat_id, text, reply_to_message_id=reply_to_message_id
            )
            return

        stop = asyncio.Event()
        typing = asyncio.create_task(self._keep_typing(ctx, chat_id, stop))
        try:
            result = await self.runner.run(chat_id, text)
        except ClaudeBusy:
            await update.message.reply_text(
                "⏳ Still working on your previous message — it'll reply when done. "
                "Send one message at a time."
            )
            return
        finally:
            stop.set()
            await asyncio.gather(typing, return_exceptions=True)

        if result.ok:
            reply = result.text if (result.text and result.text.strip()) else "✅ (Claude returned no text.)"
            await self._reply_chunked(update, reply)
        elif result.raw_external:
            # SB3/H1 (body-free): the error was derived from RAW CLI stderr / a raw parsed
            # result — it can carry file content or a secret, so render a body-free summary
            # to the chat and write the raw detail only to the LOCAL debug log (scrubbed via
            # _redact_sid_in_text; the bot token is never logged anywhere).
            log.debug(
                "raw external one-shot error for chat %s: %s",
                chat_id,
                _redact_sid_in_text(result.error),
            )
            await self._reply_chunked(update, f"⚠️ {BODY_FREE_ERROR_LINE}")
        else:
            # Bot-AUTHORED safe error (timeout / bad cwd / binary-not-found / empty prompt) —
            # helpful and secret-free, so render it readably.
            await self._reply_chunked(update, f"⚠️ {result.error or 'Something went wrong.'}")

    async def _keep_typing(self, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, stop: asyncio.Event) -> None:
        """Show the 'typing…' indicator until ``stop`` is set (Claude can be slow)."""
        try:
            while not stop.is_set():
                try:
                    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception:  # never let a transient API hiccup kill the turn
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=4.0)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:  # pragma: no cover
            pass

    async def _reply_chunked(self, update: Update, text: str) -> None:
        for chunk in split_message(text):
            if not chunk.strip():
                continue
            await update.message.reply_text(chunk)

    # ---- streaming mode (ENGINE_MODE=streaming) -----------------------------
    async def _on_message_streaming(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Drive the streaming engine for one message (delegates to StreamingSession).

        Binds send/edit closures to this chat (the actual Telegram I/O the render layer
        deferred), then hands the turn to the driver. A second concurrent message to the
        SAME running project raises :class:`StreamingBusy` (one turn per project) and we
        reply the same "still working" notice as one-shot mode. SB4: the text is the
        engine's prompt, never interpolated into a shell command/argument.
        ``reply_to_message_id`` (D5) is forwarded so a reply to a free-text prompt routes
        the answer to the project that owns that prompt.
        """
        assert self.streaming is not None
        bot = ctx.bot

        async def send(*, text: str, reply_markup=None, parse_mode=None) -> int | None:
            msg = await bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
            return getattr(msg, "message_id", None)

        async def edit(*, message_id: int, text: str, parse_mode=None) -> None:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, parse_mode=parse_mode
            )

        async def delete(*, message_id: int) -> None:
            # Clear the transient "💭 Claude is thinking…" status line at turn end so a
            # stale one does not linger (best-effort; the session swallows failures).
            await bot.delete_message(chat_id=chat_id, message_id=message_id)

        try:
            await self.streaming.handle_message(
                chat_id, text, send=send, edit=edit, delete=delete,
                reply_to_message_id=reply_to_message_id,
            )
        except StreamingBusy:
            await update.message.reply_text(
                "⏳ Still working on your previous message — it'll reply when done. "
                "Send one message at a time."
            )

    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Inline-keyboard tap handler — **the SB1 security boundary**.

        A callback tap is new attack surface (SB1). PTB's ``CallbackQueryHandler`` cannot
        be chat-filtered the way ``MessageHandler`` is (it filters by callback_data
        pattern), so THIS explicit :meth:`_authorized` recheck is the authoritative
        allowlist gate: an unauthorized / forged callback NEVER routes a decision (it
        cannot approve a plan or answer a question) — if the chat is not allowlisted we
        silently answer the callback query and return WITHOUT touching the engine. For an
        authorized chat the
        decode + routing lives in :meth:`StreamingSession.resolve_callback`, which ignores
        any ``callback_data`` that fails to decode (foreign/stale/malformed → None) and
        resolves nothing in that case (RB1). The callback query is ALWAYS answered (so the
        client's spinner stops), even when ignored.

        **Free-text prompt (P5 / ADR-005 D5).** When the tap arms free-text capture (an
        "Other"/"Reject" → ``outcome.expects_text``), the bot replies a **name-echoed**
        prompt (``✏️ <name>: reply with your answer…`` — ``render.free_text_prompt``) so the
        operator can tell WHICH project the next plain message resolves (several may be
        awaiting at once). It then maps that prompt's ``message_id -> tool_use_id``
        (``register_reply_prompt``) so a **reply-to** that prompt routes the answer by id
        (the reply-to escape hatch overriding the most-recent default).
        """
        query = update.callback_query
        if query is None:
            return
        # SB1: explicit allowlist recheck inside the handler (the filter is the first
        # gate; this is defense in depth). An unauthorized tap is answered + dropped —
        # never resolved.
        if not self._authorized(update) or self.streaming is None:
            await self._answer_callback(query)
            return
        chat = update.effective_chat
        try:
            outcome = self.streaming.resolve_callback(chat.id, query.data)
        except Exception:  # RB1: a bad/garbage callback must never crash the handler
            log.exception("error routing callback for chat %s", chat.id if chat else "?")
            await self._answer_callback(query)
            return
        await self._answer_callback(query, outcome.note if outcome.handled else None)
        if outcome.expects_text:
            # D5: name-echo the free-text prompt so the operator knows which project the
            # next message resolves; capture the prompt's message_id -> tool_use_id so a
            # reply-to it routes by id (the reply-to escape hatch). A missing project_name
            # (defensive) falls back to the plain toast note so the prompt is never empty.
            prompt = (
                free_text_prompt(outcome.project_name)
                if outcome.project_name
                else f"✏️ {outcome.note}…"
            )
            try:
                sent = await query.message.reply_text(prompt)
            except Exception:
                sent = None
            self.streaming.register_reply_prompt(
                chat.id, getattr(sent, "message_id", None), outcome.tool_use_id
            )

    @staticmethod
    async def _answer_callback(query, text: str | None = None) -> None:
        """Answer a callback query (stops the client spinner); never raise (RB1)."""
        try:
            if text:
                await query.answer(text=text)
            else:
                await query.answer()
        except Exception:
            pass

    # ---- errors -------------------------------------------------------------
    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("unhandled error while processing update", exc_info=ctx.error)
        try:
            if isinstance(update, Update) and update.effective_message and self._authorized(update):
                await update.effective_message.reply_text("⚠️ Internal error — check the bot logs on the Mac.")
        except Exception:
            pass

    # ---- startup ------------------------------------------------------------
    async def _post_init(self, app: Application) -> None:
        """T1 (P9): register the bot's ``/`` command menu once at startup (set_my_commands).

        PTB calls this after the application is initialized. It pushes :data:`COMMAND_MENU`
        (the registered commands + concise descriptions) so Telegram shows the native menu.
        Best-effort (RB1): a Telegram API hiccup is logged but never blocks startup — the bot
        runs fine without a menu. The menu is global (Telegram has no per-chat allowlist
        scope at the bot-command layer; SB1 still gates every actual command at dispatch).
        """
        try:
            await app.bot.set_my_commands(
                [BotCommand(cmd, desc) for cmd, desc in COMMAND_MENU]
            )
        except Exception:
            log.warning("failed to register the bot command menu (set_my_commands)", exc_info=True)

    # ---- wiring -------------------------------------------------------------
    def build_application(self) -> Application:
        # concurrent_updates(True) is REQUIRED by the answer-hold design: a streaming turn
        # parks its handler *inside* engine.send awaiting the operator's answer, and the
        # inline-keyboard tap that supplies that answer arrives as a SEPARATE update. With
        # PTB's default sequential processing the tap would queue behind the parked turn
        # handler — a deadlock (the turn waits for the tap; the tap waits for the turn to
        # return). Concurrent dispatch lets the callback handler run while the turn is held
        # (resolve_callback is intentionally lock-free for exactly this). One turn per chat
        # is still enforced by the StreamingBusy guard.
        # T1 (P9): register the native /-menu at startup via post_init. set_my_commands makes
        # every registered command discoverable in Telegram's UI (derived from COMMAND_MENU,
        # which the test pins to the registered CommandHandlers below). Best-effort (RB1): a
        # failed API call must not block the bot from starting — it just means no menu.
        app = (
            ApplicationBuilder()
            .token(self.config.bot_token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .build()
        )
        allowed = filters.Chat(chat_id=list(self.config.allowed_chat_ids))
        app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=allowed))
        app.add_handler(CommandHandler("status", self.cmd_status, filters=allowed))
        app.add_handler(CommandHandler("reset", self.cmd_reset, filters=allowed))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel, filters=allowed))
        app.add_handler(CommandHandler("yolo", self.cmd_yolo, filters=allowed))
        app.add_handler(CommandHandler("unyolo", self.cmd_unyolo, filters=allowed))
        # T4 (P9): per-project model routing (streaming mode only; the handlers reply a
        # streaming-only notice in one-shot). /model is the alias of /auto (so /model default
        # clears the override); both names route to cmd_auto. Registered with the SAME
        # `allowed` chat filter (SB1) and BEFORE the on_skill_command COMMAND passthrough so
        # they are consumed here, not forwarded to the session as skills.
        app.add_handler(CommandHandler("fast", self.cmd_fast, filters=allowed))
        app.add_handler(CommandHandler("deep", self.cmd_deep, filters=allowed))
        app.add_handler(CommandHandler(["auto", "model"], self.cmd_auto, filters=allowed))
        app.add_handler(CommandHandler("pwd", self.cmd_pwd, filters=allowed))
        app.add_handler(CommandHandler("cd", self.cmd_cd, filters=allowed))
        # P4 multi-project navigation (streaming mode only; the handlers reply a
        # streaming-only notice in one-shot). Registered as specific CommandHandlers with
        # the SAME `allowed` chat filter (SB1) and placed BEFORE the on_skill_command
        # COMMAND passthrough below, so first-match-wins consumes them here rather than
        # forwarding /projects · /new · /switch · /rm to the session as skills.
        app.add_handler(CommandHandler("projects", self.cmd_projects, filters=allowed))
        app.add_handler(CommandHandler("new", self.cmd_new, filters=allowed))
        app.add_handler(CommandHandler("switch", self.cmd_switch, filters=allowed))
        app.add_handler(CommandHandler("rm", self.cmd_rm, filters=allowed))
        # P5 /to <name> <text> (D5 free-text escape hatch): routes a free-text answer to a
        # named project's pending "Other"/reject. Same `allowed` chat filter (SB1) +
        # registered BEFORE the skill passthrough (first-match-wins) — no new callback
        # surface.
        app.add_handler(CommandHandler("to", self.cmd_to, filters=allowed))
        # T5 (P9): macros — /save · /run · /macros · /unsave. Per-chat prompt templates that
        # work in BOTH engine modes (a /run fires the expanded text as a normal turn). Same
        # `allowed` chat filter (SB1) + registered BEFORE the on_skill_command COMMAND
        # passthrough so first-match-wins makes /save · /run real commands (not forwarded
        # verbatim to the session as skills).
        app.add_handler(CommandHandler("save", self.cmd_save, filters=allowed))
        app.add_handler(CommandHandler("run", self.cmd_run, filters=allowed))
        app.add_handler(CommandHandler("macros", self.cmd_macros, filters=allowed))
        app.add_handler(CommandHandler("unsave", self.cmd_unsave, filters=allowed))
        app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, self.on_message))
        # P3 skill-launch passthrough (D1): forward any *unregistered* slash-command verbatim
        # to the session. Registered AFTER the specific CommandHandlers above so PTB's
        # first-match-wins routing lets a real bot command (/reset, /cd, …) be consumed by
        # its own handler — only an unregistered command (/grill, /pipeline, …) falls through
        # here. SB1: same `allowed` chat filter as every other handler (the `_ok` recheck
        # inside on_skill_command is defense in depth).
        app.add_handler(MessageHandler(allowed & filters.COMMAND, self.on_skill_command))
        # SB1 (callback taps): PTB's CallbackQueryHandler filters by callback_data
        # *pattern*, not by chat (no `filters=` like MessageHandler), so the authoritative
        # allowlist gate for a tap is the explicit `_authorized` recheck inside
        # on_callback — a non-allowlisted / forged tap is answered and dropped there,
        # never routed to a decision. (allowed_updates also only enables callback_query
        # in streaming mode; see main.py.)
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=None))
        app.add_error_handler(self.on_error)
        return app
