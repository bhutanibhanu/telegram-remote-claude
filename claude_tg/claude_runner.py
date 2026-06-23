"""Runs the Claude Code CLI in headless print mode, one session per chat."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    ok: bool
    text: str
    session_id: str | None = None
    error: str | None = None


class ClaudeBusy(Exception):
    """Raised when a chat already has a Claude turn in flight."""


class ClaudeRunner:
    """Invokes ``claude -p`` per message, keeping a resumable session per chat."""

    def __init__(self, config: Config, session_store=None):
        self.config = config
        self.store = session_store
        self._locks: dict[int, asyncio.Lock] = {}
        self._sessions: dict[int, str] = {}
        self._cwds: dict[int, str] = {}
        if self.store is not None:
            data = self.store.load()
            for key, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    cid = int(key)
                except (TypeError, ValueError):
                    continue
                if entry.get("session_id"):
                    self._sessions[cid] = entry["session_id"]
                if entry.get("cwd"):
                    self._cwds[cid] = entry["cwd"]

    # ---- session / cwd state ------------------------------------------------
    def _lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    def get_cwd(self, chat_id: int) -> str:
        return self._cwds.get(chat_id, str(self.config.workdir))

    def set_cwd(self, chat_id: int, path: str) -> str:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise NotADirectoryError(str(p))
        resolved = str(p.resolve())
        self._cwds[chat_id] = resolved
        self._persist(chat_id)
        return resolved

    def reset(self, chat_id: int) -> None:
        self._sessions.pop(chat_id, None)
        self._persist(chat_id)

    def _persist(self, chat_id: int) -> None:
        if self.store is None:
            return
        try:
            self.store.update(
                chat_id,
                session_id=self._sessions.get(chat_id),
                cwd=self._cwds.get(chat_id),
            )
        except Exception:
            log.exception("failed to persist session state for chat %s", chat_id)

    # ---- command building / invocation -------------------------------------
    def _build_cmd(self, chat_id: int) -> list[str]:
        cmd = [self.config.claude_bin, "-p", "--output-format", "json"]
        # SB5 / C1: the allow-all bypass flag is added ONLY when the operator explicitly
        # opted in (skip_permissions). The default is False (gate ON — see Config), so a
        # fresh install runs Claude's tools behind the CLI's approval prompt.
        if self.config.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.config.model:
            cmd += ["--model", self.config.model]
        session_id = self._sessions.get(chat_id)
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    async def _invoke(self, cmd: list[str], stdin_text: str, cwd: str) -> tuple[int, str, str]:
        """Run the subprocess; return (returncode, stdout, stderr). Patched in tests."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin_text.encode("utf-8")),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
            raise
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def run(self, chat_id: int, prompt: str) -> ClaudeResult:
        prompt = (prompt or "").strip()
        if not prompt:
            return ClaudeResult(ok=False, text="", error="empty prompt")

        lock = self._lock(chat_id)
        if lock.locked():
            raise ClaudeBusy()

        async with lock:
            cwd = self.get_cwd(chat_id)
            had_session = chat_id in self._sessions
            result = await self._run_once(chat_id, prompt, cwd)
            # If resuming an expired/missing session failed, drop the dead session
            # id and retry once fresh — otherwise the chat would stay stuck failing
            # every message until the user manually ran /reset.
            if had_session and not result.ok and self._is_resume_failure(result):
                log.info("resume failed for chat %s; clearing session and retrying fresh", chat_id)
                self._sessions.pop(chat_id, None)
                self._persist(chat_id)
                result = await self._run_once(chat_id, prompt, cwd)
            return result

    async def _run_once(self, chat_id: int, prompt: str, cwd: str) -> ClaudeResult:
        if not Path(cwd).is_dir():
            return ClaudeResult(
                ok=False, text="",
                error=f"Working directory does not exist: {cwd}. Use /cd to set a valid one.",
            )
        cmd = self._build_cmd(chat_id)
        try:
            code, out, err = await self._invoke(cmd, prompt, cwd)
        except asyncio.TimeoutError:
            return ClaudeResult(
                ok=False, text="",
                error=f"Claude timed out after {self.config.timeout_seconds}s.",
            )
        except FileNotFoundError:
            return ClaudeResult(
                ok=False, text="",
                error=f"Claude binary not found: {self.config.claude_bin!r}. Set CLAUDE_BIN.",
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("claude invocation failed")
            return ClaudeResult(ok=False, text="", error=f"Failed to run Claude: {exc}")

        data = self._parse(out)
        if data is None:
            fallback = (out.strip() or err.strip())
            if fallback and code == 0:
                return ClaudeResult(ok=True, text=fallback)
            return ClaudeResult(
                ok=False, text="",
                error=(err.strip() or f"Claude exited with code {code} and no parseable output.")[:1500],
            )

        session_id = data.get("session_id")
        if session_id:
            self._sessions[chat_id] = session_id
            self._persist(chat_id)

        result_text = data.get("result")
        if not isinstance(result_text, str):
            result_text = ""

        subtype = data.get("subtype")
        is_error = bool(data.get("is_error")) or (subtype is not None and subtype != "success")
        if is_error or code != 0:
            msg = result_text or err.strip() or f"Claude reported an error (subtype={subtype})."
            return ClaudeResult(ok=False, text=result_text, session_id=session_id, error=msg[:1500])

        return ClaudeResult(ok=True, text=result_text, session_id=session_id)

    @staticmethod
    def _is_resume_failure(result: ClaudeResult) -> bool:
        """Heuristic: did a --resume turn fail because the session is gone?"""
        err = (result.error or "").lower()
        if "timed out" in err or "binary not found" in err:
            return False
        return (
            "no conversation found" in err
            or "no parseable output" in err
            or ("session" in err and ("not found" in err or "invalid" in err or "expired" in err))
        )

    @staticmethod
    def _parse(out: str) -> dict | None:
        out = (out or "").strip()
        if not out:
            return None
        # Normal case: the whole stdout is one JSON object.
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        # Fallback: stray single-line log JSON around the result. Collect every
        # single-line JSON object and prefer the one that looks like a Claude
        # result, so a trailing diagnostic line is never mistaken for the answer.
        candidates: list[dict] = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    candidates.append(parsed)
        for parsed in reversed(candidates):
            if parsed.get("type") == "result" or "result" in parsed or "session_id" in parsed:
                return parsed
        return candidates[-1] if candidates else None
