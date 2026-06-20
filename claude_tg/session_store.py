"""Optional on-disk persistence of per-chat Claude session id + working dir."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class JsonSessionStore:
    """Persists ``{chat_id: {"session_id": ..., "cwd": ...}}`` to a JSON file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:  # corrupt/unreadable -> start fresh, don't crash
            log.exception("could not read session store %s; ignoring", self.path)
        return {}

    def update(self, chat_id: int, session_id: str | None, cwd: str | None) -> None:
        data = self.load()
        key = str(chat_id)
        entry = data.get(key, {}) if isinstance(data.get(key), dict) else {}
        if session_id is not None:
            entry["session_id"] = session_id
        else:
            entry.pop("session_id", None)
        if cwd is not None:
            entry["cwd"] = cwd
        if entry:
            data[key] = entry
        else:
            data.pop(key, None)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic on the same filesystem
