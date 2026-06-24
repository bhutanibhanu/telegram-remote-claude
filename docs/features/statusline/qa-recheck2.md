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
session id: 019efc02-c0d9-74c3-82da-f2bc6badbaf0
--------
user
Final re-check of the STATUSLINE feature. You previously found B2 (foreground-switch race) STILL-OPEN: the async ctx fix added a /switch window inside `_statusline_text` (it captured the foreground, then awaited get_context_usage, and a /switch during that await wrote a stale line). It's now claimed fixed. Run `git diff $(git merge-base HEAD main)..HEAD` (latest commit closes B2 residual).

Claimed fix: `_statusline_text(chat_id)` returns `(text, built_for)`; the gated write helpers (`_statusline_gated_edit`, `_statusline_send_and_pin`) do a FINAL SYNCHRONOUS `_is_foreground(chat_id, built_for)` check after ALL awaits (gate wait + the ctx await inside the rebuild), with NO await between that check and the edit/send — so a /switch during any preceding await skips the stale write; the /switch's own trigger writes the correct line.

Verify ONLY:
1. B2: is there now NO `await` between the final `_is_foreground(built_for)` check and the actual edit/send in BOTH helpers? Can any /switch during any await still cause a stale previous-project line to be written? Is the skip safe (no loop, the correct line still gets written by the switch trigger)?
2. No regression: B1 (ctx awaited, real % wins), B3 (/plan shows during the turn), pin-retry, RB1 (failures never break a turn), foreground-only concurrency, SB3 body-free — all still hold?

Respond ONLY in this format, no preamble:
## B2 status
- CLOSED | STILL-OPEN — why (cite file:line)
## Regressions
- (none, or list)
## Verdict
SHIP | NO_SHIP
## Reasoning
(2-3 sentences)
codex
I’ll inspect the current branch diff against `main` and focus only on the statusline paths you named. After that I’ll verify the exact await/check/write ordering and the previous regression cases.
exec
/bin/zsh -lc 'git diff $(git merge-base HEAD main)..HEAD --stat && git diff $(git merge-base HEAD main)..HEAD -- src || true' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
 claude_tg/bot.py                     |  187 +++++-
 claude_tg/engine/adapter_sdk.py      |  193 ++++++
 claude_tg/engine/engine.py           |   31 +
 claude_tg/render.py                  |  155 +++--
 claude_tg/session_store.py           |   65 ++
 claude_tg/stream_session.py          |  511 +++++++++++++++-
 docs/features/statusline/design.md   |  464 ++++++++++++++
 docs/features/statusline/handoff.md  |   38 ++
 docs/features/statusline/progress.md |   31 +
 docs/features/statusline/qa.md       |   13 +
 docs/features/statusline/state.json  |    7 +
 tests/test_bot_streaming.py          |  135 +++-
 tests/test_engine.py                 |  249 ++++++++
 tests/test_render.py                 |  285 +++++++--
 tests/test_security_reliability.py   |    5 +-
 tests/test_session_store.py          |   81 +++
 tests/test_skill_launch.py           |    9 +-
 tests/test_stream_session.py         | 1120 +++++++++++++++++++++++++++++++++-
 18 files changed, 3429 insertions(+), 150 deletions(-)

codex
The diff is broad, but the relevant implementation is in `claude_tg/bot.py`, `render.py`, and the stream/session tests. I’m narrowing to `_statusline_text`, the gated helpers, and the switch/statusline triggers now.
exec
/bin/zsh -lc 'rg -n "_statusline_text|_statusline_gated_edit|_statusline_send_and_pin|_is_foreground|statusline|switch|pin" claude_tg/bot.py claude_tg/stream_session.py claude_tg/render.py tests/test_bot_streaming.py tests/test_stream_session.py tests/test_render.py' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
claude_tg/render.py:7:and does the real rate-limit waiting. Keeping the decision pure makes the whole
claude_tg/render.py:60:from typing import Final, Literal, Optional
claude_tg/render.py:176:# is just a single action char (o/s/d), keeping the data tiny next to the ~49-byte id.
claude_tg/render.py:203:#: <= 36 B, well under the 64-byte limit. The tap is a NAVIGATION action (switch the active
claude_tg/render.py:209:#: 'w' are taken). Like ``switch`` it does NOT route to a held ``tool_use_id`` — it carries the
claude_tg/render.py:216:#: ask/plan/permission/other/switch callbacks (distinct kind char + a session id, not a
claude_tg/render.py:240:#: :func:`decode_callback` to reject a forged/over-long switch-callback name at the trust
claude_tg/render.py:262:    kind: Literal["ask", "other", "plan", "permission", "switch", "attach"]
claude_tg/render.py:268:    #: The TARGET project name of a ``switch`` tap (T6/P9) — the project to make active.
claude_tg/render.py:269:    #: ``None`` for every other kind (which route by ``tool_use_id``); for a ``switch`` the
claude_tg/render.py:271:    switch_to: Optional[str] = None
claude_tg/render.py:291:#: Fixed payload char for a ``switch`` callback (the kind alone + the name carry the
claude_tg/render.py:292:#: meaning; a constant payload keeps the 3-field scheme uniform). 's' for switch.
claude_tg/render.py:296:def encode_switch_callback(name: str) -> str:
claude_tg/render.py:297:    """Encode a ``[Open <project>]`` switch tap into <=64-byte ``callback_data`` (T6/P9).
claude_tg/render.py:299:    The switch kind does NOT route by ``tool_use_id`` — it carries the TARGET PROJECT NAME
claude_tg/render.py:306:        raise ValueError("project name is required for a switch callback")
claude_tg/render.py:313:#: 'x' for attach (avoiding 's', which is the switch payload — keeps the two visually distinct).
claude_tg/render.py:319:#: discovery — without hard-coding the exact UUID grouping (defensive, not a parser). It can
claude_tg/render.py:357:    id or payload. The ``switch`` kind has its own builder
claude_tg/render.py:358:    (:func:`encode_switch_callback`) — it carries a project NAME, not a ``tool_use_id``.
claude_tg/render.py:447:        # Defensive: the payload must be the fixed switch char and the name must look like
claude_tg/render.py:454:        # The id field is unused for a switch (the name rides ``switch_to``); keep a sentinel
claude_tg/render.py:456:        return Callback(kind="switch", tool_use_id="-", switch_to=tool_use_id)
claude_tg/render.py:488:    Indexing — not label round-tripping — keeps ``callback_data`` tiny AND robust to
claude_tg/render.py:739:    """Build the ``[Open <project>]`` switch button for a background ping (T6/P9).
claude_tg/render.py:741:    A single inline button whose ``callback_data`` is the compact switch encoding
claude_tg/render.py:742:    (:func:`encode_switch_callback` → ``w|<name>|s``): a tap routes through
claude_tg/render.py:744:    recheck there) → switch the chat's active project to ``name`` (reusing ``/switch``'s
claude_tg/render.py:747:    + done pings so the operator can jump straight to the project from the ping.
claude_tg/render.py:750:    nothing (the button text is plain, not HTML); :func:`encode_switch_callback` enforces the
claude_tg/render.py:758:                    callback_data=encode_switch_callback(name),
claude_tg/render.py:793:    "proceed", "keep it minimal", "explain first", "use TypeScript") are one tap — tapping a
claude_tg/render.py:857:# sends a name-prefixed ping so the operator knows WHICH project and can answer
claude_tg/render.py:858:# it. A foreground project renders inline as today (no ping). **T3 only provides
claude_tg/render.py:868:# ping, a short error label the caller already produced body-free (e.g. the
claude_tg/render.py:872:# session_store._NAME_RE), so the name is safe to interpolate with no escaping;
claude_tg/render.py:885:#: held request straight to its ping with no extra branching. The phrases are constant —
claude_tg/render.py:886:#: no event field is interpolated (SB3): a permission/ask/plan ping reveals only that the
claude_tg/render.py:895:#: did not expect; degrade to a generic, still body-free "needs attention" ping).
claude_tg/render.py:900:    """The ``" (N more waiting)"`` queued-counter suffix for a ping (T6/P9), or ``""``.
claude_tg/render.py:903:    pulls it from the per-chat run queue). When ≥1 the ping (and the ``/status`` runs line)
claude_tg/render.py:913:    """Body-free ping for a BACKGROUND project that needs the operator (D4; SB3).
claude_tg/render.py:927:    body, or tool input) is ever interpolated, so a ping cannot leak content. ``name`` is an
claude_tg/render.py:936:    """Body-free ping for a BACKGROUND project that finished cleanly (D4; SB3).
claude_tg/render.py:948:    """Body-free ping for a BACKGROUND project that errored (D4; SB3).
claude_tg/render.py:956:    ``short_error`` degrades to a generic ``error`` so the ping is never an empty tail (RB1).
claude_tg/render.py:999:# status column on ``/projects``. T3 provides ONLY the value→label mapping the
claude_tg/render.py:1037:    Pure mapping, no I/O. ``running`` → ``"running"``, ``awaiting_approval`` →
claude_tg/render.py:1231:    """Trim a relevance-ordered session list to ``limit`` rows WITHOUT dropping the operator's own.
claude_tg/render.py:1332:#: row). Keeping it small also keeps the message+keyboard well within Telegram's limits.
claude_tg/render.py:1395:# the (SB4-validated) name, the interval, a relative next-run, a ⏸ paused marker, the pinned
claude_tg/render.py:1446:    the pinned **project** if any. ⭐ The schedule's PROMPT text is **NOT** displayed (SB3 /
claude_tg/render.py:1476:        # next-run, paused marker, and pinned project ONLY. The schedule's PROMPT text is NOT
claude_tg/render.py:1518:    ``/a`` "commands") — ugly and confusing. Wrapping the path in ``<code>`` makes Telegram
claude_tg/render.py:1529:# STATUSLINE — the pinned, edited-in-place mobile statusline (T-SL-CORE / design §5)
claude_tg/render.py:1532:#: Map a model **id** to its short statusline label by family. Each pattern is matched
claude_tg/render.py:1551:    """Reduce a model **id** to its short statusline label (``opus``/``sonnet``/``haiku``).
claude_tg/render.py:1557:    as ``""`` (the caller — :func:`format_statusline` — never passes one; the active model is
claude_tg/render.py:1559:    :func:`format_statusline` escapes every interpolated field once (SB3).
claude_tg/render.py:1573:def format_statusline(
claude_tg/render.py:1582:    """Build the pinned mobile statusline body (pure; no I/O) — the owner-LOCKED format.
claude_tg/render.py:1931:    # per-turn ``· N turns · $X.XX`` footer is GONE — the pinned statusline is now the
claude_tg/render.py:2215:    sends/edits and the actual inter-edit waiting (sleeping until ``next_due_at``).
claude_tg/render.py:2529:    "encode_switch_callback",
claude_tg/render.py:2571:    # pinned mobile statusline (STATUSLINE T-SL-CORE)
claude_tg/render.py:2572:    "format_statusline",
tests/test_render.py:59:    encode_switch_callback,
tests/test_render.py:60:    format_statusline,
tests/test_render.py:137:# Event -> RenderAction mapping
tests/test_render.py:191:    # the body (wrapping is a rendering change, not a disclosure change).
tests/test_render.py:725:    # body-free "needs attention" ping rather than raising / leaking the raw kind.
tests/test_render.py:751:    # tool_input must NEVER surface in the ping — the attention phrase is FIXED, so even
tests/test_render.py:834:# render as <code> (stop Telegram /segment auto-linkify); injection-proof escaping
tests/test_render.py:846:# after dropping these tags, NO bare "<" / ">" may remain (else Telegram rejects the
tests/test_render.py:972:    # SB3 regression: wrapping in <code> is a RENDERING change only — it must NOT expand the
tests/test_render.py:1300:    # prove no HTML escaping. A tool_error would render body-free (covered above) — the
tests/test_render.py:1406:    # ``...behind_committed_backlog...`` test pins that collision-free boundary.)
tests/test_render.py:1432:    # that actually matters (ahead of FUTURE status) is pinned by the test above.
tests/test_render.py:1453:    # sends in one interval (over-budget under status churn). This pins the combined budget by
tests/test_render.py:1517:# The per-turn "· N turns · $X.XX" footer is removed from routine output: the pinned
tests/test_render.py:1518:# statusline is now the persistent "state after the turn" surface, and the turn's
tests/test_render.py:1585:#   * switch callback codec round-trip + byte budget + defensive decode.
tests/test_render.py:1592:def test_switch_callback_round_trips():
tests/test_render.py:1593:    # T6: encode_switch_callback -> decode_callback recovers kind="switch" + the project name.
tests/test_render.py:1594:    data = encode_switch_callback("alpha")
tests/test_render.py:1598:    assert cb.kind == "switch"
tests/test_render.py:1599:    assert cb.switch_to == "alpha"
tests/test_render.py:1602:def test_switch_callback_within_byte_budget_for_max_name():
tests/test_render.py:1605:    data = encode_switch_callback(name)
tests/test_render.py:1608:    assert cb is not None and cb.switch_to == name
tests/test_render.py:1611:def test_switch_callback_does_not_collide_with_hold_kinds():
tests/test_render.py:1612:    # T6: the switch kind char 'w' is distinct from ask/other/plan/permission, so a switch
tests/test_render.py:1614:    assert decode_callback(encode_switch_callback("alpha")).kind == "switch"
tests/test_render.py:1615:    # An ask/plan/permission callback never decodes to a switch.
tests/test_render.py:1621:def test_decode_rejects_forged_switch_name_and_payload():
tests/test_render.py:1622:    # T6 (SB1 trust boundary): a switch callback with a non-SB4 name (illegal chars / too
tests/test_render.py:1630:def test_encode_switch_callback_rejects_pipe_in_name():
tests/test_render.py:1633:        encode_switch_callback("a|b")
tests/test_render.py:1635:        encode_switch_callback("")
tests/test_render.py:1638:def test_open_project_keyboard_carries_switch_callback():
tests/test_render.py:1640:    # compact switch encoding (decodes back to the project name).
tests/test_render.py:1646:    assert decode_callback(buttons[0].callback_data).switch_to == "beta"
tests/test_render.py:1672:    # P11 T2: the attach kind char 't' is distinct from ask/other/plan/permission/switch, so
tests/test_render.py:1675:    assert decode_callback(encode_switch_callback("alpha")).kind == "switch"
tests/test_render.py:1686:    assert decode_callback("t|sess-1|s") is None         # wrong payload char ('s' is switch)
tests/test_render.py:1739:    # T6: the queued counter rides the attention/done/error pings when >0; omitted at 0.
tests/test_render.py:1753:    # SB3: even with the queued counter, the ping carries ONLY the name + a fixed phrase + a
tests/test_render.py:1780:from typing import Optional  # noqa: E402
tests/test_render.py:1982:    # live crash. This pins that the cap (not luck) is what keeps the message legal: if a
tests/test_render.py:2047:def test_schedule_listing_pinned_project_shown():
tests/test_render.py:2055:    # ALL — only the name/interval/next-run/project. (No escaping needed: it isn't shown.)
tests/test_render.py:2090:def test_format_statusline_full_set_exact_format():
tests/test_render.py:2092:    line = format_statusline(
tests/test_render.py:2103:def test_format_statusline_idle_has_no_working_marker():
tests/test_render.py:2105:    line = format_statusline(
tests/test_render.py:2112:def test_format_statusline_effort_none_shows_model_only():
tests/test_render.py:2114:    line = format_statusline(
tests/test_render.py:2121:def test_format_statusline_ctx_none_is_em_dash_never_zero():
tests/test_render.py:2123:    line = format_statusline(
tests/test_render.py:2131:def test_format_statusline_ctx_zero_is_a_real_zero_not_a_dash():
tests/test_render.py:2134:    line = format_statusline(
tests/test_render.py:2141:def test_format_statusline_working_marker_on_off():
tests/test_render.py:2142:    on = format_statusline(
tests/test_render.py:2145:    off = format_statusline(
tests/test_render.py:2154:def test_format_statusline_all_three_modes():
tests/test_render.py:2156:        line = format_statusline(
tests/test_render.py:2165:def test_format_statusline_escapes_angle_and_amp_in_name():
tests/test_render.py:2168:    line = format_statusline(
tests/test_render.py:2176:def test_format_statusline_path_shaped_name_wrapped_in_code_no_fake_link():
tests/test_render.py:2181:    line = format_statusline(
tests/test_render.py:2189:def test_format_statusline_path_with_special_chars_escaped_inside_code():
tests/test_render.py:2192:    line = format_statusline(
tests/test_render.py:2198:def test_format_statusline_escapes_odd_effort_and_mode_defensively():
tests/test_render.py:2201:    line = format_statusline(
tests/test_render.py:2207:# --- model_short_label mapping (regex/contains → family; unknown → raw id) ----
tests/test_render.py:2233:def test_statusline_carries_no_dollar_or_secret():
tests/test_render.py:2237:    line = format_statusline(
claude_tg/bot.py:13:from typing import Any
claude_tg/bot.py:85:    "approve/reject, /attach·/switch·/watch, /yolo); body-free, read-only (streaming mode)\n"
claude_tg/bot.py:112:    "/new <name> <path> — create a project at <path> and switch to it; <path> must be an "
claude_tg/bot.py:114:    "/switch <name> — switch the active project; the next message resumes it (streaming mode)\n"
claude_tg/bot.py:166:    ("new", "Create a project at a path and switch to it"),
claude_tg/bot.py:167:    ("switch", "Switch the active project"),
claude_tg/bot.py:243:#: plain text. The ``_strip_code_spans`` guard in ``tests/test_voice.py`` additionally pins the
claude_tg/bot.py:340:        # S4 switch: the streaming collaborator is constructed (by main.py) ONLY when
claude_tg/bot.py:505:        # startup, never updated by /switch) onto the chat's *active* project, so after
claude_tg/bot.py:506:        # restart→/switch→/reset it would clobber the active project's cwd with the
claude_tg/bot.py:518:            # busy": once /switch is free a BACKGROUND run in another project must NOT block
claude_tg/bot.py:605:        # STATUSLINE T-SL-WIRE: 🔒 gate → 🔒 yolo flips live on the pinned line.
claude_tg/bot.py:606:        await self._refresh_statusline(ctx.bot, update.effective_chat.id)
claude_tg/bot.py:626:        # STATUSLINE T-SL-WIRE: 🔒 yolo → 🔒 gate flips live on the pinned line.
claude_tg/bot.py:627:        await self._refresh_statusline(ctx.bot, update.effective_chat.id)
claude_tg/bot.py:658:        await self._refresh_statusline(ctx.bot, update.effective_chat.id)
claude_tg/bot.py:743:            await self._refresh_statusline(ctx.bot, update.effective_chat.id)
claude_tg/bot.py:763:        await self._refresh_statusline(ctx.bot, update.effective_chat.id)
claude_tg/bot.py:778:        STATUSLINE T-SL-WIRE: ``bot`` (the caller's ``ctx.bot``) drives the live statusline
claude_tg/bot.py:802:            await self._refresh_statusline(bot, update.effective_chat.id)
claude_tg/bot.py:907:        The multi-project surface (``/projects`` / ``/switch`` / ``/rm``) is a
claude_tg/bot.py:1047:        ``(session_id, cwd)`` as a bot project + switches to it, so the NEXT message resumes +
claude_tg/bot.py:1145:    async def cmd_switch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
claude_tg/bot.py:1149:        ``/switch`` while any turn was in flight *only because* the relay routed every
claude_tg/bot.py:1150:        inbound answer to ``_active_engine``, so flipping ``store.active`` mid-hold stranded
claude_tg/bot.py:1153:        ADR-005 D3), so switching away no longer strands anything — the prior run keeps
claude_tg/bot.py:1154:        running in the background and a tap on its prompt still resolves it. ``/switch`` is
claude_tg/bot.py:1172:            await update.message.reply_text("Usage: /switch <name>")
claude_tg/bot.py:1174:        reply, parse_mode = self._switch_active(chat_id, name)
claude_tg/bot.py:1176:        # STATUSLINE T-SL-WIRE: rewrite the pinned line for the newly-active project (its
claude_tg/bot.py:1178:        await self._refresh_statusline(ctx.bot, chat_id)
claude_tg/bot.py:1180:    def _switch_active(self, chat_id: int, name: str) -> tuple[str, str | None]:
claude_tg/bot.py:1183:        The shared core of ``/switch`` (the typed command) AND the ``[Open <project>]`` ping
claude_tg/bot.py:1192:          project is left UNCHANGED (``store.switch`` never called);
claude_tg/bot.py:1193:        * success → ``store.switch`` + a confirmation.
claude_tg/bot.py:1225:        # BEFORE activating (the design says re-validate "on switch/resume"; the resume
claude_tg/bot.py:1226:        # path is the authoritative gate, this closes the switch-time gap + improves UX).
claude_tg/bot.py:1228:        # active project is left UNCHANGED (store.switch is never called) on any refusal.
claude_tg/bot.py:1247:                "not switching. Use /new <name> <path> to point it somewhere allowed.",
claude_tg/bot.py:1250:        self.streaming.store.switch(chat_id, name)
claude_tg/bot.py:1251:        # P13 T-AUDIT: record the active-project switch (body-free session_event). Shared by
claude_tg/bot.py:1252:        # the /switch command AND the [Open <project>] ping button (both reach here), so a
claude_tg/bot.py:1253:        # switch is audited regardless of path. Best-effort (RB1) — never breaks the switch.
claude_tg/bot.py:1255:            KIND_SESSION_EVENT, chat_id=chat_id, summary="switch", name=name
claude_tg/bot.py:1267:        ``/switch`` away first. **Refuse a currently-RUNNING project too (P5 / ADR-005 D9):**
claude_tg/bot.py:1291:        # case-insensitively, so the guard must too) — switch away first.
claude_tg/bot.py:1296:                "/switch to another project first.",
claude_tg/bot.py:1342:        """Create a new project confined to the permitted roots, then switch to it.
claude_tg/bot.py:1361:        because it auto-switches the active project and the relay routed answers to
claude_tg/bot.py:1362:        ``_active_engine`` (a mid-hold switch deadlocked the parked turn). With id-routing
claude_tg/bot.py:1364:        resolves, so ``/new`` runs freely mid-run — same relaxation as ``/switch``.
claude_tg/bot.py:1424:        # Create + auto-switch. The resolved (contained) cwd is stored, never the raw arg.
claude_tg/bot.py:1439:            "and switched to it — your next message runs there.",
claude_tg/bot.py:1707:                "approve/reject, /attach·/switch·/watch, /yolo) are recorded here as they happen."
claude_tg/bot.py:1813:           pinned (so a later ``/switch`` doesn't retarget it).
claude_tg/bot.py:1857:        # The pinned target project = the chat's active project at creation (None if none yet;
claude_tg/bot.py:1908:        ``⏸`` paused marker, pinned project, TRUNCATED + HTML-escaped prompt preview — SB3).
claude_tg/bot.py:2042:        send, edit, delete, pin, unpin = self._make_chat_io(ctx.bot, chat_id)
claude_tg/bot.py:2046:            schedule, send=send, edit=edit, delete=delete, pin=pin, unpin=unpin
claude_tg/bot.py:2050:        """Build ``(send, edit, delete, pin, unpin)`` Telegram closures over ``bot`` for ``chat_id``.
claude_tg/bot.py:2058:        proactive turn renders exactly like a typed one. **STATUSLINE T-SL-WIRE:** the ``pin``/
claude_tg/bot.py:2059:        ``unpin`` closures (over ``Bot.pin_chat_message``/``unpin_chat_message``) let a proactive
claude_tg/bot.py:2060:        turn refresh the pinned statusline through the SAME path a typed turn does — SB1-confined
claude_tg/bot.py:2084:        async def pin(*, message_id: int, disable_notification: bool = True) -> None:
claude_tg/bot.py:2085:            # STATUSLINE T-SL-WIRE: silent pin (no re-ping — design §3.1); SB1-confined to this
claude_tg/bot.py:2086:            # chat_id. Best-effort upstream (the session swallows a pin failure, RB1).
claude_tg/bot.py:2087:            await bot.pin_chat_message(
claude_tg/bot.py:2092:        async def unpin(*, message_id: int) -> None:
claude_tg/bot.py:2093:            # STATUSLINE T-SL-WIRE: unpin a stale statusline message on orphan-recovery (the
claude_tg/bot.py:2094:            # one-pin invariant). SB1-confined to this chat_id; best-effort upstream (RB1).
claude_tg/bot.py:2095:            await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
claude_tg/bot.py:2097:        return send, edit, delete, pin, unpin
claude_tg/bot.py:2099:    async def _refresh_statusline(self, bot, chat_id: int) -> None:
claude_tg/bot.py:2100:        """Refresh the chat's pinned statusline after a COMMAND-driven state change (T-SL-WIRE).
claude_tg/bot.py:2102:        The ``/switch`` + knob commands (``/yolo``·``/unyolo``, ``/effort``, ``/fast``·``/deep``·
claude_tg/bot.py:2103:        ``/auto``, ``/plan``) change a field the statusline shows (worktree / mode / model /
claude_tg/bot.py:2104:        effort), so the pinned line is re-rendered to reflect it live (design §3.1). These
claude_tg/bot.py:2108:        Builds this chat's pin/edit/send closures over the persistent ``bot`` (SB1-confined to
claude_tg/bot.py:2109:        ``chat_id``) and delegates to the session's :meth:`_update_statusline` (fully best-effort,
claude_tg/bot.py:2110:        RB1 — a pin/edit failure can never break the command). A no-op in one-shot mode (no
claude_tg/bot.py:2116:            send, edit, _delete, pin, unpin = self._make_chat_io(bot, chat_id)
claude_tg/bot.py:2117:            await self.streaming._maybe_update_statusline(
claude_tg/bot.py:2118:                chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=None,
claude_tg/bot.py:2121:            # RB1: a statusline refresh must never break the command that triggered it.
claude_tg/bot.py:2122:            log.debug("command statusline refresh failed for chat %s (ignored)", chat_id, exc_info=True)
claude_tg/bot.py:2257:        cwd, so it replies a clean "needs streaming mode" message rather than dropping the
claude_tg/bot.py:2310:            # An escaping name that survived sanitization (belt-and-braces) — refuse cleanly.
claude_tg/bot.py:2664:        dropping the pixels and running text-only. In streaming mode the images thread through
claude_tg/bot.py:2689:        typing = asyncio.create_task(self._keep_typing(ctx, chat_id, stop))
claude_tg/bot.py:2700:            await asyncio.gather(typing, return_exceptions=True)
claude_tg/bot.py:2721:    async def _keep_typing(self, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, stop: asyncio.Event) -> None:
claude_tg/bot.py:2722:        """Show the 'typing…' indicator until ``stop`` is set (Claude can be slow)."""
claude_tg/bot.py:2796:            # session's notification sends to suppress link previews so a path/URL in a ping
claude_tg/bot.py:2818:        async def pin(*, message_id: int, disable_notification: bool = True) -> None:
claude_tg/bot.py:2819:            # STATUSLINE T-SL-WIRE: pin the chat's statusline message SILENTLY (no re-ping —
claude_tg/bot.py:2822:            # swallows a pin failure, RB1).
claude_tg/bot.py:2823:            await bot.pin_chat_message(
claude_tg/bot.py:2828:        async def unpin(*, message_id: int) -> None:
claude_tg/bot.py:2829:            # STATUSLINE T-SL-WIRE: unpin a STALE statusline message on orphan-recovery (the
claude_tg/bot.py:2830:            # one-pin invariant). SB1-confined to this chat_id; best-effort upstream (RB1).
claude_tg/bot.py:2831:            await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
claude_tg/bot.py:2846:                pin=pin, unpin=unpin,
claude_tg/bot.py:2881:        client's spinner stops), even when ignored.
claude_tg/bot.py:2907:        # T6/P9: a [Open <project>] switch tap routes by project name. The session decoded +
claude_tg/bot.py:2908:        # validated the name and returned it on ``switch_to``; the bot performs the actual
claude_tg/bot.py:2909:        # switch through the SHARED /switch helper (the SB2 path re-validation the session
claude_tg/bot.py:2911:        # non-allowlisted tap never reaches here — it is answered + dropped, switching
claude_tg/bot.py:2912:        # nothing. We answer the query (stop the spinner), perform the switch, and reply the
claude_tg/bot.py:2913:        # result; nothing else (no free-text arm) applies to a switch.
claude_tg/bot.py:2914:        if outcome.switch_to:
claude_tg/bot.py:2916:            reply, parse_mode = self._switch_active(chat.id, outcome.switch_to)
claude_tg/bot.py:2920:                log.debug("switch-button reply send failed", exc_info=True)
claude_tg/bot.py:2921:            # STATUSLINE T-SL-WIRE: the [Open <project>] tap shares /switch's core, so refresh
claude_tg/bot.py:2922:            # the pinned line for the newly-active project here too (parallel to cmd_switch).
claude_tg/bot.py:2923:            await self._refresh_statusline(ctx.bot, chat.id)
claude_tg/bot.py:2930:        # We answer the query (stop the spinner), do the attach, and reply its outcome; nothing
claude_tg/bot.py:2968:        """Answer a callback query (stops the client spinner); never raise (RB1)."""
claude_tg/bot.py:3039:        send, edit, delete, pin, unpin = self._make_chat_io(bot, schedule.chat_id)
claude_tg/bot.py:3041:            schedule, send=send, edit=edit, delete=delete, pin=pin, unpin=unpin
claude_tg/bot.py:3061:                log.warning("error stopping the proactive scheduler", exc_info=True)
claude_tg/bot.py:3082:        # which the test pins to the registered CommandHandlers below). Best-effort (RB1): a
claude_tg/bot.py:3131:        # forwarding /projects · /new · /switch · /rm to the session as skills.
claude_tg/bot.py:3134:        app.add_handler(CommandHandler("switch", self.cmd_switch, filters=allowed))
claude_tg/bot.py:3141:        # project (+ switch to it), forking it if it is live elsewhere. Same `allowed` chat
tests/test_bot_streaming.py:3:These cover the bot.py wiring: the ENGINE_MODE switch keeps one-shot the default; the
tests/test_bot_streaming.py:135:        self, chat_id, text, *, send, edit, delete=None, pin=None, unpin=None,
tests/test_bot_streaming.py:140:        # (the multimodal photo/screenshot). STATUSLINE T-SL-WIRE: + pin/unpin (the statusline
tests/test_bot_streaming.py:152:    async def fire_schedule(self, schedule, *, send, edit, delete=None, pin=None, unpin=None):
tests/test_bot_streaming.py:154:        # pin/unpin (accepted so the bot's _make_chat_io 5-tuple threads through). Record the
tests/test_bot_streaming.py:160:    async def _maybe_update_statusline(self, chat_id, *, send, edit, pin, unpin, for_project=None):
tests/test_bot_streaming.py:161:        # STATUSLINE T-SL-WIRE: the command-trigger refresh (_refresh_statusline) calls this on
tests/test_bot_streaming.py:162:        # the streaming session after /switch + the knob commands. This stand-in is a no-op (the
tests/test_bot_streaming.py:163:        # statusline pin/edit lifecycle is covered against a REAL session in test_stream_session);
tests/test_bot_streaming.py:313:# ENGINE_MODE switch: oneshot is the default and unchanged.
tests/test_bot_streaming.py:377:            self, chat_id, text, *, send, edit, delete=None, pin=None, unpin=None,
tests/test_bot_streaming.py:382:            captured["pin"] = pin
tests/test_bot_streaming.py:383:            captured["unpin"] = unpin
tests/test_bot_streaming.py:406:    # The query is answered (spinner stops) but the engine is NEVER touched.
tests/test_bot_streaming.py:510:async def test_cmd_reset_streaming_preserves_active_cwd_after_switch(tmp_path):
tests/test_bot_streaming.py:515:    never tracks /switch — so after restart→/switch→/reset, calling ``runner.reset`` would
tests/test_bot_streaming.py:519:    is built (so ``runner._cwds[chat] == "/work/alpha"`` — the stale value), then we switch
tests/test_bot_streaming.py:534:    store.switch(1, "beta")
tests/test_bot_streaming.py:536:    store.switch(1, "alpha")  # back to alpha so the runner seeds its stale cwd from alpha
tests/test_bot_streaming.py:543:    # Simulate the operator's /switch to beta (the runner does NOT track this).
tests/test_bot_streaming.py:544:    store.switch(1, "beta")
tests/test_bot_streaming.py:790:    # both globally; this pins the specific command).
tests/test_bot_streaming.py:826:    # P4/T5: /projects, /switch, /rm are specific CommandHandlers wired BEFORE the
tests/test_bot_streaming.py:843:    assert {"projects", "switch", "rm"} <= cmd_names
tests/test_bot_streaming.py:848:            {"projects", "switch", "rm"} & {c.lstrip("/").lower() for c in h.commands}
tests/test_bot_streaming.py:854:# P4 / T5 — multi-project navigation commands (/projects · /switch · /rm · /pwd · /cd).
tests/test_bot_streaming.py:859:# turn hold the lock so the load-bearing /switch busy-guard can be asserted.
tests/test_bot_streaming.py:1003:# ---- /switch --------------------------------------------------------------
tests/test_bot_streaming.py:1006:async def test_cmd_switch_happy_sets_active(tmp_path):
tests/test_bot_streaming.py:1012:    # cwds (this test exercises plain switch behavior; the in-roots/out-of-root SB2 paths
tests/test_bot_streaming.py:1017:    upd = make_update(1, "/switch beta")
tests/test_bot_streaming.py:1018:    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
tests/test_bot_streaming.py:1024:async def test_cmd_switch_in_roots_cwd_switches(tmp_path):
tests/test_bot_streaming.py:1025:    """QF2 / B2 (false-pass guard): /switch to a project whose stored cwd IS inside the
tests/test_bot_streaming.py:1026:    permitted roots still switches normally (the re-validation must not block valid cwds).
tests/test_bot_streaming.py:1044:    upd = make_update(1, "/switch beta")
tests/test_bot_streaming.py:1045:    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
tests/test_bot_streaming.py:1046:    assert store.get_active(1) == "beta"  # in-roots cwd → switched
tests/test_bot_streaming.py:1050:async def test_cmd_switch_out_of_root_cwd_refused_active_unchanged(tmp_path):
tests/test_bot_streaming.py:1051:    """QF2 / B2 (SB2 conformance): /switch to a project whose stored cwd is OUTSIDE the
tests/test_bot_streaming.py:1052:    permitted roots is refused; store.switch is NOT called and the active project is
tests/test_bot_streaming.py:1069:    # Spy on store.switch to prove the refusal leaves the store untouched.
tests/test_bot_streaming.py:1070:    switch_calls = []
tests/test_bot_streaming.py:1071:    orig_switch = store.switch
tests/test_bot_streaming.py:1072:    store.switch = lambda *a, **k: switch_calls.append((a, k))  # type: ignore[assignment]
tests/test_bot_streaming.py:1073:    upd = make_update(1, "/switch evil")
tests/test_bot_streaming.py:1074:    await bot.cmd_switch(upd, make_cmd_ctx(args=["evil"]))
tests/test_bot_streaming.py:1075:    store.switch = orig_switch  # type: ignore[assignment]
tests/test_bot_streaming.py:1079:    assert switch_calls == [], "store.switch must NOT be called for an out-of-root target"
tests/test_bot_streaming.py:1083:async def test_cmd_switch_missing_cwd_refused_fail_closed(tmp_path):
tests/test_bot_streaming.py:1085:    is refused rather than crashing or switching — defensive against a hand-edited/sparse
tests/test_bot_streaming.py:1113:    upd = make_update(1, "/switch nocwd")
tests/test_bot_streaming.py:1114:    await bot.cmd_switch(upd, make_cmd_ctx(args=["nocwd"]))
tests/test_bot_streaming.py:1120:async def test_cmd_switch_sb2_revalidation_still_fires_while_busy(tmp_path):
tests/test_bot_streaming.py:1121:    """P5/T7 (D2 relaxed): /switch no longer has a busy-guard, so the FIRST gate a busy
tests/test_bot_streaming.py:1122:    /switch hits is the SB2 cwd re-validation (it used to be shadowed by the busy refusal).
tests/test_bot_streaming.py:1123:    A /switch to an OUT-OF-ROOT target while a turn is in flight is now refused for the
tests/test_bot_streaming.py:1128:    (Was ``test_cmd_switch_busy_guard_precedes_revalidation``, which asserted the busy-guard
tests/test_bot_streaming.py:1155:    upd = make_update(1, "/switch evil")
tests/test_bot_streaming.py:1156:    await bot.cmd_switch(upd, make_cmd_ctx(args=["evil"]))
tests/test_bot_streaming.py:1158:    # The OUT-OF-ROOT message (SB2), NOT a busy message — the relaxed /switch reached SB2.
tests/test_bot_streaming.py:1162:    assert session.is_busy(1), "the held turn keeps running (switch did not disturb it)"
tests/test_bot_streaming.py:1168:async def test_cmd_switch_while_busy_succeeds_prior_run_untouched(tmp_path):
tests/test_bot_streaming.py:1169:    # P5/T7 — THE HEADLINE RELAXATION (D2). While a turn holds project alpha's lock, /switch
tests/test_bot_streaming.py:1170:    # to beta now SUCCEEDS (no busy refusal): store.switch IS called, active becomes beta,
tests/test_bot_streaming.py:1176:    # (was ``test_cmd_switch_while_busy_refused_store_untouched``). If the is_busy refusal is
tests/test_bot_streaming.py:1177:    # re-added to cmd_switch, store.switch is no longer called and this fails — so the
tests/test_bot_streaming.py:1178:    # relaxation is pinned, not merely uncovered.
tests/test_bot_streaming.py:1183:    # allow_any_path=True so /switch's SB2 cwd re-validation no-ops for the fake /work/beta
tests/test_bot_streaming.py:1200:    # /switch beta WHILE alpha is mid-run → SUCCEEDS (the relaxation). Spy store.switch to
tests/test_bot_streaming.py:1202:    switch_calls = []
tests/test_bot_streaming.py:1203:    orig_switch = store.switch
tests/test_bot_streaming.py:1204:    store.switch = lambda *a, **k: (switch_calls.append((a, k)), orig_switch(*a, **k))[1]  # type: ignore[assignment]
tests/test_bot_streaming.py:1205:    upd = make_update(1, "/switch beta")
tests/test_bot_streaming.py:1206:    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
tests/test_bot_streaming.py:1207:    store.switch = orig_switch  # type: ignore[assignment]
tests/test_bot_streaming.py:1211:    assert "switched to <b>beta</b>" in reply.lower(), reply
tests/test_bot_streaming.py:1213:    assert switch_calls, "store.switch MUST be called now that /switch is free mid-run"
tests/test_bot_streaming.py:1216:    assert session.is_busy(1, "alpha"), "the prior run must keep running after the switch"
tests/test_bot_streaming.py:1224:async def test_cmd_switch_unknown_name_lists_available(tmp_path):
tests/test_bot_streaming.py:1230:    upd = make_update(1, "/switch nope")
tests/test_bot_streaming.py:1231:    await bot.cmd_switch(upd, make_cmd_ctx(args=["nope"]))
tests/test_bot_streaming.py:1243:async def test_cmd_switch_no_arg_usage(tmp_path):
tests/test_bot_streaming.py:1248:    upd = make_update(1, "/switch")
tests/test_bot_streaming.py:1249:    await bot.cmd_switch(upd, make_cmd_ctx(args=[]))
tests/test_bot_streaming.py:1253:async def test_cmd_switch_oneshot_streaming_only_notice():
tests/test_bot_streaming.py:1255:    upd = make_update(1, "/switch beta")
tests/test_bot_streaming.py:1256:    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
tests/test_bot_streaming.py:1260:async def test_cmd_switch_unauthorized_ignored(tmp_path):
tests/test_bot_streaming.py:1265:    upd = make_update(999, "/switch alpha")
tests/test_bot_streaming.py:1266:    await bot.cmd_switch(upd, make_cmd_ctx(args=["alpha"]))
tests/test_bot_streaming.py:1294:    assert "active" in reply.lower() and "/switch" in reply
tests/test_bot_streaming.py:1301:    # pins the common name-bearing replies (/rm success + active-refusal, /new duplicate,
tests/test_bot_streaming.py:1302:    # /cancel named-nothing, /switch success) so the styling can't silently drift back.
tests/test_bot_streaming.py:1340:    # /switch success → bold name, HTML.
tests/test_bot_streaming.py:1343:    up = make_update(1, "/switch delta")
tests/test_bot_streaming.py:1344:    await bot.cmd_switch(up, make_cmd_ctx(args=["delta"]))
tests/test_bot_streaming.py:1478:async def test_cmd_switch_no_store_is_graceful_not_crash():
tests/test_bot_streaming.py:1480:    /switch must reply gracefully, never AttributeError on a None store."""
tests/test_bot_streaming.py:1483:    upd = make_update(1, "/switch alpha")
tests/test_bot_streaming.py:1484:    await bot.cmd_switch(upd, make_cmd_ctx(args=["alpha"]))
tests/test_bot_streaming.py:1608:    upd_sw = make_update(1, "/switch other")
tests/test_bot_streaming.py:1609:    await bot.cmd_switch(upd_sw, make_cmd_ctx(args=["other"]))
tests/test_bot_streaming.py:1612:    # turn, not on the bot-level switch), so /rm has a real runtime to purge.
tests/test_bot_streaming.py:1623:    # Re-create `work` at a DIFFERENT (in-roots) cwd and switch to it.
tests/test_bot_streaming.py:1626:    assert store.get_active(1) == "work"  # /new auto-switches
tests/test_bot_streaming.py:1700:    await bot.cmd_switch(make_update(1, "/switch other"), make_cmd_ctx(args=["other"]))
tests/test_bot_streaming.py:1735:async def test_cmd_new_happy_creates_resolved_cwd_and_switches(tmp_path):
tests/test_bot_streaming.py:1875:    # metachar, so it survives escaping unchanged.
tests/test_bot_streaming.py:1931:    # its lock in the background). Same id-routing safety as /switch (ADR-005 D3).
tests/test_bot_streaming.py:1935:    # refusal to cmd_new makes store.create not fire → this fails. Coverage pinned.
tests/test_bot_streaming.py:1975:    assert store.get_active(1) == "work"  # /new auto-switched
tests/test_bot_streaming.py:2136:    /reset. Mirrors the /switch and /new busy-guards."""
tests/test_bot_streaming.py:2204:async def test_cmd_switch_to_idle_project_while_another_busy_succeeds(tmp_path):
tests/test_bot_streaming.py:2205:    # P5/T7: /switch to an IDLE project while a DIFFERENT project (alpha) is mid-run
tests/test_bot_streaming.py:2221:    await bot.cmd_switch(make_update(1, "/switch beta"), make_cmd_ctx(args=["beta"]))
tests/test_bot_streaming.py:2222:    assert store.get_active(1) == "beta"  # switched while alpha busy
tests/test_bot_streaming.py:2244:    # Drive BETA busy while alpha (the active project) is idle: switch to beta, start its
tests/test_bot_streaming.py:2245:    # turn (parks), then switch BACK so alpha is active + idle while beta runs in background.
tests/test_bot_streaming.py:2248:    store.switch(1, "beta")
tests/test_bot_streaming.py:2251:    store.switch(1, "alpha")  # alpha is now the active project, and it is idle
tests/test_bot_streaming.py:2318:    store.switch(1, "beta")
tests/test_bot_streaming.py:2321:    store.switch(1, "alpha")  # restore alpha active (cosmetic; status is per-project)
tests/test_bot_streaming.py:2361:    store.switch(1, "beta")
tests/test_bot_streaming.py:2510:    store.switch(1, "beta")
tests/test_bot_streaming.py:2513:    store.switch(1, "alpha")
tests/test_bot_streaming.py:2556:    store.switch(1, "beta")
tests/test_bot_streaming.py:2616:    # /status specifically must be documented (the bug this guard pins).
tests/test_bot_streaming.py:2790:    # Flip BETA into /yolo while it is the active project, then switch back to alpha so beta
tests/test_bot_streaming.py:2792:    store.switch(1, "beta")
tests/test_bot_streaming.py:2794:    store.switch(1, "alpha")
tests/test_bot_streaming.py:3072:    # "Other" (arming free-text capture on alpha). They then /switch to the IDLE beta and
tests/test_bot_streaming.py:3101:    # /switch to the idle beta (allowed mid-run), then /run the macro.
tests/test_bot_streaming.py:3102:    await bot.cmd_switch(make_update(1, "/switch beta"), make_cmd_ctx(args=["beta"]))
tests/test_bot_streaming.py:3153:#   * [Open <project>] switch tap → on_callback performs the switch (SB1-gated).
tests/test_bot_streaming.py:3159:from claude_tg.render import encode_switch_callback  # noqa: E402
tests/test_bot_streaming.py:3162:async def test_switch_button_tap_switches_active_project_at_bot(tmp_path):
tests/test_bot_streaming.py:3163:    # T6.2: a [Open beta] tap from an AUTHORIZED chat switches the active project (via the
tests/test_bot_streaming.py:3164:    # shared /switch helper, with SB2 path revalidation). Uses a REAL StreamingSession + store.
tests/test_bot_streaming.py:3178:    upd = make_callback_update(chat_id=1, data=encode_switch_callback("beta"))
tests/test_bot_streaming.py:3180:    assert store.get_active(1) == "beta", "the switch tap must change the active project"
tests/test_bot_streaming.py:3185:async def test_switch_button_tap_from_unauthorized_chat_never_switches(tmp_path):
tests/test_bot_streaming.py:3186:    # ⭐ SB1 (mutation probe): a [Open beta] tap from a NON-allowlisted chat must NEVER switch.
tests/test_bot_streaming.py:3187:    # If on_callback skipped the _authorized recheck for switch taps, the active project would
tests/test_bot_streaming.py:3188:    # flip — this guards that the switch is gated exactly like every other callback.
tests/test_bot_streaming.py:3202:    upd = make_callback_update(chat_id=999, data=encode_switch_callback("beta"))  # NOT allowlisted
tests/test_bot_streaming.py:3204:    assert store.get_active(1) == "alpha", "an unauthorized switch tap must NOT change the active project"
tests/test_bot_streaming.py:3205:    upd.callback_query.answer.assert_awaited()  # spinner stops
tests/test_bot_streaming.py:3206:    # No switch reply was sent (the handler dropped it before resolve_callback).
tests/test_bot_streaming.py:3238:        chat_id, text, *, send, edit, delete=None, pin=None, unpin=None,
tests/test_bot_streaming.py:3660:    # remaining temp-name write-through. Together: no path writes through an escaping symlink.)
tests/test_bot_streaming.py:4504:    # guards already assert menu==registered; this pins the new command specifically).
tests/test_bot_streaming.py:4514:    # pins the fix end-to-end: 200 sessions (adversarial long cwds + titles) → EVERY sent chunk
tests/test_bot_streaming.py:4659:    upd.callback_query.answer.assert_awaited()  # spinner stopped
tests/test_bot_streaming.py:4673:    upd.callback_query.answer.assert_awaited()  # query answered (spinner stops)
tests/test_bot_streaming.py:4791:    # handlers — already pinned by test_command_menu_matches_registered_handlers) AND in HELP.
tests/test_bot_streaming.py:4814:    # RB1: a failure during shutdown must not block the bot from stopping.
tests/test_bot_streaming.py:4947:    # hot-switch mode), but it must RESUME the project's persisted session_id — so the plan
tests/test_bot_streaming.py:4999:# and _ensure_engine's SB2). These pin every cited path: the marker is ALWAYS consumed once a
tests/test_bot_streaming.py:5244:# P13 T-AUDIT — bot-side records: /yolo, /switch, /attach, /watch, /reset.
tests/test_bot_streaming.py:5262:async def test_switch_records_session_event(tmp_path):
tests/test_bot_streaming.py:5263:    """A successful ``/switch`` (via the bot's _switch_active) writes a ``switch`` session_event."""
tests/test_bot_streaming.py:5269:    await bot.cmd_switch(make_update(chat_id=1, text="/switch beta"), make_cmd_ctx(args=["beta"]))
tests/test_bot_streaming.py:5271:    assert any(e.kind == KIND_SESSION_EVENT and e.summary == "switch" and e.chat_id == 1 for e in events)
tests/test_bot_streaming.py:5289:# dormant data. The menu/HELP lock-step + the unchanged 1438-baseline are pinned
tests/test_bot_streaming.py:5321:async def test_cmd_every_pins_active_project(tmp_path):
tests/test_bot_streaming.py:5325:    assert store.get_schedule(1, "task").project == "proj"  # pinned at create
claude_tg/stream_session.py:16:  D1: several projects' engines may be live at once** — switching the active project no
claude_tg/stream_session.py:70:from typing import Any, Literal, Optional, Protocol
claude_tg/stream_session.py:115:    format_statusline,
claude_tg/stream_session.py:152:#: to ``Bot.pin_chat_message`` with ``disable_notification=True`` (a silent pin — design §3.1).
claude_tg/stream_session.py:156:#: best-effort drop the stale pin before re-pinning the fresh one (the "one pinned message"
claude_tg/stream_session.py:157:#: invariant; Telegram's current pin is the newest, so the bar self-corrects). Best-effort (RB1).
claude_tg/stream_session.py:158:UnpinFn = Callable[..., Awaitable[None]]
claude_tg/stream_session.py:452:    # project, so the statusline shows ``🔒 plan`` for the live plan turn's duration. The
claude_tg/stream_session.py:455:    # False — reading it in :meth:`_statusline_text` would wrongly show ``gate`` DURING the plan
claude_tg/stream_session.py:467:    # creation — the mode can't be hot-switched). Transient in-memory (RB3); a restart rebuilds
claude_tg/stream_session.py:483:    # turn (thinking is a session-creation knob; it can't be hot-switched), in EITHER direction
claude_tg/stream_session.py:492:    # hot-switched), in either direction. UNLIKE ``engine_thinking`` the override itself is
claude_tg/stream_session.py:626:    (dropping the entry + releasing any transferred slot) → the turn task raises
claude_tg/stream_session.py:692:    # P5 / ADR-005 D4 (T8): throttle for the proactive background pings, so a project
claude_tg/stream_session.py:693:    # bursting does not spam the chat with duplicate 🔔 pings. The key is
claude_tg/stream_session.py:694:    # (project_name, ping_kind) for a TERMINAL/non-actionable ping (done/error) and
claude_tg/stream_session.py:695:    # (project_name, ping_kind, tool_use_id) for an ACTIONABLE hold (permission/ask/plan) —
claude_tg/stream_session.py:698:    # the key -> the monotonic time the last such ping was SENT; a duplicate within the gate
claude_tg/stream_session.py:716:    # STATUSLINE T-SL-CORE (design §3.1 / §4 RB3) — the ONE pinned statusline message per chat.
claude_tg/stream_session.py:717:    # ``statusline_message_id`` is the Telegram id of the pinned line (None before the first
claude_tg/stream_session.py:718:    # update / after an orphan-recovery clears it); ``statusline_text`` is the last body shown,
claude_tg/stream_session.py:723:    # ``send_gate``/``status_message_id``, the live pin id is never persisted.
claude_tg/stream_session.py:724:    statusline_message_id: Optional[int] = None
claude_tg/stream_session.py:725:    statusline_text: Optional[str] = None
claude_tg/stream_session.py:726:    # STATUSLINE T-SL-WIRE (pin-retry fix): whether the held ``statusline_message_id`` is
claude_tg/stream_session.py:727:    # actually PINNED. The send and the pin are separate Telegram calls — a send can succeed
claude_tg/stream_session.py:728:    # (id stored) while the pin RAISES (rate-limit, perms, hiccup), leaving the line sent but
claude_tg/stream_session.py:730:    # and the line would stay unpinned forever. So on a failed pin we leave this False and RETRY
claude_tg/stream_session.py:731:    # the pin on the next update even when the text is unchanged. Transient in-memory (RB3).
claude_tg/stream_session.py:732:    statusline_pinned: bool = False
claude_tg/stream_session.py:752:    switching stopped the previously-started engine). T5 **removes** that stop-the-other
claude_tg/stream_session.py:753:    behavior — switching away no longer kills another project's in-flight run, so N engines
claude_tg/stream_session.py:914:        # tool-line NOISE (keeping text + result indicators) + emits a coalesced "… N events
claude_tg/stream_session.py:966:        URL in a background ping does not balloon into a Telegram preview card. It is passed
claude_tg/stream_session.py:995:    def _is_foreground(self, chat_id: int, name: Optional[str]) -> bool:
claude_tg/stream_session.py:1000:        becomes a name-prefixed 🔔/✅/⚠️ ping instead. "Foreground" is the store's
claude_tg/stream_session.py:1005:        ``/projects``/``/switch``.
claude_tg/stream_session.py:1044:        self, state: _ChatState, name: str, ping_kind: str, *, dedup_id: Optional[str] = None
claude_tg/stream_session.py:1046:        """Throttle duplicate pings of one ``ping_kind`` for one project (D4 coalescing).
claude_tg/stream_session.py:1048:        A background project must not spam the chat with duplicate 🔔 pings — but the unit
claude_tg/stream_session.py:1049:        of "duplicate" differs for actionable vs non-actionable pings:
claude_tg/stream_session.py:1052:          A permission / plan / ask ping carries an *answerable keyboard*; each DISTINCT
claude_tg/stream_session.py:1059:          ping's button still routes the tap, D3).
claude_tg/stream_session.py:1060:        * **Non-actionable / terminal pings (``dedup_id`` omitted).** Repeated status /
claude_tg/stream_session.py:1061:          attention pings of the same ``(project, kind)`` (e.g. ``done``/``error``, or a
claude_tg/stream_session.py:1065:        Records the send time on the way through (so the first ping of a key always goes).
claude_tg/stream_session.py:1066:        ``ping_kind`` is the notification class — the held :data:`PendingKind`
claude_tg/stream_session.py:1067:        (``permission``/``ask``/``plan``) for an attention ping, or ``done``/``error`` for a
claude_tg/stream_session.py:1068:        terminal — so e.g. a permission ping never suppresses a later error ping.
claude_tg/stream_session.py:1071:        # / non-actionable pings dedup per (project, kind) as before.
claude_tg/stream_session.py:1072:        key: tuple[str, ...] = (name, ping_kind) if dedup_id is None else (name, ping_kind, dedup_id)
claude_tg/stream_session.py:1084:        A background ping for a hold (permission/ask/plan) must carry the SAME keyboard the
claude_tg/stream_session.py:1099:        """Append an ``[Open <name>]`` switch row to ``base`` (or build it standalone — T6/P9).
claude_tg/stream_session.py:1101:        A background needs-attention ping carries a ``📂 Open <name>`` switch button
claude_tg/stream_session.py:1103:        project from the ping. When the ping also carries the hold's verdict/approve keyboard
claude_tg/stream_session.py:1104:        (permission/plan ``base``), the switch button is appended as an EXTRA ROW beneath it
claude_tg/stream_session.py:1106:        no base keyboard (the ask bell line), the switch button stands alone. The switch tap's
claude_tg/stream_session.py:1127:        """Send a body-free ``🔔 <name> — …`` attention ping for a BACKGROUND hold (D4/SB3).
claude_tg/stream_session.py:1130:        permission/ask/plan becomes a name-prefixed ping carrying the SAME keyboard the
claude_tg/stream_session.py:1149:        # T6/P9: the ping carries the hold's verdict/approve keyboard PLUS an [Open <name>]
claude_tg/stream_session.py:1150:        # switch row (the operator can act on the hold OR jump to the project), a queued
claude_tg/stream_session.py:1169:        """Background ask ping: the ``🔔 <name> — asks a question`` line + each question's
claude_tg/stream_session.py:1172:        The WHOLE ping (the bell line + every question's option keyboard) is gated by a
claude_tg/stream_session.py:1183:          set, and the first ping's buttons still route the tap by the D3 index. The round-1 fix
claude_tg/stream_session.py:1192:        # BLOCKER (round 2): one throttle decision gates the ENTIRE ask ping. A same-id re-emit
claude_tg/stream_session.py:1199:        # T6/P9: the bell line carries the [Open <name>] switch button (the per-question
claude_tg/stream_session.py:1200:        # keyboards below carry the option taps, so the switch button rides the bell), the
claude_tg/stream_session.py:1236:        """Send a body-free terminal ping for a BACKGROUND project (D4/SB3).
claude_tg/stream_session.py:1240:        (T3-review):** the error ping passes the engine's **body-free**
claude_tg/stream_session.py:1250:            # T6/P9: queued counter + no link preview (the error ping carries no switch button
claude_tg/stream_session.py:1251:            # per the T6 scope — that is on the attention + done pings).
claude_tg/stream_session.py:1263:            # An is_error ResultEvent is a failed turn — ping it as an error too (its
claude_tg/stream_session.py:1281:            # T6/P9: the done ping carries the [Open <name>] switch button (jump to the
claude_tg/stream_session.py:1307:          UX), then resolve it. If a ``default`` exists but isn't active, switch to it.
claude_tg/stream_session.py:1335:        """Resolve a NAMED project's ``(name, runtime)`` for a pinned turn (P14 T-FIRE).
claude_tg/stream_session.py:1338:        targets ITS project, pinned at create). Looks the project up in the store
claude_tg/stream_session.py:1355:                # itself); the scheduler pins the project name from ``get_active`` at create,
claude_tg/stream_session.py:1366:        """Auto-create (or switch to) a ``default`` project for a chat with no active one.
claude_tg/stream_session.py:1370:        send a message. If ``default`` already exists but isn't active, switch to it
claude_tg/stream_session.py:1381:            self.store.switch(chat_id, DEFAULT_PROJECT)
claude_tg/stream_session.py:1698:          ``_stop_other_started`` cross-project stop so switching away never kills another
claude_tg/stream_session.py:1757:        # in EITHER direction — a session built in ``"default"`` can't be hot-switched to plan
claude_tg/stream_session.py:1767:        # not hot-switchable). So /thinking on→off (or off→on) rebuilds the session on the next
claude_tg/stream_session.py:1773:        # hot-switchable). So changing /effort (e.g. high→max, or set→cleared) rebuilds the
claude_tg/stream_session.py:1896:                #     not "active", so a concurrent /switch can't redirect the clear.
claude_tg/stream_session.py:1995:        ``/reset`` · ``/switch`` → ``session_event``; ``/yolo`` · ``/unyolo`` →
claude_tg/stream_session.py:2140:        cwd + composite liveness), then create/adopt it as a bot project pinned to that
claude_tg/stream_session.py:2176:          chat, just switch to it (idempotent — re-attaching the same id never forks a
claude_tg/stream_session.py:2243:        # Idempotent re-attach: if a project already points at this id, just switch to it
claude_tg/stream_session.py:2248:            self.store.switch(chat_id, existing)
claude_tg/stream_session.py:2257:                    f"✅ Already attached as <b>{name_html}</b> — switched to it; your next "
claude_tg/stream_session.py:2294:        # from the just-pinned id (redacted — never the raw resumable id). Best-effort (RB1).
claude_tg/stream_session.py:2345:        """The chat's project (stored name) already pinned to ``session_id``, or ``None``.
claude_tg/stream_session.py:2347:        Makes attach idempotent: a re-attach of an id the chat already adopted just switches
claude_tg/stream_session.py:2419:            # No resolvable, in-tree transcript path (missing cwd, or a symlink escaping
claude_tg/stream_session.py:2545:        this exact task (a new /watch may have already replaced it). Pure bookkeeping; never
claude_tg/stream_session.py:2558:        ``/switch`` work on it. We build a friendly base from the session's title (the first
claude_tg/stream_session.py:2702:                    "(ignored — dropping the runtime regardless)",
claude_tg/stream_session.py:2802:        persist now that ``/switch`` is free):
claude_tg/stream_session.py:2809:          ``/switch`` no longer waits for the run to finish (the lock-P-drive-Q /
claude_tg/stream_session.py:2838:          match, so ``/projects`` and ``/switch WORK`` agree). A project with no runtime is
claude_tg/stream_session.py:2842:          ``/switch``/``/new``/``/reset`` guards still call this form; **T7** rewires
claude_tg/stream_session.py:2843:          ``/reset`` to the per-project form and drops the guard from ``/switch``/``/new``.
claude_tg/stream_session.py:2867:        (mirroring the store's name match) so ``/projects`` and ``/switch WORK`` agree.
claude_tg/stream_session.py:2930:        pin: Optional[PinFn] = None,
claude_tg/stream_session.py:2931:        unpin: Optional[UnpinFn] = None,
claude_tg/stream_session.py:2942:          (not whatever is active), or the active project if the task pinned none / it is gone.
claude_tg/stream_session.py:3021:                pin=pin,
claude_tg/stream_session.py:3022:                unpin=unpin,
claude_tg/stream_session.py:3039:            log.exception("proactive fire of %s failed (skipping this tick)", name)
claude_tg/stream_session.py:3059:        pin: Optional[PinFn] = None,
claude_tg/stream_session.py:3060:        unpin: Optional[UnpinFn] = None,
claude_tg/stream_session.py:3089:        pinned ``target``; an unknown/blank name falls back to the active project (the
claude_tg/stream_session.py:3199:        # _drive_turn re-resolve + pin the active project (the busy-guards keep it stable for
claude_tg/stream_session.py:3200:        # the turn in T5; T7 frees /switch but _drive_turn still pins the turn's project).
claude_tg/stream_session.py:3270:            # in _acquire_slot does the slot bookkeeping); the outer finally still clears inflight.
claude_tg/stream_session.py:3352:                        pin=pin, unpin=unpin, target=target,
claude_tg/stream_session.py:3360:                # turn-exit too (normal / error / cancel / resume-failure). Pure bookkeeping +
claude_tg/stream_session.py:3543:        pin: Optional[PinFn] = None,
claude_tg/stream_session.py:3544:        unpin: Optional[UnpinFn] = None,
claude_tg/stream_session.py:3610:        # Foreground-only: the background branch pings ✅/🔔 and continues before the render
claude_tg/stream_session.py:3621:        # STATUSLINE T-SL-WIRE (B3 fix): mark the LIVE plan-mode flag for the statusline's
claude_tg/stream_session.py:3626:        # statusline trigger so that first render already reads ``plan``.
claude_tg/stream_session.py:3631:        # invariant). Best-effort (RB1): pins/edits can't break the turn (the helper swallows).
claude_tg/stream_session.py:3632:        await self._maybe_update_statusline(
claude_tg/stream_session.py:3633:            chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=turn_name,
claude_tg/stream_session.py:3639:        if turn_rt.policy.yolo and self._is_foreground(chat_id, turn_name):
claude_tg/stream_session.py:3699:                    # rendered inline or pinged in the background.)
claude_tg/stream_session.py:3702:                        # the active one — once /switch is free the active project can change
claude_tg/stream_session.py:3705:                        # the project handle_message pinned at message time.
claude_tg/stream_session.py:3727:                # EVENT — /switch is free (T7), so the foreground can change mid-turn; an
claude_tg/stream_session.py:3729:                # BACKGROUND project becomes a name-prefixed 🔔/✅/⚠️ ping (the operator is
claude_tg/stream_session.py:3731:                # status inline — its progress is summarized by the ping + the /projects
claude_tg/stream_session.py:3734:                if not self._is_foreground(chat_id, turn_name):
claude_tg/stream_session.py:3839:            # lingering 🔒 plan. Cleared BEFORE the turn-end statusline trigger. (A freshly-armed
claude_tg/stream_session.py:3845:            # _statusline_text's engine.context_percentage() reads the fresh figure). FOREGROUND-
claude_tg/stream_session.py:3849:            await self._maybe_update_statusline(
claude_tg/stream_session.py:3850:                chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=turn_name,
claude_tg/stream_session.py:3922:        #    to resume), not the active one, since /switch may have moved active mid-turn.
claude_tg/stream_session.py:3934:        #    stop() the connected-but-dead engine BEFORE dropping the reference so its SDK
claude_tg/stream_session.py:4119:            # message, which is exactly the status-line spam we must avoid. Skipping BEFORE
claude_tg/stream_session.py:4157:    # -- the pinned mobile statusline (STATUSLINE T-SL-CORE, design §3.1/§4) --
claude_tg/stream_session.py:4159:    async def _maybe_update_statusline(
claude_tg/stream_session.py:4165:        pin: Optional[PinFn],
claude_tg/stream_session.py:4166:        unpin: Optional[UnpinFn],
claude_tg/stream_session.py:4169:        """Refresh the pinned statusline IFF this is the chat's FOREGROUND project (T-SL-WIRE).
claude_tg/stream_session.py:4171:        ⭐ **The make-or-break wiring invariant (design §3.1).** The pinned line reflects the
claude_tg/stream_session.py:4174:        the line, or two concurrent turns would stomp each other's state and the single pinned
claude_tg/stream_session.py:4178:        * **skips** when ``for_project`` is not the chat's foreground (:meth:`_is_foreground`) —
claude_tg/stream_session.py:4180:        * **skips** when any closure is missing (a caller/test that didn't inject pin/unpin —
claude_tg/stream_session.py:4181:          back-compat: the statusline simply isn't driven, the turn is unaffected);
claude_tg/stream_session.py:4182:        * otherwise delegates to :meth:`_update_statusline` (itself fully best-effort, RB1).
claude_tg/stream_session.py:4185:        paths: ``/switch`` + the knob setters always act on the active project), so the
claude_tg/stream_session.py:4188:        statusline is an observer off the turn's critical path.
claude_tg/stream_session.py:4190:        if send is None or edit is None or pin is None or unpin is None:
claude_tg/stream_session.py:4193:            if for_project is not None and not self._is_foreground(chat_id, for_project):
claude_tg/stream_session.py:4196:            await self._update_statusline(
claude_tg/stream_session.py:4197:                chat_id, send=send, edit=edit, pin=pin, unpin=unpin
claude_tg/stream_session.py:4201:            # _update_statusline already swallows its own I/O; this guards the gate itself).
claude_tg/stream_session.py:4202:            log.debug("statusline trigger failed for chat (ignored)", exc_info=True)
claude_tg/stream_session.py:4204:    async def _statusline_text(self, chat_id: int) -> Optional[tuple[str, str]]:
claude_tg/stream_session.py:4205:        """Build the CURRENT statusline body + the project it was built FOR (``(text, name)``).
claude_tg/stream_session.py:4209:        — and renders it through :func:`~claude_tg.render.format_statusline`. Foreground-only
claude_tg/stream_session.py:4210:        (design §3.1): a background project's turn never rewrites the line, so the single pinned
claude_tg/stream_session.py:4214:        False`` so a statusline refresh NEVER creates a project as a side effect; with no active
claude_tg/stream_session.py:4221:        FINAL pre-write foreground re-check (B2): the ctx ``await`` below is a switch window, so
claude_tg/stream_session.py:4262:                # ⭐ The ONLY await in this builder — and a /switch window (B2): the returned
claude_tg/stream_session.py:4267:        body = format_statusline(
claude_tg/stream_session.py:4277:    async def _update_statusline(
claude_tg/stream_session.py:4283:        pin: PinFn,
claude_tg/stream_session.py:4284:        unpin: UnpinFn,
claude_tg/stream_session.py:4286:        """Refresh the chat's ONE pinned statusline — send+pin on first use, edit thereafter.
claude_tg/stream_session.py:4288:        STATUSLINE T-SL-CORE (design §3.1/§4). Builds the current foreground statusline body
claude_tg/stream_session.py:4289:        (:meth:`_statusline_text`) and reconciles it with the chat's pinned line:
claude_tg/stream_session.py:4294:          DISABLED (a silent pin — design §3.1); store the id + text.
claude_tg/stream_session.py:4295:        * **subsequent update** → EDIT in place only (no re-pin, no re-send; a pinned message
claude_tg/stream_session.py:4296:          edited in place stays pinned and silent).
claude_tg/stream_session.py:4297:        * **edit FAILURE** (the operator unpinned/deleted it → "message to edit not found", an
claude_tg/stream_session.py:4299:          stale one (the "one pinned message" invariant — Telegram's current pin is the newest,
claude_tg/stream_session.py:4303:        **⭐ RB1 — a pin/edit/send failure NEVER breaks or wedges a turn.** This is an observer
claude_tg/stream_session.py:4305:        ``send``/``edit``/``pin``/``unpin``, a build error) is logged at debug and swallowed —
claude_tg/stream_session.py:4308:        :meth:`_gated_edit`), so the statusline can never flood and never starves a real
claude_tg/stream_session.py:4309:        answer/prompt. **One id invariant** — exactly one ``statusline_message_id`` is ever held
claude_tg/stream_session.py:4312:        ``send``/``edit``/``pin``/``unpin`` are injected by ``bot.py`` (the same pattern as the
claude_tg/stream_session.py:4316:        **⭐ B2 fix — no stale line across a ``/switch`` (the FINAL guard).** The body is built
claude_tg/stream_session.py:4318:        the rebuild are ``/switch`` windows. So the gated write helpers (1) REBUILD the body from
claude_tg/stream_session.py:4321:        await between that check and issuing the ``edit``/``send``. If a ``/switch`` happened
claude_tg/stream_session.py:4323:        ``/switch``'s own statusline trigger writes the correct line — no loop, no stale write).
claude_tg/stream_session.py:4324:        **Pin-retry** — a send that succeeded while its pin RAISED leaves the line UNPINNED
claude_tg/stream_session.py:4325:        (``statusline_pinned`` False); a later update RETRIES the pin even if the text is
claude_tg/stream_session.py:4326:        unchanged, so a transient pin failure self-heals instead of sticking unpinned forever.
claude_tg/stream_session.py:4329:            built = await self._statusline_text(chat_id)  # async (B1: awaits the SDK ctx %)
claude_tg/stream_session.py:4331:                return  # no foreground project to describe — nothing to pin/edit.
claude_tg/stream_session.py:4334:            # Pin-retry: if we hold a sent id whose pin FAILED, retry the pin even on identical
claude_tg/stream_session.py:4335:            # text (the identical-text skip below would otherwise leave it unpinned forever).
claude_tg/stream_session.py:4337:                state.statusline_message_id is not None
claude_tg/stream_session.py:4338:                and not state.statusline_pinned
claude_tg/stream_session.py:4339:                and body == state.statusline_text
claude_tg/stream_session.py:4341:                await self._statusline_pin(state, state.statusline_message_id, pin=pin)
claude_tg/stream_session.py:4343:            if body == state.statusline_text:
claude_tg/stream_session.py:4344:                # Identical to what's pinned — skip BEFORE the gate so an unchanged refresh
claude_tg/stream_session.py:4347:            if state.statusline_message_id is None:
claude_tg/stream_session.py:4348:                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
claude_tg/stream_session.py:4351:                # B2: rebuild the body AFTER the gate wait (inside _statusline_gated_edit) so a
claude_tg/stream_session.py:4352:                # /switch during the wait writes the now-current line, never the stale snapshot.
claude_tg/stream_session.py:4353:                await self._statusline_gated_edit(
claude_tg/stream_session.py:4354:                    chat_id, state, state.statusline_message_id, edit=edit
claude_tg/stream_session.py:4357:                # Orphan recovery (design §4 RB1): the pinned line is gone (unpinned/deleted by
claude_tg/stream_session.py:4359:                # the stale one (one-pin invariant), then re-send + re-pin a fresh line. The
claude_tg/stream_session.py:4361:                log.debug("statusline edit failed for chat; re-sending + re-pinning", exc_info=True)
claude_tg/stream_session.py:4362:                stale_id = state.statusline_message_id
claude_tg/stream_session.py:4363:                state.statusline_message_id = None
claude_tg/stream_session.py:4364:                state.statusline_text = None
claude_tg/stream_session.py:4365:                state.statusline_pinned = False
claude_tg/stream_session.py:4367:                    await unpin(message_id=stale_id)
claude_tg/stream_session.py:4369:                    log.debug("stale statusline unpin failed (ignored)", exc_info=True)
claude_tg/stream_session.py:4370:                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
claude_tg/stream_session.py:4372:            # ⭐ The make-or-break swallow (RB1): NOTHING the statusline does may escape to the
claude_tg/stream_session.py:4375:            log.debug("statusline update failed for chat (ignored)", exc_info=True)
claude_tg/stream_session.py:4377:    async def _statusline_gated_edit(
claude_tg/stream_session.py:4380:        """Edit the pinned line through the gate, REBUILDING + RE-CHECKING foreground (B2).
claude_tg/stream_session.py:4383:        the statusline body + the project it was built for from CURRENT state. Two awaits precede
claude_tg/stream_session.py:4384:        the write — the gate wait AND the ctx ``await`` inside :meth:`_statusline_text` — both
claude_tg/stream_session.py:4385:        ``/switch`` windows. So immediately before the raw edit we do a FINAL **synchronous**
claude_tg/stream_session.py:4386:        foreground re-check (``_is_foreground(built_for)``) with NO await between it and the
claude_tg/stream_session.py:4387:        ``edit``: if a ``/switch`` happened during ANY await, ``built_for`` is no longer
claude_tg/stream_session.py:4388:        foreground → SKIP (the switch's own trigger writes the correct line — no stale write, no
claude_tg/stream_session.py:4395:        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
claude_tg/stream_session.py:4399:        if body == state.statusline_text:
claude_tg/stream_session.py:4402:        # chat's foreground at THIS instant — no await between here and the edit, so a /switch
claude_tg/stream_session.py:4403:        # during any preceding await is caught. A stale body (built_for switched away) is dropped.
claude_tg/stream_session.py:4404:        if not self._is_foreground(chat_id, built_for):
claude_tg/stream_session.py:4407:        state.statusline_text = body
claude_tg/stream_session.py:4409:    async def _statusline_send_and_pin(
claude_tg/stream_session.py:4415:        pin: PinFn,
claude_tg/stream_session.py:4417:        """Send the statusline body (gated, non-verbatim) then PIN it silently (design §3.1).
claude_tg/stream_session.py:4421:        send — the gate wait AND the ctx ``await`` inside :meth:`_statusline_text` — both
claude_tg/stream_session.py:4422:        ``/switch`` windows (B2). So immediately before the raw send we do a FINAL **synchronous**
claude_tg/stream_session.py:4423:        foreground re-check (``_is_foreground(built_for)``) with NO await between it and the
claude_tg/stream_session.py:4424:        ``send``: a ``/switch`` during any preceding await makes ``built_for`` no longer
claude_tg/stream_session.py:4425:        foreground → SKIP (the switch's own trigger sends the correct line — no stale send, no
claude_tg/stream_session.py:4426:        loop). A best-effort silent pin follows (``disable_notification=True`` — a pin must never
claude_tg/stream_session.py:4427:        re-ping). The id/text are stored ONLY when the send returns an id (so a ``None`` send does
claude_tg/stream_session.py:4429:        ``statusline_pinned=False`` so the next update retries the pin. Called from
claude_tg/stream_session.py:4430:        :meth:`_update_statusline` inside its best-effort guard, so a raising ``send`` propagates
claude_tg/stream_session.py:4436:        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
claude_tg/stream_session.py:4441:        # chat's foreground at THIS instant — no await between here and the send, so a /switch
claude_tg/stream_session.py:4443:        # send is dropped (the switch's own statusline trigger sends the correct line).
claude_tg/stream_session.py:4444:        if not self._is_foreground(chat_id, built_for):
claude_tg/stream_session.py:4451:        state.statusline_message_id = mid
claude_tg/stream_session.py:4452:        state.statusline_text = body
claude_tg/stream_session.py:4453:        state.statusline_pinned = False  # not pinned until the pin call below succeeds.
claude_tg/stream_session.py:4454:        await self._statusline_pin(state, mid, pin=pin)
claude_tg/stream_session.py:4456:    async def _statusline_pin(self, state: _ChatState, message_id: int, *, pin: PinFn) -> None:
claude_tg/stream_session.py:4457:        """Best-effort SILENT pin of the statusline message; record whether it stuck (pin-retry).
claude_tg/stream_session.py:4459:        A pin must never re-ping (``disable_notification=True``) and never break the turn (RB1).
claude_tg/stream_session.py:4460:        On success ``statusline_pinned`` is set True; on failure it stays/!becomes False and is
claude_tg/stream_session.py:4461:        swallowed — :meth:`_update_statusline` then RETRIES the pin on the next update (even with
claude_tg/stream_session.py:4462:        unchanged text) so a transient pin failure self-heals instead of leaving the line unpinned
claude_tg/stream_session.py:4463:        forever. Only the pinned-bar placement is ever at stake here, never the turn.
claude_tg/stream_session.py:4466:            await pin(message_id=message_id, disable_notification=True)
claude_tg/stream_session.py:4467:            state.statusline_pinned = True
claude_tg/stream_session.py:4469:            state.statusline_pinned = False
claude_tg/stream_session.py:4470:            log.debug("statusline pin failed (will retry on next update)", exc_info=True)
claude_tg/stream_session.py:4495:        Mapping:
claude_tg/stream_session.py:4513:        # T6/P9: a [Open <project>] switch tap routes by PROJECT NAME, not a tool_use_id, and
claude_tg/stream_session.py:4517:        # for a switch (the bot's /switch helper does the SB2 path re-validation + the store
claude_tg/stream_session.py:4518:        # write); we just decode + return the target name. A switch never resolves a decision.
claude_tg/stream_session.py:4519:        if decoded.kind == "switch":
claude_tg/stream_session.py:4520:            return self._resolve_switch(chat_id, decoded)
claude_tg/stream_session.py:4522:        # pending hold — handle it BEFORE the pending-index lookup, like switch. The bot has
claude_tg/stream_session.py:4553:    def _resolve_switch(self, chat_id: int, decoded: Callback) -> "CallbackOutcome":
claude_tg/stream_session.py:4554:        """Route a ``[Open <project>]`` switch tap (T6/P9) — decode-only; bot does the switch.
claude_tg/stream_session.py:4556:        The switch tap carries the TARGET PROJECT NAME (``decoded.switch_to``), already
claude_tg/stream_session.py:4558:        This returns a :class:`CallbackOutcome` with ``switch_to`` set so the bot's
claude_tg/stream_session.py:4559:        ``on_callback`` performs the actual switch through its shared ``/switch`` helper —
claude_tg/stream_session.py:4563:        switch is navigation, not an answer). With no store there is nothing to switch within
claude_tg/stream_session.py:4567:        name = decoded.switch_to
claude_tg/stream_session.py:4571:            # No registry to switch within (single implicit project) — benign no-op (RB1).
claude_tg/stream_session.py:4573:        # Hand the (decoded) name to the bot to switch + path-revalidate; the toast is set by
claude_tg/stream_session.py:4574:        # the bot after the switch. ``handled`` is True (we recognized + routed the tap).
claude_tg/stream_session.py:4575:        return CallbackOutcome(handled=True, note=f"Opening {name}…", switch_to=name)
claude_tg/stream_session.py:4586:        (mirroring ``_resolve_switch``). With no store there is nothing to attach into (single
claude_tg/stream_session.py:4666:        Re-tapping a question overwrites its answer (count unchanged), so the operator can
claude_tg/stream_session.py:4865:        runtime lookup), so escaping is both consistency AND defense-in-depth.
claude_tg/stream_session.py:5040:        throughout (RB1): a failure stopping one engine / cancelling one watch never blocks the
claude_tg/stream_session.py:5052:                        log.exception("error stopping engine during shutdown")
claude_tg/stream_session.py:5296:    feed it through the EXACT same heuristic by wrapping it in a ``ClaudeResult`` — no
claude_tg/stream_session.py:5343:    Only foreground renders feed this (the driver's background branch pings ``✅``/``🔔``
claude_tg/stream_session.py:5453:    * ``switch_to``    — (T6/P9) the TARGET project name of a ``[Open <project>]`` switch tap.
claude_tg/stream_session.py:5454:                         The session does NOT touch the store for a switch (it needs the bot's
claude_tg/stream_session.py:5455:                         SB2 path re-validation, the same as ``/switch``); it decodes + routes
claude_tg/stream_session.py:5456:                         and returns the name so the bot performs the switch via its shared
claude_tg/stream_session.py:5457:                         ``/switch`` helper. ``None`` for every non-switch outcome.
claude_tg/stream_session.py:5464:    (the "Other"/"Reject" arm); ``switch_to`` only for a switch tap; ``attach_session_id``
claude_tg/stream_session.py:5473:    switch_to: Optional[str] = None
tests/test_stream_session.py:35:from claude_tg.render import RenderAction, encode_callback, encode_switch_callback
tests/test_stream_session.py:96:        # STATUSLINE T-SL-CORE: the ctx % the statusline reads via engine.context_percentage().
tests/test_stream_session.py:147:        # STATUSLINE T-SL-CORE / T-SL-WIRE (B1): the best-effort ctx % the statusline reads
tests/test_stream_session.py:461:    """Re-tapping a question before completion overwrites that answer (count unchanged),
tests/test_stream_session.py:499:    # Answer Q2 by tapping its option — not complete yet (Q1 still open).
tests/test_stream_session.py:1442:    # build/resume the OTHER project. P5/T5: switching does NOT stop the previously-started
tests/test_stream_session.py:1443:    # engine (the single-active-run stop is removed so background runs survive a switch).
tests/test_stream_session.py:1450:    store.switch(1, "beta")
tests/test_stream_session.py:1452:    store.switch(1, "alpha")  # back to alpha for the first turn
tests/test_stream_session.py:1474:    # Operator switches active project to beta (registry op); next turn uses beta.
tests/test_stream_session.py:1475:    store.switch(1, "beta")
tests/test_stream_session.py:1480:    # P5/T5: switching no longer stops alpha's previously-started engine — a switched-away
tests/test_stream_session.py:1510:async def test_result_persists_to_captured_project_not_active_after_mid_turn_switch(tmp_path):
tests/test_stream_session.py:1512:    # per-project persist now that /switch is free). Drive a turn in ALPHA that parks at a
tests/test_stream_session.py:1513:    # HOLD *before* its ResultEvent; WHILE parked, /switch the active project to BETA (now
tests/test_stream_session.py:1519:    # *active* project — so a mid-turn switch would clobber BETA with alpha-new and leave
tests/test_stream_session.py:1530:        # park BEFORE the result so we can switch active to beta mid-turn, then land it.
tests/test_stream_session.py:1545:    # Operator SWITCHES active to beta WHILE alpha's turn is parked (the freed /switch).
tests/test_stream_session.py:1546:    store.switch(1, "beta")
tests/test_stream_session.py:1558:    assert store.get_active(1) == "beta"  # the switch stands
tests/test_stream_session.py:1875:    # Belt-and-suspenders: stripping the only real tags (<code>…</code>) must leave NO stray
tests/test_stream_session.py:2073:#   * (P5/T5) switch-no-longer-stops-the-other-engine — see
tests/test_stream_session.py:2074:#     test_switch_does_not_stop_the_previously_started_engine above (the old
tests/test_stream_session.py:2124:async def test_switch_does_not_stop_the_previously_started_engine(tmp_path):
tests/test_stream_session.py:2125:    # P5/T5: switching the active project must NOT tear down the previously-started
tests/test_stream_session.py:2127:    # switched-away project keeps its engine live for a background run). After turn 1 on
tests/test_stream_session.py:2128:    # alpha + switch to beta + turn 2 on beta, alpha's engine was never stopped and its
tests/test_stream_session.py:2129:    # runtime is still live. (Was the old "stop-failure-on-switch swallow" test, whose
tests/test_stream_session.py:2130:    # premise — switching stops the other engine — no longer holds.)
tests/test_stream_session.py:2140:            # stopped True on the switched-away alpha — the assertion below would catch it.
tests/test_stream_session.py:2162:    # Switch to beta; turn 2 runs beta WITHOUT stopping alpha (concurrent runs, T5).
tests/test_stream_session.py:2163:    store.switch(1, "beta")
tests/test_stream_session.py:2185:    """Spin the loop until ``is_busy(chat_id, name) is want`` (bounded; no real sleep)."""
tests/test_stream_session.py:2197:    # stopped by the switch). Resolve each INDEPENDENTLY via the id-routed callback path
tests/test_stream_session.py:2234:    store.switch(1, "beta")
tests/test_stream_session.py:2243:    assert eng_alpha.stopped is False  # the switch did NOT tear alpha down
tests/test_stream_session.py:2265:    # P5 / ADR-005 D4 (T8): alpha is now a BACKGROUND project (the store switched to beta),
tests/test_stream_session.py:2270:    assert any(s["text"] == "✅ alpha — done" for s in rec.sends)  # background ping instead
tests/test_stream_session.py:2303:    # Matched case-insensitively (mirrors the store), so /projects + /switch WORK agree.
tests/test_stream_session.py:2431:    """Spin the loop until ``session._running == n`` (bounded; no real sleep)."""
tests/test_stream_session.py:2474:    store.switch(1, "beta")
tests/test_stream_session.py:2481:    store.switch(1, "gamma")
tests/test_stream_session.py:2534:    store.switch(1, "beta")
tests/test_stream_session.py:2542:    store.switch(1, "gamma")
tests/test_stream_session.py:2580:    store.switch(1, "beta")
tests/test_stream_session.py:2586:    store.switch(1, "gamma")
tests/test_stream_session.py:2671:    store.switch(1, "beta")
tests/test_stream_session.py:2731:    # (non-probe) companion to the TOCTOU test, pinning that inflight didn't become a global
tests/test_stream_session.py:2749:    store.switch(1, "beta")
tests/test_stream_session.py:2767:    # accepted (the guard rejects only WHILE a turn is in flight, not forever). Also pins the
tests/test_stream_session.py:2831:    store.switch(1, "beta")
tests/test_stream_session.py:2869:    store.switch(1, "beta")
tests/test_stream_session.py:2916:    store.switch(1, "beta")
tests/test_stream_session.py:2926:    # /cancel targets the ACTIVE project; switch back to alpha to cancel it.
tests/test_stream_session.py:2927:    store.switch(1, "alpha")
tests/test_stream_session.py:3177:# and durably pinned. Both use a REAL JsonSessionStore (so fork_pending is observable on disk).
tests/test_stream_session.py:3284:    # Adopt: pins base-1 + fork_pending=True.
tests/test_stream_session.py:3309:    This pins the boundary against a "simplify: clear fork_pending at connect" regression —
tests/test_stream_session.py:4303:    # store's name match), so /projects and /switch WORK agree on a project's status.
tests/test_stream_session.py:4313:# becomes a name-prefixed 🔔/✅/⚠️ ping (the operator isn't watching it); a
tests/test_stream_session.py:4323:    pinned ``target`` so ``_drive_turn`` acts on THAT project (its engine + foreground
tests/test_stream_session.py:4337:async def test_background_permission_hold_sends_attention_ping(tmp_path):
tests/test_stream_session.py:4339:    # "🔔 alpha — Claude needs approval" ping is sent, carrying the SAME [Allow/Deny]
tests/test_stream_session.py:4353:    # Wait until the attention ping is sent (the turn then parks at the HOLD).
tests/test_stream_session.py:4359:        raise AssertionError("background permission ping was never sent")
tests/test_stream_session.py:4360:    # The ping is the name-prefixed bell, NOT the inline "🔐 Permission needed" prompt.
tests/test_stream_session.py:4361:    ping = next(s for s in rec.sends if s["text"].startswith("🔔"))
tests/test_stream_session.py:4362:    assert ping["text"] == "🔔 alpha — Claude needs approval"
tests/test_stream_session.py:4363:    assert ping["reply_markup"] is not None  # carries the verdict keyboard (routes by id)
tests/test_stream_session.py:4373:async def test_foreground_permission_hold_renders_inline_no_ping(tmp_path):
tests/test_stream_session.py:4375:    # it renders inline (the "🔐 Permission needed" prompt) with NO "🔔" ping (no duplicate).
tests/test_stream_session.py:4392:    # Inline prompt present, NO background ping.
tests/test_stream_session.py:4507:async def test_background_ask_pings_then_sends_question_keyboards(tmp_path):
tests/test_stream_session.py:4508:    # A BACKGROUND ask → a "🔔 alpha — asks a question" ping + each question's option
tests/test_stream_session.py:4526:        raise AssertionError("background ask ping was never sent")
tests/test_stream_session.py:4527:    # The bell ping is body-free; the question keyboard rides its own (safe) message.
tests/test_stream_session.py:4537:async def test_background_done_sends_check_ping(tmp_path):
tests/test_stream_session.py:4550:async def test_background_error_ping_is_body_free_kind_only_sb3(tmp_path):
tests/test_stream_session.py:4551:    # ⭐ The load-bearing SB3 check (T3-review SB3): a BACKGROUND error pings the body-free
tests/test_stream_session.py:4553:    # secret is ABSENT from the ping and the kind label is present.
tests/test_stream_session.py:4564:    # The error ping is "⚠️ alpha — tool_error" — the body-free ErrorKind, never the message.
tests/test_stream_session.py:4574:    # incremental text) does NOT spam the chat; only the terminal ✅ ping is sent. (The
tests/test_stream_session.py:4585:    # Only the done ping — no status line, no tool_use one-liner sent for the background run.
tests/test_stream_session.py:4590:async def test_background_attention_pings_are_throttled(tmp_path):
tests/test_stream_session.py:4592:    # 🔔 pings within the send interval. Drive _notify_background directly (the throttle is
tests/test_stream_session.py:4594:    # identical ping is suppressed.
tests/test_stream_session.py:4599:        clock=lambda: 100.0,            # frozen — both pings arrive at the same instant
tests/test_stream_session.py:4608:    # Only ONE 🔔 ping (the second was throttled — the operator already knows; the first
tests/test_stream_session.py:4609:    # ping's keyboard still routes the tap, D3/D4).
tests/test_stream_session.py:4619:#     NON-actionable status/attention pings; a SECOND DISTINCT-tool_use_id permission /
tests/test_stream_session.py:4631:    let a buggy "drop the 2nd distinct-id ping" path pass the test vacuously. Here the clock
tests/test_stream_session.py:4633:    OPEN for every ping in the test: a second ping is suppressed UNLESS the distinct-id
tests/test_stream_session.py:4636:    EITHER (the keyboard the ping must carry).
tests/test_stream_session.py:4651:        clock=lambda: 100.0,        # frozen — every ping arrives inside the throttle window
tests/test_stream_session.py:4664:    # WITHIN the (open) throttle window, must BOTH send an answerable keyboard — and tapping
tests/test_stream_session.py:4666:    # 2nd distinct-id ping is suppressed by the (project, kind) throttle → only ONE bell →
tests/test_stream_session.py:4680:    # ping each. Both are background "permission" pings within the SAME open throttle window.
tests/test_stream_session.py:4686:    # BOTH pings sent, each carrying a verdict keyboard (the operator can act on each).
tests/test_stream_session.py:4691:    # Tapping EACH resolves the RIGHT request against alpha's engine (the index owns id->proj).
tests/test_stream_session.py:4725:    # throttle anything", which would regress test_background_attention_pings_are_throttled.
tests/test_stream_session.py:4726:    # Uses the OPEN-window rig so the throttle is genuinely active for the 2nd same-id ping.
tests/test_stream_session.py:4753:    # suppressed on the 2nd ping but the question keyboard(s) are re-sent → TWO keyboards for
tests/test_stream_session.py:4755:    # interval) makes the throttle genuinely active for the 2nd same-id ping (non-vacuous).
tests/test_stream_session.py:4774:    # bell line itself now carries an [Open <name>] switch button, so count only the QUESTION
tests/test_stream_session.py:4784:    # Belt-and-braces: the 2nd ping sent NOTHING new at all (mirrors the permission/plan path).
tests/test_stream_session.py:4812:    # T6/P9: the bell carries an [Open <name>] switch button now, so count only QUESTION
tests/test_stream_session.py:4823:    # 1 bell + 2 questions, and nothing from the 2nd ping.
tests/test_stream_session.py:4830:    # (the same-id coalesce must NOT regress the distinct-id BLOCKER-1 fix). And tapping each
tests/test_stream_session.py:4852:    # T6/P9: the bell carries an [Open <name>] switch button now, so count only QUESTION
tests/test_stream_session.py:4873:    # ping BOTH (each keyboard answerable), and resolving each unblocks the turn to its
tests/test_stream_session.py:4875:    # every distinct hold, not just the first. The OPEN-window rig makes the 2nd ping
tests/test_stream_session.py:4876:    # genuinely throttle-gated (with the bug the 2nd bell never fires → _wait_pings(2) times
tests/test_stream_session.py:4898:    # Wait for the FIRST ping, then resolve perm1 → the turn advances to perm2.
tests/test_stream_session.py:4899:    async def _wait_pings(n):
tests/test_stream_session.py:4904:        raise AssertionError(f"expected >= {n} background permission pings")
tests/test_stream_session.py:4906:    await _wait_pings(1)
tests/test_stream_session.py:4908:    # The SECOND distinct-id hold must ALSO ping (this is exactly what the throttle dropped).
tests/test_stream_session.py:4909:    await _wait_pings(2)
tests/test_stream_session.py:5342:    # This pins the ``rt.inflight`` term of the window-abort predicate — without it, an idle
tests/test_stream_session.py:5372:    store.switch(1, "beta")
tests/test_stream_session.py:5428:    store.switch(1, "beta")
tests/test_stream_session.py:5461:    store.switch(1, "beta")
tests/test_stream_session.py:5498:    store.switch(1, "beta")
tests/test_stream_session.py:5512:    # NB3 test below pins that reset ALONE cancels the queued turn.)
tests/test_stream_session.py:5543:    store.switch(1, "beta")  # beta is now the ACTIVE (foreground) project…
tests/test_stream_session.py:5590:    store.switch(1, "beta")
tests/test_stream_session.py:5661:    store.switch(1, "beta")
tests/test_stream_session.py:5670:    store.switch(1, "alpha")
tests/test_stream_session.py:5711:    # ``test_slot_leak_safety_*`` test above pins the SLOT side of this raise; this pins
tests/test_stream_session.py:5763:    # post-wait re-raise on a DEQUEUED turn and pins that its marker is cleared. cap=1.
tests/test_stream_session.py:5785:    store.switch(1, "gamma")
tests/test_stream_session.py:5790:    store.switch(1, "beta")
tests/test_stream_session.py:5836:    # asserts the refusal text + ``is_busy`` (lock) side; this pins the INFLIGHT side + the
tests/test_stream_session.py:5902:    Mirrors the helper in test_tool_path_confinement — no real wait; spins the loop until
tests/test_stream_session.py:5928:    # (1) Direct teeth: the live config's path context reached the engine. Dropping the
tests/test_stream_session.py:5994:    # Teeth: the configured bound reached the engine. Dropping the __init__ binding leaves
tests/test_stream_session.py:6004:    # driver_error. (RED if anyone re-pins the live bound back to 120 s.)
tests/test_stream_session.py:6372:    # session-creation knob baked into ClaudeAgentOptions — not hot-switchable). A back-to-back
tests/test_stream_session.py:6414:#   1. no link previews on background pings
tests/test_stream_session.py:6415:#   2. [Open <project>] switch button on attention + done pings; SB1-gated switch routing
tests/test_stream_session.py:6422:async def test_background_pings_disable_link_preview(tmp_path):
tests/test_stream_session.py:6423:    # T6.1: a background hold/terminal ping passes link_preview_options(is_disabled=True) so
tests/test_stream_session.py:6424:    # a path/URL in the (body-free) ping never balloons into a Telegram preview card.
tests/test_stream_session.py:6448:async def test_attention_ping_carries_open_project_button(tmp_path):
tests/test_stream_session.py:6449:    # T6.2: a background "needs attention" ping (permission) carries BOTH the verdict keyboard
tests/test_stream_session.py:6450:    # AND an [Open <name>] switch button row.
tests/test_stream_session.py:6463:    ping = next(s for s in rec.sends if s["text"].startswith("🔔 alpha"))
tests/test_stream_session.py:6464:    buttons = [b for row in ping["reply_markup"].inline_keyboard for b in row]
tests/test_stream_session.py:6466:    # The three verdict buttons PLUS the [Open alpha] switch row.
tests/test_stream_session.py:6469:    # The switch button's callback decodes to a switch for alpha.
tests/test_stream_session.py:6471:    assert open_btn.callback_data == encode_switch_callback("alpha")
tests/test_stream_session.py:6474:async def test_done_ping_carries_open_project_button(tmp_path):
tests/test_stream_session.py:6475:    # T6.2: a background "done" ping carries the [Open <name>] switch button (jump to project).
tests/test_stream_session.py:6488:    ping = next(s for s in rec.sends if s["text"].startswith("✅ alpha"))
tests/test_stream_session.py:6489:    buttons = [b for row in ping["reply_markup"].inline_keyboard for b in row]
tests/test_stream_session.py:6491:    assert buttons[0].callback_data == encode_switch_callback("alpha")
tests/test_stream_session.py:6494:async def test_error_ping_has_no_open_button_per_t6_scope(tmp_path):
tests/test_stream_session.py:6495:    # T6.2 scope: the switch button is on attention + done; an ERROR ping carries none.
tests/test_stream_session.py:6508:    ping = next(s for s in rec.sends if s["text"].startswith("⚠️ alpha"))
tests/test_stream_session.py:6509:    assert ping["reply_markup"] is None
tests/test_stream_session.py:6512:async def test_switch_button_tap_switches_active_project(tmp_path):
tests/test_stream_session.py:6513:    # T6.2: tapping [Open <name>] routes a switch outcome carrying the target name. The session
tests/test_stream_session.py:6514:    # does NOT mutate the store (the bot's /switch helper does the SB2 path revalidation +
tests/test_stream_session.py:6526:    out = session.resolve_callback(1, encode_switch_callback("beta"))
tests/test_stream_session.py:6528:    assert out.switch_to == "beta"
tests/test_stream_session.py:6529:    # The session itself does not switch (no SB2 path check available here) — that is the bot.
tests/test_stream_session.py:6533:async def test_switch_button_tap_no_store_is_benign_noop(tmp_path):
tests/test_stream_session.py:6534:    # RB1: a switch tap with no registry is a benign no-op (nothing to switch within).
tests/test_stream_session.py:6540:    out = session.resolve_callback(1, encode_switch_callback("beta"))
tests/test_stream_session.py:6542:    assert out.switch_to is None
tests/test_stream_session.py:6545:async def test_switch_button_forged_callback_resolves_nothing(tmp_path):
tests/test_stream_session.py:6546:    # MUTATION PROBE / SB1 defense-in-depth: a FORGED switch callback (non-SB4 name) decodes
tests/test_stream_session.py:6547:    # to None → resolve_callback handles nothing and switches nothing.
tests/test_stream_session.py:6559:    assert out.switch_to is None
tests/test_stream_session.py:6563:async def test_queued_counter_in_pings_when_projects_queued(tmp_path):
tests/test_stream_session.py:6564:    # T6.3: with N projects parked behind the cap, the ping shows "(N more waiting)". Build the
tests/test_stream_session.py:6565:    # queue state directly (a real waiter future) and assert the counter rides the ping.
tests/test_stream_session.py:6586:    ping = next(s for s in rec.sends if s["text"].startswith("✅ alpha"))
tests/test_stream_session.py:6587:    assert ping["text"] == "✅ alpha — done (2 more waiting)"
tests/test_stream_session.py:6750:    """Attach adopts the session as an active project pinned to the base id, persists the
tests/test_stream_session.py:6762:    assert rec["session_id"] == "sess-1"          # pinned to the base id
tests/test_stream_session.py:6818:    so /projects + /switch work on it (SB4-valid name)."""
tests/test_stream_session.py:6852:def test_attach_same_id_twice_is_idempotent_switch_no_duplicate(tmp_path):
tests/test_stream_session.py:7038:    the SAME id moved OUT-of-root → refused. Dropping resolve_within_roots would FAIL the
tests/test_stream_session.py:7117:    # A first turn that PARKS (holds), keeping the project busy.
tests/test_stream_session.py:7165:async def test_fire_schedule_pins_the_override_project(tmp_path):
tests/test_stream_session.py:7166:    """``project_override`` (the task's pinned project) targets THAT project even when another
tests/test_stream_session.py:7167:    is active — a scheduled task runs its OWN project, not whatever the chat switched to."""
tests/test_stream_session.py:7181:    # The turn ran against alpha's runtime (the pinned project), not the active beta.
tests/test_stream_session.py:7183:    # beta remains the store's active project (the fire did not switch it).
tests/test_stream_session.py:7204:async def test_handle_message_project_override_pins_named_project(tmp_path):
tests/test_stream_session.py:7206:    a proactive task targets ITS pinned project (the turn never retargets to the active one)."""
tests/test_stream_session.py:7219:    # The turn built alpha's runtime (the pinned project), beta stays the store's active.
tests/test_stream_session.py:7272:    # A first turn that PARKS, keeping the chat busy.
tests/test_stream_session.py:7299:# STATUSLINE T-SL-CORE — the pinned statusline pin/edit lifecycle.
tests/test_stream_session.py:7301:# _update_statusline builds the foreground statusline body from CURRENT state and reconciles
tests/test_stream_session.py:7302:# it with the chat's ONE pinned line: first use SENDS + PINS (silently); a changed state EDITS
tests/test_stream_session.py:7303:# in place; an identical state is a no-op; an edit FAILURE clears the id + re-sends + re-pins
tests/test_stream_session.py:7304:# (orphan recovery); and a pin/edit/send raising is SWALLOWED (RB1 — never breaks a turn). All
tests/test_stream_session.py:7307:# These exercise the machinery directly with fake send/edit/pin/unpin closures (T-SL-WIRE will
tests/test_stream_session.py:7308:# call _update_statusline from the live turn path; this unit is machinery-only).
tests/test_stream_session.py:7313:    """Captures the send/edit/pin/unpin calls _update_statusline performs (with fault injection).
tests/test_stream_session.py:7316:    unpinned/deleted the line). ``fail_pin`` / ``fail_send`` make pin / send raise (the RB1
tests/test_stream_session.py:7317:    swallow probe). ``fail_pin_times=N`` makes only the first N pins raise (then succeed — the
tests/test_stream_session.py:7318:    pin-retry probe). Each call is recorded so the sequence + the silent-pin flag are assertable.
tests/test_stream_session.py:7321:    def __init__(self, *, fail_edit=False, fail_pin=False, fail_send=False, fail_pin_times=0):
tests/test_stream_session.py:7324:        self.pins: list[dict] = []
tests/test_stream_session.py:7325:        self.unpins: list[dict] = []
tests/test_stream_session.py:7328:        self._fail_pin = fail_pin
tests/test_stream_session.py:7330:        self._fail_pin_remaining = fail_pin_times
tests/test_stream_session.py:7345:    async def pin(self, *, message_id, disable_notification=None) -> None:
tests/test_stream_session.py:7346:        self.pins.append({"message_id": message_id, "disable_notification": disable_notification})
tests/test_stream_session.py:7347:        if self._fail_pin:
tests/test_stream_session.py:7348:            raise RuntimeError("Telegram error: pin failed")
tests/test_stream_session.py:7349:        if self._fail_pin_remaining > 0:
tests/test_stream_session.py:7350:            self._fail_pin_remaining -= 1
tests/test_stream_session.py:7351:            raise RuntimeError("Telegram error: pin failed (transient)")
tests/test_stream_session.py:7353:    async def unpin(self, *, message_id) -> None:
tests/test_stream_session.py:7354:        self.unpins.append({"message_id": message_id})
tests/test_stream_session.py:7357:def _prime_statusline_project(session, *, chat_id=1, engine=None, status="running"):
tests/test_stream_session.py:7358:    """Resolve the chat's active project + give its runtime an engine + status (statusline read).
tests/test_stream_session.py:7360:    _update_statusline reads the FOREGROUND project's live state. With no store this auto-creates
tests/test_stream_session.py:7371:async def test_update_statusline_first_use_sends_then_pins_silently():
tests/test_stream_session.py:7375:    name, _rt = _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7377:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7380:    assert len(rec.pins) == 1, f"first use must PIN exactly once, pins={rec.pins!r}"
tests/test_stream_session.py:7381:    # The pin is SILENT (disable_notification=True) — design §3.1 (no re-ping).
tests/test_stream_session.py:7382:    assert rec.pins[0]["disable_notification"] is True
tests/test_stream_session.py:7383:    # The pinned id is the just-sent id (501 — the recorder hands out 501, 502, …).
tests/test_stream_session.py:7384:    assert rec.pins[0]["message_id"] == 501
tests/test_stream_session.py:7393:    # The id + text are tracked on the chat (the one-pin invariant).
tests/test_stream_session.py:7395:    assert state.statusline_message_id == 501
tests/test_stream_session.py:7396:    assert state.statusline_text == body
tests/test_stream_session.py:7399:async def test_update_statusline_second_changed_edits_in_place_no_repin():
tests/test_stream_session.py:7400:    # A SUBSEQUENT update with changed state EDITS in place — no re-pin, no re-send.
tests/test_stream_session.py:7403:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7405:    # First update → send + pin.
tests/test_stream_session.py:7406:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7411:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7414:    assert len(rec.pins) == 1, "the second update must NOT re-pin"
tests/test_stream_session.py:7417:    assert edited["message_id"] == 501  # the SAME pinned message is edited
tests/test_stream_session.py:7422:    assert session._chat(1).statusline_text == edited["text"]
tests/test_stream_session.py:7425:async def test_update_statusline_identical_state_is_no_io():
tests/test_stream_session.py:7430:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7432:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7434:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7438:    assert len(rec.pins) == 1
tests/test_stream_session.py:7441:async def test_update_statusline_edit_failure_resends_and_repins_orphan_recovery():
tests/test_stream_session.py:7442:    # The operator unpinned/deleted the line → the in-place edit raises "message to edit not
tests/test_stream_session.py:7446:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7448:    # First update → send (id 501) + pin.
tests/test_stream_session.py:7449:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7450:    assert session._chat(1).statusline_message_id == 501
tests/test_stream_session.py:7451:    # Change state → an EDIT is attempted; it fails → recovery re-sends (id 502) + re-pins.
tests/test_stream_session.py:7453:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7456:    assert len(rec.pins) == 2, f"recovery must re-PIN the fresh line, pins={rec.pins!r}"
tests/test_stream_session.py:7457:    # The stale id (501) was best-effort UNPINNED before re-pinning (one-pin invariant).
tests/test_stream_session.py:7458:    assert {u["message_id"] for u in rec.unpins} == {501}
tests/test_stream_session.py:7460:    assert session._chat(1).statusline_message_id == 502
tests/test_stream_session.py:7461:    assert rec.pins[-1]["message_id"] == 502
tests/test_stream_session.py:7462:    assert rec.pins[-1]["disable_notification"] is True
tests/test_stream_session.py:7465:async def test_update_statusline_pin_failure_is_swallowed_turn_unaffected():
tests/test_stream_session.py:7466:    # ⭐ RB1: a PIN that raises must NEVER escape — _update_statusline returns normally, the
tests/test_stream_session.py:7470:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7471:    rec = StatuslineRecorder(fail_pin=True)
tests/test_stream_session.py:7473:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7475:    assert len(rec.pins) == 1  # the pin was attempted (and raised, swallowed)
tests/test_stream_session.py:7477:    assert session._chat(1).statusline_message_id == 501
tests/test_stream_session.py:7480:async def test_update_statusline_send_failure_is_swallowed_turn_unaffected():
tests/test_stream_session.py:7481:    # ⭐ RB1: a SEND that raises must NEVER escape — _update_statusline returns normally and no
tests/test_stream_session.py:7485:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7488:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7489:    # The send raised before any id was returned → nothing pinned, nothing tracked.
tests/test_stream_session.py:7490:    assert rec.pins == []
tests/test_stream_session.py:7491:    assert session._chat(1).statusline_message_id is None
tests/test_stream_session.py:7492:    assert session._chat(1).statusline_text is None
tests/test_stream_session.py:7495:async def test_update_statusline_only_one_id_ever_held_across_many_updates():
tests/test_stream_session.py:7496:    # The one-pin invariant: across many state changes, exactly one id is held and only edits
tests/test_stream_session.py:7497:    # happen after the first pin.
tests/test_stream_session.py:7500:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7504:        await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7507:    assert len(rec.pins) == 1, "only ONE pin ever"
tests/test_stream_session.py:7511:    assert session._chat(1).statusline_message_id == 501
tests/test_stream_session.py:7514:async def test_update_statusline_no_foreground_project_is_noop(tmp_path):
tests/test_stream_session.py:7523:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7524:    assert rec.sends == [] and rec.pins == [] and rec.edits == []
tests/test_stream_session.py:7529:async def test_update_statusline_yolo_mode_shows_in_line():
tests/test_stream_session.py:7533:    _name, rt = _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7536:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7540:async def test_update_statusline_plan_armed_shows_in_line():
tests/test_stream_session.py:7544:    _name, rt = _prime_statusline_project(session, engine=eng, status="idle")
tests/test_stream_session.py:7547:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7551:async def test_update_statusline_ctx_none_when_no_engine_shows_em_dash():
tests/test_stream_session.py:7554:    _prime_statusline_project(session, engine=None, status="idle")
tests/test_stream_session.py:7556:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7561:async def test_update_statusline_engine_ctx_raises_is_swallowed_shows_dash():
tests/test_stream_session.py:7568:    _prime_statusline_project(session, engine=_BoomCtxEngine([]), status="running")
tests/test_stream_session.py:7571:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7576:async def test_update_statusline_edit_and_resend_both_raise_still_swallowed():
tests/test_stream_session.py:7578:    # ALSO raises (the chat is fully wedged at the Telegram layer). _update_statusline must STILL
tests/test_stream_session.py:7583:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7585:    # First update with a working send/pin to establish the pinned id.
tests/test_stream_session.py:7587:    await session._update_statusline(1, send=good.send, edit=good.edit, pin=good.pin, unpin=good.unpin)
tests/test_stream_session.py:7588:    assert session._chat(1).statusline_message_id == 501
tests/test_stream_session.py:7597:    async def boom_unpin(*, message_id):
tests/test_stream_session.py:7598:        raise RuntimeError("unpin failed too")
tests/test_stream_session.py:7602:    await session._update_statusline(1, send=boom_send, edit=boom_edit, pin=good.pin, unpin=boom_unpin)
tests/test_stream_session.py:7604:    assert session._chat(1).statusline_message_id is None
tests/test_stream_session.py:7608:# STATUSLINE T-SL-WIRE — the statusline wired into the LIVE turn lifecycle.
tests/test_stream_session.py:7610:# These drive REAL turns / commands through the session and assert the pinned line is
tests/test_stream_session.py:7612:#   * turn START pins the line with the working ⚙️ marker ON; turn END edits it OFF + ctx %;
tests/test_stream_session.py:7613:#   * /switch (the session-level _maybe_update_statusline, for_project=None) rewrites the line
tests/test_stream_session.py:7617:# All triggers are best-effort (a pin/edit failure never breaks the turn).
tests/test_stream_session.py:7622:    """Captures pin/unpin calls (the statusline's send/edit ride the regular Recorder).
tests/test_stream_session.py:7625:    statusline, so the integration tests use the regular :class:`Recorder` for send/edit
tests/test_stream_session.py:7626:    (statusline lines are identified by the 📁 glyph) and THIS for pin/unpin.
tests/test_stream_session.py:7630:        self.pins: list[dict] = []
tests/test_stream_session.py:7631:        self.unpins: list[dict] = []
tests/test_stream_session.py:7633:    async def pin(self, *, message_id, disable_notification=None) -> None:
tests/test_stream_session.py:7634:        self.pins.append({"message_id": message_id, "disable_notification": disable_notification})
tests/test_stream_session.py:7636:    async def unpin(self, *, message_id) -> None:
tests/test_stream_session.py:7637:        self.unpins.append({"message_id": message_id})
tests/test_stream_session.py:7640:def _statusline_sends(rec: "Recorder") -> list[dict]:
tests/test_stream_session.py:7641:    """The subset of ``rec.sends`` that are statusline lines (carry the 📁 worktree glyph)."""
tests/test_stream_session.py:7645:def _statusline_edits(rec: "Recorder") -> list[dict]:
tests/test_stream_session.py:7646:    """The subset of ``rec.edits`` that are statusline lines (carry the 📁 worktree glyph)."""
tests/test_stream_session.py:7650:async def test_foreground_turn_pins_at_start_then_refreshes_at_end():
tests/test_stream_session.py:7653:    # turn-lifecycle wiring (T7): _drive_turn calls _update_statusline at start + end.
tests/test_stream_session.py:7662:    # Prime the active project's runtime with the SAME engine so _statusline_text reads ctx 12%.
tests/test_stream_session.py:7666:    pins = PinRecorder()
tests/test_stream_session.py:7672:            pin=pins.pin, unpin=pins.unpin, target=(name, rt),
tests/test_stream_session.py:7676:    sl_sends = _statusline_sends(rec)
tests/test_stream_session.py:7677:    sl_edits = _statusline_edits(rec)
tests/test_stream_session.py:7678:    # Turn START: exactly one statusline SEND, PINNED silently, with the working ⚙️ marker ON.
tests/test_stream_session.py:7679:    assert len(sl_sends) == 1, f"turn start must pin the line once, statusline sends={sl_sends!r}"
tests/test_stream_session.py:7682:    assert len(pins.pins) == 1 and pins.pins[0]["disable_notification"] is True
tests/test_stream_session.py:7683:    # Turn END: the line is EDITED in place (same pinned id), marker OFF (idle), ctx refreshed.
tests/test_stream_session.py:7684:    assert sl_edits, "turn end must edit the statusline (marker off + ctx refresh)"
tests/test_stream_session.py:7688:    assert end["message_id"] == pins.pins[0]["message_id"], "the SAME pinned line is edited"
tests/test_stream_session.py:7689:    # Only ONE pin across the whole turn (the one-pin invariant holds through the lifecycle).
tests/test_stream_session.py:7690:    assert len(pins.pins) == 1
tests/test_stream_session.py:7693:async def test_turn_without_pin_closures_still_runs_no_statusline():
tests/test_stream_session.py:7694:    # Back-compat: a caller that does NOT inject pin/unpin (every pre-T-SL-WIRE path / test)
tests/test_stream_session.py:7695:    # drives the turn normally — the statusline is simply not pinned/edited, the turn is
tests/test_stream_session.py:7696:    # unaffected. (_maybe_update_statusline no-ops when any closure is missing.)
tests/test_stream_session.py:7704:        session.handle_message(1, "go", send=rec.send, edit=rec.edit),  # no pin/unpin
tests/test_stream_session.py:7708:    assert _statusline_sends(rec) == [], "no pin closures → no statusline send"
tests/test_stream_session.py:7709:    assert _statusline_edits(rec) == []
tests/test_stream_session.py:7712:async def test_background_turn_does_not_rewrite_foreground_statusline(tmp_path):
tests/test_stream_session.py:7714:    # BETA is the active/foreground project) must NEVER touch the pinned statusline — the line
tests/test_stream_session.py:7715:    # describes the FOREGROUND project only. _drive_turn gates its start/end statusline triggers
tests/test_stream_session.py:7716:    # on _is_foreground(turn_name); a background turn skips them.
tests/test_stream_session.py:7720:    # statusline send/edit/pin for the backgrounded alpha turn.
tests/test_stream_session.py:7728:    pins = PinRecorder()
tests/test_stream_session.py:7730:    # Drive ALPHA (a BACKGROUND project — beta is active) to completion WITH pin/unpin wired.
tests/test_stream_session.py:7735:            pin=pins.pin, unpin=pins.unpin, target=("alpha", rt_alpha),
tests/test_stream_session.py:7739:    # The turn RAN as a BACKGROUND turn (its terminal is a "✅ alpha — done" ping, NOT inline
tests/test_stream_session.py:7743:    # … but the foreground statusline was NEVER written — no 📁 send/edit, no pin.
tests/test_stream_session.py:7744:    assert _statusline_sends(rec) == [], "a BACKGROUND turn must NOT pin/send the foreground line"
tests/test_stream_session.py:7745:    assert _statusline_edits(rec) == [], "a BACKGROUND turn must NOT edit the foreground line"
tests/test_stream_session.py:7746:    assert pins.pins == [], "a BACKGROUND turn must NOT pin the foreground line"
tests/test_stream_session.py:7747:    # And no statusline id was established for the chat (nothing was pinned).
tests/test_stream_session.py:7748:    assert session._chat(1).statusline_message_id is None
tests/test_stream_session.py:7753:    # active), its turn DOES pin/refresh the line — so the gate keys on foreground, not on
tests/test_stream_session.py:7754:    # "two projects exist". (Together with the background test this pins the invariant exactly.)
tests/test_stream_session.py:7762:    pins = PinRecorder()
tests/test_stream_session.py:7768:            pin=pins.pin, unpin=pins.unpin, target=("alpha", rt_alpha),
tests/test_stream_session.py:7772:    sl_sends = _statusline_sends(rec)
tests/test_stream_session.py:7773:    assert len(sl_sends) == 1, "the FOREGROUND project's turn pins the line"
tests/test_stream_session.py:7775:    assert len(pins.pins) == 1
tests/test_stream_session.py:7778:async def test_switch_rewrites_statusline_to_new_project(tmp_path):
tests/test_stream_session.py:7779:    # /switch's session-level refresh (_maybe_update_statusline with for_project=None — the
tests/test_stream_session.py:7780:    # command path is foreground by definition) REWRITES the pinned line for the NOW-active
tests/test_stream_session.py:7781:    # project. Establish a line on alpha, switch active to beta, refresh → the line names beta.
tests/test_stream_session.py:7792:    pins = PinRecorder()
tests/test_stream_session.py:7793:    # First refresh (alpha active) → pin a line naming alpha.
tests/test_stream_session.py:7794:    await session._maybe_update_statusline(
tests/test_stream_session.py:7795:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7797:    assert _statusline_sends(rec) and "📁 alpha" in _statusline_sends(rec)[0]["text"]
tests/test_stream_session.py:7798:    # /switch → beta is now the active/foreground project; refresh rewrites the SAME line.
tests/test_stream_session.py:7799:    store.switch(1, "beta")
tests/test_stream_session.py:7800:    await session._maybe_update_statusline(
tests/test_stream_session.py:7801:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7803:    sl_edits = _statusline_edits(rec)
tests/test_stream_session.py:7804:    assert sl_edits, "the switch must EDIT the existing pinned line (not re-send)"
tests/test_stream_session.py:7805:    assert "📁 beta" in sl_edits[-1]["text"], "the line now names the switched-to project (beta)"
tests/test_stream_session.py:7806:    assert len(pins.pins) == 1, "switch edits in place — no re-pin"
tests/test_stream_session.py:7809:async def test_yolo_change_flips_mode_on_statusline():
tests/test_stream_session.py:7811:    # _maybe_update_statusline for_project=None).
tests/test_stream_session.py:7814:    _prime_statusline_project(session, engine=eng, status="idle")
tests/test_stream_session.py:7816:    pins = PinRecorder()
tests/test_stream_session.py:7818:    await session._maybe_update_statusline(
tests/test_stream_session.py:7819:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7821:    assert "🔒 gate" in _statusline_sends(rec)[0]["text"]
tests/test_stream_session.py:7824:    await session._maybe_update_statusline(
tests/test_stream_session.py:7825:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7827:    assert "🔒 yolo" in _statusline_edits(rec)[-1]["text"]
tests/test_stream_session.py:7830:async def test_effort_change_flips_model_suffix_on_statusline(tmp_path):
tests/test_stream_session.py:7832:    # ·max). Uses a real store so set_effort persists the override the statusline reads back.
tests/test_stream_session.py:7842:    pins = PinRecorder()
tests/test_stream_session.py:7844:    await session._maybe_update_statusline(
tests/test_stream_session.py:7845:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7847:    assert "·max" not in _statusline_sends(rec)[0]["text"]
tests/test_stream_session.py:7850:    await session._maybe_update_statusline(
tests/test_stream_session.py:7851:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7853:    assert "·max" in _statusline_edits(rec)[-1]["text"], "the 🤖 model·effort suffix flips to ·max"
tests/test_stream_session.py:7856:async def test_fast_model_change_flips_label_on_statusline(tmp_path):
tests/test_stream_session.py:7867:    pins = PinRecorder()
tests/test_stream_session.py:7868:    await session._maybe_update_statusline(
tests/test_stream_session.py:7869:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7873:    await session._maybe_update_statusline(
tests/test_stream_session.py:7874:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7876:    assert "🤖 haiku" in _statusline_edits(rec)[-1]["text"], "/fast → the model label flips to haiku"
tests/test_stream_session.py:7879:async def test_maybe_update_statusline_missing_closures_is_noop():
tests/test_stream_session.py:7880:    # The closure-presence gate: if ANY of send/edit/pin/unpin is None (a caller that didn't
tests/test_stream_session.py:7881:    # wire the statusline), _maybe_update_statusline is a pure no-op (the turn is unaffected).
tests/test_stream_session.py:7884:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7886:    pins = PinRecorder()
tests/test_stream_session.py:7887:    # pin=None → no-op (no send/edit/pin attempted).
tests/test_stream_session.py:7888:    await session._maybe_update_statusline(
tests/test_stream_session.py:7889:        1, send=rec.send, edit=rec.edit, pin=None, unpin=pins.unpin, for_project=None
tests/test_stream_session.py:7891:    assert rec.sends == [] and rec.edits == [] and pins.pins == []
tests/test_stream_session.py:7894:async def test_maybe_update_statusline_background_gate_is_noop(tmp_path):
tests/test_stream_session.py:7895:    # The foreground gate at the helper level: _maybe_update_statusline with a for_project that
tests/test_stream_session.py:7899:    pins = PinRecorder()
tests/test_stream_session.py:7901:    await session._maybe_update_statusline(
tests/test_stream_session.py:7902:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project="alpha"
tests/test_stream_session.py:7904:    assert rec.sends == [] and rec.edits == [] and pins.pins == []
tests/test_stream_session.py:7908:# STATUSLINE T-SL-WIRE — Codex NO_SHIP follow-up fixes (B1/B2/B3 + pin-retry).
tests/test_stream_session.py:7913:    # ⭐ B1 (make-or-break): the statusline body reads the ctx % via an AWAITED async
tests/test_stream_session.py:7919:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:7921:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7925:async def test_statusline_text_is_async_and_awaits_ctx_and_returns_built_for():
tests/test_stream_session.py:7926:    # B1 at the builder level: _statusline_text is a coroutine that awaits the async ctx source.
tests/test_stream_session.py:7931:    name, _rt = _prime_statusline_project(session, engine=eng, status="idle")
tests/test_stream_session.py:7932:    built = await session._statusline_text(1)
tests/test_stream_session.py:7939:async def test_statusline_text_none_when_no_foreground(tmp_path):
tests/test_stream_session.py:7945:    assert await session._statusline_text(1) is None
tests/test_stream_session.py:7948:def _two_project_statusline_session(tmp_path):
tests/test_stream_session.py:7949:    """A real-store session with alpha (active) + beta, each engine ready for a statusline read."""
tests/test_stream_session.py:7969:async def test_switch_after_snapshot_writes_current_line_not_stale(tmp_path):
tests/test_stream_session.py:7970:    # ⭐⭐ B2 (the foreground-switch race): _update_statusline snapshots the body, THEN the gated
tests/test_stream_session.py:7971:    # send awaits — a /switch in that window must NOT write the stale previous-project line. The
tests/test_stream_session.py:7972:    # fix REBUILDS the body from current state right before the write. We wrap _statusline_text
tests/test_stream_session.py:7977:    # this FAILS (it requires beta, the post-switch foreground).
tests/test_stream_session.py:7978:    session, store = _two_project_statusline_session(tmp_path)
tests/test_stream_session.py:7979:    real_text = session._statusline_text
tests/test_stream_session.py:7986:            # The snapshot read just returned alpha; a /switch lands BEFORE the rebuild read.
tests/test_stream_session.py:7987:            store.switch(1, "beta")
tests/test_stream_session.py:7990:    session._statusline_text = racing_text
tests/test_stream_session.py:7992:    pins = PinRecorder()
tests/test_stream_session.py:7993:    await session._update_statusline(
tests/test_stream_session.py:7994:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
tests/test_stream_session.py:7997:    sl_sends = _statusline_sends(rec)
tests/test_stream_session.py:7999:    assert "📁 beta" in sl_sends[0]["text"], "B2: the line names the POST-switch foreground (beta)"
tests/test_stream_session.py:8000:    assert "📁 alpha" not in sl_sends[0]["text"], "B2: never the stale pre-switch project (alpha)"
tests/test_stream_session.py:8003:async def test_switch_after_snapshot_on_edit_writes_current_line(tmp_path):
tests/test_stream_session.py:8004:    # B2 on the EDIT path: an established line, then a /switch between the edit's snapshot and its
tests/test_stream_session.py:8006:    session, store = _two_project_statusline_session(tmp_path)
tests/test_stream_session.py:8007:    # alpha starts RUNNING so its first pinned line differs from the idle line the 2nd update
tests/test_stream_session.py:8011:    pins = PinRecorder()
tests/test_stream_session.py:8012:    # Establish a pinned line on alpha first (no racing wrapper yet).
tests/test_stream_session.py:8013:    await session._update_statusline(
tests/test_stream_session.py:8014:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
tests/test_stream_session.py:8016:    assert "📁 alpha" in _statusline_sends(rec)[0]["text"]
tests/test_stream_session.py:8017:    # Now wrap _statusline_text so a /switch lands between the edit's snapshot and its rebuild.
tests/test_stream_session.py:8018:    real_text = session._statusline_text
tests/test_stream_session.py:8025:            store.switch(1, "beta")  # switch AFTER the snapshot read, BEFORE the rebuild
tests/test_stream_session.py:8028:    session._statusline_text = racing_text
tests/test_stream_session.py:8030:    await session._update_statusline(
tests/test_stream_session.py:8031:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
tests/test_stream_session.py:8033:    sl_edits = _statusline_edits(rec)
tests/test_stream_session.py:8040:    """A FakeEngine whose ASYNC context_percentage() performs a /switch mid-await (the residual
tests/test_stream_session.py:8042:    B2 window Codex flagged): _statusline_text captures the foreground project, THEN awaits
tests/test_stream_session.py:8043:    context_percentage() — this fake switches the store's active project DURING that await, so the
tests/test_stream_session.py:8044:    body built by THAT call is for the OLD (pre-switch) project. The final pre-write foreground
tests/test_stream_session.py:8047:    ``switch_on_call`` selects WHICH ctx call performs the switch (1-based). _update_statusline
tests/test_stream_session.py:8048:    calls _statusline_text twice — the top-level snapshot (call 1) and the gated-helper REBUILD
tests/test_stream_session.py:8049:    (call 2). To exercise the residual race we switch on the REBUILD call so it captures the old
tests/test_stream_session.py:8053:    def __init__(self, *, store, switch_to, switch_on_call=2, **kw):
tests/test_stream_session.py:8056:        self._switch_to = switch_to
tests/test_stream_session.py:8057:        self._switch_on_call = switch_on_call
tests/test_stream_session.py:8062:        if self._calls == self._switch_on_call:
tests/test_stream_session.py:8063:            self._store.switch(1, self._switch_to)  # ⭐ /switch lands DURING this ctx await
tests/test_stream_session.py:8067:async def test_switch_during_ctx_await_skips_stale_write_send(tmp_path):
tests/test_stream_session.py:8068:    # ⭐⭐ B2 RESIDUAL (Codex's re-opened probe): the B1 ctx-await is itself a /switch window.
tests/test_stream_session.py:8069:    # _statusline_text captures alpha, then awaits context_percentage() — which switches active to
tests/test_stream_session.py:8073:    # MUTATION PROBE: remove the post-await `_is_foreground(built_for)` guard in
tests/test_stream_session.py:8074:    # _statusline_send_and_pin and this FAILS — the stale alpha line is sent (exactly Codex's bug).
tests/test_stream_session.py:8082:    eng = _SwitchDuringCtxEngine(store=store, switch_to="beta", ctx_pct=5)
tests/test_stream_session.py:8092:    pins = PinRecorder()
tests/test_stream_session.py:8094:    # await switches active→beta; the rebuilt body is alpha's but built_for="alpha" is no longer
tests/test_stream_session.py:8096:    await session._update_statusline(
tests/test_stream_session.py:8097:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
tests/test_stream_session.py:8101:        "B2 residual: a stale alpha line must NOT be sent when /switch lands during the ctx await"
tests/test_stream_session.py:8102:    assert pins.pins == [], "nothing pinned (the stale send was skipped)"
tests/test_stream_session.py:8103:    assert session._chat(1).statusline_message_id is None, "no half-set state from a skipped send"
tests/test_stream_session.py:8104:    # The foreground is now beta (the switch took effect); a SUBSEQUENT update writes beta.
tests/test_stream_session.py:8108:async def test_switch_during_ctx_await_skips_stale_write_edit(tmp_path):
tests/test_stream_session.py:8109:    # B2 RESIDUAL on the EDIT path: an established (pinned) line, then a later update whose ctx
tests/test_stream_session.py:8110:    # await switches active→beta → the rebuilt body is alpha's (built_for="alpha", no longer
tests/test_stream_session.py:8111:    # foreground) → the stale EDIT is SKIPPED (the pinned line keeps its last good text).
tests/test_stream_session.py:8119:    # A plain engine for the FIRST (line-establishing) update; swap in the switching engine after.
tests/test_stream_session.py:8132:    pins = PinRecorder()
tests/test_stream_session.py:8133:    # 1) Establish + pin alpha's line.
tests/test_stream_session.py:8134:    await session._update_statusline(
tests/test_stream_session.py:8135:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
tests/test_stream_session.py:8137:    assert "📁 alpha" in _statusline_sends(rec)[0]["text"]
tests/test_stream_session.py:8138:    pinned_text_before = session._chat(1).statusline_text
tests/test_stream_session.py:8139:    # 2) Now alpha's engine switches active→beta DURING the next update's ctx await; alpha's status
tests/test_stream_session.py:8141:    rt_alpha.engine = _SwitchDuringCtxEngine(store=store, switch_to="beta", ctx_pct=5)
tests/test_stream_session.py:8143:    await session._update_statusline(
tests/test_stream_session.py:8144:        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
tests/test_stream_session.py:8147:    assert all("📁 alpha" not in (e.get("text") or "") for e in _statusline_edits(rec)), \
tests/test_stream_session.py:8148:        "B2 residual (edit): a stale alpha edit must NOT land when /switch hits during ctx await"
tests/test_stream_session.py:8149:    assert session._chat(1).statusline_text == pinned_text_before, "the pinned text is left intact"
tests/test_stream_session.py:8158:    # MUTATION PROBE: if _statusline_text still read only plan_next (consumed → False), the turn
tests/test_stream_session.py:8168:    pins = PinRecorder()
tests/test_stream_session.py:8176:            pin=pins.pin, unpin=pins.unpin, target=(name, rt), plan_turn=True,
tests/test_stream_session.py:8180:    sl_sends = _statusline_sends(rec)
tests/test_stream_session.py:8181:    sl_edits = _statusline_edits(rec)
tests/test_stream_session.py:8200:    pins = PinRecorder()
tests/test_stream_session.py:8206:            pin=pins.pin, unpin=pins.unpin, target=(name, rt),  # plan_turn defaults False
tests/test_stream_session.py:8210:    for s in _statusline_sends(rec):
tests/test_stream_session.py:8212:    assert "🔒 gate" in _statusline_sends(rec)[0]["text"]
tests/test_stream_session.py:8216:async def test_pin_fails_then_retried_on_next_update():
tests/test_stream_session.py:8218:    # + tracked but UNPINNED (statusline_pinned False). A later update RETRIES the pin even when
tests/test_stream_session.py:8219:    # the text is unchanged — so a transient pin failure self-heals instead of sticking unpinned.
tests/test_stream_session.py:8222:    # second update would NOT pin (pins stays length 1) → this FAILS.
tests/test_stream_session.py:8225:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:8226:    rec = StatuslineRecorder(fail_pin_times=1)  # the FIRST pin raises, later pins succeed
tests/test_stream_session.py:8227:    # First update: send ok, pin raises → tracked but not pinned.
tests/test_stream_session.py:8228:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8230:    assert len(rec.pins) == 1, "the first pin was attempted (and raised)"
tests/test_stream_session.py:8232:    assert state.statusline_message_id is not None
tests/test_stream_session.py:8233:    assert state.statusline_pinned is False, "a failed pin leaves the line UNPINNED"
tests/test_stream_session.py:8234:    # Second update with IDENTICAL state: must RETRY the pin (not skip past the unpinned state).
tests/test_stream_session.py:8235:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8237:    assert len(rec.pins) == 2, "the pin was RETRIED on the next update (pin-retry fix)"
tests/test_stream_session.py:8238:    assert rec.pins[-1]["message_id"] == state.statusline_message_id
tests/test_stream_session.py:8239:    assert state.statusline_pinned is True, "the retry succeeded → now pinned"
tests/test_stream_session.py:8240:    # A THIRD identical update is now a true no-op (pinned + identical → skip).
tests/test_stream_session.py:8241:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8242:    assert len(rec.pins) == 2, "once pinned, an identical update skips (no needless re-pin)"
tests/test_stream_session.py:8245:async def test_successful_pin_sets_pinned_flag():
tests/test_stream_session.py:8246:    # The happy path of the pin-retry bookkeeping: a successful first pin sets statusline_pinned
tests/test_stream_session.py:8250:    _prime_statusline_project(session, engine=eng, status="running")
tests/test_stream_session.py:8252:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8253:    assert session._chat(1).statusline_pinned is True
tests/test_stream_session.py:8254:    assert len(rec.pins) == 1

exec
/bin/zsh -lc 'git diff $(git merge-base HEAD main)..HEAD -- claude_tg/bot.py' in /Users/ray/dev/claude-telegram-bot-statusline
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

exec
/bin/zsh -lc 'git diff $(git merge-base HEAD main)..HEAD -- claude_tg/render.py tests/test_bot_streaming.py' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
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

codex
I found the residual B2 tests explicitly added for “switch during ctx await” on both send and edit paths. I’m reading the implementation around those helpers now to confirm the tests match the actual synchronization boundary.
exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '6300,6505p'" in /Users/ray/dev/claude-telegram-bot-statusline
exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '6505,6705p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
 succeeded in 0ms:
exec
/bin/zsh -lc "nl -ba tests/test_stream_session.py | sed -n '7908,8258p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
  7908	# STATUSLINE T-SL-WIRE — Codex NO_SHIP follow-up fixes (B1/B2/B3 + pin-retry).
  7909	# ---------------------------------------------------------------------------
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
  7925	async def test_statusline_text_is_async_and_awaits_ctx_and_returns_built_for():
  7926	    # B1 at the builder level: _statusline_text is a coroutine that awaits the async ctx source.
  7927	    # B2: it returns (text, built_for_project) — the project the body describes, for the final
  7928	    # pre-write foreground re-check.
  7929	    session = make_session(FakeEngine([]))
  7930	    eng = FakeEngine([], ctx_pct=21)
  7931	    name, _rt = _prime_statusline_project(session, engine=eng, status="idle")
  7932	    built = await session._statusline_text(1)
  7933	    assert built is not None
  7934	    body, built_for = built
  7935	    assert "🧠 ctx 21%" in body
  7936	    assert built_for == name  # the (text, built_for) contract — B2
  7937	
  7938	
  7939	async def test_statusline_text_none_when_no_foreground(tmp_path):
  7940	    # No active project → None (the write helpers treat None as "nothing to write").
  7941	    from claude_tg.session_store import JsonSessionStore
  7942	
  7943	    store = JsonSessionStore(tmp_path / "state.json")
  7944	    session = make_session(FakeEngine([]), store=store)
  7945	    assert await session._statusline_text(1) is None
  7946	
  7947	
  7948	def _two_project_statusline_session(tmp_path):
  7949	    """A real-store session with alpha (active) + beta, each engine ready for a statusline read."""
  7950	    from claude_tg.session_store import JsonSessionStore
  7951	
  7952	    store = JsonSessionStore(tmp_path / "state.json")
  7953	    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
  7954	    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
  7955	    (tmp_path / "a").mkdir()
  7956	    (tmp_path / "b").mkdir()
  7957	    eng = FakeEngine([], ctx_pct=5)
  7958	    cfg = make_config(allowed_roots=(str(tmp_path),), allow_any_path=False)
  7959	    session = StreamingSession(
  7960	        cfg, session_store=store,
  7961	        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
  7962	        clock=lambda: 0.0,
  7963	    )
  7964	    session._runtime(1, "alpha", str(tmp_path / "a")).engine = eng
  7965	    session._runtime(1, "beta", str(tmp_path / "b")).engine = eng
  7966	    return session, store
  7967	
  7968	
  7969	async def test_switch_after_snapshot_writes_current_line_not_stale(tmp_path):
  7970	    # ⭐⭐ B2 (the foreground-switch race): _update_statusline snapshots the body, THEN the gated
  7971	    # send awaits — a /switch in that window must NOT write the stale previous-project line. The
  7972	    # fix REBUILDS the body from current state right before the write. We wrap _statusline_text
  7973	    # so the SWITCH lands between the snapshot (1st call) and the rebuild (2nd call) — exactly the
  7974	    # race window — and assert the line that LANDS names the NEW project (beta), not alpha.
  7975	    #
  7976	    # MUTATION PROBE: revert the rebuild-after-wait and the SEND carries alpha (the snapshot) →
  7977	    # this FAILS (it requires beta, the post-switch foreground).
  7978	    session, store = _two_project_statusline_session(tmp_path)
  7979	    real_text = session._statusline_text
  7980	    calls = {"n": 0}
  7981	
  7982	    async def racing_text(chat_id):
  7983	        calls["n"] += 1
  7984	        body = await real_text(chat_id)  # 1st call → alpha (the snapshot); 2nd → beta (rebuild)
  7985	        if calls["n"] == 1:
  7986	            # The snapshot read just returned alpha; a /switch lands BEFORE the rebuild read.
  7987	            store.switch(1, "beta")
  7988	        return body
  7989	
  7990	    session._statusline_text = racing_text
  7991	    rec = Recorder()
  7992	    pins = PinRecorder()
  7993	    await session._update_statusline(
  7994	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  7995	    )
  7996	    assert calls["n"] >= 2, "the body must be REBUILT after the snapshot (B2)"
  7997	    sl_sends = _statusline_sends(rec)
  7998	    assert sl_sends, "the line was sent"
  7999	    assert "📁 beta" in sl_sends[0]["text"], "B2: the line names the POST-switch foreground (beta)"
  8000	    assert "📁 alpha" not in sl_sends[0]["text"], "B2: never the stale pre-switch project (alpha)"
  8001	
  8002	
  8003	async def test_switch_after_snapshot_on_edit_writes_current_line(tmp_path):
  8004	    # B2 on the EDIT path: an established line, then a /switch between the edit's snapshot and its
  8005	    # rebuild → the now-current project (beta) is edited in, never the stale snapshot (alpha).
  8006	    session, store = _two_project_statusline_session(tmp_path)
  8007	    # alpha starts RUNNING so its first pinned line differs from the idle line the 2nd update
  8008	    # builds → the 2nd update reaches the EDIT path (not the identical-text skip).
  8009	    session._runtime(1, "alpha", str(tmp_path / "a")).status = "running"
  8010	    rec = Recorder()
  8011	    pins = PinRecorder()
  8012	    # Establish a pinned line on alpha first (no racing wrapper yet).
  8013	    await session._update_statusline(
  8014	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  8015	    )
  8016	    assert "📁 alpha" in _statusline_sends(rec)[0]["text"]
  8017	    # Now wrap _statusline_text so a /switch lands between the edit's snapshot and its rebuild.
  8018	    real_text = session._statusline_text
  8019	    calls = {"n": 0}
  8020	
  8021	    async def racing_text(chat_id):
  8022	        calls["n"] += 1
  8023	        body = await real_text(chat_id)
  8024	        if calls["n"] == 1:
  8025	            store.switch(1, "beta")  # switch AFTER the snapshot read, BEFORE the rebuild
  8026	        return body
  8027	
  8028	    session._statusline_text = racing_text
  8029	    session._runtime(1, "alpha", str(tmp_path / "a")).status = "idle"  # alpha line now differs
  8030	    await session._update_statusline(
  8031	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  8032	    )
  8033	    sl_edits = _statusline_edits(rec)
  8034	    assert sl_edits, "an edit happened"
  8035	    assert "📁 beta" in sl_edits[-1]["text"], "B2 (edit): the now-current project is written"
  8036	    assert "📁 alpha" not in sl_edits[-1]["text"]
  8037	
  8038	
  8039	class _SwitchDuringCtxEngine(FakeEngine):
  8040	    """A FakeEngine whose ASYNC context_percentage() performs a /switch mid-await (the residual
  8041	
  8042	    B2 window Codex flagged): _statusline_text captures the foreground project, THEN awaits
  8043	    context_percentage() — this fake switches the store's active project DURING that await, so the
  8044	    body built by THAT call is for the OLD (pre-switch) project. The final pre-write foreground
  8045	    re-check must then SKIP the stale write.
  8046	
  8047	    ``switch_on_call`` selects WHICH ctx call performs the switch (1-based). _update_statusline
  8048	    calls _statusline_text twice — the top-level snapshot (call 1) and the gated-helper REBUILD
  8049	    (call 2). To exercise the residual race we switch on the REBUILD call so it captures the old
  8050	    project just before its await, then finds itself no-longer-foreground at the guard.
  8051	    """
  8052	
  8053	    def __init__(self, *, store, switch_to, switch_on_call=2, **kw):
  8054	        super().__init__([], **kw)
  8055	        self._store = store
  8056	        self._switch_to = switch_to
  8057	        self._switch_on_call = switch_on_call
  8058	        self._calls = 0
  8059	
  8060	    async def context_percentage(self):
  8061	        self._calls += 1
  8062	        if self._calls == self._switch_on_call:
  8063	            self._store.switch(1, self._switch_to)  # ⭐ /switch lands DURING this ctx await
  8064	        return self._ctx_pct
  8065	
  8066	
  8067	async def test_switch_during_ctx_await_skips_stale_write_send(tmp_path):
  8068	    # ⭐⭐ B2 RESIDUAL (Codex's re-opened probe): the B1 ctx-await is itself a /switch window.
  8069	    # _statusline_text captures alpha, then awaits context_percentage() — which switches active to
  8070	    # beta mid-await — so the rebuilt body is alpha's (built_for="alpha"). The FINAL sync
  8071	    # foreground re-check (built_for still foreground?) is now beta → SKIP the stale alpha write.
  8072	    #
  8073	    # MUTATION PROBE: remove the post-await `_is_foreground(built_for)` guard in
  8074	    # _statusline_send_and_pin and this FAILS — the stale alpha line is sent (exactly Codex's bug).
  8075	    from claude_tg.session_store import JsonSessionStore
  8076	
  8077	    store = JsonSessionStore(tmp_path / "state.json")
  8078	    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
  8079	    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
  8080	    (tmp_path / "a").mkdir()
  8081	    (tmp_path / "b").mkdir()
  8082	    eng = _SwitchDuringCtxEngine(store=store, switch_to="beta", ctx_pct=5)
  8083	    cfg = make_config(allowed_roots=(str(tmp_path),), allow_any_path=False)
  8084	    session = StreamingSession(
  8085	        cfg, session_store=store,
  8086	        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
  8087	        clock=lambda: 0.0,
  8088	    )
  8089	    session._runtime(1, "alpha", str(tmp_path / "a")).engine = eng
  8090	    session._runtime(1, "beta", str(tmp_path / "b")).engine = eng
  8091	    rec = Recorder()
  8092	    pins = PinRecorder()
  8093	    # First update (no line yet → the send path). alpha is foreground at the snapshot; the ctx
  8094	    # await switches active→beta; the rebuilt body is alpha's but built_for="alpha" is no longer
  8095	    # foreground → the stale send is SKIPPED.
  8096	    await session._update_statusline(
  8097	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  8098	    )
  8099	    # NO stale alpha line was sent (the residual race is closed).
  8100	    assert all("📁 alpha" not in (s.get("text") or "") for s in rec.sends), \
  8101	        "B2 residual: a stale alpha line must NOT be sent when /switch lands during the ctx await"
  8102	    assert pins.pins == [], "nothing pinned (the stale send was skipped)"
  8103	    assert session._chat(1).statusline_message_id is None, "no half-set state from a skipped send"
  8104	    # The foreground is now beta (the switch took effect); a SUBSEQUENT update writes beta.
  8105	    assert store.get_active(1) == "beta"
  8106	
  8107	
  8108	async def test_switch_during_ctx_await_skips_stale_write_edit(tmp_path):
  8109	    # B2 RESIDUAL on the EDIT path: an established (pinned) line, then a later update whose ctx
  8110	    # await switches active→beta → the rebuilt body is alpha's (built_for="alpha", no longer
  8111	    # foreground) → the stale EDIT is SKIPPED (the pinned line keeps its last good text).
  8112	    from claude_tg.session_store import JsonSessionStore
  8113	
  8114	    store = JsonSessionStore(tmp_path / "state.json")
  8115	    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
  8116	    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
  8117	    (tmp_path / "a").mkdir()
  8118	    (tmp_path / "b").mkdir()
  8119	    # A plain engine for the FIRST (line-establishing) update; swap in the switching engine after.
  8120	    plain = FakeEngine([], ctx_pct=5)
  8121	    cfg = make_config(allowed_roots=(str(tmp_path),), allow_any_path=False)
  8122	    session = StreamingSession(
  8123	        cfg, session_store=store,
  8124	        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: plain,
  8125	        clock=lambda: 0.0,
  8126	    )
  8127	    rt_alpha = session._runtime(1, "alpha", str(tmp_path / "a"))
  8128	    rt_alpha.engine = plain
  8129	    rt_alpha.status = "running"  # the first line is the running line
  8130	    session._runtime(1, "beta", str(tmp_path / "b")).engine = plain
  8131	    rec = Recorder()
  8132	    pins = PinRecorder()
  8133	    # 1) Establish + pin alpha's line.
  8134	    await session._update_statusline(
  8135	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  8136	    )
  8137	    assert "📁 alpha" in _statusline_sends(rec)[0]["text"]
  8138	    pinned_text_before = session._chat(1).statusline_text
  8139	    # 2) Now alpha's engine switches active→beta DURING the next update's ctx await; alpha's status
  8140	    #    changes so a (stale) edit WOULD be attempted — but the final foreground guard skips it.
  8141	    rt_alpha.engine = _SwitchDuringCtxEngine(store=store, switch_to="beta", ctx_pct=5)
  8142	    rt_alpha.status = "idle"
  8143	    await session._update_statusline(
  8144	        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
  8145	    )
  8146	    # NO stale alpha edit landed (the residual race is closed); the tracked text is unchanged.
  8147	    assert all("📁 alpha" not in (e.get("text") or "") for e in _statusline_edits(rec)), \
  8148	        "B2 residual (edit): a stale alpha edit must NOT land when /switch hits during ctx await"
  8149	    assert session._chat(1).statusline_text == pinned_text_before, "the pinned text is left intact"
  8150	    assert store.get_active(1) == "beta"
  8151	
  8152	
  8153	async def test_plan_turn_shows_plan_mode_while_running_then_gate(tmp_path):
  8154	    # ⭐ B3: during an ACTUAL plan-mode turn the line shows 🔒 plan (not 🔒 gate). plan_next is
  8155	    # consumed by handle_message BEFORE _drive_turn, so the live flag is in_plan_turn (set at
  8156	    # turn start from the consumed plan_turn, cleared at turn end). Turn start → plan; end → gate.
  8157	    #
  8158	    # MUTATION PROBE: if _statusline_text still read only plan_next (consumed → False), the turn
  8159	    # would show 🔒 gate and this FAILS.
  8160	    engine = FakeEngine(
  8161	        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="planned")],
  8162	        ctx_pct=8,
  8163	    )
  8164	    session = make_session(engine)
  8165	    name, rt = session._active_runtime(1, create_default=True)
  8166	    rt.engine = engine
  8167	    rec = Recorder()
  8168	    pins = PinRecorder()
  8169	    state = session._chat(1)
  8170	    # Drive a PLAN turn (plan_turn=True — the value handle_message would pass after consuming
  8171	    # the one-shot plan_next).
  8172	    await asyncio.wait_for(
  8173	        session._drive_turn(
  8174	            state, 1, engine, "go",
  8175	            send=rec.send, edit=rec.edit, delete=rec.delete,
  8176	            pin=pins.pin, unpin=pins.unpin, target=(name, rt), plan_turn=True,
  8177	        ),
  8178	        timeout=2.0,
  8179	    )
  8180	    sl_sends = _statusline_sends(rec)
  8181	    sl_edits = _statusline_edits(rec)
  8182	    # Turn START line → 🔒 plan (the live plan turn).
  8183	    assert sl_sends and "🔒 plan" in sl_sends[0]["text"], "B3: a running plan turn shows 🔒 plan"
  8184	    # Turn END line → back to 🔒 gate (in_plan_turn cleared; plan_next was already consumed).
  8185	    assert sl_edits and "🔒 gate" in sl_edits[-1]["text"], "B3: after the plan turn → 🔒 gate"
  8186	    # The live flag is cleared after the turn (no lingering plan mode).
  8187	    assert rt.in_plan_turn is False
  8188	
  8189	
  8190	async def test_non_plan_turn_does_not_show_plan_mode():
  8191	    # B3 complement: a NORMAL turn (plan_turn=False) never shows 🔒 plan — it shows 🔒 gate.
  8192	    engine = FakeEngine(
  8193	        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
  8194	        ctx_pct=8,
  8195	    )
  8196	    session = make_session(engine)
  8197	    name, rt = session._active_runtime(1, create_default=True)
  8198	    rt.engine = engine
  8199	    rec = Recorder()
  8200	    pins = PinRecorder()
  8201	    state = session._chat(1)
  8202	    await asyncio.wait_for(
  8203	        session._drive_turn(
  8204	            state, 1, engine, "go",
  8205	            send=rec.send, edit=rec.edit, delete=rec.delete,
  8206	            pin=pins.pin, unpin=pins.unpin, target=(name, rt),  # plan_turn defaults False
  8207	        ),
  8208	        timeout=2.0,
  8209	    )
  8210	    for s in _statusline_sends(rec):
  8211	        assert "🔒 plan" not in s["text"]
  8212	    assert "🔒 gate" in _statusline_sends(rec)[0]["text"]
  8213	    assert rt.in_plan_turn is False
  8214	
  8215	
  8216	async def test_pin_fails_then_retried_on_next_update():
  8217	    # ⭐ Pin-retry (non-blocking): the SEND succeeds but the first PIN raises → the line is sent
  8218	    # + tracked but UNPINNED (statusline_pinned False). A later update RETRIES the pin even when
  8219	    # the text is unchanged — so a transient pin failure self-heals instead of sticking unpinned.
  8220	    #
  8221	    # MUTATION PROBE: without the retry, the identical-text skip would short-circuit and the
  8222	    # second update would NOT pin (pins stays length 1) → this FAILS.
  8223	    session = make_session(FakeEngine([]))
  8224	    eng = FakeEngine([], ctx_pct=6)
  8225	    _prime_statusline_project(session, engine=eng, status="running")
  8226	    rec = StatuslineRecorder(fail_pin_times=1)  # the FIRST pin raises, later pins succeed
  8227	    # First update: send ok, pin raises → tracked but not pinned.
  8228	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  8229	    assert len(rec.sends) == 1
  8230	    assert len(rec.pins) == 1, "the first pin was attempted (and raised)"
  8231	    state = session._chat(1)
  8232	    assert state.statusline_message_id is not None
  8233	    assert state.statusline_pinned is False, "a failed pin leaves the line UNPINNED"
  8234	    # Second update with IDENTICAL state: must RETRY the pin (not skip past the unpinned state).
  8235	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  8236	    assert len(rec.sends) == 1, "no re-send (the line is already sent)"
  8237	    assert len(rec.pins) == 2, "the pin was RETRIED on the next update (pin-retry fix)"
  8238	    assert rec.pins[-1]["message_id"] == state.statusline_message_id
  8239	    assert state.statusline_pinned is True, "the retry succeeded → now pinned"
  8240	    # A THIRD identical update is now a true no-op (pinned + identical → skip).
  8241	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  8242	    assert len(rec.pins) == 2, "once pinned, an identical update skips (no needless re-pin)"
  8243	
  8244	
  8245	async def test_successful_pin_sets_pinned_flag():
  8246	    # The happy path of the pin-retry bookkeeping: a successful first pin sets statusline_pinned
  8247	    # True so subsequent identical updates correctly skip.
  8248	    session = make_session(FakeEngine([]))
  8249	    eng = FakeEngine([], ctx_pct=6)
  8250	    _prime_statusline_project(session, engine=eng, status="running")
  8251	    rec = StatuslineRecorder()
  8252	    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
  8253	    assert session._chat(1).statusline_pinned is True
  8254	    assert len(rec.pins) == 1

codex
The test coverage is targeting the right failure mode, but the verdict depends on the production line ordering, not just tests. I’m locating the actual helper definitions because the earlier line window missed them.
exec
/bin/zsh -lc 'wc -l claude_tg/stream_session.py' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
    5487 claude_tg/stream_session.py

exec
/bin/zsh -lc 'rg -n "async def _statusline_text|async def _statusline_gated_edit|async def _statusline_send_and_pin|def _is_foreground|async def _maybe_update_statusline|async def _update_statusline|in_plan_turn|context_percentage|statusline_pinned" claude_tg/stream_session.py' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
459:    in_plan_turn: bool = False
732:    statusline_pinned: bool = False
995:    def _is_foreground(self, chat_id: int, name: Optional[str]) -> bool:
3627:        turn_rt.in_plan_turn = plan_turn
3841:            turn_rt.in_plan_turn = False
3845:            # _statusline_text's engine.context_percentage() reads the fresh figure). FOREGROUND-
4159:    async def _maybe_update_statusline(
4204:    async def _statusline_text(self, chat_id: int) -> Optional[tuple[str, str]]:
4226:        ⭐ **ASYNC (B1 fix):** the ctx % comes from ``Engine.context_percentage()`` which AWAITS
4235:          is RUNNING (``in_plan_turn`` — B3) OR a ``/plan`` is armed for the next turn
4239:        * ``ctx_pct`` — the live engine's :meth:`~claude_tg.engine.engine.Engine.context_percentage`
4249:        # (``in_plan_turn``, B3 — ``plan_next`` is already consumed by the time the turn streams)
4253:        elif bool(getattr(rt, "in_plan_turn", False)) or bool(getattr(rt, "plan_next", False)):
4264:                ctx_pct = await engine.context_percentage()
4277:    async def _update_statusline(
4325:        (``statusline_pinned`` False); a later update RETRIES the pin even if the text is
4338:                and not state.statusline_pinned
4365:                state.statusline_pinned = False
4377:    async def _statusline_gated_edit(
4409:    async def _statusline_send_and_pin(
4429:        ``statusline_pinned=False`` so the next update retries the pin. Called from
4453:        state.statusline_pinned = False  # not pinned until the pin call below succeeds.
4460:        On success ``statusline_pinned`` is set True; on failure it stays/!becomes False and is
4467:            state.statusline_pinned = True
4469:            state.statusline_pinned = False

exec
/bin/zsh -lc 'git diff $(git merge-base HEAD main)..HEAD -- claude_tg/stream_session.py' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
diff --git a/claude_tg/stream_session.py b/claude_tg/stream_session.py
index 3bb30b5..e593fc6 100644
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
@@ -3966,6 +4154,321 @@ class StreamingSession:
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
+    async def _statusline_text(self, chat_id: int) -> Optional[tuple[str, str]]:
+        """Build the CURRENT statusline body + the project it was built FOR (``(text, name)``).
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
+        default (``ctx —``, ``gate``) rather than raising.
+
+        ⭐ **Returns ``(text, built_for)``** — the rendered body AND the project NAME it describes
+        — or ``None`` when there is no foreground project. The caller uses ``built_for`` for the
+        FINAL pre-write foreground re-check (B2): the ctx ``await`` below is a switch window, so
+        the only safe guarantee is "the project this text was built for is STILL foreground at the
+        instant just before the write" — a sync check the write helpers do with no await between
+        it and the ``edit``/``send``.
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
+                # ⭐ The ONLY await in this builder — and a /switch window (B2): the returned
+                # ``built_for`` lets the write helpers re-check foreground AFTER this await.
+                ctx_pct = await engine.context_percentage()
+            except Exception:  # pragma: no cover - the engine call is already best-effort (RB1)
+                ctx_pct = None
+        body = format_statusline(
+            worktree=worktree,
+            model_label=model_label,
+            effort=effort,
+            ctx_pct=ctx_pct,
+            mode=mode,
+            working=working,
+        )
+        return body, name
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
+        **⭐ B2 fix — no stale line across a ``/switch`` (the FINAL guard).** The body is built
+        from the FOREGROUND project's state, but BOTH the gate's wait AND the ctx ``await`` inside
+        the rebuild are ``/switch`` windows. So the gated write helpers (1) REBUILD the body from
+        CURRENT state after the wait, then (2) do a FINAL **synchronous** foreground re-check — is
+        the project the rebuilt text was BUILT FOR still the chat's active/foreground? — with NO
+        await between that check and issuing the ``edit``/``send``. If a ``/switch`` happened
+        during ANY await, ``built_for`` is no longer foreground → the stale write is SKIPPED (the
+        ``/switch``'s own statusline trigger writes the correct line — no loop, no stale write).
+        **Pin-retry** — a send that succeeded while its pin RAISED leaves the line UNPINNED
+        (``statusline_pinned`` False); a later update RETRIES the pin even if the text is
+        unchanged, so a transient pin failure self-heals instead of sticking unpinned forever.
+        """
+        try:
+            built = await self._statusline_text(chat_id)  # async (B1: awaits the SDK ctx %)
+            if not built:
+                return  # no foreground project to describe — nothing to pin/edit.
+            body, _built_for = built  # body for the skip/decision; the helpers rebuild + re-check
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
+        """Edit the pinned line through the gate, REBUILDING + RE-CHECKING foreground (B2).
+
+        Reserves the per-chat gate slot and awaits its wait (non-verbatim — RB5), THEN re-derives
+        the statusline body + the project it was built for from CURRENT state. Two awaits precede
+        the write — the gate wait AND the ctx ``await`` inside :meth:`_statusline_text` — both
+        ``/switch`` windows. So immediately before the raw edit we do a FINAL **synchronous**
+        foreground re-check (``_is_foreground(built_for)``) with NO await between it and the
+        ``edit``: if a ``/switch`` happened during ANY await, ``built_for`` is no longer
+        foreground → SKIP (the switch's own trigger writes the correct line — no stale write, no
+        loop). An empty rebuild (foreground vanished — e.g. ``/rm``) or identical text also skips.
+        A raise propagates to the caller's orphan-recovery (the message may be gone).
+        """
+        wait = self._gate(state).reserve(verbatim=False)
+        if wait > 0:
+            await self._sleep(wait)
+        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
+        if built is None:
+            return  # foreground vanished mid-wait → no stale write.
+        body, built_for = built
+        if body == state.statusline_text:
+            return  # nothing changed → no no-op "not modified" edit.
+        # ⭐ FINAL sync guard (B2): only write if the project this body describes is STILL the
+        # chat's foreground at THIS instant — no await between here and the edit, so a /switch
+        # during any preceding await is caught. A stale body (built_for switched away) is dropped.
+        if not self._is_foreground(chat_id, built_for):
+            return
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
+        rebuild the body + the project it was built for from CURRENT state. Two awaits precede the
+        send — the gate wait AND the ctx ``await`` inside :meth:`_statusline_text` — both
+        ``/switch`` windows (B2). So immediately before the raw send we do a FINAL **synchronous**
+        foreground re-check (``_is_foreground(built_for)``) with NO await between it and the
+        ``send``: a ``/switch`` during any preceding await makes ``built_for`` no longer
+        foreground → SKIP (the switch's own trigger sends the correct line — no stale send, no
+        loop). A best-effort silent pin follows (``disable_notification=True`` — a pin must never
+        re-ping). The id/text are stored ONLY when the send returns an id (so a ``None`` send does
+        not leave a half-set state). A PIN failure is swallowed (RB1) AND records
+        ``statusline_pinned=False`` so the next update retries the pin. Called from
+        :meth:`_update_statusline` inside its best-effort guard, so a raising ``send`` propagates
+        to that guard's swallow.
+        """
+        wait = self._gate(state).reserve(verbatim=False)
+        if wait > 0:
+            await self._sleep(wait)
+        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
+        if built is None:
+            return  # foreground vanished mid-wait — nothing to send.
+        body, built_for = built
+        # ⭐ FINAL sync guard (B2): only send if the project this body describes is STILL the
+        # chat's foreground at THIS instant — no await between here and the send, so a /switch
+        # during any preceding await (the gate wait OR the ctx await) is caught and the stale
+        # send is dropped (the switch's own statusline trigger sends the correct line).
+        if not self._is_foreground(chat_id, built_for):
+            return
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

codex
The production helpers do have a synchronous foreground check immediately before the Telegram call in each path. I’m doing one last pass around switch-trigger wiring and the existing safeguards before giving the requested compact verdict.
exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '4155,4475p'" in /Users/ray/dev/claude-telegram-bot-statusline
exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '985,1005p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
   985	        gate (so a concurrent project's status churn never starves a prompt). Reserves +
   986	        awaits like :meth:`_gated_send`, then performs the real ``edit``.
   987	        """
   988	        wait = self._gate(state).reserve(verbatim=False)
   989	        if wait > 0:
   990	            await self._sleep(wait)
   991	        await edit(**kwargs)
   992	
   993	    # -- foreground (inline-vs-notify decision, ADR-005 D4) ------------------
   994	
   995	    def _is_foreground(self, chat_id: int, name: Optional[str]) -> bool:
   996	        """Whether ``name`` is the chat's current foreground (the store's active) project.
   997	
   998	        The notification send-decision (D4): an event for the **foreground** project
   999	        renders inline (as P4); a **background** (non-foreground) project's hold/terminal
  1000	        becomes a name-prefixed 🔔/✅/⚠️ ping instead. "Foreground" is the store's
  1001	        ``active`` (a per-chat marker); reading it is **read-only** (never creates a
  1002	        project — RB1). With no store there is a single implicit project, so it is always
  1003	        foreground (one-project deployments never notify — no behavior change). Matched
  1004	        case-insensitively (mirroring the store's name match) so it agrees with
  1005	        ``/projects``/``/switch``.

 succeeded in 0ms:
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
  4204	    async def _statusline_text(self, chat_id: int) -> Optional[tuple[str, str]]:
  4205	        """Build the CURRENT statusline body + the project it was built FOR (``(text, name)``).
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
  4217	        default (``ctx —``, ``gate``) rather than raising.
  4218	
  4219	        ⭐ **Returns ``(text, built_for)``** — the rendered body AND the project NAME it describes
  4220	        — or ``None`` when there is no foreground project. The caller uses ``built_for`` for the
  4221	        FINAL pre-write foreground re-check (B2): the ctx ``await`` below is a switch window, so
  4222	        the only safe guarantee is "the project this text was built for is STILL foreground at the
  4223	        instant just before the write" — a sync check the write helpers do with no await between
  4224	        it and the ``edit``/``send``.
  4225	
  4226	        ⭐ **ASYNC (B1 fix):** the ctx % comes from ``Engine.context_percentage()`` which AWAITS
  4227	        the SDK's coroutine ``get_context_usage()`` — so this method is async and awaits it. The
  4228	        await is still fully best-effort (any raise → ``ctx —``, never a fabricated number); it
  4229	        is the only await here (every other field is a pure in-memory read).
  4230	
  4231	        * ``worktree`` — the active project NAME (SB4-validated charset, so inert — SB3).
  4232	        * ``model`` — :meth:`_resolve_project_model` reduced by :func:`model_short_label`.
  4233	        * ``effort`` — :meth:`_resolve_project_effort` (``None`` → model-only).
  4234	        * ``mode`` — ``yolo`` if the project's policy is allow-all, else ``plan`` if a plan turn
  4235	          is RUNNING (``in_plan_turn`` — B3) OR a ``/plan`` is armed for the next turn
  4236	          (``plan_next``), else ``gate`` (the fail-closed default).
  4237	        * ``working`` — the per-project status enum is a working state (``running`` /
  4238	          ``awaiting_*`` / ``queued``) vs ``idle``.
  4239	        * ``ctx_pct`` — the live engine's :meth:`~claude_tg.engine.engine.Engine.context_percentage`
  4240	          (``None`` → ``ctx —``, never a fabricated number).
  4241	        """
  4242	        name, rt = self._active_runtime(chat_id, create_default=False)
  4243	        if name is None or rt is None:
  4244	            return None
  4245	        worktree = name  # the SB4-validated project name (no path; SB3-inert).
  4246	        model_label = model_short_label(self._resolve_project_model(chat_id, name))
  4247	        effort = self._resolve_project_effort(chat_id, name)
  4248	        # mode: yolo (allow-all) wins; else plan — either a plan turn is RUNNING NOW
  4249	        # (``in_plan_turn``, B3 — ``plan_next`` is already consumed by the time the turn streams)
  4250	        # OR a ``/plan`` is armed for the NEXT turn (``plan_next``); else the fail-closed gate.
  4251	        if bool(getattr(rt.policy, "yolo", False)):
  4252	            mode = "yolo"
  4253	        elif bool(getattr(rt, "in_plan_turn", False)) or bool(getattr(rt, "plan_next", False)):
  4254	            mode = "plan"
  4255	        else:
  4256	            mode = "gate"
  4257	        working = rt.status in ("running", "awaiting_approval", "awaiting_answer", "awaiting_plan", "queued")
  4258	        ctx_pct: Optional[int] = None
  4259	        engine = rt.engine
  4260	        if engine is not None:
  4261	            try:
  4262	                # ⭐ The ONLY await in this builder — and a /switch window (B2): the returned
  4263	                # ``built_for`` lets the write helpers re-check foreground AFTER this await.
  4264	                ctx_pct = await engine.context_percentage()
  4265	            except Exception:  # pragma: no cover - the engine call is already best-effort (RB1)
  4266	                ctx_pct = None
  4267	        body = format_statusline(
  4268	            worktree=worktree,
  4269	            model_label=model_label,
  4270	            effort=effort,
  4271	            ctx_pct=ctx_pct,
  4272	            mode=mode,
  4273	            working=working,
  4274	        )
  4275	        return body, name
  4276	
  4277	    async def _update_statusline(
  4278	        self,
  4279	        chat_id: int,
  4280	        *,
  4281	        send: SendFn,
  4282	        edit: EditFn,
  4283	        pin: PinFn,
  4284	        unpin: UnpinFn,
  4285	    ) -> None:
  4286	        """Refresh the chat's ONE pinned statusline — send+pin on first use, edit thereafter.
  4287	
  4288	        STATUSLINE T-SL-CORE (design §3.1/§4). Builds the current foreground statusline body
  4289	        (:meth:`_statusline_text`) and reconciles it with the chat's pinned line:
  4290	
  4291	        * **identical text** → skip entirely (no I/O — a no-op edit raises "message is not
  4292	          modified" AND wastes a send slot; mirrors :meth:`_edit_status`).
  4293	        * **first update** (no id held) → SEND the body then PIN it with the notification
  4294	          DISABLED (a silent pin — design §3.1); store the id + text.
  4295	        * **subsequent update** → EDIT in place only (no re-pin, no re-send; a pinned message
  4296	          edited in place stays pinned and silent).
  4297	        * **edit FAILURE** (the operator unpinned/deleted it → "message to edit not found", an
  4298	          API hiccup, too old) → ORPHAN RECOVERY: clear the stored id, best-effort UNPIN the
  4299	          stale one (the "one pinned message" invariant — Telegram's current pin is the newest,
  4300	          so the bar self-corrects), then re-SEND + re-PIN a fresh line (mirrors the orphaned
  4301	          status-line recovery in :meth:`_edit_status`).
  4302	
  4303	        **⭐ RB1 — a pin/edit/send failure NEVER breaks or wedges a turn.** This is an observer
  4304	        OFF the turn's critical path: the WHOLE body is wrapped so ANY exception (a raising
  4305	        ``send``/``edit``/``pin``/``unpin``, a build error) is logged at debug and swallowed —
  4306	        the caller (the turn loop / a command) is unaffected. **RB5** — every send/edit funnels
  4307	        through the per-chat gate as the **non-verbatim** kind (:meth:`_gated_send`/
  4308	        :meth:`_gated_edit`), so the statusline can never flood and never starves a real
  4309	        answer/prompt. **One id invariant** — exactly one ``statusline_message_id`` is ever held
  4310	        per chat; we only ever edit it, and on recovery re-point it.
  4311	
  4312	        ``send``/``edit``/``pin``/``unpin`` are injected by ``bot.py`` (the same pattern as the
  4313	        existing send/edit/delete closures) targeting THIS chat — so the line is SB1-confined to
  4314	        the operator's allowlisted chat (no new outbound surface).
  4315	
  4316	        **⭐ B2 fix — no stale line across a ``/switch`` (the FINAL guard).** The body is built
  4317	        from the FOREGROUND project's state, but BOTH the gate's wait AND the ctx ``await`` inside
  4318	        the rebuild are ``/switch`` windows. So the gated write helpers (1) REBUILD the body from
  4319	        CURRENT state after the wait, then (2) do a FINAL **synchronous** foreground re-check — is
  4320	        the project the rebuilt text was BUILT FOR still the chat's active/foreground? — with NO
  4321	        await between that check and issuing the ``edit``/``send``. If a ``/switch`` happened
  4322	        during ANY await, ``built_for`` is no longer foreground → the stale write is SKIPPED (the
  4323	        ``/switch``'s own statusline trigger writes the correct line — no loop, no stale write).
  4324	        **Pin-retry** — a send that succeeded while its pin RAISED leaves the line UNPINNED
  4325	        (``statusline_pinned`` False); a later update RETRIES the pin even if the text is
  4326	        unchanged, so a transient pin failure self-heals instead of sticking unpinned forever.
  4327	        """
  4328	        try:
  4329	            built = await self._statusline_text(chat_id)  # async (B1: awaits the SDK ctx %)
  4330	            if not built:
  4331	                return  # no foreground project to describe — nothing to pin/edit.
  4332	            body, _built_for = built  # body for the skip/decision; the helpers rebuild + re-check
  4333	            state = self._chat(chat_id)
  4334	            # Pin-retry: if we hold a sent id whose pin FAILED, retry the pin even on identical
  4335	            # text (the identical-text skip below would otherwise leave it unpinned forever).
  4336	            if (
  4337	                state.statusline_message_id is not None
  4338	                and not state.statusline_pinned
  4339	                and body == state.statusline_text
  4340	            ):
  4341	                await self._statusline_pin(state, state.statusline_message_id, pin=pin)
  4342	                return
  4343	            if body == state.statusline_text:
  4344	                # Identical to what's pinned — skip BEFORE the gate so an unchanged refresh
  4345	                # never consumes a send slot and never triggers a no-op "not modified" edit.
  4346	                return
  4347	            if state.statusline_message_id is None:
  4348	                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
  4349	                return
  4350	            try:
  4351	                # B2: rebuild the body AFTER the gate wait (inside _statusline_gated_edit) so a
  4352	                # /switch during the wait writes the now-current line, never the stale snapshot.
  4353	                await self._statusline_gated_edit(
  4354	                    chat_id, state, state.statusline_message_id, edit=edit
  4355	                )
  4356	            except Exception:
  4357	                # Orphan recovery (design §4 RB1): the pinned line is gone (unpinned/deleted by
  4358	                # the operator) / too old / an API hiccup. Clear the dead id, best-effort UNPIN
  4359	                # the stale one (one-pin invariant), then re-send + re-pin a fresh line. The
  4360	                # turn is unaffected either way (this whole method is best-effort).
  4361	                log.debug("statusline edit failed for chat; re-sending + re-pinning", exc_info=True)
  4362	                stale_id = state.statusline_message_id
  4363	                state.statusline_message_id = None
  4364	                state.statusline_text = None
  4365	                state.statusline_pinned = False
  4366	                try:
  4367	                    await unpin(message_id=stale_id)
  4368	                except Exception:
  4369	                    log.debug("stale statusline unpin failed (ignored)", exc_info=True)
  4370	                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
  4371	        except Exception:
  4372	            # ⭐ The make-or-break swallow (RB1): NOTHING the statusline does may escape to the
  4373	            # turn. A build/gate/closure failure is logged at debug and dropped — the next state
  4374	            # change re-creates the line.
  4375	            log.debug("statusline update failed for chat (ignored)", exc_info=True)
  4376	
  4377	    async def _statusline_gated_edit(
  4378	        self, chat_id: int, state: _ChatState, message_id: int, *, edit: EditFn
  4379	    ) -> None:
  4380	        """Edit the pinned line through the gate, REBUILDING + RE-CHECKING foreground (B2).
  4381	
  4382	        Reserves the per-chat gate slot and awaits its wait (non-verbatim — RB5), THEN re-derives
  4383	        the statusline body + the project it was built for from CURRENT state. Two awaits precede
  4384	        the write — the gate wait AND the ctx ``await`` inside :meth:`_statusline_text` — both
  4385	        ``/switch`` windows. So immediately before the raw edit we do a FINAL **synchronous**
  4386	        foreground re-check (``_is_foreground(built_for)``) with NO await between it and the
  4387	        ``edit``: if a ``/switch`` happened during ANY await, ``built_for`` is no longer
  4388	        foreground → SKIP (the switch's own trigger writes the correct line — no stale write, no
  4389	        loop). An empty rebuild (foreground vanished — e.g. ``/rm``) or identical text also skips.
  4390	        A raise propagates to the caller's orphan-recovery (the message may be gone).
  4391	        """
  4392	        wait = self._gate(state).reserve(verbatim=False)
  4393	        if wait > 0:
  4394	            await self._sleep(wait)
  4395	        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
  4396	        if built is None:
  4397	            return  # foreground vanished mid-wait → no stale write.
  4398	        body, built_for = built
  4399	        if body == state.statusline_text:
  4400	            return  # nothing changed → no no-op "not modified" edit.
  4401	        # ⭐ FINAL sync guard (B2): only write if the project this body describes is STILL the
  4402	        # chat's foreground at THIS instant — no await between here and the edit, so a /switch
  4403	        # during any preceding await is caught. A stale body (built_for switched away) is dropped.
  4404	        if not self._is_foreground(chat_id, built_for):
  4405	            return
  4406	        await edit(message_id=message_id, text=body, parse_mode="HTML")
  4407	        state.statusline_text = body
  4408	
  4409	    async def _statusline_send_and_pin(
  4410	        self,
  4411	        chat_id: int,
  4412	        state: _ChatState,
  4413	        *,
  4414	        send: SendFn,
  4415	        pin: PinFn,
  4416	    ) -> None:
  4417	        """Send the statusline body (gated, non-verbatim) then PIN it silently (design §3.1).
  4418	
  4419	        The first-use + orphan-recovery primitive: reserve the gate slot, await its wait, THEN
  4420	        rebuild the body + the project it was built for from CURRENT state. Two awaits precede the
  4421	        send — the gate wait AND the ctx ``await`` inside :meth:`_statusline_text` — both
  4422	        ``/switch`` windows (B2). So immediately before the raw send we do a FINAL **synchronous**
  4423	        foreground re-check (``_is_foreground(built_for)``) with NO await between it and the
  4424	        ``send``: a ``/switch`` during any preceding await makes ``built_for`` no longer
  4425	        foreground → SKIP (the switch's own trigger sends the correct line — no stale send, no
  4426	        loop). A best-effort silent pin follows (``disable_notification=True`` — a pin must never
  4427	        re-ping). The id/text are stored ONLY when the send returns an id (so a ``None`` send does
  4428	        not leave a half-set state). A PIN failure is swallowed (RB1) AND records
  4429	        ``statusline_pinned=False`` so the next update retries the pin. Called from
  4430	        :meth:`_update_statusline` inside its best-effort guard, so a raising ``send`` propagates
  4431	        to that guard's swallow.
  4432	        """
  4433	        wait = self._gate(state).reserve(verbatim=False)
  4434	        if wait > 0:
  4435	            await self._sleep(wait)
  4436	        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
  4437	        if built is None:
  4438	            return  # foreground vanished mid-wait — nothing to send.
  4439	        body, built_for = built
  4440	        # ⭐ FINAL sync guard (B2): only send if the project this body describes is STILL the
  4441	        # chat's foreground at THIS instant — no await between here and the send, so a /switch
  4442	        # during any preceding await (the gate wait OR the ctx await) is caught and the stale
  4443	        # send is dropped (the switch's own statusline trigger sends the correct line).
  4444	        if not self._is_foreground(chat_id, built_for):
  4445	            return
  4446	        mid = await send(text=body, reply_markup=None, parse_mode="HTML")
  4447	        if mid is None:
  4448	            # The send produced no id (a closure that returns None) — don't store a half state;
  4449	            # the next update will try a fresh send.
  4450	            return
  4451	        state.statusline_message_id = mid
  4452	        state.statusline_text = body
  4453	        state.statusline_pinned = False  # not pinned until the pin call below succeeds.
  4454	        await self._statusline_pin(state, mid, pin=pin)
  4455	
  4456	    async def _statusline_pin(self, state: _ChatState, message_id: int, *, pin: PinFn) -> None:
  4457	        """Best-effort SILENT pin of the statusline message; record whether it stuck (pin-retry).
  4458	
  4459	        A pin must never re-ping (``disable_notification=True``) and never break the turn (RB1).
  4460	        On success ``statusline_pinned`` is set True; on failure it stays/!becomes False and is
  4461	        swallowed — :meth:`_update_statusline` then RETRIES the pin on the next update (even with
  4462	        unchanged text) so a transient pin failure self-heals instead of leaving the line unpinned
  4463	        forever. Only the pinned-bar placement is ever at stake here, never the turn.
  4464	        """
  4465	        try:
  4466	            await pin(message_id=message_id, disable_notification=True)
  4467	            state.statusline_pinned = True
  4468	        except Exception:
  4469	            state.statusline_pinned = False
  4470	            log.debug("statusline pin failed (will retry on next update)", exc_info=True)
  4471	
  4472	    # -- the callback resolve path (LOCK-FREE: SB1 enforced at the bot) ------
  4473	
  4474	    def resolve_callback(self, chat_id: int, data: object) -> "CallbackOutcome":
  4475	        """Route a decoded inline-keyboard tap to its OWNING project's pending request.

exec
/bin/zsh -lc "nl -ba claude_tg/bot.py | sed -n '1140,1182p'; nl -ba claude_tg/bot.py | sed -n '2865,2925p'; nl -ba claude_tg/bot.py | sed -n '2110,2135p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
  1140	            return
  1141	        assert self.streaming is not None
  1142	        chat_id = update.effective_chat.id
  1143	        await update.message.reply_text(self.streaming.unwatch(chat_id))
  1144	
  1145	    async def cmd_switch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
  1146	        """Switch the chat's active project (streaming mode only).
  1147	
  1148	        **No busy-guard (P5 / ADR-005 D2 — the headline relaxation).** P4 refused
  1149	        ``/switch`` while any turn was in flight *only because* the relay routed every
  1150	        inbound answer to ``_active_engine``, so flipping ``store.active`` mid-hold stranded
  1151	        the parked turn against the wrong engine (ADR-004 D2's deadlock). P5 routes every
  1152	        decision-in by its ``tool_use_id`` to the **owning** project (the pending index,
  1153	        ADR-005 D3), so switching away no longer strands anything — the prior run keeps
  1154	        running in the background and a tap on its prompt still resolves it. ``/switch`` is
  1155	        therefore **free while other projects (or this one) are mid-run** — that is the
  1156	        point of background concurrency.
  1157	
  1158	        Everything else is unchanged: no arg → usage; unknown name → error listing the
  1159	        available names (RB1); before activating, the TARGET project's stored cwd is
  1160	        re-validated against the permitted roots (SB2/B2) — an out-of-root (or missing) cwd
  1161	        is refused and the active project is left unchanged. On success the active project
  1162	        changes and the next message resumes it.
  1163	        """
  1164	        if not await self._ok(update) or update.message is None:
  1165	            return
  1166	        if not await self._require_streaming(update):
  1167	            return
  1168	        assert self.streaming is not None
  1169	        chat_id = update.effective_chat.id
  1170	        name = " ".join(ctx.args).strip() if ctx.args else ""
  1171	        if not name:
  1172	            await update.message.reply_text("Usage: /switch <name>")
  1173	            return
  1174	        reply, parse_mode = self._switch_active(chat_id, name)
  1175	        await update.message.reply_text(reply, parse_mode=parse_mode)
  1176	        # STATUSLINE T-SL-WIRE: rewrite the pinned line for the newly-active project (its
  1177	        # worktree + per-project model/effort/mode all change). Best-effort (RB1).
  1178	        await self._refresh_statusline(ctx.bot, chat_id)
  1179	
  1180	    def _switch_active(self, chat_id: int, name: str) -> tuple[str, str | None]:
  1181	        """Switch the chat's active project to ``name``; return ``(reply, parse_mode)`` (T6/P9).
  1182	
  2865	            except Exception:
  2866	                log.debug("quick-reply chip dismissal failed", exc_info=True)
  2867	
  2868	    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
  2869	        """Inline-keyboard tap handler — **the SB1 security boundary**.
  2870	
  2871	        A callback tap is new attack surface (SB1). PTB's ``CallbackQueryHandler`` cannot
  2872	        be chat-filtered the way ``MessageHandler`` is (it filters by callback_data
  2873	        pattern), so THIS explicit :meth:`_authorized` recheck is the authoritative
  2874	        allowlist gate: an unauthorized / forged callback NEVER routes a decision (it
  2875	        cannot approve a plan or answer a question) — if the chat is not allowlisted we
  2876	        silently answer the callback query and return WITHOUT touching the engine. For an
  2877	        authorized chat the
  2878	        decode + routing lives in :meth:`StreamingSession.resolve_callback`, which ignores
  2879	        any ``callback_data`` that fails to decode (foreign/stale/malformed → None) and
  2880	        resolves nothing in that case (RB1). The callback query is ALWAYS answered (so the
  2881	        client's spinner stops), even when ignored.
  2882	
  2883	        **Free-text prompt (P5 / ADR-005 D5).** When the tap arms free-text capture (an
  2884	        "Other"/"Reject" → ``outcome.expects_text``), the bot replies a **name-echoed**
  2885	        prompt (``✏️ <name>: reply with your answer…`` — ``render.free_text_prompt``) so the
  2886	        operator can tell WHICH project the next plain message resolves (several may be
  2887	        awaiting at once). It then maps that prompt's ``message_id -> tool_use_id``
  2888	        (``register_reply_prompt``) so a **reply-to** that prompt routes the answer by id
  2889	        (the reply-to escape hatch overriding the most-recent default).
  2890	        """
  2891	        query = update.callback_query
  2892	        if query is None:
  2893	            return
  2894	        # SB1: explicit allowlist recheck inside the handler (the filter is the first
  2895	        # gate; this is defense in depth). An unauthorized tap is answered + dropped —
  2896	        # never resolved.
  2897	        if not self._authorized(update) or self.streaming is None:
  2898	            await self._answer_callback(query)
  2899	            return
  2900	        chat = update.effective_chat
  2901	        try:
  2902	            outcome = self.streaming.resolve_callback(chat.id, query.data)
  2903	        except Exception:  # RB1: a bad/garbage callback must never crash the handler
  2904	            log.exception("error routing callback for chat %s", chat.id if chat else "?")
  2905	            await self._answer_callback(query)
  2906	            return
  2907	        # T6/P9: a [Open <project>] switch tap routes by project name. The session decoded +
  2908	        # validated the name and returned it on ``switch_to``; the bot performs the actual
  2909	        # switch through the SHARED /switch helper (the SB2 path re-validation the session
  2910	        # can't do). SB1 is already enforced above (the _authorized recheck), so a
  2911	        # non-allowlisted tap never reaches here — it is answered + dropped, switching
  2912	        # nothing. We answer the query (stop the spinner), perform the switch, and reply the
  2913	        # result; nothing else (no free-text arm) applies to a switch.
  2914	        if outcome.switch_to:
  2915	            await self._answer_callback(query, outcome.note if outcome.handled else None)
  2916	            reply, parse_mode = self._switch_active(chat.id, outcome.switch_to)
  2917	            try:
  2918	                await query.message.reply_text(reply, parse_mode=parse_mode)
  2919	            except Exception:
  2920	                log.debug("switch-button reply send failed", exc_info=True)
  2921	            # STATUSLINE T-SL-WIRE: the [Open <project>] tap shares /switch's core, so refresh
  2922	            # the pinned line for the newly-active project here too (parallel to cmd_switch).
  2923	            await self._refresh_statusline(ctx.bot, chat.id)
  2924	            return
  2925	        # P11 T2: an [Attach] tap routes by session id. The session decoded + validated the id
  2110	        RB1 — a pin/edit failure can never break the command). A no-op in one-shot mode (no
  2111	        streaming session) and wrapped so a build/dispatch error never escapes the command.
  2112	        """
  2113	        if self.streaming is None:
  2114	            return
  2115	        try:
  2116	            send, edit, _delete, pin, unpin = self._make_chat_io(bot, chat_id)
  2117	            await self.streaming._maybe_update_statusline(
  2118	                chat_id, send=send, edit=edit, pin=pin, unpin=unpin, for_project=None,
  2119	            )
  2120	        except Exception:
  2121	            # RB1: a statusline refresh must never break the command that triggered it.
  2122	            log.debug("command statusline refresh failed for chat %s (ignored)", chat_id, exc_info=True)
  2123	
  2124	    # ---- messages -----------------------------------------------------------
  2125	    @staticmethod
  2126	    def _reply_to_id(update: Update) -> int | None:
  2127	        """The message_id this message is a reply-to, or ``None`` (D5 free-text routing).
  2128	
  2129	        A plain message that is a Telegram reply-to carries the replied-to message under
  2130	        ``update.message.reply_to_message``; its ``message_id`` lets the streaming session
  2131	        route a free-text answer to the project that owns the replied-to prompt (the D5
  2132	        reply-to escape hatch). ``None`` when the message is not a reply (the common case).
  2133	        Defensive (RB1): any missing attribute → ``None``.
  2134	        """
  2135	        msg = getattr(update, "message", None)

codex
There is one subtlety: after the final check, the `await send/edit` itself can still race with a future `/switch`, but that is unavoidable once the Telegram call is issued; the claimed invariant is specifically “no await between check and issuing the call,” and that holds. I’m checking whether any non-statusline fallback path writes without that guard.
exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '1005,1028p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
  1005	        ``/projects``/``/switch``.
  1006	        """
  1007	        if name is None:
  1008	            return True
  1009	        if self.store is None:
  1010	            return True  # single implicit project — always the foreground.
  1011	        active = self.store.get_active(chat_id)
  1012	        if active is None:
  1013	            return True  # nothing active yet → treat the turn's project as foreground.
  1014	        return isinstance(active, str) and active.casefold() == name.casefold()
  1015	
  1016	    # -- queued counter for notifications + /status (T6/P9) -----------------
  1017	
  1018	    def _queued_waiting(self, state: _ChatState) -> int:
  1019	        """The number of turns parked behind the cap in THIS chat's run queue (T6/P9).
  1020	
  1021	        Pulled straight from the per-chat FIFO :attr:`~_ChatState.run_queue` (D6): the count
  1022	        of still-pending waiters (a drained/transferred entry has a done future, so it is
  1023	        excluded). The notification builders append a ``" (N more waiting)"`` counter when
  1024	        this is ≥1 so the operator knows work is backed up; 0 → no suffix. Read-only / pure
  1025	        (never mutates the queue, never raises) so it is safe to call on any send path. The
  1026	        counter is per-chat (the queue is per-chat — D6); the global RUNNING count is
  1027	        :meth:`active_run_count`.
  1028	        """

exec
/bin/zsh -lc "rg -n \"_maybe_update_statusline\\(|_update_statusline\\(|_statusline_send_and_pin\\(|_statusline_gated_edit\\(|_statusline_text\\(\" claude_tg tests -g '*.py'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
claude_tg/stream_session.py:3632:        await self._maybe_update_statusline(
claude_tg/stream_session.py:3849:            await self._maybe_update_statusline(
claude_tg/stream_session.py:4159:    async def _maybe_update_statusline(
claude_tg/stream_session.py:4196:            await self._update_statusline(
claude_tg/stream_session.py:4204:    async def _statusline_text(self, chat_id: int) -> Optional[tuple[str, str]]:
claude_tg/stream_session.py:4277:    async def _update_statusline(
claude_tg/stream_session.py:4329:            built = await self._statusline_text(chat_id)  # async (B1: awaits the SDK ctx %)
claude_tg/stream_session.py:4348:                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
claude_tg/stream_session.py:4353:                await self._statusline_gated_edit(
claude_tg/stream_session.py:4370:                await self._statusline_send_and_pin(chat_id, state, send=send, pin=pin)
claude_tg/stream_session.py:4377:    async def _statusline_gated_edit(
claude_tg/stream_session.py:4395:        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
claude_tg/stream_session.py:4409:    async def _statusline_send_and_pin(
claude_tg/stream_session.py:4436:        built = await self._statusline_text(chat_id)  # rebuilt AFTER the wait (B2)
tests/test_stream_session.py:7377:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7406:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7411:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7432:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7434:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7449:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7453:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7473:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7488:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7504:        await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7523:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7536:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7547:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7556:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7571:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7587:    await session._update_statusline(1, send=good.send, edit=good.edit, pin=good.pin, unpin=good.unpin)
tests/test_stream_session.py:7602:    await session._update_statusline(1, send=boom_send, edit=boom_edit, pin=good.pin, unpin=boom_unpin)
tests/test_stream_session.py:7794:    await session._maybe_update_statusline(
tests/test_stream_session.py:7800:    await session._maybe_update_statusline(
tests/test_stream_session.py:7818:    await session._maybe_update_statusline(
tests/test_stream_session.py:7824:    await session._maybe_update_statusline(
tests/test_stream_session.py:7844:    await session._maybe_update_statusline(
tests/test_stream_session.py:7850:    await session._maybe_update_statusline(
tests/test_stream_session.py:7868:    await session._maybe_update_statusline(
tests/test_stream_session.py:7873:    await session._maybe_update_statusline(
tests/test_stream_session.py:7888:    await session._maybe_update_statusline(
tests/test_stream_session.py:7901:    await session._maybe_update_statusline(
tests/test_stream_session.py:7921:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:7932:    built = await session._statusline_text(1)
tests/test_stream_session.py:7945:    assert await session._statusline_text(1) is None
tests/test_stream_session.py:7993:    await session._update_statusline(
tests/test_stream_session.py:8013:    await session._update_statusline(
tests/test_stream_session.py:8030:    await session._update_statusline(
tests/test_stream_session.py:8096:    await session._update_statusline(
tests/test_stream_session.py:8134:    await session._update_statusline(
tests/test_stream_session.py:8143:    await session._update_statusline(
tests/test_stream_session.py:8228:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8235:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8241:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
tests/test_stream_session.py:8252:    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
claude_tg/bot.py:2117:            await self.streaming._maybe_update_statusline(
tests/test_bot_streaming.py:160:    async def _maybe_update_statusline(self, chat_id, *, send, edit, pin, unpin, for_project=None):

exec
/bin/zsh -lc "nl -ba claude_tg/stream_session.py | sed -n '3595,3865p'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
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
  3861	        # QF3 (B3/RB3): finalize the resume verification AFTER the stream has fully drained
  3862	        # (so we never re-enter the render loop mid-turn). Either recover from a detected
  3863	        # resume failure, or confirm the resume good by clearing the flag.
  3864	        recovered = False
  3865	        if check_resume:

codex
The only production call sites are turn start/end and command refresh, all through `_maybe_update_statusline`/`_update_statusline`. I’m running the focused statusline regression tests now so the final answer can distinguish code inspection from test confirmation.
exec
/bin/zsh -lc "pytest -q tests/test_stream_session.py -k 'statusline or switch_during_ctx or plan_turn_shows_plan_mode or non_plan_turn_does_not_show_plan_mode or pin_fails_then_retried or successful_pin_sets_pinned_flag'" in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 368ms:
.............................                                            [100%]

codex
## B2 status
- CLOSED — `_statusline_text` returns `(body, name)` after awaiting ctx, and both helpers rebuild then do the final sync `_is_foreground(chat_id, built_for)` check with no await before issuing `edit`/`send` (`claude_tg/stream_session.py:4264`, `claude_tg/stream_session.py:4275`, `claude_tg/stream_session.py:4395`, `claude_tg/stream_session.py:4404`, `claude_tg/stream_session.py:4406`, `claude_tg/stream_session.py:4436`, `claude_tg/stream_session.py:4444`, `claude_tg/stream_session.py:4446`).
## Regressions
- none
## Verdict
SHIP
## Reasoning
A `/switch` during the gate wait or ctx await now makes `built_for` non-foreground, so the stale write returns before any edit/send; `/switch` and switch-button paths trigger their own foreground refresh afterward (`claude_tg/bot.py:1174`, `claude_tg/bot.py:1178`, `claude_tg/bot.py:2916`, `claude_tg/bot.py:2923`). B1, B3, pin-retry, RB1, foreground-only concurrency, and SB3 still hold in code and the focused statusline regression tests pass (`29 passed`).
tokens used
71,037
## B2 status
- CLOSED — `_statusline_text` returns `(body, name)` after awaiting ctx, and both helpers rebuild then do the final sync `_is_foreground(chat_id, built_for)` check with no await before issuing `edit`/`send` (`claude_tg/stream_session.py:4264`, `claude_tg/stream_session.py:4275`, `claude_tg/stream_session.py:4395`, `claude_tg/stream_session.py:4404`, `claude_tg/stream_session.py:4406`, `claude_tg/stream_session.py:4436`, `claude_tg/stream_session.py:4444`, `claude_tg/stream_session.py:4446`).
## Regressions
- none
## Verdict
SHIP
## Reasoning
A `/switch` during the gate wait or ctx await now makes `built_for` non-foreground, so the stale write returns before any edit/send; `/switch` and switch-button paths trigger their own foreground refresh afterward (`claude_tg/bot.py:1174`, `claude_tg/bot.py:1178`, `claude_tg/bot.py:2916`, `claude_tg/bot.py:2923`). B1, B3, pin-retry, RB1, foreground-only concurrency, and SB3 still hold in code and the focused statusline regression tests pass (`29 passed`).
