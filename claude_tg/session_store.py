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
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

#: The current on-disk schema version. A doc with this version is used as-is; a
#: doc with no version is treated as v1 and migrated; any other value is unknown
#: (future) and fails safe to empty.
SCHEMA_VERSION = 2

#: The project name a migrated v1 entry (and a fresh one-shot chat) is filed under.
DEFAULT_PROJECT = "default"


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
