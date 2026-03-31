"""Simple JSON-backed state persistence for ops-pilot.

Stores agent outputs keyed by failure ID so any agent can look up what
a previous agent produced without passing objects through function calls.
Thread-safe for single-process use (file write is atomic via temp-rename).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class StateStore:
    """Persistent key-value store backed by a single JSON file.

    Keys are namespaced as ``{failure_id}:{namespace}`` so the monitor,
    triage, fix, and notify outputs for a given failure don't collide.

    Example:
        store = StateStore()
        store.set("null_pointer_auth", "triage", {"severity": "high", ...})
        result = store.get("null_pointer_auth", "triage")
    """

    def __init__(self, path: str = "ops_pilot_state.json") -> None:
        """Initialize the store, loading existing state if the file exists."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def set(self, failure_id: str, namespace: str, value: dict) -> None:
        """Persist ``value`` under ``{failure_id}:{namespace}``."""
        key = f"{failure_id}:{namespace}"
        self._data[key] = value
        self._flush()

    def get(self, failure_id: str, namespace: str) -> dict | None:
        """Retrieve a previously stored value, or None if not found."""
        return self._data.get(f"{failure_id}:{namespace}")

    def get_all(self, failure_id: str) -> dict[str, dict]:
        """Return all namespaced values for a given failure ID."""
        prefix = f"{failure_id}:"
        return {
            k[len(prefix):]: v
            for k, v in self._data.items()
            if k.startswith(prefix)
        }

    def delete(self, failure_id: str, namespace: str) -> None:
        """Remove a stored value."""
        key = f"{failure_id}:{namespace}"
        self._data.pop(key, None)
        self._flush()

    def clear_failure(self, failure_id: str) -> None:
        """Remove all stored values for a given failure ID."""
        prefix = f"{failure_id}:"
        keys_to_delete = [k for k in self._data if k.startswith(prefix)]
        for k in keys_to_delete:
            del self._data[k]
        self._flush()

    def _flush(self) -> None:
        """Write state to disk atomically using a temp file + rename."""
        dir_path = self.path.parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=dir_path,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            json.dump(self._data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, self.path)
