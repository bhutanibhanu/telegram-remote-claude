"""T13 — Substrate B (raw ``claude`` CLI ``stream-json``) session-driver harness.

This stands up SUBSTRATE B for the session-substrate feasibility spike: it drives
the host ``claude`` CLI *directly* over the bidirectional ``stream-json`` protocol
(subprocess + manual NDJSON on stdin/stdout), with NO claude-agent-sdk dependency.
It is the foundation the T14 (C2 permission), T15 (C3 AskUserQuestion) and T16 (C4
ExitPlanMode) over-the-wire checks are built on.

It is deliberately written by hand against the on-the-wire protocol. The SDK's own
transport (``claude_agent_sdk/_internal/transport/subprocess_cli.py`` +
``_internal/query.py``) was read as the authoritative reference for the exact wire
shapes and then MIRRORED here with the standard library only -- the SDK is NOT
imported by this driver.

==============================================================================
THE WIRE PROTOCOL (verified against the SDK source + live against CLI 2.1.185)
==============================================================================
Spawn (mirrors ``_build_command`` + ``connect``):

    claude --output-format stream-json --verbose -p \
           --input-format stream-json \
           [--permission-mode default] \
           [--permission-prompt-tool stdio]   # <-- THE permission switch (see below) \
           [--allowedTools a,b] [--disallowedTools c,d]

  * ``--verbose`` is mandatory with ``--output-format stream-json``.
  * ``-p/--print`` is required for stream-json I/O.
  * ``--permission-prompt-tool stdio`` is THE switch that activates the programmatic
    permission mechanism: it tells the CLI to route ``can_use_tool`` permission
    requests BACK to the driver over the stream-json control plane (instead of the
    CLI auto-deciding from --allowedTools / --permission-mode). This flag is
    UNDOCUMENTED (absent from ``--help``) but present and functional; the SDK sets
    it to ``stdio`` automatically whenever a ``can_use_tool`` callback is provided
    (see ``_internal/client.py`` ``permission_prompt_tool_name="stdio"``). WITHOUT
    it the CLI never sends ``can_use_tool`` and silently allows/denies tools itself
    per --allowedTools (verified live in the T13 discovery run). The harness sets it
    automatically whenever ``can_use_tool`` is provided.
  * stdin=PIPE, stdout=PIPE, cwd=<temp fixture>. We pipe stderr too (drained on a
    thread) so a noisy CLI can't deadlock on a full stderr buffer.
  * ENV: we inherit os.environ but DROP ``CLAUDECODE`` (so the spawned CLI does not
    think it is nested inside a Claude Code parent -- the SDK does the same, see
    upstream issue #573) and set ``CLAUDE_CODE_ENTRYPOINT=sdk-py``. No API key is
    read or set; the CLI uses the host's existing login. Run with
    ANTHROPIC_API_KEY unset to guarantee the no-API-key constraint.

stdout is NDJSON: one JSON object per line. Observed top-level ``type`` values:
  * ``system``  (subtype ``init`` carries session_id, tools, mcp_servers, the
                 permissionMode, slash_commands, model, cwd, ...)
  * ``assistant`` / ``user``  (message frames; ``message`` is an Anthropic-style
                 message dict with a ``content`` block list)
  * ``result``  (terminal per-turn frame: subtype success/error_*, session_id,
                 is_error, num_turns, duration, usage, total_cost_usd, result text)
  * ``stream_event``  (only with --include-partial-messages)
  * ``control_request`` / ``control_response``  (the control plane, see below)

The CONTROL PLANE (the actual permission mechanism on this CLI version):
  * HANDSHAKE -- the DRIVER sends an ``initialize`` control_request on stdin and
    waits for the matching ``control_response`` (subtype ``success``):
        {"type":"control_request","request_id":"req_1_ab12",
         "request":{"subtype":"initialize","hooks":null}}
    -> {"type":"control_response",
        "response":{"subtype":"success","request_id":"req_1_ab12",
                    "response":{...commands/output_style/models/account/pid...}}}
    ``initialize`` is part of the control handshake the SDK always performs. T13
    finding (verified live): what actually causes the CLI to route ``can_use_tool``
    back to the driver is the ``--permission-prompt-tool stdio`` SPAWN FLAG, not the
    initialize frame -- with the flag absent, a Write was auto-decided by the CLI and
    NO can_use_tool arrived even after a successful initialize; with the flag set to
    ``stdio`` the can_use_tool round-trip works. We send initialize (matching the SDK)
    AND set the flag whenever a permission callback is provided.
  * USER MESSAGE (driver -> CLI on stdin):
        {"type":"user",
         "message":{"role":"user","content":"<prompt>"},
         "parent_tool_use_id":null,"session_id":"default"}
  * PERMISSION REQUEST (CLI -> driver):
        {"type":"control_request","request_id":"R",
         "request":{"subtype":"can_use_tool","tool_name":"Write",
                    "input":{...},"permission_suggestions":[...],...}}
  * PERMISSION RESPONSE (driver -> CLI):  ALLOW:
        {"type":"control_response",
         "response":{"subtype":"success","request_id":"R",
                     "response":{"behavior":"allow","updatedInput":<input>}}}
    DENY:
        {"type":"control_response",
         "response":{"subtype":"success","request_id":"R",
                     "response":{"behavior":"deny","message":"<reason>"}}}
    (Note the wire DENY still rides a ``subtype:success`` control_response -- the
    "success" refers to the control round-trip, not to allowing the tool.)

==============================================================================
SEAMS for T14/T15/T16
==============================================================================
    h = CLISessionHarness(cwd=fixture, permission_mode="default",
                          allowed_tools=[...], disallowed_tools=[...],
                          can_use_tool=callback)
    h.start()                         # spawn + reader thread
    h.initialize()                    # control handshake (routes can_use_tool back)
    for ev in h.send("do a thing"):   # one turn; yields parsed NDJSON dicts
        ...                           # h.session_id populated from system/init
    h.stop()                          # close stdin, terminate, join threads

  * ``can_use_tool``: Callable[[tool_name:str, input:dict, meta:dict], decision].
    Return ``{"behavior":"allow"[, "updatedInput":{...}]}`` or
    ``{"behavior":"deny","message":"..."}``; ``allow_tool()`` / ``deny_tool()``
    build these. Invoked by the reader thread on each ``can_use_tool`` request and
    answered via a control_response. If no callback is set, every request is
    DENIED (fail-safe) so an un-handled risky tool never silently runs.
  * Event stream out: ``send()`` yields every parsed NDJSON dict for the turn,
    terminating at that turn's ``result`` frame.
  * ``session_id``: captured from ``system``/``init`` (and refreshed from
    ``result``); ``resume`` is exposed as a constructor arg seam (proven later).
  * BOUNDED everywhere: ``send()`` has a wall-clock budget AND a per-line idle
    timeout, so the harness FAILS CLEAN (raises) instead of hanging (RB2).
  * ``stop()`` closes stdin, terminates (then kills) the subprocess, and joins the
    reader/stderr threads -- no leaked process, idempotent.

Throwaway-quality but readable (the spike's explicit policy). Standard library
only (subprocess, json, threading, queue). It uses blocking subprocess pipes +
two daemon reader threads rather than asyncio so it composes trivially with the
synchronous check scripts T14-T16 will be.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

# The T3 recorder (which imports the T2 scrubber) lives beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence_recorder import record_criterion  # noqa: E402

# A decision is the dict written into the control_response ``response`` field.
Decision = Dict[str, Any]
# can_use_tool(tool_name, input, meta) -> Decision
PermissionCallback = Callable[[str, Dict[str, Any], Dict[str, Any]], Decision]


def allow_tool(updated_input: Optional[Dict[str, Any]] = None) -> Decision:
    """Build an ALLOW decision; optionally override the tool input.

    If ``updated_input`` is omitted the harness fills ``updatedInput`` with the
    ORIGINAL tool input before sending (the CLI control schema requires the field
    to be a record on an allow; omitting it raises a ZodError and fails the tool).
    """
    d: Decision = {"behavior": "allow"}
    if updated_input is not None:
        d["updatedInput"] = updated_input
    return d


def deny_tool(message: str = "denied by harness") -> Decision:
    """Build a DENY decision carrying a reason message."""
    return {"behavior": "deny", "message": message}


class CLIDriverError(RuntimeError):
    """Raised on a fail-clean condition (timeout, dead process, bad state)."""


class CLISessionHarness:
    """Drive one ``claude`` CLI session over the bidirectional stream-json wire.

    Not thread-safe for the *caller*: drive it from a single thread. Internally it
    runs two daemon threads (stdout reader, stderr drainer); the reader answers
    ``can_use_tool`` control_requests inline so permission decisions are honored
    even while the caller is iterating a turn.
    """

    def __init__(
        self,
        *,
        cwd: Optional[str | os.PathLike] = None,
        permission_mode: str = "default",
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        can_use_tool: Optional[PermissionCallback] = None,
        include_partial_messages: bool = False,
        resume: Optional[str] = None,
        cli_path: str = "claude",
        permission_prompt_tool: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        self._cwd = str(cwd) if cwd is not None else None
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools
        self._can_use_tool = can_use_tool
        # The undocumented switch that routes can_use_tool back to us. The SDK sets
        # it to "stdio" whenever a callback exists; we mirror that default. An
        # explicit value (incl. None) overrides the auto-default.
        if permission_prompt_tool is not None:
            self._permission_prompt_tool: Optional[str] = permission_prompt_tool
        elif can_use_tool is not None:
            self._permission_prompt_tool = "stdio"
        else:
            self._permission_prompt_tool = None
        self._include_partial = include_partial_messages
        self._resume = resume
        self._cli_path = cli_path
        self._extra_args = list(extra_args) if extra_args else []

        self._proc: Optional[subprocess.Popen[str]] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stdin_lock = threading.Lock()

        # Parsed non-control NDJSON dicts the reader hands to the caller's turn.
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        # request_id -> control_response ``response`` dict (e.g. initialize reply).
        self._control_responses: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stderr_lines: List[str] = []

        self._request_counter = 0
        self._initialized = False
        self._closed = False

        #: Claude session id, captured from system/init (and result frames).
        self.session_id: Optional[str] = None
        #: The init system frame (tools / mcp_servers / permissionMode / model...).
        self.init_frame: Optional[Dict[str, Any]] = None
        #: Every can_use_tool request the CLI sent us (shape evidence for T13).
        self.permission_requests: List[Dict[str, Any]] = []

    # -- command building (mirrors the SDK's _build_command, by hand) ----------

    def _build_command(self) -> List[str]:
        cmd = [self._cli_path, "--output-format", "stream-json", "--verbose", "-p"]
        cmd += ["--input-format", "stream-json"]
        if self._permission_mode:
            cmd += ["--permission-mode", self._permission_mode]
        if self._permission_prompt_tool:
            # THE switch: routes can_use_tool control_requests back to the driver.
            cmd += ["--permission-prompt-tool", self._permission_prompt_tool]
        if self._allowed_tools:
            cmd += ["--allowedTools", ",".join(self._allowed_tools)]
        if self._disallowed_tools:
            cmd += ["--disallowedTools", ",".join(self._disallowed_tools)]
        if self._include_partial:
            cmd += ["--include-partial-messages"]
        if self._resume:
            cmd += ["--resume", self._resume]
        cmd += self._extra_args
        return cmd

    def _build_env(self) -> Dict[str, str]:
        # Inherit env but DROP CLAUDECODE (so the child doesn't think it is nested
        # in a Claude Code parent -- mirrors the SDK). Set the SDK entrypoint tag.
        # No API key is injected; the CLI uses the host's existing login.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
        if self._cwd:
            env["PWD"] = self._cwd
        return env

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> "CLISessionHarness":
        """Spawn the CLI subprocess and start the reader/stderr threads."""
        if self._proc is not None:
            raise CLIDriverError("already started; call stop() first")
        cmd = self._build_command()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            env=self._build_env(),
            text=True,
            bufsize=1,  # line-buffered
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        return self

    def initialize(self, timeout: float = 60.0) -> Dict[str, Any]:
        """Perform the ``initialize`` control handshake.

        Sending this is what makes the CLI route ``can_use_tool`` permission
        requests back to us over the control plane (the T13 finding). Returns the
        CLI's initialize control_response payload. Raises (fail-clean) on timeout.
        """
        if self._initialized:
            return {}
        req = {"subtype": "initialize", "hooks": None}
        resp = self._send_control_request(req, timeout=timeout)
        self._initialized = True
        return resp

    def send(
        self,
        prompt: str,
        *,
        turn_timeout: float = 180.0,
        idle_timeout: float = 120.0,
    ) -> Iterator[Dict[str, Any]]:
        """Send one operator turn; yield the parsed NDJSON dicts streamed back.

        Iteration terminates at this turn's ``result`` frame. Bounded two ways so
        the harness FAILS CLEAN rather than hanging (RB2):
          * ``turn_timeout`` -- total wall-clock budget for the whole turn;
          * ``idle_timeout`` -- max gap between two consecutive stdout lines.
        Either bound exceeded -> CLIDriverError. ``can_use_tool`` requests are
        answered by the reader thread out-of-band, so they do not consume the
        per-line idle budget on the caller side.
        """
        if self._proc is None:
            raise CLIDriverError("not started; call start() first")
        if not self._initialized:
            # initialize is required for the permission round-trip; do it lazily.
            self.initialize()

        user_msg = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
            "session_id": "default",
        }
        self._write_line(json.dumps(user_msg))

        deadline = time.monotonic() + turn_timeout
        while True:
            self._assert_alive()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CLIDriverError(
                    f"turn exceeded turn_timeout={turn_timeout}s (fail-clean)"
                )
            try:
                ev = self._events.get(timeout=min(idle_timeout, remaining))
            except queue.Empty:
                raise CLIDriverError(
                    f"no stdout line for idle_timeout={idle_timeout}s "
                    f"(turn budget {remaining:.0f}s left); fail-clean"
                )
            if ev.get("__reader_error__"):
                raise CLIDriverError(f"reader thread failed: {ev.get('detail')}")
            self._capture_session_id(ev)
            yield ev
            if ev.get("type") == "result":
                return

    def stop(self) -> None:
        """Close stdin, terminate (then kill) the CLI, join threads. Idempotent."""
        if self._proc is None:
            return
        self._closed = True
        proc, self._proc = self._proc, None
        try:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        finally:
            for t in (self._reader, self._stderr_thread):
                if t is not None:
                    t.join(timeout=5)
            self._reader = None
            self._stderr_thread = None

    # -- stdin writers ---------------------------------------------------------

    def _write_line(self, line: str) -> None:
        self._assert_alive()
        with self._stdin_lock:
            assert self._proc is not None and self._proc.stdin is not None
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise CLIDriverError(f"stdin write failed: {exc}") from exc

    def _send_control_request(
        self, request: Dict[str, Any], timeout: float
    ) -> Dict[str, Any]:
        """Send a control_request and block for its matching control_response."""
        self._request_counter += 1
        request_id = f"req_{self._request_counter}_{uuid.uuid4().hex[:8]}"
        frame = {"type": "control_request", "request_id": request_id, "request": request}
        self._write_line(json.dumps(frame))

        deadline = time.monotonic() + timeout
        while True:
            self._assert_alive()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CLIDriverError(
                    f"control_request {request.get('subtype')!r} timed out "
                    f"after {timeout}s (fail-clean)"
                )
            try:
                resp = self._control_responses.get(timeout=min(5.0, remaining))
            except queue.Empty:
                continue
            if resp.get("request_id") != request_id:
                # Not ours (shouldn't normally interleave); keep waiting.
                continue
            if resp.get("subtype") == "error":
                raise CLIDriverError(
                    f"control_request {request.get('subtype')!r} errored: "
                    f"{resp.get('error')}"
                )
            return resp.get("response", {}) or {}

    def _send_control_response(self, request_id: str, response: Dict[str, Any]) -> None:
        """Reply to a CLI-initiated control_request (e.g. can_use_tool)."""
        frame = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        }
        self._write_line(json.dumps(frame))

    # -- reader thread ---------------------------------------------------------

    def _read_loop(self) -> None:
        """Parse NDJSON stdout lines and route them.

        control_response  -> matched by _send_control_request via a queue
        control_request   -> can_use_tool answered inline here; others acked-ish
        everything else   -> handed to the caller's turn via self._events
        """
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for raw in self._proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON noise on stdout (shouldn't happen on stream-json);
                    # surface it as an event so the caller can see/record it.
                    self._events.put({"type": "__nonjson__", "raw": line})
                    continue
                self._route(msg)
        except Exception as exc:  # noqa: BLE001 -- surface to caller, fail-clean
            self._events.put({"__reader_error__": True, "detail": f"{type(exc).__name__}: {exc}"})
        finally:
            # EOF on stdout: signal end so a blocked send()/initialize() fails clean
            # instead of hanging forever.
            self._events.put({"__reader_error__": True, "detail": "stdout EOF"})
            self._control_responses.put(
                {"request_id": None, "subtype": "error", "error": "stdout EOF"}
            )

    def _route(self, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "control_response":
            self._control_responses.put(msg.get("response", {}) or {})
            return
        if mtype == "control_request":
            self._handle_control_request(msg)
            return
        # system/init: capture the init frame eagerly (even before send() runs).
        if mtype == "system" and msg.get("subtype") == "init":
            self.init_frame = msg
            self._capture_session_id(msg)
        # Everything else is a turn event for the caller.
        self._events.put(msg)

    def _handle_control_request(self, msg: Dict[str, Any]) -> None:
        request_id = msg.get("request_id")
        req = msg.get("request", {}) or {}
        subtype = req.get("subtype")
        if subtype == "can_use_tool":
            self.permission_requests.append(msg)
            tool_name = req.get("tool_name", "")
            tool_input = req.get("input", {}) or {}
            meta = {k: v for k, v in req.items() if k not in ("subtype", "tool_name", "input")}
            try:
                if self._can_use_tool is None:
                    # Fail-safe: with no callback, DENY (never silently run a tool).
                    decision = deny_tool("no permission callback set on harness")
                else:
                    decision = self._can_use_tool(tool_name, tool_input, meta)
            except Exception as exc:  # noqa: BLE001 -- never crash the reader
                decision = deny_tool(f"permission callback raised: {type(exc).__name__}")
            # The CLI's control schema REQUIRES `updatedInput` (a record) on an
            # allow -- omitting it yields a ZodError and a failed tool. Mirror the
            # SDK: default updatedInput to the ORIGINAL tool input when the
            # decision didn't override it. (Verified live: missing updatedInput ->
            # "Tool permission request failed: ZodError ... expected record".)
            if isinstance(decision, dict) and decision.get("behavior") == "allow":
                if decision.get("updatedInput") is None:
                    decision = {**decision, "updatedInput": tool_input}
            if request_id is not None:
                self._send_control_response(request_id, decision)
            return
        # Any other CLI-initiated control_request (hook_callback, mcp_message...)
        # is not handled by this thin driver: reply with an error so the CLI does
        # not block waiting on us. (T13 records which subtypes actually appear.)
        if request_id is not None:
            err = {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": f"unhandled control_request subtype: {subtype}",
                },
            }
            self._write_line(json.dumps(err))

    def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            for raw in self._proc.stderr:
                s = raw.rstrip()
                if s:
                    self._stderr_lines.append(s)
        except Exception:
            pass

    # -- helpers ---------------------------------------------------------------

    def _capture_session_id(self, msg: Dict[str, Any]) -> None:
        sid: Optional[str] = None
        if msg.get("type") == "system":
            sid = msg.get("session_id")
        elif msg.get("type") == "result":
            sid = msg.get("session_id")
        if sid and not self.session_id:
            self.session_id = sid

    def _assert_alive(self) -> None:
        if self._proc is None:
            raise CLIDriverError("subprocess not running (stopped or never started)")
        rc = self._proc.poll()
        if rc is not None:
            tail = " | ".join(self._stderr_lines[-5:])
            raise CLIDriverError(
                f"CLI subprocess exited (returncode={rc}); stderr tail: {tail}"
            )

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)


# ===========================================================================
# T13 LIVE DISCOVERY + EVIDENCE
# ===========================================================================
# Runs a contained, light demonstration in a disposable temp fixture:
#   1. spawn the CLI over stream-json + initialize handshake;
#   2. observe the system/init frame shape + recorded event types;
#   3. send a prompt that induces a Write tool use, OBSERVE the can_use_tool
#      control_request arriving over the wire, ALLOW it, and confirm the CLI
#      honored it (the sentinel file is created);
#   4. record the ACTUAL on-the-wire permission mechanism on this CLI version.
# This is the LIGHT mechanism demo; the rigorous allow/deny + containment proof
# is T14. Containment here: a disposable temp cwd, allow only the in-fixture
# Write, deny everything else; repo status is untouched.
# ---------------------------------------------------------------------------

import shutil  # noqa: E402
import tempfile  # noqa: E402


def _summarize_block(block: Dict[str, Any]) -> str:
    """One-line shape summary of an assistant/user content block."""
    btype = block.get("type")
    if btype == "text":
        return f"text(len={len(block.get('text',''))})"
    if btype == "tool_use":
        return f"tool_use(name={block.get('name')!r}, input_keys={sorted((block.get('input') or {}).keys())})"
    if btype == "tool_result":
        return f"tool_result(is_error={block.get('is_error')})"
    return f"{btype}"


def _selfcheck() -> int:
    report: List[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T13 DISCOVERY — substrate B: raw `claude` CLI stream-json driver ===")
    # CLI version (contained: run in /tmp via cwd of the subprocess call).
    try:
        ver = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=20, cwd="/tmp"
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        ver = f"<version probe failed: {exc}>"
    log(f"claude --version: {ver}")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} "
        f"(must be False — no API key)")

    # Does --permission-prompt-tool exist in --help on this version? (ADR note was
    # about v2.1.183; record the ACTUAL state on the installed version.)
    try:
        helptext = subprocess.run(
            ["claude", "--help"], capture_output=True, text=True, timeout=20, cwd="/tmp"
        ).stdout
    except Exception as exc:  # noqa: BLE001
        helptext = ""
        log(f"(--help probe failed: {exc})")
    ppt_present = "--permission-prompt-tool" in helptext
    log(f"`--permission-prompt-tool` present in --help: {ppt_present} "
        f"(ABSENT from --help, but the flag IS accepted and functional — it is the "
        f"undocumented switch whose value `stdio` routes can_use_tool over the wire)")

    workdir = tempfile.mkdtemp(prefix="t13_cli_")
    sentinel = Path(workdir) / "t13_probe.txt"
    log(f"cwd (temp, isolated from repo): {workdir}")

    spawn_cmd_preview: List[str] = []
    decisions: List[str] = []
    event_types: Dict[str, int] = {}
    init_keys: List[str] = []
    init_perm_mode: Optional[str] = None
    canusetool_seen = False
    canusetool_req_shape: Dict[str, Any] = {}
    tool_use_seen = False
    result_subtype: Optional[str] = None
    sid: Optional[str] = None
    deny_honored = False

    def cb(tool_name: str, tool_input: Dict[str, Any], meta: Dict[str, Any]) -> Decision:
        # CONTAINMENT (X1, light): allow ONLY an in-fixture Write of the sentinel;
        # deny everything else. The rigorous allow/deny proof is T14.
        nonlocal canusetool_seen, canusetool_req_shape
        canusetool_seen = True
        canusetool_req_shape = {
            "tool_name": tool_name,
            "input_keys": sorted(tool_input.keys()),
            "meta_keys": sorted(meta.keys()),
        }
        target = str(tool_input.get("file_path", ""))
        try:
            # Relative paths are resolved against the CLI's cwd (the fixture), NOT
            # the driver's cwd. This is the X1 resolved-target containment check.
            p = Path(target)
            resolved = p if p.is_absolute() else (Path(workdir) / p)
            resolved = resolved.resolve()
            wd = Path(workdir).resolve()
            in_fixture = wd in resolved.parents or resolved == wd
        except Exception:
            resolved = Path(target)
            in_fixture = False
        if tool_name == "Write" and in_fixture and resolved == sentinel.resolve():
            decisions.append(f"ALLOW {tool_name} -> {resolved}")
            return allow_tool()
        decisions.append(f"DENY {tool_name} -> {target!r} (outside fixture / not sentinel)")
        return deny_tool("T13 light demo: only the in-fixture sentinel Write is allowed")

    harness = CLISessionHarness(
        cwd=workdir,
        permission_mode="default",
        # Auto-approve nothing risky; route Write through the callback. Read/LS are
        # harmless and auto-allowed so the model can orient without extra prompts.
        allowed_tools=["Read", "LS", "Glob", "Grep"],
        disallowed_tools=["Bash", "Edit", "NotebookEdit", "WebFetch", "WebSearch"],
        can_use_tool=cb,
    )
    spawn_cmd_preview = harness._build_command()
    log(f"spawn command: {' '.join(spawn_cmd_preview)}")

    verdict, reason = "FAIL", "discovery did not complete"
    init_resp: Dict[str, Any] = {}
    try:
        harness.start()
        log("start(): subprocess spawned; reader thread running")

        init_resp = harness.initialize(timeout=60)
        log(f"initialize(): control_response received; reply keys="
            f"{sorted(init_resp.keys()) if isinstance(init_resp, dict) else type(init_resp)}")

        # init frame may arrive on stdout right after spawn (system/init).
        if harness.init_frame is not None:
            if_ = harness.init_frame
            init_keys = sorted(if_.keys())
            init_perm_mode = if_.get("permissionMode")
            sid = if_.get("session_id")
            log(f"system/init keys: {init_keys}")
            log(f"system/init: session_id={sid} permissionMode={init_perm_mode!r} "
                f"model={if_.get('model')!r} tools(n)={len(if_.get('tools') or [])} "
                f"mcp_servers(n)={len(if_.get('mcp_servers') or [])}")

        prompt = (
            "Create a file named t13_probe.txt in the current directory containing "
            "exactly the text HELLO using the Write tool. Do not use any other tool."
        )
        log(f"send prompt: {prompt!r}")
        for ev in harness.send(prompt, turn_timeout=180, idle_timeout=120):
            mtype = ev.get("type", "?")
            event_types[mtype] = event_types.get(mtype, 0) + 1
            if mtype == "system" and ev.get("subtype") == "init" and not init_keys:
                init_keys = sorted(ev.keys())
                init_perm_mode = ev.get("permissionMode")
            if mtype == "assistant":
                blocks = (ev.get("message") or {}).get("content") or []
                shapes = [_summarize_block(b) for b in blocks if isinstance(b, dict)]
                if any("tool_use" in s for s in shapes):
                    tool_use_seen = True
                log(f"  assistant content blocks: {shapes}")
            elif mtype == "user":
                blocks = (ev.get("message") or {}).get("content") or []
                shapes = [_summarize_block(b) for b in blocks if isinstance(b, dict)]
                log(f"  user(tool_result) blocks: {shapes}")
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        ctext = c if isinstance(c, str) else json.dumps(c)[:300]
                        log(f"    tool_result content: {ctext[:300]!r}")
            elif mtype == "result":
                result_subtype = ev.get("subtype")
                sid = sid or ev.get("session_id")
                log(f"  result: subtype={result_subtype!r} is_error={ev.get('is_error')} "
                    f"num_turns={ev.get('num_turns')} session_id={ev.get('session_id')}")

        log("")
        log(f"observed event types (count): {event_types}")
        log(f"can_use_tool control_request observed: {canusetool_seen}")
        if canusetool_seen:
            log(f"can_use_tool request shape: {canusetool_req_shape}")
        log(f"permission decisions taken: {decisions}")
        log(f"assistant tool_use observed: {tool_use_seen}")

        # Did the CLI honor the ALLOW (sentinel created with the right content)?
        sentinel_exists = sentinel.exists()
        sentinel_content = sentinel.read_text().strip() if sentinel_exists else "<absent>"
        log(f"sentinel {sentinel.name}: exists={sentinel_exists} content={sentinel_content!r}")

        # --- LIGHT DENY demo (mechanism completeness; rigorous proof is T14) ---
        # A second turn asks to Write a DIFFERENT file; the callback denies it
        # (not the sentinel). Confirm the CLI honored the deny: the file is NOT
        # created. This shows the same channel carries deny, not just allow.
        denied_target = Path(workdir) / "t13_should_not_exist.txt"
        deny_prompt = (
            "Now create a file named t13_should_not_exist.txt in the current "
            "directory containing the text NOPE using the Write tool."
        )
        log("")
        log(f"send prompt (deny demo): {deny_prompt!r}")
        deny_canusetool = False
        try:
            for ev in harness.send(deny_prompt, turn_timeout=180, idle_timeout=120):
                mtype = ev.get("type", "?")
                if mtype == "user":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            c = b.get("content")
                            ctext = c if isinstance(c, str) else json.dumps(c)[:200]
                            log(f"    deny tool_result is_error={b.get('is_error')} "
                                f"content={ctext[:160]!r}")
                elif mtype == "result":
                    log(f"  deny result: subtype={ev.get('subtype')!r} "
                        f"is_error={ev.get('is_error')}")
            deny_canusetool = any(
                (m.get('request') or {}).get('tool_name') == 'Write'
                and 't13_should_not_exist' in str((m.get('request') or {}).get('input', {}))
                for m in harness.permission_requests
            )
        except CLIDriverError as exc:
            log(f"  deny demo turn ended early (non-fatal for T13): {exc}")
        denied_exists = denied_target.exists()
        deny_honored = (not denied_exists)
        log(f"deny demo: denied-target exists={denied_exists} (expected False); "
            f"deny_honored={deny_honored}; can_use_tool seen for denied write={deny_canusetool}")

        harness.stop()
        log("stop(): stdin closed, subprocess terminated, threads joined")
        if harness.stderr_text:
            log(f"stderr tail: {harness.stderr_text[-300:]}")

        # Verdict logic. The deliverable is: can the raw CLI be driven
        # bidirectionally over stream-json AND is there a working programmatic
        # permission mechanism?
        wire_ok = (
            harness._initialized
            and bool(event_types)
            and "result" in event_types
            and bool(sid)
        )
        perm_ok = canusetool_seen and tool_use_seen and sentinel_exists and "HELLO" in sentinel_content
        if wire_ok and perm_ok:
            verdict = "PASS"
            reason = (
                "Raw `claude` CLI driven bidirectionally over stream-json: initialize "
                "handshake succeeded, NDJSON event stream parsed (system/init, assistant, "
                "user, result), and the programmatic permission mechanism WORKS — with "
                "`--permission-prompt-tool stdio` set, a can_use_tool control_request "
                "arrived over the wire and our allow control_response was honored (sentinel "
                "created with HELLO). Mechanism = stream-json control protocol, ACTIVATED by "
                f"the undocumented `--permission-prompt-tool stdio` spawn flag (absent from "
                f"--help: present_in_help={ppt_present}). Light deny demo over the same "
                f"channel: deny_honored={deny_honored} (denied Write produced no file). "
                f"Rigorous allow+deny+containment proof is T14."
            )
        elif wire_ok and canusetool_seen:
            verdict = "PARTIAL"
            reason = (
                "Bidirectional stream-json driving works and a can_use_tool control_request "
                f"arrived, but the allow round-trip was not fully confirmed: tool_use={tool_use_seen}, "
                f"sentinel_exists={sentinel_exists}, content={sentinel_content!r}."
            )
        elif wire_ok:
            verdict = "PARTIAL"
            reason = (
                "Bidirectional stream-json driving works (initialize + parsed event stream + "
                "result), but NO can_use_tool control_request was observed for the Write — the "
                "permission mechanism could not be demonstrated this run (the CLI may have "
                "auto-handled or refused the tool). event_types="
                f"{event_types}, decisions={decisions}."
            )
        else:
            verdict = "FAIL"
            reason = (
                f"Could not drive the CLI over stream-json as expected: initialized="
                f"{harness._initialized}, event_types={event_types}, session_id={sid}."
            )
    except Exception as exc:  # noqa: BLE001 -- fail-clean, record the reason
        verdict, reason = "FAIL", f"exception: {type(exc).__name__}: {exc}"
        log(reason)
        try:
            harness.stop()
        except Exception:
            pass
    finally:
        # Clean the disposable fixture + any project transcript dir the CLI made.
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass
        _clean_project_transcript_dir(workdir, log)

    log("")
    log("=== PERMISSION MECHANISM (T13 finding, CLI on this host) ===")
    log("Mechanism: the bidirectional stream-json CONTROL PROTOCOL, ACTIVATED by the")
    log("undocumented `--permission-prompt-tool stdio` spawn flag.")
    log("  * Spawn `claude --output-format stream-json --verbose -p --input-format "
        "stream-json --permission-prompt-tool stdio [--permission-mode default]`.")
    log("  * The driver sends an `initialize` control_request on stdin (control "
        "handshake) and then user messages as {type:user,message:{role,content},...}.")
    log("  * With `--permission-prompt-tool stdio` set, the CLI routes per-tool "
        "permission checks BACK to the driver as `can_use_tool` control_request frames; "
        "the driver answers each with a `control_response` "
        "(`{behavior:allow,updatedInput}` or `{behavior:deny,message}`).")
    log("  * THE SWITCH (T13 key finding): `--permission-prompt-tool stdio` is what "
        "enables the round-trip. WITHOUT the flag, the CLI auto-decides tools itself "
        "from --allowedTools / --permission-mode and NEVER emits can_use_tool — verified "
        "live (a run with the flag absent: Write was auto-handled by the CLI, NO "
        "can_use_tool reached the driver). The SDK sets this flag to `stdio` automatically "
        "whenever a can_use_tool callback is provided "
        "(claude_agent_sdk/_internal/client.py: permission_prompt_tool_name='stdio').")
    log("  * ALLOW WIRE SHAPE GOTCHA (T13 key finding): an allow control_response MUST "
        "carry `updatedInput` as a record; omitting it returns 'Tool permission request "
        "failed: ZodError ... expected record' and the tool fails. The harness defaults "
        "`updatedInput` to the original tool input on allow (mirrors the SDK).")
    log(f"  * `initialize` handshake completed: {harness._initialized} (sent matching "
        "the SDK; the activating switch is the spawn flag, not the initialize frame).")
    log(f"  * `--permission-prompt-tool` present in --help: {ppt_present}. UPDATES "
        "ADR-001's stale note (was: 'absent on v2.1.183'): on THIS version (2.1.185) the "
        "flag is STILL ABSENT FROM --help, but it IS accepted and functional — the real "
        "programmatic permission mechanism is `--permission-prompt-tool stdio` + the "
        "stream-json control protocol, NOT a separate MCP permission tool and NOT 'none'.")
    log("")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)

    # The session_id is not a secret, but pass it as an extra literal to the
    # scrubber out of caution alongside the routine pattern scrub.
    extra = [sid] if sid else None
    with record_criterion("cli_permission_mechanism", extra_secrets=extra) as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    return 0 if verdict in ("PASS", "PARTIAL") else 1


def _clean_project_transcript_dir(workdir: str, log: Callable[[str], None]) -> None:
    """Remove the ~/.claude/projects/<sanitized-cwd> dir the CLI created, if any.

    The CLI persists transcripts under a sanitized form of the cwd. We only ever
    delete a dir whose sanitized name maps to OUR disposable temp workdir, so this
    can never touch a real project's transcripts.
    """
    try:
        projects = Path.home() / ".claude" / "projects"
        if not projects.is_dir():
            return
        # The CLI persists transcripts under a sanitized cwd: path separators,
        # dots AND underscores are all collapsed to '-'. So a workdir basename like
        # ``t13_cli_abcd`` appears in the dir name as ``t13-cli-abcd``. Match on the
        # sanitized basename TOKEN (which embeds the unique mkdtemp suffix) so we
        # only ever delete OUR disposable fixture's dir, never a real project.
        def _san(s: str) -> str:
            return s.replace("/", "-").replace(".", "-").replace("_", "-")
        token = _san(Path(workdir).name)  # e.g. "t13-cli-abcd1234"
        removed = []
        for child in projects.iterdir():
            if child.is_dir() and token in child.name:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child.name)
        if removed:
            log(f"cleaned CLI project transcript dir(s): {removed}")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
