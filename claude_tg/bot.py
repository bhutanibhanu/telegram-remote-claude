"""Telegram transport: routes allowlisted messages to Claude and replies."""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import shutil
import tempfile
import time

from telegram import BotCommand, InputFile, Update
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
from .engine import ImageInput, ImageMediaType
from .paths import PathNotAllowed, resolve_within_roots
from .render import (
    BODY_FREE_ERROR_LINE,
    ProjectMark,
    code_path,
    free_text_prompt,
    project_status_label,
    quick_reply_dismiss,
    quick_reply_keyboard,
    sessions_keyboard,
    sessions_listing,
    yolo_banner,
)
from .session_store import (
    DuplicateProject,
    InvalidProjectName,
    UnknownProject,
    validate_project_name,
)
from .sessions_discovery import discover_sessions
from .stream_session import StreamingBusy, StreamingSession
from .util import _redact_sid_in_text, expand_macro, split_message
from .voice import TranscriptionError, TranscriptionUnavailable, transcribe

log = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 *Claude Code remote*\n\n"
    "Just send me a message and I'll run it through Claude Code on the Mac and reply.\n"
    "Send a photo and Claude sees it; send a file and it's saved into the project for "
    "Claude to read (streaming mode); pull a file back with /get. Send a voice note and, "
    "with a transcriber configured, it's transcribed and run as a turn (otherwise you get "
    "a quick setup tip).\n\n"
    "Commands:\n"
    "/help — this help\n"
    "/status — health: uptime, mode, gate, runs, per-project status + cost\n"
    "/reset — start a fresh Claude session (forget context)\n"
    "/cancel [name|all] — abort the in-flight run: the active project, a named project, "
    "or every running/queued project (streaming mode)\n"
    "/to <name> <text> — send a free-text answer/feedback to a named project's pending "
    "“Other”/“Reject” prompt (streaming mode; or just reply to the prompt)\n"
    "/yolo — run every tool with NO approval prompt this session (streaming mode)\n"
    "/unyolo — restore the per-tool permission gate (streaming mode)\n"
    "/plan — run your next message in plan mode: Claude proposes a plan and you Approve "
    "(it executes, still per-tool gated) or Reject with feedback (it revises) (streaming mode)\n"
    "/fast — use the fast model (Haiku) for this project's next turn (streaming mode)\n"
    "/deep — use the deep model (Opus) for this project's next turn (streaming mode)\n"
    "/auto (or /model default) — clear the model override, back to the default (streaming mode)\n"
    "/projects — list your projects and which one is active (streaming mode)\n"
    "/sessions — list every Claude Code session on the Mac (running/idle), including the "
    "one running right now, merged with your projects (read-only)\n"
    "/attach <session-id> — adopt any Mac session (from /sessions) as a project and drive "
    "it; a session that's live elsewhere is attached as a FORK so it isn't corrupted "
    "(streaming mode)\n"
    "/watch <session-id> — live-mirror any Mac session's transcript onto this chat, "
    "read-only (it relays text + tool activity body-free; never drives it) (streaming mode)\n"
    "/unwatch — stop the active live-mirror (streaming mode)\n"
    "/new <name> <path> — create a project at <path> and switch to it; <path> must be an "
    "existing directory inside the permitted roots (streaming mode)\n"
    "/switch <name> — switch the active project; the next message resumes it (streaming mode)\n"
    "/rm <name> — drop a project from the registry, leaving its transcript on disk (streaming mode)\n"
    "/pwd — show the active project's working directory\n"
    "/get <path> — send a file from the project back to you (path-confined to the "
    "permitted roots; relative to the active project, or an absolute path)\n"
    "/cd <path> — change the working directory (one-shot mode only; in streaming mode "
    "the cwd is fixed per project — use /new to work elsewhere)\n"
    "/save <name> <text> — save a reusable prompt template (macro)\n"
    "/run <name> [args…] — run a saved macro (expands `$1` `$2` … and `$*` = all args)\n"
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
    ("plan", "Run the next message in plan mode (approve the plan first)"),
    ("fast", "Use the fast model (Haiku) for this project's next turn"),
    ("deep", "Use the deep model (Opus) for this project's next turn"),
    ("auto", "Clear the model override (back to the default)"),
    ("model", "Clear the model override (alias of /auto)"),
    ("projects", "List your projects and which one is active"),
    ("sessions", "List all Claude sessions on the Mac (running/idle)"),
    ("attach", "Adopt + drive any Mac session: /attach <session-id>"),
    ("watch", "Live-mirror any Mac session (read-only): /watch <session-id>"),
    ("unwatch", "Stop the active live-mirror"),
    ("new", "Create a project at a path and switch to it"),
    ("switch", "Switch the active project"),
    ("rm", "Drop a project from the registry"),
    ("pwd", "Show the active project's working directory"),
    ("get", "Send a file from the project back to you: /get <path>"),
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


#: P10 T1 (multimodal): the default prompt when an operator sends a photo with NO caption.
#: The caption is normally the turn's prompt; with none we give Claude a sensible
#: look-at-this instruction so a bare screenshot still does something useful.
DEFAULT_IMAGE_PROMPT = "Look at this image and tell me what you see / help me with it."

#: P10 T3 (file receive): a safe fallback filename when a Telegram ``Document`` carries no
#: usable ``file_name`` (or one that sanitizes to nothing). Saved into the active project's
#: cwd so Claude can Read it; never executed.
DEFAULT_INBOUND_FILENAME = "upload.bin"

#: P10 T2 (voice): the graceful-off message when no ``TRANSCRIBE_CMD`` is configured. Voice
#: transcription is operator-provided infra (no hard dependency); with none set up, a voice
#: note gets this clean, actionable setup message instead of a crash (RB2).
#:
#: BUG A: this is sent as PLAIN TEXT (the ``on_voice`` send-sites pass no ``parse_mode``). The
#: ``TRANSCRIBE_CMD`` token + the backtick spans below would, under ``parse_mode="Markdown"``,
#: leave an unterminated italic / code span and make Telegram REJECT the entire send (the user
#: then gets nothing — the common no-transcriber case). The backticks here render literally as
#: plain text. The ``_strip_code_spans`` guard in ``tests/test_voice.py`` additionally pins the
#: emphasis markers balanced so a future Markdown send can't silently re-break it.
VOICE_SETUP_MESSAGE = (
    "🎙️ Voice transcription isn't set up. Install a transcriber "
    "(e.g. `brew install whisper-cpp` + a model) and set the `TRANSCRIBE_CMD` "
    "environment variable — or just type your message."
)


def _safe_filename(name: str | None) -> str:
    """Reduce a Telegram-supplied ``file_name`` to a SINGLE, inert basename (P10 T3 / SB2).

    The destination is built by joining this name onto the active project's cwd and then
    re-confined through :func:`~claude_tg.paths.resolve_within_roots` — but we ALSO strip the
    name to a bare basename here so a hostile ``../../etc/passwd`` or an absolute
    ``/etc/cron.d/x`` can never even *form* a traversal before confinement runs (defense in
    depth; the SB2 resolve is the authoritative boundary, this is the belt). Steps, pure (no
    I/O):

    * take only the final path component (``os.path.basename`` after normalizing both
      separators) — drops every directory part, so ``../`` and a leading ``/`` are gone;
    * reject the special ``.`` / ``..`` components and any empty result;
    * strip control chars / NULs that could confuse the filesystem.

    Returns a clean basename, or :data:`DEFAULT_INBOUND_FILENAME` when nothing usable
    remains. The caller STILL runs the confinement resolve on the joined path — this only
    guarantees the join starts from a single inert component.
    """
    raw = (name or "").strip()
    # Normalize Windows separators too, then take the final component only. This discards
    # any directory portion (../, leading /, nested dirs) regardless of OS.
    raw = raw.replace("\\", "/")
    base = os.path.basename(raw)
    # Strip NUL / control characters (defensive — a filename should be printable text).
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    if not base or base in (".", ".."):
        return DEFAULT_INBOUND_FILENAME
    return base

#: P10 T1: map a Telegram mime-type / file extension to an Anthropic image ``media_type``.
#: Telegram compresses inbound *photos* to JPEG (no mime on a PhotoSize), so a photo with
#: no usable hint defaults to ``image/jpeg``; an image *document* carries a mime_type /
#: file_name we map explicitly. Only these four are supported by the model; anything else
#: returns None and the handler refuses it (never guesses a wrong media_type).
_MIME_TO_MEDIA_TYPE: dict[str, ImageMediaType] = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
}
_EXT_TO_MEDIA_TYPE: dict[str, ImageMediaType] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _media_type_for(
    *, mime_type: str | None, file_name: str | None, is_photo: bool
) -> ImageMediaType | None:
    """Derive the Anthropic image ``media_type`` from a Telegram attachment's hints.

    Precedence: an explicit ``mime_type`` (image documents carry one) wins; else the
    ``file_name`` extension; else — for a compressed Telegram *photo*, which has neither —
    default to ``image/jpeg`` (Telegram re-encodes photos to JPEG). Returns ``None`` for an
    unsupported / unknown type so the caller refuses it rather than mislabeling the bytes
    (which the model would reject). Pure (no I/O); case-insensitive on mime + extension.
    """
    if mime_type:
        mapped = _MIME_TO_MEDIA_TYPE.get(mime_type.strip().casefold())
        if mapped is not None:
            return mapped
        # An explicit non-image (or unsupported image) mime → refuse, never fall through to
        # a JPEG default (an image document the model can't read must be rejected cleanly).
        return None
    if file_name and "." in file_name:
        ext = "." + file_name.rsplit(".", 1)[1].strip().casefold()
        mapped = _EXT_TO_MEDIA_TYPE.get(ext)
        if mapped is not None:
            return mapped
    # A compressed photo has no mime/name — Telegram serves it as JPEG.
    return "image/jpeg" if is_photo else None


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
        # P9 P2: uptime is a fixed bot-derived duration (digits + ``d/h/m/s`` only — it can
        # never contain ``& < >``), so it needs no html.escape (names/mode below still are).
        lines = ["📊 <b>Status</b>", f"Uptime: {_format_uptime(time.time() - self._start_time)}"]
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
            # T6/P9: surface the queued counter alongside the active/cap runs so the operator
            # sees work backed up behind the cap (read-only; 0 → no suffix).
            queued = self.streaming.queued_waiting(chat_id)
            queued_suffix = f" ({queued} more waiting)" if queued > 0 else ""
            lines.append(f"Runs: {active_runs} active / {cap} max concurrent{queued_suffix}")
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
                    # P9 fix: /yolo is PER-PROJECT, but the single global "Permission gate" line
                    # above reflects only the ACTIVE project — a wide-open BACKGROUND project
                    # would be hidden. Surface each project's yolo posture HERE so an allow-all
                    # project is never silent on /status (read-only; a project with no runtime
                    # reads False — the fail-closed default). The ⚠️ glyph mirrors the loud
                    # enable banner.
                    yolo_html = (
                        " · ⚠️ yolo"
                        if self.streaming.get_project_yolo(chat_id, name)
                        else ""
                    )
                    lines.append(
                        f"{marker} <b>{html.escape(str(name), quote=False)}</b> — "
                        f"{cwd_html} ({status}){model_html}{cost_html}{yolo_html}"
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
        is_all = bool(name) and name.casefold() == "all"
        cancelled = self.streaming.handle_cancel(update.effective_chat.id, name)
        if cancelled and is_all:
            # P9 wording: ``/cancel all`` is project-scoped — the unit count (pending requests
            # + drained queued turns) conflates projects with prompts, so for ``all`` we phrase
            # it as projects, not a raw aborted-unit count. The count stays on the single-project
            # ``/cancel <name>`` / ``/cancel`` (active) branch below, where one project = one unit
            # of work the operator was watching.
            await update.message.reply_text("🛑 Cancelled all running/queued projects.")
        elif cancelled:
            await update.message.reply_text(f"🛑 Cancelled ({cancelled} aborted).")
        elif name and name.casefold() != "all":
            # A named target with nothing to cancel — not running and not queued. A clear,
            # honest message (NB1: a drained queued turn would have counted above, so reaching
            # here means the project really had no in-flight or queued turn). P9: the project
            # name is bolded + escaped (HTML), uniform with every other name-bearing reply.
            await update.message.reply_text(
                f"Nothing in flight to cancel for <b>{html.escape(name, quote=False)}</b> "
                "(it may have already finished).",
                parse_mode="HTML",
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

    async def cmd_plan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """``/plan`` — run the NEXT message in plan mode and show the plan for approval (P12).

        Arms a per-project, ONE-SHOT plan marker on the active project (via
        :meth:`~claude_tg.stream_session.StreamingSession.arm_plan`): the next turn for that
        project is driven in ``permission_mode="plan"``, so Claude reasons + proposes a plan and
        surfaces ``ExitPlanMode`` through the SHIPPED Approve / Reject+feedback keyboard. Approve
        resumes execution (still per-tool gated — ADR-001 C4); Reject feeds the feedback back as
        a revision. The marker is one-shot, so the turn AFTER the planned one is normal.

        Streaming mode only — the plan flow rides the streaming engine's permission gate (one-
        shot mode has no per-tool holds), so one-shot replies a clear notice rather than half-
        working (mirrors :meth:`cmd_yolo`). SB1: allowlist-gated by the ``_ok`` recheck first,
        exactly like every command (an unauthorized chat does nothing). RB3: the marker is
        transient (never persisted; dropped on restart).
        """
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is None:
            await update.message.reply_text(
                "Plan mode (/plan) applies to streaming mode only — one-shot mode has no "
                "per-tool approval prompts to surface a plan for approval."
            )
            return
        self.streaming.arm_plan(update.effective_chat.id)
        await update.message.reply_text(
            "📋 Next message runs in plan mode — I'll show the plan for approval."
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

    def _bot_project_marks(self, chat_id: int) -> dict[str, ProjectMark]:
        """Map ``session_id -> ProjectMark`` for the chat's OWN projects (``/sessions`` merge).

        Built from ``streaming.store.list_projects`` so ``/sessions`` can mark a discovered
        machine session that is ALSO a known bot project (its name + ``✓``) and flag the
        chat's active project (``→``). Keyed by each project's stored ``session_id`` (a
        project with no session yet — never run — has none, so it can't be matched to a
        discovered session and is skipped). Read-only + defensive (RB1): no store (one-shot
        mode, or an unconfigured streaming store) → ``{}`` (the listing then just shows the
        discovered sessions unannotated). Never raises.
        """
        if self.streaming is None or self.streaming.store is None:
            return {}
        try:
            chat_id_active = self.streaming.store.get_active(chat_id)
            projects = self.streaming.store.list_projects(chat_id)
        except Exception:  # a misbehaving store must never break /sessions (RB1)
            log.debug("could not read bot projects for /sessions merge", exc_info=True)
            return {}
        marks: dict[str, ProjectMark] = {}
        for name, record in projects.items():
            if not isinstance(record, dict):
                continue
            sid = record.get("session_id")
            if not sid:
                continue  # a project that never ran has no session id to merge on
            marks[str(sid)] = ProjectMark(name=str(name), active=(name == chat_id_active))
        return marks

    async def cmd_sessions(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """List ALL Claude Code sessions on the Mac, merged with the bot's own projects (P11 T1).

        Read-only discovery (no attach in T1): :func:`~claude_tg.sessions_discovery.discover_sessions`
        enumerates every session under ``~/.claude`` (started in a terminal, an IDE, or by the
        bot — *including the orchestrator running right now*) with a composite running/idle
        hint. Each row shows a short session id, the cwd (``<code>``-wrapped, R6), a truncated
        title/first-prompt, a relative last-active, and a 🟢/⚪ marker;
        :func:`~claude_tg.render.sessions_listing` then **merges + dedups** the discovered list
        (by ``session_id``) with the chat's OWN projects so a bot-known session is marked with
        its project name + ``✓`` and the active one with ``→``.

        Works in BOTH engine modes — discovery is machine-wide, independent of the streaming
        registry; the bot-project annotation is simply empty in one-shot (no store). SB1: the
        ``_ok`` allowlist recheck gates it (a non-allowlisted chat gets nothing). SB3: the
        listing is body-free (metadata only — title/first-prompt is truncated + escaped, never
        a transcript body). RB1: a discovery failure / empty ``~/.claude`` replies a clean
        "no sessions found" notice (``sessions_listing`` returns it for an empty list), never a
        crash. Read-only — no on-disk write, no attach.
        """
        if not await self._ok(update) or update.message is None:
            return
        chat_id = update.effective_chat.id
        try:
            sessions = discover_sessions()
        except Exception:  # discover_sessions is already RB1-total; belt-and-braces here too.
            log.warning("session discovery failed for /sessions", exc_info=True)
            sessions = []
        marks = self._bot_project_marks(chat_id)
        text = sessions_listing(sessions, marks, now=time.time())
        # P11 T2: in STREAMING mode attach a [📎 Attach <shortid>] button per discovered
        # session so the operator can adopt + drive any of them in one tap (the typed
        # /attach <id> works too, incl. for sessions past the button cap — the id is on the
        # row). The keyboard's buttons are the most-RELEVANT sessions (active → bot-known →
        # most-recent — the SAME order as the listing rows, via `marks`), not an arbitrary
        # first-N. One-shot mode has no project registry to attach into, so it gets the listing
        # alone (no keyboard) — discovery there is read-only, exactly as T1. A keyboard is only
        # attached when there ARE sessions (sessions_keyboard returns None for an empty list).
        # SB1 already gated this above.
        keyboard = (
            sessions_keyboard(sessions, marks=marks) if self.streaming is not None else None
        )
        # P11 T1 live-fix: a real Mac can have hundreds of sessions; even after the relevance
        # cap a listing with long cwds/titles could approach Telegram's 4096-char limit and
        # throw BadRequest "message too long" (the live bug). Route the reply through the
        # chunked HTML sender so it can NEVER overflow; the keyboard rides only the last chunk.
        await self._reply_html_chunked(update, text, keyboard)

    async def cmd_attach(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Adopt ANY discovered Claude session as a controllable project (``/attach <id>``, P11 T2).

        The "drive any session from your phone" command: ``/attach <session-id>`` (the id is
        shown on each ``/sessions`` row) looks the session up machine-wide, then adopts its
        ``(session_id, cwd)`` as a bot project + switches to it, so the NEXT message resumes +
        drives it through the **normal turn + permission gate** path. Streaming mode only (the
        project registry is a streaming concept — one-shot replies the streaming-only notice).
        Order (fail-fast):

        1. ``_ok`` allowlist recheck (SB1) + ``_require_streaming`` one-shot notice.
        2. Parse the session id (all args joined — an id has no spaces, but be forgiving);
           missing → usage (RB1).
        3. Delegate to :meth:`~claude_tg.stream_session.StreamingSession.attach_session`,
           which does ALL the policy — the SB2 cwd confinement (refuse an out-of-roots cwd),
           the **fork-if-live** decision (a session live elsewhere is adopted as a FORK, never
           co-driven), the SB4-named registry write, and the operator-facing reply. The bot is
           a pure renderer of its :class:`~claude_tg.stream_session.AttachOutcome` (no policy
           here — same posture as ``/sessions``).
        """
        if not await self._ok(update) or update.message is None:
            return
        if not await self._require_streaming(update):
            return
        assert self.streaming is not None
        chat_id = update.effective_chat.id
        # An id carries no spaces; join defensively so a stray paste with a trailing space
        # still works. Empty → usage.
        session_id = " ".join(ctx.args).strip() if ctx.args else ""
        if not session_id:
            await update.message.reply_text("Usage: /attach <session-id>")
            return
        outcome = self.streaming.attach_session(chat_id, session_id)
        await update.message.reply_text(outcome.message, parse_mode=outcome.parse_mode)

    async def cmd_watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Live-mirror ANY discovered Claude session's transcript onto this chat (``/watch <id>``, P11 T3).

        Read-only FOLLOW (the other half of the "entirety remote" headline): ``/watch
        <session-id>`` (the id is shown on each ``/sessions`` row) tails that session's
        append-only transcript and relays each line — body-free (SB3) — to the chat as it is
        written. It NEVER drives or writes the watched session (use ``/attach`` to drive).
        Streaming mode only — the live mirror needs ``ENGINE_MODE=streaming`` (one-shot has no
        per-chat send gate / background task surface); one-shot replies the streaming-only
        notice rather than half-working. Order (fail-fast):

        1. ``_ok`` allowlist recheck (SB1) + ``_require_streaming`` one-shot notice.
        2. Parse the session id (all args joined — an id has no spaces, but be forgiving);
           missing → usage (RB1).
        3. Build a PERSISTENT per-chat ``send`` closure (it captures the long-lived
           :class:`telegram.Bot` + the chat id, so the background watch task can send after
           this handler returns) and delegate to
           :meth:`~claude_tg.stream_session.StreamingSession.watch_session`, which does ALL the
           policy — id lookup, transcript-path resolution (symlink-confined), the ONE-watch-per-
           chat replacement, the read-only tail task, and the operator-facing reply. The bot is
           a pure renderer of its :class:`~claude_tg.stream_session.WatchOutcome`.
        """
        if not await self._ok(update) or update.message is None:
            return
        if not await self._require_streaming(update):
            return
        assert self.streaming is not None
        chat_id = update.effective_chat.id
        session_id = " ".join(ctx.args).strip() if ctx.args else ""
        if not session_id:
            await update.message.reply_text("Usage: /watch <session-id>")
            return
        bot = ctx.bot

        async def send(
            *, text: str, reply_markup=None, parse_mode=None, link_preview_options=None
        ) -> int | None:
            # The persistent send closure the background watch task uses. It captures the
            # long-lived Bot + chat id (NOT the per-update ctx), so it keeps working after this
            # handler returns. Mirror of the _on_message_streaming send closure (same kwargs).
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                link_preview_options=link_preview_options,
            )
            return getattr(msg, "message_id", None)

        outcome = self.streaming.watch_session(chat_id, session_id, send=send)
        await update.message.reply_text(outcome.message, parse_mode=outcome.parse_mode)

    async def cmd_unwatch(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Stop this chat's active live-mirror (``/unwatch``, P11 T3; streaming mode only).

        Cancels the read-only tail task started by ``/watch`` (idempotent — a clean notice if
        none is active). SB1 (``_ok``) + the streaming-only notice gate it, exactly like
        ``/watch``. The bot is a pure renderer of
        :meth:`~claude_tg.stream_session.StreamingSession.unwatch`'s reply string.
        """
        if not await self._ok(update) or update.message is None:
            return
        if not await self._require_streaming(update):
            return
        assert self.streaming is not None
        chat_id = update.effective_chat.id
        await update.message.reply_text(self.streaming.unwatch(chat_id))

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
        reply, parse_mode = self._switch_active(chat_id, name)
        await update.message.reply_text(reply, parse_mode=parse_mode)

    def _switch_active(self, chat_id: int, name: str) -> tuple[str, str | None]:
        """Switch the chat's active project to ``name``; return ``(reply, parse_mode)`` (T6/P9).

        The shared core of ``/switch`` (the typed command) AND the ``[Open <project>]`` ping
        button (``on_callback``) — extracted so both go through the SAME SB2 path
        re-validation + store write (no divergence). ``name`` is the requested target
        (case-insensitive, like the store):

        * no store → "no projects" notice (RB1 — never deref a None store);
        * unknown name → an error listing the available names (escaped, R6);
        * SB2/B2: the TARGET project's stored cwd is re-validated against the permitted roots
          BEFORE activating — a missing/empty or out-of-root cwd is refused and the active
          project is left UNCHANGED (``store.switch`` never called);
        * success → ``store.switch`` + a confirmation.

        Returns the operator-facing reply + its ``parse_mode``. **P9 styling unification:**
        EVERY reply that names a project renders it as ``<b>{html.escape(name)}</b>`` and is
        sent ``"HTML"`` (the names are SB4-validated, but escaped anyway, defense-in-depth) —
        no more bare-``{name}`` interpolation. Pure of Telegram I/O (the caller sends) so the
        button + command paths share it.
        """
        assert self.streaming is not None
        if self.streaming.store is None:
            return ("No projects yet. Create one with /new <name> <path>.", None)
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
            return (
                f"❌ No project named <b>{html.escape(name, quote=False)}</b>. "
                f"Available: {available}",
                "HTML",
            )
        # SB2/B2: re-validate the TARGET project's stored cwd against the permitted roots
        # BEFORE activating (the design says re-validate "on switch/resume"; the resume
        # path is the authoritative gate, this closes the switch-time gap + improves UX).
        # Fail-closed: a missing/empty stored cwd is refused rather than crashing, and the
        # active project is left UNCHANGED (store.switch is never called) on any refusal.
        cwd = record.get("cwd")
        name_html = html.escape(name, quote=False)
        if not cwd:
            return (
                f"❌ <b>{name_html}</b> has no recorded directory — "
                "re-create it with /new <name> <path>.",
                "HTML",
            )
        try:
            resolve_within_roots(
                cwd,
                cwd=cwd,
                allowed_roots=self.config.allowed_roots,
                allow_any=self.config.allow_any_path,
            )
        except PathNotAllowed:
            return (
                f"❌ <b>{name_html}</b>'s directory is no longer within the permitted roots — "
                "not switching. Use /new <name> <path> to point it somewhere allowed.",
                "HTML",
            )
        self.streaming.store.switch(chat_id, name)
        return (
            f"✅ Switched to <b>{name_html}</b> — your next message resumes that project.",
            "HTML",
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
                f"❌ <b>{html.escape(name, quote=False)}</b> is the active project — "
                "/switch to another project first.",
                parse_mode="HTML",
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
            name_html = html.escape(name, quote=False)
            await update.message.reply_text(
                f"❌ <b>{name_html}</b> has a turn in flight — "
                f"/cancel {name_html} first, then /rm it.",
                parse_mode="HTML",
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
            f"🗑️ Removed <b>{html.escape(name, quote=False)}</b> "
            "(its Claude transcript is left on disk).",
            parse_mode="HTML",
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
            # P9: bold + escape the name (HTML), uniform with every name-bearing reply. The
            # name passed SB4 above (safe charset) but escape it anyway, defense-in-depth.
            await update.message.reply_text(
                f"❌ A project named <b>{html.escape(name, quote=False)}</b> already exists.",
                parse_mode="HTML",
            )
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
        # P9: resolve_to returns an HTML-styled reply (the project name bolded + escaped),
        # uniform with every name-bearing operator reply — send it parse_mode="HTML".
        reply = self.streaming.resolve_to(chat_id, name, text)
        await update.message.reply_text(reply, parse_mode="HTML")

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
                f"❌ No macro named <b>{html.escape(name, quote=False)}</b>.",
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
                f"❌ No macro named <b>{html.escape(name, quote=False)}</b>. "
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
        # command_initiated=True: a macro /run is a DELIBERATE start-a-turn command — it must
        # NEVER be swallowed as the answer to a pending "Other"/plan-reject free-text hold
        # (the Codex blocker). The flag tells the streaming session to skip free-text capture
        # for this turn so the expanded macro always opens a fresh turn through the permission
        # path. A PLAIN typed message keeps the flag False and still answers a pending prompt.
        await self._run_turn(update, ctx, chat_id, expanded, command_initiated=True)

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

    async def on_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """P10 T1 — a photo / image-document → a native multimodal turn (SB1-gated).

        **SB1 security boundary.** Registered with the ``allowed`` chat filter, AND this
        explicit :meth:`_ok` recheck — a new inbound surface gets the same allowlist gate as
        every other handler (defense in depth). A non-allowlisted chat never reaches the
        download / engine.

        Flow: take the LARGEST photo size (Telegram sends a size ladder) or the image
        ``Document``, **size-cap** it (refuse > ``config.image_max_bytes`` with a clean
        message — never download an oversized image into a turn), download the bytes,
        base64-encode them, derive the ``media_type`` from the mime/extension (a compressed
        photo with no hint → JPEG; an unsupported type is refused), and run the turn with the
        caption as the prompt (a sensible default when there is no caption). The pixels thread
        through ``_run_turn`` → ``handle_message`` → ``engine.send(images=…)``.

        **SB3 — never log the image bytes / base64.** We log only a size summary ("received
        an image (<N> KB)"); the base64 ``data`` is placed on the
        :class:`~claude_tg.engine.ImageInput` (whose ``repr`` elides it) and never logged.
        The pixels are operator-supplied (acceptable to forward to Claude). RB1: any
        download/decode failure is caught and surfaced as a clean message, never a crash.
        """
        if not await self._ok(update) or update.message is None:
            return
        await self._maybe_welcome(update)
        msg = update.message
        # Resolve the attachment: prefer a photo (largest size = last in the ladder), else an
        # image document. The registration filter guarantees one of these is present, but be
        # defensive (RB1) — a message with neither is a clean no-op.
        photo = msg.photo[-1] if msg.photo else None
        document = msg.document if (msg.document is not None and not photo) else None
        if photo is None and document is None:
            return
        is_photo = photo is not None
        media_type = _media_type_for(
            mime_type=getattr(document, "mime_type", None),
            file_name=getattr(document, "file_name", None),
            is_photo=is_photo,
        )
        if media_type is None:
            # An image document of an unsupported type (or a non-image document that slipped
            # past the filter) — refuse cleanly rather than mislabel the bytes (RB2/SB6).
            await msg.reply_text(
                "🖼️ I can only read JPEG, PNG, WebP, or GIF images. "
                "Send the screenshot as a photo, or a supported image file."
            )
            return
        # Size-cap BEFORE download (SB3/RB2): Telegram reports file_size on both PhotoSize and
        # Document. A missing/odd size (defensive) falls through to download + a post-download
        # cap so an over-cap image can never reach the engine either way.
        cap = self.config.image_max_bytes
        attachment = photo if is_photo else document
        declared = getattr(attachment, "file_size", None)
        if isinstance(declared, int) and declared > cap:
            await msg.reply_text(
                f"🖼️ That image is too large ({declared // 1024} KB) — "
                f"the limit is {cap // 1024} KB. Send a smaller screenshot."
            )
            return
        try:
            tg_file = await attachment.get_file()
            raw = await tg_file.download_as_bytearray()
        except Exception:
            # RB1: a transient download failure must never crash the handler — clean message.
            log.debug("image download failed for chat %s", update.effective_chat.id, exc_info=True)
            await msg.reply_text("⚠️ Couldn't download that image — please try sending it again.")
            return
        # Post-download cap (defense-in-depth: a missing declared size, or a server that
        # under-reported it). Never thread an over-cap image into a turn (SB3/RB2).
        if len(raw) > cap:
            await msg.reply_text(
                f"🖼️ That image is too large ({len(raw) // 1024} KB) — "
                f"the limit is {cap // 1024} KB. Send a smaller screenshot."
            )
            return
        # SB3: log a SIZE SUMMARY only — never the bytes / base64.
        log.info("chat %s received an image (%d KB)", update.effective_chat.id, len(raw) // 1024)
        data_b64 = base64.b64encode(bytes(raw)).decode("ascii")
        image = ImageInput(data=data_b64, media_type=media_type)
        # The caption is the prompt; a bare image gets a sensible default look-at-this prompt.
        prompt = (msg.caption or "").strip() or DEFAULT_IMAGE_PROMPT
        await self._run_turn(
            update, ctx, update.effective_chat.id, prompt,
            reply_to_message_id=self._reply_to_id(update),
            images=[image],
        )

    async def on_document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """P10 T3 — a NON-image ``Document`` → saved (path-confined) into the project (SB1/SB2).

        **Registration boundary (no collision with T1's image-Document handler).** Registered
        as ``MessageHandler(allowed & filters.Document.ALL & ~filters.Document.IMAGE, …)`` —
        the EXACT complement of T1's ``filters.PHOTO | filters.Document.IMAGE``: an image
        document (``image/*``) routes to :meth:`on_photo` (the multimodal path) and a
        non-image document (a ``.py`` / ``.log`` / ``.pdf`` / ``.zip`` …) routes HERE. The two
        document filters are disjoint, so neither steals the other's messages.

        **SB1 security boundary.** The ``allowed`` chat filter on the registration AND this
        explicit :meth:`_ok` recheck — same allowlist gate as every handler (defense in
        depth). A non-allowlisted chat never reaches the download / disk write.

        **Streaming-only.** Saving a file requires the active project's cwd (a per-project
        streaming concept) + the "offer it to Claude" turn; one-shot mode has no per-project
        cwd, so it replies a clean "needs streaming mode" message rather than dropping the
        file silently.

        Flow: **size-cap** (refuse > ``config.file_max_bytes`` BEFORE download — never pull an
        oversized file onto disk), **sanitize** the Telegram ``file_name`` to a single inert
        basename, **SB2-confine** the destination by resolving ``cwd / basename`` through
        :func:`~claude_tg.paths.resolve_within_roots` (a ``../`` / absolute filename can never
        escape the permitted roots — the SAME resolver ``/cd`` · ``/new`` use), download +
        write the bytes atomically, then fire a normal turn ("I've added <file> …") so Claude
        can Read it. **Never auto-executed.** RB1: any download/write failure → a clean message,
        never a crash. SB3: only a size SUMMARY is logged — never the file bytes.
        """
        if not await self._ok(update) or update.message is None:
            return
        await self._maybe_welcome(update)
        msg = update.message
        document = msg.document
        if document is None:  # RB1: the filter guarantees a document, but be defensive.
            return
        chat_id = update.effective_chat.id
        # Streaming-only: the inbound save needs the active project's cwd. One-shot mode has
        # no per-project cwd → a clean refusal (never silently drop the file).
        if self.streaming is None:
            await msg.reply_text(
                "📎 Receiving a file needs streaming mode (ENGINE_MODE=streaming). "
                "In one-shot mode I can't save attachments into a project yet."
            )
            return
        # Size-cap BEFORE download (RB2): Telegram reports file_size on a Document. A
        # missing/odd size (defensive) falls through to download + a post-download cap so an
        # over-cap file can never land on disk either way.
        cap = self.config.file_max_bytes
        declared = getattr(document, "file_size", None)
        if isinstance(declared, int) and declared > cap:
            await msg.reply_text(
                f"📎 That file is too large ({declared // 1024} KB) — "
                f"the limit is {cap // 1024} KB."
            )
            return
        # SB2: build the destination from the active project's cwd + a SANITIZED basename,
        # then re-confine through resolve_within_roots. A ``../`` / absolute file_name is
        # stripped to a basename above AND cannot escape the roots here (the authoritative
        # boundary). allow_any_path=true is the explicit opt-out, identical to /cd · /new.
        cwd = self.streaming.get_cwd(chat_id)
        filename = _safe_filename(getattr(document, "file_name", None))
        try:
            dest = resolve_within_roots(
                filename,
                cwd=cwd,
                allowed_roots=self.config.allowed_roots,
                allow_any=self.config.allow_any_path,
            )
        except PathNotAllowed:
            # An escaping name that survived sanitization (belt-and-braces) — refuse cleanly.
            await msg.reply_text(
                f"📎 Can't save that file — its name resolves outside the permitted "
                f"roots: {code_path(filename)}",
                parse_mode="HTML",
            )
            return
        try:
            tg_file = await document.get_file()
            raw = await tg_file.download_as_bytearray()
        except Exception:
            # RB1: a transient download failure must never crash the handler — clean message.
            log.debug("file download failed for chat %s", chat_id, exc_info=True)
            await msg.reply_text("⚠️ Couldn't download that file — please try sending it again.")
            return
        # Post-download cap (defense-in-depth: a missing/under-reported declared size). Never
        # write an over-cap file to disk.
        if len(raw) > cap:
            await msg.reply_text(
                f"📎 That file is too large ({len(raw) // 1024} KB) — "
                f"the limit is {cap // 1024} KB."
            )
            return
        try:
            # Write atomically (random tmp + os.replace) so a partial download never leaves a
            # truncated file in the project. The destination is the SB2-confined path.
            #
            # BUG B (Codex B1 — symlink-escape on save): we MUST NOT open a *predictable*-named
            # temp like ``<dest.name>.part`` for write — an attacker could pre-place an in-root
            # symlink of that exact name pointing OUT of the allowed roots, and open-for-write
            # would FOLLOW it and clobber the out-of-root target (a confinement escape). Instead
            # create a RANDOM temp name in the SAME (confined) parent dir via mkstemp — a random
            # name cannot have been pre-placed as a symlink — write to its fd, then os.replace()
            # it onto ``dest``. os.replace() swaps the directory entry: if ``dest`` itself is a
            # symlink, replace overwrites the LINK (it does not write through it the way an
            # open-for-write would). Both moves stay inside ``dest.parent`` (the confined dir).
            fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".tg-", suffix=".part")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(bytes(raw))
                os.replace(tmp_name, dest)
            except BaseException:
                # Best-effort cleanup of the temp on any failure (it's a random in-root name,
                # never a symlink) so a failed save can't leave a stray ``.tg-…part`` behind.
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError:
            # RB1: a write failure (permissions / no space / the path is a directory) → a
            # clean message, never a crash. SB3: no file content in the log.
            log.debug("file save failed for chat %s", chat_id, exc_info=True)
            await msg.reply_text(
                f"⚠️ Couldn't save {code_path(filename)} into the project — check the bot logs.",
                parse_mode="HTML",
            )
            return
        # SB3: log a SIZE SUMMARY only (name + KB) — never the file bytes.
        log.info("chat %s received a file %r (%d KB)", chat_id, dest.name, len(raw) // 1024)
        # Offer it to Claude as a NORMAL turn (never auto-executed): the caption is the
        # operator's instruction, with a sensible default. The path is wrapped in <code> so
        # it renders inert (R6), but the turn text Claude receives is plain so it can Read it.
        caption = (msg.caption or "").strip()
        instruction = caption or "Read it and tell me what it is, or help me with it."
        prompt = f"I've added the file {dest} to the project — {instruction}"
        await self._run_turn(
            update, ctx, chat_id, prompt,
            reply_to_message_id=self._reply_to_id(update),
        )

    async def on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """P10 T2 — a voice note / audio → transcribe (pluggable) → run as a turn (SB1).

        **SB1 security boundary.** Registered with the ``allowed`` chat filter AND this
        explicit :meth:`_ok` recheck — a new inbound surface gets the same allowlist gate as
        every other handler (defense in depth). A non-allowlisted chat never reaches the
        download / transcriber.

        **Pluggable + graceful-off.** Transcription is operator-provided infra, NOT a hard
        dependency. When ``TRANSCRIBE_CMD`` is unset (the default), a voice note gets the clean
        :data:`VOICE_SETUP_MESSAGE` ("…install a transcriber and set TRANSCRIBE_CMD…") — never
        a crash (RB2). When it IS set, the bot:

        1. downloads the Telegram voice ``.ogg``/opus (or audio) to a per-turn TEMP dir;
        2. runs the configured transcriber over it (see :mod:`claude_tg.voice` for the
           ``{audio}`` / ``{out}`` placeholder contract + injection-safety — the template is
           split with ``shlex`` and run via ``create_subprocess_exec``, NEVER a shell);
        3. **echoes the transcript back quoted** (``🎙️ "…"``) so the operator sees what was
           heard;
        4. fires the transcript as a NORMAL turn (the same ``_run_turn`` path a typed message
           takes) against the active project.

        **Streaming-only.** Running the transcript as a turn rides the streaming turn path; in
        one-shot mode it replies a clean "needs streaming mode" notice (the message-as-turn
        machinery + per-project session live there — mirrors :meth:`on_document`). The audio (+
        any transcript ``.txt`` the transcriber writes) lives under a ``TemporaryDirectory`` and
        is cleaned in a ``finally`` (RB1). **SB3:** only a size SUMMARY is logged — never the
        audio bytes nor the raw transcript (the transcript echo to the chat is by design; the
        logs stay body-free).
        """
        if not await self._ok(update) or update.message is None:
            return
        await self._maybe_welcome(update)
        msg = update.message
        chat_id = update.effective_chat.id
        # The attachment is a Telegram Voice (opus .ogg) or an Audio (music/voice file). The
        # registration filter guarantees one is present; be defensive (RB1) about neither.
        attachment = msg.voice or msg.audio
        if attachment is None:
            return
        # Graceful-off FIRST: with no transcriber configured we never download — just point the
        # operator at the setup (RB2). Sent as PLAIN TEXT (no parse_mode): the copy names the
        # literal env var TRANSCRIBE_CMD, whose underscore would open an unterminated Markdown
        # italic span and make Telegram REJECT the whole send (BUG A — same class as the P9
        # `/help $*` break, where the user then gets nothing). Plain text can't be misparsed.
        if not (self.config.transcribe_cmd or "").strip():
            await msg.reply_text(VOICE_SETUP_MESSAGE)
            return
        # Streaming-only: the transcript runs as a turn (a per-project streaming concept). In
        # one-shot mode reply a clean notice rather than transcribing into nothing.
        if self.streaming is None:
            await msg.reply_text(
                "🎙️ Voice notes need streaming mode (ENGINE_MODE=streaming) — the transcript "
                "runs as a turn against a project. In one-shot mode, please type your message."
            )
            return
        # Size-cap BEFORE download (Codex B3 / RB2): Telegram reports file_size on a Voice /
        # Audio. A declared size over the cap is refused cleanly — never pull oversized audio
        # into memory. A missing/odd size (defensive) falls through to the post-download cap
        # below so an over-cap (or under-reported) file can never be transcribed either way.
        cap = self.config.file_max_bytes
        declared = getattr(attachment, "file_size", None)
        if isinstance(declared, int) and declared > cap:
            await msg.reply_text(
                f"🎙️ That voice note is too large ({declared // 1024} KB) — "
                f"the limit is {cap // 1024} KB."
            )
            return
        # Download + transcribe under a per-turn temp dir, cleaned in finally (RB1). The audio
        # path is a temp file the bot names (never operator-controlled), so the transcribe
        # subprocess gets only a controlled path (SB3/injection-safety — see voice.py).
        tmpdir = tempfile.mkdtemp(prefix="tg-voice-")
        try:
            audio_path = os.path.join(tmpdir, "audio.ogg")
            try:
                tg_file = await attachment.get_file()
                raw = await tg_file.download_as_bytearray()
            except Exception:
                # RB1: a transient download failure must never crash the handler.
                log.debug("voice download failed for chat %s", chat_id, exc_info=True)
                await msg.reply_text(
                    "⚠️ Couldn't download that voice note — please try sending it again."
                )
                return
            # SB3: log a SIZE SUMMARY only — never the audio bytes.
            log.info("chat %s received a voice note (%d KB)", chat_id, len(raw) // 1024)
            # Post-download cap (Codex B3 / defense-in-depth): a missing/under-reported declared
            # size means the pre-check above passed — re-check the ACTUAL bytes and never write /
            # transcribe an over-cap file.
            if len(raw) > cap:
                await msg.reply_text(
                    f"🎙️ That voice note is too large ({len(raw) // 1024} KB) — "
                    f"the limit is {cap // 1024} KB."
                )
                return
            try:
                with open(audio_path, "wb") as fh:
                    fh.write(bytes(raw))
            except OSError:
                log.debug("voice temp write failed for chat %s", chat_id, exc_info=True)
                await msg.reply_text("⚠️ Couldn't process that voice note — check the bot logs.")
                return
            try:
                transcript = await transcribe(
                    template=self.config.transcribe_cmd,
                    audio_path=audio_path,
                    work_dir=tmpdir,
                    timeout=self.config.transcribe_timeout_seconds,
                )
            except TranscriptionUnavailable:
                # Defensive: the cmd was set at handler entry but normalizes to empty here —
                # treat as graceful-off (RB2). (The entry guard above normally catches this.)
                # Plain text, same as the entry-guard send — never Markdown (BUG A).
                await msg.reply_text(VOICE_SETUP_MESSAGE)
                return
            except TranscriptionError as exc:
                # A real transcriber failure — bot-authored, body-free message (SB3); the raw
                # detail is already logged at DEBUG inside voice.transcribe. ``str(exc)`` is a
                # fixed bot-authored summary (exit code / "timed out" / "empty"), never raw stderr.
                log.debug("transcription failed for chat %s: %s", chat_id, exc)
                await msg.reply_text(
                    f"⚠️ Couldn't transcribe that voice note — {exc}. "
                    "Check TRANSCRIBE_CMD and the bot logs, or type your message."
                )
                return
        finally:
            # RB1: always clean the temp dir (audio + any transcript .txt the transcriber wrote),
            # even on an early return / exception. Best-effort — a cleanup failure never raises.
            shutil.rmtree(tmpdir, ignore_errors=True)
        # Echo the transcript back QUOTED so the operator sees what was heard (R6: escape it so
        # a stray </>& renders inert; the transcript is operator speech, not a command).
        await msg.reply_text(
            f'🎙️ "{html.escape(transcript, quote=False)}"', parse_mode="HTML"
        )
        # Fire the transcript as a NORMAL turn — the exact path a typed message takes.
        await self._run_turn(
            update, ctx, chat_id, transcript,
            reply_to_message_id=self._reply_to_id(update),
        )

    async def cmd_get(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """P10 T3 — ``/get <path>`` uploads an in-root file back to the chat (SB1/SB2/RB2).

        SB1: allowlist-gated (the ``_ok`` recheck). **Streaming-only:** ``<path>`` resolves
        relative to the active project's cwd, a per-project streaming concept; one-shot mode
        replies a clean notice. Order, fail-fast + secure:

        1. ``_ok`` + streaming check + a usage message for a missing arg.
        2. **SB2 confinement** — resolve ``<path>`` (relative to the active cwd, or absolute)
           through :func:`~claude_tg.paths.resolve_within_roots`. An out-of-root target
           (``../`` / absolute / a symlink that points outside) is REFUSED ("outside the
           permitted roots") and nothing is read (the SAME resolver ``/cd`` · ``/new`` use).
        3. Missing / not-a-regular-file → refuse cleanly (RB2).
        4. **Size-cap** — a file over ``config.file_max_bytes`` is refused (never uploaded).
        5. Else ``bot.send_document`` it to the chat. RB1: a send failure → a clean message.

        The shown path is wrapped in ``<code>`` (P8/R6) so it renders inert. SB3: only a size
        summary is logged — never the file bytes.
        """
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is None:
            await update.message.reply_text(
                "📎 /get applies to streaming mode only — it sends a file from the active "
                "project, and one-shot mode has no per-project working directory."
            )
            return
        chat_id = update.effective_chat.id
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if not arg:
            await update.message.reply_text("Usage: /get <path>")
            return
        cwd = self.streaming.get_cwd(chat_id)
        # SB2: canonicalize (resolves symlinks AND ..) and confine to ALLOWED_ROOTS. A path
        # that escapes the permitted roots is refused here and never read/uploaded.
        try:
            target = resolve_within_roots(
                arg,
                cwd=cwd,
                allowed_roots=self.config.allowed_roots,
                allow_any=self.config.allow_any_path,
            )
        except PathNotAllowed:
            await update.message.reply_text(
                f"❌ Path not allowed (outside the permitted roots): {code_path(arg)}",
                parse_mode="HTML",
            )
            return
        # Must be an existing REGULAR file (not a directory / device / missing). RB2.
        if not target.is_file():
            await update.message.reply_text(
                f"❌ No such file: {code_path(arg)}", parse_mode="HTML"
            )
            return
        cap = self.config.file_max_bytes
        try:
            size = target.stat().st_size
        except OSError:
            await update.message.reply_text(
                f"❌ Couldn't read {code_path(arg)}.", parse_mode="HTML"
            )
            return
        if size > cap:
            await update.message.reply_text(
                f"❌ That file is too large to send ({size // 1024} KB) — "
                f"the limit is {cap // 1024} KB."
            )
            return
        # SB3: log a size SUMMARY only — never the file bytes.
        log.info("chat %s requested /get %r (%d KB)", chat_id, target.name, size // 1024)
        try:
            # Codex NB: open the file inside a context manager so its descriptor is ALWAYS
            # closed (no fd leak) — PTB reads InputFile's stream into the outgoing request
            # before this await resolves, so closing on the way out is safe.
            with open(target, "rb") as fh:
                await ctx.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(fh, filename=target.name),
                )
        except Exception:
            # RB1: a transient upload failure must never crash the handler — clean message.
            log.debug("send_document failed for chat %s", chat_id, exc_info=True)
            await update.message.reply_text(
                f"⚠️ Couldn't send {code_path(target.name)} — please try again.",
                parse_mode="HTML",
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
        command_initiated: bool = False,
        images: list[ImageInput] | None = None,
    ) -> None:
        """Run ``text`` as one turn for ``chat_id`` — the shared dispatch both the message
        handler and the skill-launch passthrough route through (one path, no duplication).

        Streaming mode hands the turn to the :class:`StreamingSession`; one-shot mode runs
        it through the runner and replies (chunked). Callers MUST have already done the
        ``_ok`` allowlist recheck and the empty-text guard. ``reply_to_message_id`` (D5) is
        forwarded to the streaming session for free-text reply-to routing; it is ignored in
        one-shot mode (no interactive holds there). ``command_initiated`` (a macro ``/run``)
        is likewise forwarded so the streaming session opens a FRESH turn instead of letting
        the expanded text satisfy a pending free-text hold; one-shot mode has no holds, so it
        ignores the flag and just runs the text.

        **P10 T1 — ``images`` (multimodal).** When the operator sends a photo/image-document
        the handler passes the decoded :class:`~claude_tg.engine.ImageInput` list here. It is
        a STREAMING-only path (the SDK's ``--input-format stream-json`` is the proven
        mechanism; oneshot multimodal is the deferred bigger lift), so in one-shot mode the
        turn is refused with a clean "images need streaming mode" message rather than silently
        dropping the pixels and running text-only. In streaming mode the images thread through
        to ``handle_message`` → ``engine.send(images=…)``.
        """
        if self.streaming is not None:
            await self._on_message_streaming(
                update, ctx, chat_id, text,
                reply_to_message_id=reply_to_message_id,
                command_initiated=command_initiated,
                images=images,
            )
            return

        # P10 T1 oneshot fallback (documented choice): the multimodal turn needs the SDK's
        # streaming ``--input-format stream-json`` path, which one-shot mode does not run. The
        # lower-risk behavior is a clean refusal (NOT silently running the caption text-only,
        # which would hide that the image was ignored). The text/turn path is otherwise 100%
        # unchanged for one-shot.
        if images:
            await update.message.reply_text(
                "🖼️ Sending an image needs streaming mode (ENGINE_MODE=streaming). "
                "In one-shot mode I can't see attached images yet."
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

    async def _reply_html_chunked(
        self, update: Update, text: str, keyboard=None
    ) -> None:
        """Send ``text`` as ``parse_mode="HTML"``, split to Telegram-safe chunks (P11 T1 live-fix).

        The hard backstop against BadRequest "message too long": a ``/sessions`` listing on a
        machine with hundreds of sessions (or any long HTML reply) is routed through
        :func:`~claude_tg.util.split_message` so no single send exceeds Telegram's 4096-UTF-16
        limit. ``split_message`` **prefers to break on a newline**, and every session row is a
        complete, self-contained line (no ``<code>``/``<b>`` span crosses a ``\\n``), so a chunk
        boundary never splits an HTML tag and each chunk stays valid HTML. The optional inline
        ``keyboard`` is attached to the **last** chunk only — never duplicated per chunk. Blank
        chunks are skipped; an all-blank/empty text still sends one (possibly empty) message so
        the operator always gets a reply. ``update.message`` is non-None at the call sites.
        """
        chunks = [c for c in split_message(text) if c.strip()] or [text]
        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            await update.message.reply_text(
                chunk,
                parse_mode="HTML",
                reply_markup=keyboard if i == last else None,
            )

    # ---- streaming mode (ENGINE_MODE=streaming) -----------------------------
    async def _on_message_streaming(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        command_initiated: bool = False,
        images: list[ImageInput] | None = None,
    ) -> None:
        """Drive the streaming engine for one message (delegates to StreamingSession).

        Binds send/edit closures to this chat (the actual Telegram I/O the render layer
        deferred), then hands the turn to the driver. A second concurrent message to the
        SAME running project raises :class:`StreamingBusy` (one turn per project) and we
        reply the same "still working" notice as one-shot mode. SB4: the text is the
        engine's prompt, never interpolated into a shell command/argument.
        ``reply_to_message_id`` (D5) is forwarded so a reply to a free-text prompt routes
        the answer to the project that owns that prompt. **P10 T1:** ``images`` (the decoded
        photo/screenshot) is forwarded so the turn is multimodal.
        """
        assert self.streaming is not None
        bot = ctx.bot

        async def send(
            *, text: str, reply_markup=None, parse_mode=None, link_preview_options=None
        ) -> int | None:
            # T6/P9: ``link_preview_options`` (a telegram.LinkPreviewOptions) is set by the
            # session's notification sends to suppress link previews so a path/URL in a ping
            # does not balloon into a preview card; it is None (Telegram default) for ordinary
            # sends. Forwarded straight to Bot.send_message (PTB 21.x API).
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                link_preview_options=link_preview_options,
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
            # P10 T1: pass ``images`` ONLY when present, so a pure TEXT turn calls
            # handle_message with the EXACT pre-P10 signature — every existing FakeStreaming
            # (whose handle_message has no ``images`` kwarg) keeps working verbatim. The image
            # path supplies the kwarg to the real StreamingSession (which accepts it).
            extra = {"images": images} if images else {}
            captured_free_text = await self.streaming.handle_message(
                chat_id, text, send=send, edit=edit, delete=delete,
                reply_to_message_id=reply_to_message_id,
                command_initiated=command_initiated,
                **extra,
            )
        except StreamingBusy:
            await update.message.reply_text(
                "⏳ Still working on your previous message — it'll reply when done. "
                "Send one message at a time."
            )
            return
        # T6/P9: the message was consumed as a free-text capture (a reply to an "Other"/reject
        # prompt) → dismiss the one-time quick-reply chips so they don't linger over the next,
        # unrelated turn. Best-effort (RB1): a failed remove must never break the turn — the
        # chips are one_time_keyboard anyway, so this is the belt-and-braces scope guard. The
        # remove rides its own minimal message (Telegram has no standalone "remove keyboard").
        if captured_free_text:
            try:
                await update.message.reply_text("✓", reply_markup=quick_reply_dismiss())
            except Exception:
                log.debug("quick-reply chip dismissal failed", exc_info=True)

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
        # T6/P9: a [Open <project>] switch tap routes by project name. The session decoded +
        # validated the name and returned it on ``switch_to``; the bot performs the actual
        # switch through the SHARED /switch helper (the SB2 path re-validation the session
        # can't do). SB1 is already enforced above (the _authorized recheck), so a
        # non-allowlisted tap never reaches here — it is answered + dropped, switching
        # nothing. We answer the query (stop the spinner), perform the switch, and reply the
        # result; nothing else (no free-text arm) applies to a switch.
        if outcome.switch_to:
            await self._answer_callback(query, outcome.note if outcome.handled else None)
            reply, parse_mode = self._switch_active(chat.id, outcome.switch_to)
            try:
                await query.message.reply_text(reply, parse_mode=parse_mode)
            except Exception:
                log.debug("switch-button reply send failed", exc_info=True)
            return
        # P11 T2: an [Attach] tap routes by session id. The session decoded + validated the id
        # (the session-id shape) and returned it on ``attach_session_id``; the bot performs the
        # actual adopt through ``streaming.attach_session`` — which does ALL the policy (the
        # SB2 cwd confinement + the fork-if-live decision + the registry write). SB1 is already
        # enforced above (the _authorized recheck), so a non-allowlisted tap never reaches here.
        # We answer the query (stop the spinner), do the attach, and reply its outcome; nothing
        # else (no free-text arm) applies to an attach.
        if outcome.attach_session_id and self.streaming is not None:
            await self._answer_callback(query, outcome.note if outcome.handled else None)
            result = self.streaming.attach_session(chat.id, outcome.attach_session_id)
            try:
                await query.message.reply_text(result.message, parse_mode=result.parse_mode)
            except Exception:
                log.debug("attach-button reply send failed", exc_info=True)
            return
        await self._answer_callback(query, outcome.note if outcome.handled else None)
        if outcome.expects_text:
            # D5: name-echo the free-text prompt so the operator knows which project the
            # next message resolves; capture the prompt's message_id -> tool_use_id so a
            # reply-to it routes by id (the reply-to escape hatch). A missing project_name
            # (defensive) falls back to the plain toast note so the prompt is never empty.
            # T6/P9: attach the one-time quick-reply chips (ReplyKeyboardMarkup) so common
            # answers ("proceed", "keep it minimal", …) are one tap; they type the operator's
            # next message and the free-text path resolves it as usual. The chips are
            # one_time_keyboard AND explicitly removed once the free text is captured (see
            # _on_message_streaming), so they stay scoped to THIS prompt.
            prompt = (
                free_text_prompt(outcome.project_name)
                if outcome.project_name
                else f"✏️ {outcome.note}…"
            )
            try:
                sent = await query.message.reply_text(
                    prompt, reply_markup=quick_reply_keyboard()
                )
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

    async def _post_shutdown(self, _app: Application) -> None:
        """Clean shutdown: stop every engine + cancel every live-mirror watch (P11 T3).

        PTB calls this as the application stops. It delegates to
        :meth:`~claude_tg.stream_session.StreamingSession.shutdown`, which cancels all
        read-only ``/watch`` tail tasks (so no mirror outlives the bot) and stops every
        started engine. Best-effort (RB1): a failure here must never block the shutdown — a
        crash on exit is logged, not raised. No-op in one-shot mode (no streaming session).
        """
        if self.streaming is None:
            return
        try:
            await self.streaming.shutdown()
        except Exception:
            log.warning("error during streaming-session shutdown", exc_info=True)

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
            .post_shutdown(self._post_shutdown)
            .build()
        )
        allowed = filters.Chat(chat_id=list(self.config.allowed_chat_ids))
        app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=allowed))
        app.add_handler(CommandHandler("status", self.cmd_status, filters=allowed))
        app.add_handler(CommandHandler("reset", self.cmd_reset, filters=allowed))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel, filters=allowed))
        app.add_handler(CommandHandler("yolo", self.cmd_yolo, filters=allowed))
        app.add_handler(CommandHandler("unyolo", self.cmd_unyolo, filters=allowed))
        # P12 T-PLAN-2: /plan arms the next turn as a plan turn (streaming mode only; the
        # handler replies a streaming-only notice in one-shot). Same `allowed` chat filter
        # (SB1) + registered BEFORE the on_skill_command COMMAND passthrough so it is consumed
        # here, not forwarded to the session as a skill.
        app.add_handler(CommandHandler("plan", self.cmd_plan, filters=allowed))
        # T4 (P9): per-project model routing (streaming mode only; the handlers reply a
        # streaming-only notice in one-shot). /model is the alias of /auto (so /model default
        # clears the override); both names route to cmd_auto. Registered with the SAME
        # `allowed` chat filter (SB1) and BEFORE the on_skill_command COMMAND passthrough so
        # they are consumed here, not forwarded to the session as skills.
        app.add_handler(CommandHandler("fast", self.cmd_fast, filters=allowed))
        app.add_handler(CommandHandler("deep", self.cmd_deep, filters=allowed))
        app.add_handler(CommandHandler(["auto", "model"], self.cmd_auto, filters=allowed))
        app.add_handler(CommandHandler("pwd", self.cmd_pwd, filters=allowed))
        # P10 T3: /get <path> uploads an in-root file back to the chat (streaming mode only;
        # one-shot replies a notice). Same `allowed` chat filter (SB1) + registered BEFORE
        # the on_skill_command COMMAND passthrough so first-match-wins consumes it here
        # rather than forwarding /get to the session as a skill.
        app.add_handler(CommandHandler("get", self.cmd_get, filters=allowed))
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
        # P11 T1: /sessions — read-only discovery of ALL Claude sessions on the Mac (incl.
        # the live orchestrator), merged with the bot's own projects. Same `allowed` chat
        # filter (SB1) + registered BEFORE the skill passthrough so it isn't forwarded.
        app.add_handler(CommandHandler("sessions", self.cmd_sessions, filters=allowed))
        # P11 T2: /attach <session-id> — adopt ANY discovered Claude session as a controllable
        # project (+ switch to it), forking it if it is live elsewhere. Same `allowed` chat
        # filter (SB1) + registered BEFORE the skill passthrough so it isn't forwarded as a
        # skill. Streaming mode only (cmd_attach replies the one-shot notice otherwise).
        app.add_handler(CommandHandler("attach", self.cmd_attach, filters=allowed))
        # P11 T3: /watch <session-id> — READ-ONLY live mirror of any Mac session's transcript
        # onto this chat (the follow half of "entirety remote"); /unwatch stops it. Streaming
        # mode only (the per-chat send gate + background-task surface). Both reply the one-shot
        # notice otherwise. Registered as specific CommandHandlers so PTB's first-match-wins
        # routing keeps them off the skill-forwarding path.
        app.add_handler(CommandHandler("watch", self.cmd_watch, filters=allowed))
        app.add_handler(CommandHandler("unwatch", self.cmd_unwatch, filters=allowed))
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
        # P10 T1 (multimodal): a photo OR an image-document → on_photo (a native multimodal
        # turn). SB1: the SAME `allowed` chat filter as every other handler (the `_ok` recheck
        # inside on_photo is defense in depth). filters.Document.IMAGE matches image/* sent as
        # an uncompressed file (so a PNG screenshot keeps full fidelity); filters.PHOTO matches
        # Telegram's compressed photo. Registered BEFORE the COMMAND passthrough; neither
        # overlaps a TEXT/COMMAND message, so handler ordering is unaffected.
        app.add_handler(
            MessageHandler(allowed & (filters.PHOTO | filters.Document.IMAGE), self.on_photo)
        )
        # P10 T3 (file receive): a NON-image Document → on_document (saved, path-confined, into
        # the active project's cwd). SB1: the SAME `allowed` chat filter as every other handler
        # (the `_ok` recheck inside on_document is defense in depth). The filter is the EXACT
        # COMPLEMENT of on_photo's image-document filter — ``filters.Document.ALL &
        # ~filters.Document.IMAGE`` — so an image document still routes to on_photo (T1's
        # multimodal path) and only a non-image document (.py/.log/.pdf/.zip …) reaches here.
        # The two are disjoint; neither steals the other's messages. Registered BEFORE the
        # COMMAND passthrough; a Document message is neither TEXT nor COMMAND, so handler
        # ordering is unaffected.
        app.add_handler(
            MessageHandler(
                allowed & filters.Document.ALL & ~filters.Document.IMAGE, self.on_document
            )
        )
        # P10 T2 (voice): a voice note OR an audio file → on_voice (transcribe → run as a turn).
        # SB1: the SAME `allowed` chat filter as every other handler (the `_ok` recheck inside
        # on_voice is defense in depth). filters.VOICE matches Telegram's opus voice note;
        # filters.AUDIO matches an audio file. Neither overlaps PHOTO/Document/TEXT/COMMAND, so
        # it never collides with the photo/document handlers above or the skill passthrough
        # below. Voice is pluggable + graceful-off: with no TRANSCRIBE_CMD configured the
        # handler replies a clean setup message (not a command — no COMMAND_MENU change).
        app.add_handler(
            MessageHandler(allowed & (filters.VOICE | filters.AUDIO), self.on_voice)
        )
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
