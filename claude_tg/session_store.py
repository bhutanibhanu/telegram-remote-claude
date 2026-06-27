"""On-disk persistence of per-chat Claude sessions (schema v2, P4).

Two views over **one** file (ADR-004 D8):

* **Flat view (one-shot).** :meth:`load` / :meth:`update` preserve the *exact*
  pre-P4 contract — ``{"<chat_id>": {"session_id": ..., "cwd": ...}}`` over each
  chat's **active** project. The live one-shot runner and the current streaming
  session consume only this view, so it must stay byte-for-byte compatible.
* **Registry view (streaming).** The full v2 document — a per-chat named-project
  registry — is read/written via the private :meth:`_load_raw` / :meth:`_save_raw`.
  A later task (T3) builds typed CRUD accessors on top of these.

Schema v2 (on disk)::

    { "version": 2,
      "chats": { "<chat_id>": { "active": "<name|null>",
          "projects": { "<name>": { "cwd": "/abs", "session_id": "<id|null>",
                                    "created_at": "<iso>", "last_active": "<iso>" } } } } }

A **v1** doc is the legacy flat shape ``{"<chat_id>": {"session_id", "cwd"}}`` with
**no** ``version`` key; it is migrated on load by wrapping each chat's entry as a
single active project named ``"default"`` (ADR-004 D6) — idempotent, atomic,
one-way. A corrupt doc, a non-dict, or an unknown/future ``version`` loads as
**empty** (``{"version": 2, "chats": {}}``) and never raises — today's fail-safe
``load`` (SB6, RB6).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid an import cycle at runtime (scheduler imports from this module)
    from .scheduler import Schedule

log = logging.getLogger(__name__)

#: The current on-disk schema version. A doc with this version is used as-is; a
#: doc with no version is treated as v1 and migrated; any other value is unknown
#: (future) and fails safe to empty.
SCHEMA_VERSION = 2

#: The project name a migrated v1 entry (and a fresh one-shot chat) is filed under.
DEFAULT_PROJECT = "default"

#: SB4 project-name rule (ADR-004): non-empty, ≤32 chars, ASCII letters/digits/
#: underscore/hyphen only — no spaces, slashes, dots, ``..``, or unicode.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: The reasoning-EFFORT levels the SDK accepts (``ClaudeAgentOptions.effort`` —
#: ``EffortLevel = Literal['low','medium','high','xhigh','max']``), in ascending order
#: (the canonical ordering for the ``/effort`` usage string + the statusline). The
#: per-project ``/effort`` override (T-EFFORT) is validated against this; anything else
#: normalizes to ``None`` (a cleared override → the SDK default), so a garbage value can
#: never wedge the project on an effort the SDK would reject (RB1). Matched
#: case-insensitively (the stored value is the lowercased canonical level). Public so the
#: bot's ``/effort`` command + the streaming session validate against ONE source of truth.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: Set form for O(1) membership tests (validation). Kept in lock-step with
#: :data:`EFFORT_LEVELS` (the ordered display form).
_EFFORT_LEVELS = frozenset(EFFORT_LEVELS)


# ---- registry errors (raised by the typed CRUD API, T3) --------------------


class InvalidProjectName(ValueError):
    """A project name fails the SB4 rule (``^[A-Za-z0-9_-]{1,32}$``)."""


class DuplicateProject(ValueError):
    """A project with that name already exists (case-insensitive) for the chat."""


class UnknownProject(KeyError):
    """No project with that name (case-insensitive) exists for the chat."""


class MaxSchedulesExceeded(ValueError):
    """A chat already holds the per-chat schedule cap (``SCHEDULE_MAX_TASKS_PER_CHAT``).

    Raised by :meth:`JsonSessionStore.add_schedule` when creating a NEW (non-overwrite)
    schedule would push the chat over its cap — a fail-closed DoS-by-schedule guard
    (P14 T2 / design §6). Overwriting an existing same-name schedule never trips it (it
    does not grow the count). The bot turns this into a clean "you've hit the limit"
    reply (RB1) rather than silently over-capping.
    """


def validate_project_name(name: str) -> None:
    """Raise :class:`InvalidProjectName` unless ``name`` satisfies SB4.

    A valid name is non-empty, at most 32 characters, and composed only of ASCII
    letters, digits, ``_`` and ``-`` (no spaces, slashes, dots, ``..``, or
    unicode). Pure + side-effect free, so it is reusable by the bot commands
    (T5/T6) and unit-testable on its own.
    """
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise InvalidProjectName(
            f"invalid project name {name!r}: must match {_NAME_RE.pattern}"
        )


def _now() -> str:
    """An ISO-8601 timestamp in UTC (e.g. ``2026-06-22T12:34:56.789+00:00``)."""
    return datetime.now(timezone.utc).isoformat()


def _empty_v2() -> dict:
    """A fresh, well-formed empty v2 document (the fail-safe load result)."""
    return {"version": SCHEMA_VERSION, "chats": {}}


class JsonSessionStore:
    """Persists the per-chat project registry (schema v2) to a JSON file.

    The public :meth:`load` / :meth:`update` expose the legacy **flat** view over
    each chat's active project (pre-P4 contract); :meth:`_load_raw` /
    :meth:`_save_raw` expose the full v2 document for the registry layer.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ---- raw v2 read / write (the registry foundation) ---------------------

    def _load_raw(self) -> dict:
        """Read the file, migrating v1→v2 on the way; return the v2 document.

        Never raises (SB6): a missing file, corrupt JSON, a non-dict payload, or
        an unknown/future ``version`` all yield a fresh empty v2 doc — mirroring
        the pre-P4 fail-safe ``load``. Migration of a v1 doc is idempotent: a v2
        doc is returned unchanged.
        """
        try:
            if not self.path.is_file():
                return _empty_v2()
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # corrupt / unreadable -> start fresh, don't crash
            log.exception("could not read session store %s; ignoring", self.path)
            return _empty_v2()

        if not isinstance(data, dict):
            log.warning("session store %s is not a JSON object; ignoring", self.path)
            return _empty_v2()

        version = data.get("version")
        if version is None:
            # No version key -> legacy v1 flat doc. Migrate it.
            return _migrate_v1_to_v2(data)
        if version == SCHEMA_VERSION:
            # Already v2. Normalize the container shape defensively (a hand-edited
            # file might miss "chats") but do not rewrite valid contents.
            chats = data.get("chats")
            if not isinstance(chats, dict):
                return _empty_v2()
            return data
        # Unknown / future version -> fail safe to empty (never guess, never crash).
        log.warning(
            "session store %s has unknown version %r; ignoring", self.path, version
        )
        return _empty_v2()

    def _save_raw(self, data: dict) -> None:
        """Atomically write the v2 ``data`` with ``0600`` perms (RB6).

        Same discipline as the pre-P4 store: ``mkdir -p`` the parent, write to a
        sibling ``<name>.tmp``, best-effort ``chmod 0o600`` (the file holds chat
        ids, cwds, and Claude session ids), then ``replace`` onto the target —
        atomic on the same filesystem.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)  # atomic on the same filesystem

    # ---- flat view (the pre-P4 one-shot contract — PRESERVE) ---------------

    def load(self) -> dict:
        """Return the flat view ``{"<chat_id>": {"session_id", "cwd"}}``.

        Derived from each chat's **active** project. A chat with no active project
        (or whose active project has neither a ``session_id`` nor a ``cwd``) is
        **absent** from the result — exactly as the pre-P4 store omitted a chat
        with no entry. The shape is byte-for-byte what ``claude_runner`` and
        ``stream_session`` consume (they read ``entry.get("session_id")`` /
        ``entry.get("cwd")``). Never raises.
        """
        raw = self._load_raw()
        flat: dict[str, dict] = {}
        chats = raw.get("chats", {})
        if not isinstance(chats, dict):
            return flat
        for chat_key, chat in chats.items():
            if not isinstance(chat, dict):
                continue
            project = _active_project(chat)
            if project is None:
                continue
            entry: dict[str, str] = {}
            session_id = project.get("session_id")
            if session_id:
                entry["session_id"] = session_id
            cwd = project.get("cwd")
            if cwd:
                entry["cwd"] = cwd
            if entry:  # mirror pre-P4 "pop empty" — omit a contentless chat
                flat[str(chat_key)] = entry
        return flat

    def update(self, chat_id: int, session_id: str | None, cwd: str | None) -> None:
        """Write ``session_id`` / ``cwd`` to the chat's **active** project.

        Preserves the pre-P4 semantics over the active project:

        * ``session_id`` set -> stored; ``session_id=None`` -> **cleared**.
        * ``cwd`` set -> stored; ``cwd=None`` -> left **unchanged**.

        If the chat has no active project yet, a ``default`` project is created
        and made active (so a fresh one-shot chat behaves exactly as before — one
        session under the hood). The project record is **kept** even when it ends
        up with neither field (it still carries durable identity — name +
        timestamps, ADR-004 D3); the now-empty chat simply drops out of the flat
        :meth:`load` view, which is the observable pre-P4 "pop empty" behavior.
        """
        raw = self._load_raw()
        chats = raw.setdefault("chats", {})
        key = str(chat_id)
        chat = chats.get(key)
        if not isinstance(chat, dict):
            chat = {"active": None, "projects": {}}
            chats[key] = chat

        project = _active_project(chat)
        if project is None:
            project = _new_project(cwd)
            projects = chat.setdefault("projects", {})
            if not isinstance(projects, dict):
                projects = {}
                chat["projects"] = projects
            projects[DEFAULT_PROJECT] = project
            chat["active"] = DEFAULT_PROJECT

        if session_id is not None:
            project["session_id"] = session_id
        else:
            project["session_id"] = None
        if cwd is not None:
            project["cwd"] = cwd
        project["last_active"] = _now()

        self._save_raw(raw)

    # ---- registry view (streaming) — typed CRUD over the v2 doc (T3) -------

    def get_active(self, chat_id: int) -> str | None:
        """The chat's active project name, or ``None`` if no chat/active project.

        Returns the stored (as-created) name. Never raises.
        """
        chat = self._chat(self._load_raw(), chat_id)
        active = chat.get("active") if chat is not None else None
        return active if isinstance(active, str) else None

    def list_projects(self, chat_id: int) -> dict[str, dict]:
        """``{name: record}`` for the chat — empty if the chat has no projects.

        Each record is the stored ``{cwd, session_id, created_at, last_active}``
        dict (as-created name keys, original case). The returned mapping is a
        shallow copy, so mutating it does not disturb on-disk state (the inner
        records are read-only by contract — callers should not mutate them).
        Never raises.
        """
        chat = self._chat(self._load_raw(), chat_id)
        if chat is None:
            return {}
        projects = chat.get("projects")
        if not isinstance(projects, dict):
            return {}
        return {
            name: record
            for name, record in projects.items()
            if isinstance(name, str) and isinstance(record, dict)
        }

    def get_project(self, chat_id: int, name: str) -> dict | None:
        """The record for ``name`` (matched case-insensitively), or ``None``.

        Returns the stored record dict; never raises.
        """
        chat = self._chat(self._load_raw(), chat_id)
        if chat is None:
            return None
        projects = chat.get("projects")
        if not isinstance(projects, dict):
            return None
        key = _resolve_name(projects, name)
        if key is None:
            return None
        record = projects[key]
        return record if isinstance(record, dict) else None

    def create(
        self, chat_id: int, name: str, cwd: str, *, make_active: bool = True
    ) -> None:
        """Add a fresh project ``name`` (cwd ``cwd``) to the chat's registry.

        Validates the name first (SB4) — raises :class:`InvalidProjectName` on a
        bad name **before** any write. Raises :class:`DuplicateProject` if a
        project with that name already exists (case-insensitive) for the chat;
        the as-given case is what gets stored/displayed. Creates the chat
        container if absent. When ``make_active`` (the default), the chat's
        ``active`` becomes this project. Persists.
        """
        validate_project_name(name)
        raw = self._load_raw()
        chat = self._ensure_chat(raw, chat_id)
        projects = chat.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = {}
            chat["projects"] = projects
        if _resolve_name(projects, name) is not None:
            raise DuplicateProject(f"project {name!r} already exists")
        projects[name] = _new_project(cwd)
        if make_active:
            chat["active"] = name
        self._save_raw(raw)

    def switch(self, chat_id: int, name: str) -> None:
        """Make ``name`` the chat's active project (matched case-insensitively).

        Raises :class:`UnknownProject` if no such project exists. The stored
        (as-created) key is what becomes ``active``. Persists.
        """
        raw = self._load_raw()
        chat, _projects, key = self._resolve(raw, chat_id, name)
        chat["active"] = key
        self._save_raw(raw)

    def remove(self, chat_id: int, name: str) -> None:
        """Delete the project ``name`` (matched case-insensitively).

        Raises :class:`UnknownProject` if no such project exists. If the removed
        project was the active one, the chat's ``active`` is set to ``None`` (the
        store stays policy-free and merely consistent — the bot enforces the
        "don't remove the active project" UX). Persists.
        """
        raw = self._load_raw()
        chat, projects, key = self._resolve(raw, chat_id, name)
        del projects[key]
        if chat.get("active") == key:
            chat["active"] = None
        self._save_raw(raw)

    def touch(self, chat_id: int, name: str) -> None:
        """Set the project ``name``'s ``last_active`` to now (case-insensitive).

        Raises :class:`UnknownProject` if no such project exists (so a caller can
        rely on the project being present after a successful call). Persists.
        """
        raw = self._load_raw()
        _chat, projects, key = self._resolve(raw, chat_id, name)
        record = projects[key]
        if not isinstance(record, dict):
            raise UnknownProject(name)
        record["last_active"] = _now()
        self._save_raw(raw)

    def set_session_id(
        self, chat_id: int, name: str, session_id: str | None
    ) -> None:
        """Write ``session_id`` to the **named** project (matched case-insensitively).

        The targeted counterpart of the flat :meth:`update` (which only ever touches a
        chat's *active* project). P5/ADR-005 needs this because, with ``/switch`` now
        free (the busy-guard relaxed in D2), the active project can change **mid-turn**;
        a concurrent run must persist its result ``session_id`` to the project it ran
        **on** — the captured target — not to whatever is active when the result lands
        (the lock-P-drive-Q / persist-drift hazard). Like :meth:`update`'s session field:
        a value is stored; ``None`` **clears** it (a reset / dead-resume drop). The
        project's fixed ``cwd`` is left untouched (D4); ``last_active`` is bumped. Raises
        :class:`UnknownProject` if no such project exists (the caller — the streaming
        session — only ever passes a project it just ran, so absence is a real error, not
        a silent no-op). Persists.
        """
        raw = self._load_raw()
        _chat, projects, key = self._resolve(raw, chat_id, name)
        record = projects[key]
        if not isinstance(record, dict):
            raise UnknownProject(name)
        record["session_id"] = session_id
        record["last_active"] = _now()
        self._save_raw(raw)

    def add_cost(self, chat_id: int, name: str, cost_usd: float) -> float:
        """Add ``cost_usd`` to the **named** project's cumulative cost; return the new total.

        T3 (P9): each turn's SDK-reported ``total_cost_usd`` is accumulated into a durable
        per-project ``cost_usd`` field so ``/status`` (T2) can show the project's lifetime
        spend. Like :meth:`set_session_id` this targets the project the turn ran **on** (the
        captured name — the active project can move mid-turn now ``/switch`` is free), matched
        case-insensitively, and is atomic + ``0600`` (RB6) via :meth:`_save_raw`. A
        non-finite / non-numeric / negative ``cost_usd`` (defensive — the SDK should give a
        small positive float) is treated as ``0.0`` so a bad value can never corrupt the
        running total or crash the turn (RB1). Raises :class:`UnknownProject` if no such
        project exists (the caller only ever passes a project it just ran). Persists; returns
        the cumulative total after the add (also when the add is 0).
        """
        try:
            delta = float(cost_usd)
        except (TypeError, ValueError):
            delta = 0.0
        if not (delta == delta) or delta in (float("inf"), float("-inf")) or delta < 0:
            # NaN (delta != delta), ±inf, or negative — ignore the delta (RB1), still return
            # the current total so the caller's surfacing is unaffected.
            delta = 0.0
        raw = self._load_raw()
        _chat, projects, key = self._resolve(raw, chat_id, name)
        record = projects[key]
        if not isinstance(record, dict):
            raise UnknownProject(name)
        prior = record.get("cost_usd")
        try:
            prior_val = float(prior) if prior is not None else 0.0
        except (TypeError, ValueError):
            prior_val = 0.0
        total = prior_val + delta
        record["cost_usd"] = total
        record["last_active"] = _now()
        self._save_raw(raw)
        return total

    def _persist_field(
        self, chat_id: int, name: str, field: str, value: object | None
    ) -> None:
        """Write (or clear) one already-normalized ``field`` on the **named** project.

        The single generalized per-project-knob setter the typed ``set_*`` overrides share
        (model, effort — see :meth:`set_model` / :meth:`set_effort`, both now thin wrappers).
        Performs the one common dance, byte-for-byte what each setter did inline: ``_load_raw``
        → :meth:`_resolve` (matches ``name`` case-insensitively) → if ``value is None``,
        ``record.pop(field, None)`` (clear the override) else ``record[field] = value`` → bump
        ``last_active`` → atomic + ``0600`` :meth:`_save_raw`. Targets a NAMED project (the
        active project can move mid-turn now ``/switch`` is free); the project's fixed
        ``cwd``/``session_id`` are left untouched (a knob applies on the NEXT fresh session — it
        is a session-creation param). Raises :class:`UnknownProject` if no such project exists.

        ``value`` is the caller's responsibility to normalize/validate first (the wrappers do —
        model: strip-or-``None``; effort: validate against ``{low…max}`` or ``None``); this
        helper only persists, so ``None`` always means "clear the override". Persists.
        """
        raw = self._load_raw()
        _chat, projects, key = self._resolve(raw, chat_id, name)
        record = projects[key]
        if not isinstance(record, dict):
            raise UnknownProject(name)
        if value is None:
            record.pop(field, None)
        else:
            record[field] = value
        record["last_active"] = _now()
        self._save_raw(raw)

    def set_model(self, chat_id: int, name: str, model: str | None) -> None:
        """Write the per-project model override to the **named** project (case-insensitive).

        T4 (P9): ``/fast`` · ``/deep`` store a model id here; ``/auto`` clears it (``None``)
        back to the configured default. Like :meth:`set_session_id` this targets a named
        project (the active project can move mid-turn now ``/switch`` is free) and is
        atomic + ``0600`` (RB6) via :meth:`_save_raw`. A non-string / empty ``model`` is
        normalized to ``None`` (a cleared override) so a bad value can never wedge the
        project on an unusable id (RB1) — the turn falls back to the default. ``last_active``
        is bumped; the project's fixed ``cwd``/``session_id`` are left untouched (the model
        applies on the NEXT fresh session — it is a session-creation param). Raises
        :class:`UnknownProject` if no such project exists. Persists.

        Thin wrapper: normalize, then delegate the shared persist dance to
        :meth:`_persist_field` (the dedup of the byte-for-byte parallel model/effort setters).
        """
        normalized = model.strip() if isinstance(model, str) and model.strip() else None
        self._persist_field(chat_id, name, "model", normalized)

    def get_model(self, chat_id: int, name: str) -> str | None:
        """The named project's per-project model override (case-insensitive), or ``None``.

        Read-only (never raises): an unknown project, a missing ``model`` field, or a
        non-string/empty stored value all read as ``None`` (RB1) — meaning "no override",
        so the turn uses the configured ``CLAUDE_MODEL`` / SDK default. Used by the turn
        path (thread into ``ClaudeAgentOptions`` / oneshot ``--model``) and ``/status``.
        """
        record = self.get_project(chat_id, name)
        if not isinstance(record, dict):
            return None
        value = record.get("model")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def set_effort(self, chat_id: int, name: str, effort: str | None) -> None:
        """Write the per-project reasoning-EFFORT override to the **named** project (case-insensitive).

        T-EFFORT (STATUSLINE): ``/effort <level>`` stores one of the five SDK levels here;
        ``/effort default`` clears it (``None``) back to the SDK default. Exactly parallel to
        :meth:`set_model` (the model override): targets a named
        project (the active project can move mid-turn now ``/switch`` is free), atomic +
        ``0600`` (RB6) via :meth:`_save_raw`, and bumps ``last_active``; the project's fixed
        ``cwd``/``session_id`` are left untouched (effort applies on the NEXT fresh session — it
        is a session-creation param baked into ``ClaudeAgentOptions``). The value is **validated
        against** ``{low, medium, high, xhigh, max}`` (case-insensitively) and stored lowercased;
        a non-string, empty, or unrecognized value normalizes to ``None`` (a cleared override) so
        a garbage level can never wedge the project on an effort the SDK would reject (RB1) — the
        turn falls back to the SDK default. Raises :class:`UnknownProject` if no such project
        exists (the bot only ever passes a project it just resolved/created). Persists.

        Thin wrapper: validate/normalize against ``{low…max}``, then delegate the shared persist
        dance to :meth:`_persist_field` (the dedup of the byte-for-byte parallel model/effort
        setters).
        """
        normalized = (
            effort.strip().lower()
            if isinstance(effort, str) and effort.strip().lower() in _EFFORT_LEVELS
            else None
        )
        self._persist_field(chat_id, name, "effort", normalized)

    def get_effort(self, chat_id: int, name: str) -> str | None:
        """The named project's per-project effort override (case-insensitive), or ``None``.

        Read-only (never raises, RB1): an unknown project, a missing ``effort`` field, or a
        stored value that is NOT one of ``{low, medium, high, xhigh, max}`` (e.g. a hand-edited
        garbage level) all read as ``None`` (meaning "no override", so the turn omits the
        ``effort`` kwarg and the SDK default applies). Matched/returned as the lowercased
        canonical level. Used by the turn path (thread into ``ClaudeAgentOptions(effort=…)``)
        and the statusline display.
        """
        record = self.get_project(chat_id, name)
        if not isinstance(record, dict):
            return None
        value = record.get("effort")
        if isinstance(value, str) and value.strip().lower() in _EFFORT_LEVELS:
            return value.strip().lower()
        return None

    def get_cost(self, chat_id: int, name: str) -> float:
        """The named project's cumulative cost in USD (case-insensitive), or ``0.0``.

        Read-only (never raises): an unknown project, a missing/odd ``cost_usd`` field, or a
        non-numeric stored value all read as ``0.0`` (RB1). Used by ``/status`` (T2) to show
        a project's lifetime spend.
        """
        record = self.get_project(chat_id, name)
        if not isinstance(record, dict):
            return 0.0
        value = record.get("cost_usd")
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def set_fork_pending(self, chat_id: int, name: str, pending: bool) -> None:
        """Set/clear the **persisted** ``fork_pending`` marker on a project (P11 T2 / B2+B3).

        An ADOPTED session (``/attach``) is pinned to a base ``session_id`` it may NOT continue
        in place if that session is live elsewhere — it must FORK. The fork-vs-continue decision
        is re-derived from a FRESH liveness probe at the moment of the first write
        (``StreamingSession._ensure_engine``), and ``fork_pending`` is the DURABLE "this is an
        adopted-not-yet-resumed session — re-probe before the first resume" marker that survives
        a restart (the in-memory ``attach_fork`` does not). ``True`` on adopt; **cleared
        (``False``) after the first successful turn** so subsequent resumes are ordinary
        continues (never re-fork). A non-bool value is normalized; when clearing, the field is
        removed entirely (a clean record). Atomic + ``0600`` (RB6). Targets a NAMED project
        (case-insensitive, like :meth:`set_session_id`) — the active project can move mid-turn.
        Raises :class:`UnknownProject` if no such project exists. Persists.
        """
        raw = self._load_raw()
        _chat, projects, key = self._resolve(raw, chat_id, name)
        record = projects[key]
        if not isinstance(record, dict):
            raise UnknownProject(name)
        if pending:
            record["fork_pending"] = True
        else:
            record.pop("fork_pending", None)
        record["last_active"] = _now()
        self._save_raw(raw)

    def get_fork_pending(self, chat_id: int, name: str) -> bool:
        """Whether the named project is an adopted-not-yet-resumed session (P11 T2 / B2+B3).

        Read-only (never raises): an unknown project or a missing/non-truthy ``fork_pending``
        field reads as ``False`` (RB1 — not an adopted-pending project, so an ordinary
        continue). ``StreamingSession._ensure_engine`` reads this BEFORE the first resume of an
        adopted session: when ``True`` it RE-PROBES the base id's current liveness and forks on
        live-or-uncertain (never co-driving), then clears it after the first successful turn.
        Survives a restart (it is persisted), so a restart before the first turn re-decides from
        a fresh probe instead of co-driving a stale-in-memory continue.
        """
        record = self.get_project(chat_id, name)
        if not isinstance(record, dict):
            return False
        return bool(record.get("fork_pending"))

    # ---- macros (per-chat prompt templates — T5 / P9) ----------------------

    def save_macro(self, chat_id: int, name: str, body: str) -> None:
        """Store a prompt template ``body`` under ``name`` for the chat (atomic + ``0600``, RB6).

        T5 (P9): ``/save <name> <prompt…>``. Validates ``name`` against the SB4 rule
        (``^[A-Za-z0-9_-]{1,32}$``) FIRST — raises :class:`InvalidProjectName` on a bad name
        **before** any write (so ``../``, spaces, > 32 chars, or empty are rejected, never
        persisted). Macros live on the chat (``chats[<id>]["macros"]``), separate from the
        project registry. ``get_macro``/``remove_macro`` resolve names case-insensitively, so
        ``save_macro`` must too: an existing case-insensitive key is **overwritten in place**
        (mirroring ``create``/``_resolve_name``) — ``/save Work x`` then ``/save work y`` keeps
        exactly ONE macro (the original-case key, the latest body), never two colliding entries
        that ``/run work`` could resolve to the wrong one. A genuinely new name is stored
        as-given. Creates the chat container if absent. ``body`` is the operator-authored
        template stored verbatim (no escaping/validation — it is fired as a normal turn).
        Persists.
        """
        validate_project_name(name)
        raw = self._load_raw()
        chat = self._ensure_chat(raw, chat_id)
        macros = chat.setdefault("macros", {})
        if not isinstance(macros, dict):
            macros = {}
            chat["macros"] = macros
        # Overwrite an existing case-insensitive key in place (so a re-save under a different
        # case never forks a duplicate the case-insensitive get/remove would disagree about).
        existing = _resolve_name(macros, name)
        macros[existing or name] = body
        self._save_raw(raw)

    def get_macro(self, chat_id: int, name: str) -> str | None:
        """The macro ``body`` stored under ``name`` for the chat, or ``None`` (case-insensitive).

        Read-only (never raises): an unknown chat / macro, a non-dict ``macros`` map, or a
        non-string stored body all read as ``None`` (RB1). Matched case-insensitively
        (mirroring the project-name match) so ``/run WORK`` finds a macro saved as ``work``.
        """
        chat = self._chat(self._load_raw(), chat_id)
        if chat is None:
            return None
        macros = chat.get("macros")
        if not isinstance(macros, dict):
            return None
        key = _resolve_name(macros, name)
        if key is None:
            return None
        body = macros[key]
        return body if isinstance(body, str) else None

    def list_macros(self, chat_id: int) -> dict[str, str]:
        """``{name: body}`` of the chat's macros — empty if the chat has none (T5 ``/macros``).

        Read-only (never raises): a missing chat / non-dict map reads as ``{}``. A shallow copy
        with only the well-formed (``str`` name → ``str`` body) entries, in insertion order.
        """
        chat = self._chat(self._load_raw(), chat_id)
        if chat is None:
            return {}
        macros = chat.get("macros")
        if not isinstance(macros, dict):
            return {}
        return {
            name: body
            for name, body in macros.items()
            if isinstance(name, str) and isinstance(body, str)
        }

    def remove_macro(self, chat_id: int, name: str) -> bool:
        """Delete the macro ``name`` (case-insensitive); return whether one was removed (T5).

        ``/unsave <name>``. Never raises (RB1): a missing chat / macro returns ``False`` (a
        clean "no such macro" the bot reports). Persists only when something was actually
        removed.
        """
        raw = self._load_raw()
        chat = self._chat(raw, chat_id)
        if chat is None:
            return False
        macros = chat.get("macros")
        if not isinstance(macros, dict):
            return False
        key = _resolve_name(macros, name)
        if key is None:
            return False
        del macros[key]
        self._save_raw(raw)
        return True

    # ---- proactive schedules (per-chat, P14 T2) ----------------------------
    #
    # Scheduled-task DEFINITIONS persist alongside macros/projects under
    # ``chats[<id>]["schedules"]`` ({name: record}), reusing the SAME atomic + 0600
    # write discipline (:meth:`_save_raw`, RB6). A record is the serialized form of a
    # :class:`~claude_tg.scheduler.Schedule`. This task persists the dormant data only —
    # nothing here fires a schedule (that is T-FIRE).
    #
    # RB3/RB6 — NEVER replay a missed fire. The stored ``next_run`` round-trips faithfully
    # (so a test can assert the persisted record), but the runtime is meant to RE-ARM every
    # schedule from *now* at startup via :meth:`rearm_all_schedules` (next_run = now +
    # interval) BEFORE the firing loop consults them — so a window that elapsed while the bot
    # was down fires at the NEXT interval, never as a burst of stacked catch-up runs.

    def add_schedule(
        self, schedule: "Schedule", *, max_per_chat: int | None = None
    ) -> None:
        """Persist (create or overwrite) a proactive schedule for its chat (atomic + 0600).

        ``/every`` create. The schedule's name is SB4-validated by :class:`Schedule`'s own
        constructor (an invalid name can't reach here). Stored under
        ``chats[<schedule.chat_id>]["schedules"][<name>]`` with the name resolved
        **case-insensitively** (mirroring :meth:`save_macro`/``create``): an existing
        same-name (case-insensitive) schedule is **overwritten in place** — so re-creating
        ``/every 1h CI …`` then ``/every 2h ci …`` keeps exactly ONE schedule (the
        original-case key, the latest definition), never two colliding entries.

        ``max_per_chat`` (the ``SCHEDULE_MAX_TASKS_PER_CHAT`` cap, T3) is enforced
        **fail-closed** for a genuinely NEW name: if the chat already holds ``max_per_chat``
        schedules and this name is not one of them, raises :class:`MaxSchedulesExceeded`
        **before any write** (a DoS-by-schedule guard). Overwriting an existing name never
        trips the cap (it does not grow the count). ``None`` (the default) disables the cap
        check (the pure store stays usable without config). Persists.
        """
        raw = self._load_raw()
        chat = self._ensure_chat(raw, schedule.chat_id)
        schedules = chat.setdefault("schedules", {})
        if not isinstance(schedules, dict):
            schedules = {}
            chat["schedules"] = schedules
        existing = _resolve_name(schedules, schedule.name)
        if (
            existing is None
            and max_per_chat is not None
            and len(schedules) >= max_per_chat
        ):
            # Fail-closed: refuse a NEW schedule that would exceed the cap (an overwrite of
            # an existing name is fine — it does not grow the count). Never silently over-cap.
            raise MaxSchedulesExceeded(
                f"chat already has {len(schedules)} schedules (max {max_per_chat})"
            )
        schedules[existing or schedule.name] = _serialize_schedule(schedule)
        self._save_raw(raw)

    def list_schedules(self, chat_id: int) -> list["Schedule"]:
        """The chat's schedules as :class:`~claude_tg.scheduler.Schedule` objects (``/schedules``).

        Read-only (never raises, RB1): a missing chat / non-dict ``schedules`` map / a
        malformed record all degrade — a bad record is skipped, an absent chat reads as ``[]``.
        Returns the schedules **as stored** (the persisted ``next_run`` round-trips faithfully)
        in insertion order. The runtime re-arms ``next_run`` from now via
        :meth:`rearm_all_schedules` at startup (RB3) — this accessor itself does not mutate
        time, so the persisted record is observable for tests + ``/schedules``.
        """
        chat = self._chat(self._load_raw(), chat_id)
        if chat is None:
            return []
        raw_schedules = chat.get("schedules")
        if not isinstance(raw_schedules, dict):
            return []
        out: list[Schedule] = []
        for name, record in raw_schedules.items():
            if not isinstance(name, str) or not isinstance(record, dict):
                continue
            schedule = _deserialize_schedule(name, chat_id, record)
            if schedule is not None:
                out.append(schedule)
        return out

    def get_schedule(self, chat_id: int, name: str) -> "Schedule | None":
        """The chat's schedule named ``name`` (case-insensitive), or ``None`` (read-only, RB1).

        Mirrors :meth:`get_macro`: a missing chat / schedule / malformed record reads as
        ``None``. Never raises.
        """
        chat = self._chat(self._load_raw(), chat_id)
        if chat is None:
            return None
        raw_schedules = chat.get("schedules")
        if not isinstance(raw_schedules, dict):
            return None
        key = _resolve_name(raw_schedules, name)
        if key is None:
            return None
        record = raw_schedules[key]
        if not isinstance(record, dict):
            return None
        return _deserialize_schedule(key, chat_id, record)

    def remove_schedule(self, chat_id: int, name: str) -> bool:
        """Delete the schedule ``name`` (case-insensitive); return whether one was removed.

        ``/unschedule <name>``. Never raises (RB1): a missing chat / schedule returns
        ``False`` (a clean "no such schedule" the bot reports). Persists only when something
        was actually removed.
        """
        raw = self._load_raw()
        chat = self._chat(raw, chat_id)
        if chat is None:
            return False
        schedules = chat.get("schedules")
        if not isinstance(schedules, dict):
            return False
        key = _resolve_name(schedules, name)
        if key is None:
            return False
        del schedules[key]
        self._save_raw(raw)
        return True

    def set_schedule_paused(self, chat_id: int, name: str, paused: bool) -> bool:
        """Pause/resume the schedule ``name`` (case-insensitive); return whether it was found.

        ``/pause`` · ``/resume``. Flips the persisted ``paused`` flag without disturbing
        ``next_run`` (so the cadence resumes where it left off). Never raises (RB1): a
        missing chat / schedule returns ``False`` (the bot reports "no such schedule");
        a found schedule is updated + persisted and returns ``True``. A flip to the value it
        already holds still persists + returns ``True`` (idempotent, simplest contract).
        """
        raw = self._load_raw()
        chat = self._chat(raw, chat_id)
        if chat is None:
            return False
        schedules = chat.get("schedules")
        if not isinstance(schedules, dict):
            return False
        key = _resolve_name(schedules, name)
        if key is None:
            return False
        record = schedules[key]
        if not isinstance(record, dict):
            return False
        record["paused"] = bool(paused)
        self._save_raw(raw)
        return True

    def all_schedules(self) -> list["Schedule"]:
        """EVERY chat's schedules as :class:`~claude_tg.scheduler.Schedule` objects (P14 T-FIRE).

        The flat, cross-chat view the firing driver consults each tick to find what is due
        (``list_schedules`` is per-chat; the driver fires for ALL chats). Each schedule carries
        its own ``chat_id`` (so the driver knows where to fire) and the persisted ``next_run``.
        Read-only / RB1: a missing/non-dict ``chats`` map reads as ``[]``; a malformed chat or
        schedule record is skipped (never raises). Returned in chat-then-insertion order (a
        stable, deterministic order so the driver's due-list + tests are reproducible).
        """
        raw = self._load_raw()
        chats = raw.get("chats")
        if not isinstance(chats, dict):
            return []
        out: list[Schedule] = []
        for chat_key, chat in chats.items():
            if not isinstance(chat, dict):
                continue
            try:
                chat_id = int(chat_key)
            except (TypeError, ValueError):
                continue  # a non-int chat key can't own a routable schedule (defensive)
            schedules = chat.get("schedules")
            if not isinstance(schedules, dict):
                continue
            for name, record in schedules.items():
                if not isinstance(name, str) or not isinstance(record, dict):
                    continue
                schedule = _deserialize_schedule(name, chat_id, record)
                if schedule is not None:
                    out.append(schedule)
        return out

    def rearm_all_schedules(self, now: float) -> None:
        """Re-arm EVERY chat's schedules' ``next_run`` to ``now + interval`` — RB3/RB6.

        Called ONCE at startup (by the future firing driver, T-FIRE) BEFORE the loop
        consults the schedules: it rewrites each schedule's ``next_run`` to ``now +
        interval_seconds`` and persists, so a fire window that elapsed while the bot was
        down is **never replayed** as a burst — every schedule fires at its NEXT interval
        from process start (the abandon-and-lazy-resume posture, design §5.5). ``now`` is
        injected (a float epoch) so it is deterministic in tests; the bot passes
        ``time.time()``. Paused schedules are re-armed too (so a later ``/resume`` resumes
        on a fresh cadence, never a stale stored fire). A malformed record is skipped
        defensively (RB1); persists once if anything changed. Never raises on a normal store.
        """
        raw = self._load_raw()
        chats = raw.get("chats")
        if not isinstance(chats, dict):
            return
        changed = False
        for chat in chats.values():
            if not isinstance(chat, dict):
                continue
            schedules = chat.get("schedules")
            if not isinstance(schedules, dict):
                continue
            for record in schedules.values():
                if not isinstance(record, dict):
                    continue
                interval = record.get("interval_seconds")
                if not isinstance(interval, int) or interval <= 0:
                    continue  # a malformed interval can't be re-armed; leave it for list to skip
                record["next_run"] = now + interval
                changed = True
        if changed:
            self._save_raw(raw)

    # ---- registry internals -----------------------------------------------

    @staticmethod
    def _chat(raw: dict, chat_id: int) -> dict | None:
        """The chat container for ``chat_id`` in ``raw``, or ``None`` if absent."""
        chats = raw.get("chats")
        if not isinstance(chats, dict):
            return None
        chat = chats.get(str(chat_id))
        return chat if isinstance(chat, dict) else None

    @classmethod
    def _resolve(
        cls, raw: dict, chat_id: int, name: str
    ) -> tuple[dict, dict, str]:
        """Resolve ``(chat, projects, stored_key)`` for ``name`` in ``raw``.

        Matches ``name`` case-insensitively against the chat's projects and
        returns the chat container, its projects dict, and the **stored** key.
        Raises :class:`UnknownProject` if the chat, its projects map, or a
        matching project is absent — the shared lookup for ``switch`` /
        ``remove`` / ``touch``.
        """
        chat = cls._chat(raw, chat_id)
        projects = chat.get("projects") if chat is not None else None
        if chat is None or not isinstance(projects, dict):
            raise UnknownProject(name)
        key = _resolve_name(projects, name)
        if key is None:
            raise UnknownProject(name)
        return chat, projects, key

    @staticmethod
    def _ensure_chat(raw: dict, chat_id: int) -> dict:
        """The chat container for ``chat_id``, creating an empty one if absent."""
        chats = raw.setdefault("chats", {})
        if not isinstance(chats, dict):
            chats = {}
            raw["chats"] = chats
        key = str(chat_id)
        chat = chats.get(key)
        if not isinstance(chat, dict):
            chat = {"active": None, "projects": {}}
            chats[key] = chat
        return chat


def _resolve_name(projects: dict, name: str) -> str | None:
    """Resolve ``name`` to its actual stored key, case-insensitively.

    Returns the real (as-created) key whose casefold matches ``name`` — so all
    registry operations match case-insensitively while storage/display keep the
    original case — or ``None`` if no project matches. Defensive against a
    hand-edited file with non-``str`` keys.
    """
    if not isinstance(name, str):
        return None
    target = name.casefold()
    for key in projects:
        if isinstance(key, str) and key.casefold() == target:
            return key
    return None


def _serialize_schedule(schedule: "Schedule") -> dict:
    """A JSON-serializable record for a :class:`~claude_tg.scheduler.Schedule` (P14 T2).

    The chat id is the dict KEY in the store (``chats[<id>]``) and the name is the
    schedules-map key, so neither is duplicated into the record body — the record carries
    the fields that vary per schedule. ``prompt`` is the operator-authored turn text stored
    verbatim (SB4: it is fired as an engine prompt, never a shell command). Pure (no I/O).
    """
    return {
        "interval_seconds": schedule.interval_seconds,
        "prompt": schedule.prompt,
        "project": schedule.project,
        "next_run": schedule.next_run,
        "paused": bool(schedule.paused),
        "created_at": schedule.created_at,
    }


def _deserialize_schedule(name: str, chat_id: int, record: dict) -> "Schedule | None":
    """Rebuild a :class:`~claude_tg.scheduler.Schedule` from a stored record, or ``None``.

    Defensive (RB1): a record missing/own-bad ``interval_seconds`` (the one field with no
    safe default — a schedule with no interval is meaningless) yields ``None`` so a
    hand-edited / corrupt entry is skipped by the caller rather than crashing the listing.
    A bad ``name`` (SB4) likewise yields ``None`` (the :class:`Schedule` constructor would
    raise — we catch it). Missing optional fields fall back to safe defaults
    (``project=None``, ``next_run=0.0``, ``paused=False``, ``created_at=0.0``). The import
    is local to break the module cycle (scheduler imports from this module).
    """
    from .scheduler import InvalidInterval, InvalidScheduleName, Schedule

    interval = record.get("interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        return None
    next_run = record.get("next_run")
    next_run_val = float(next_run) if isinstance(next_run, (int, float)) else 0.0
    created_at = record.get("created_at")
    created_val = float(created_at) if isinstance(created_at, (int, float)) else 0.0
    project = record.get("project")
    project_val = project if isinstance(project, str) and project else None
    prompt = record.get("prompt")
    prompt_val = prompt if isinstance(prompt, str) else ""
    try:
        return Schedule(
            name=name,
            interval_seconds=interval,
            prompt=prompt_val,
            chat_id=chat_id,
            next_run=next_run_val,
            project=project_val,
            paused=bool(record.get("paused")),
            created_at=created_val,
        )
    except (InvalidScheduleName, InvalidInterval):
        return None


def _active_project(chat: dict) -> dict | None:
    """The chat's active project record, or ``None`` if there is no usable one."""
    active = chat.get("active")
    if not isinstance(active, str):
        return None
    projects = chat.get("projects")
    if not isinstance(projects, dict):
        return None
    project = projects.get(active)
    return project if isinstance(project, dict) else None


def _new_project(cwd: str | None) -> dict:
    """A fresh project record (timestamps = now; ``session_id`` empty)."""
    ts = _now()
    return {
        "cwd": cwd,
        "session_id": None,
        "created_at": ts,
        "last_active": ts,
    }


def _migrate_v1_to_v2(data: dict) -> dict:
    """Wrap each legacy flat entry as a single active ``default`` project (D6).

    The v1 shape is ``{"<chat_id>": {"session_id"?, "cwd"?}}``. Each chat's entry
    becomes ``{"active": "default", "projects": {"default": {...}}}`` preserving
    ``session_id``+``cwd`` (``created_at``/``last_active`` = now). Entries that are
    not dicts are skipped (defensive). The result is a valid v2 doc; feeding it
    back through migration is a no-op (it now carries ``version``), so migration
    is idempotent and one-way.
    """
    ts = _now()
    chats: dict[str, dict] = {}
    for chat_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        project: dict = {
            "cwd": entry.get("cwd"),
            "session_id": entry.get("session_id"),
            "created_at": ts,
            "last_active": ts,
        }
        chats[str(chat_key)] = {
            "active": DEFAULT_PROJECT,
            "projects": {DEFAULT_PROJECT: project},
        }
    return {"version": SCHEMA_VERSION, "chats": chats}
