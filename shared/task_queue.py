"""File-based task queue with advisory locking.

Inspired by the Anthropic multi-agent compiler article pattern:
tasks are represented as JSON files in ./current_tasks/. Claiming a task
is done by an atomic rename, which is safe on any POSIX filesystem and
works cleanly with git (no external dependencies, no database).

Usage:
    queue = TaskQueue()
    task_id = queue.enqueue({"failure_id": "null_pointer_auth", ...})
    task = queue.claim_next()          # returns Task or None
    queue.complete(task.id, result)
    queue.fail(task.id, "error msg")
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class TaskState(str, Enum):
    """Lifecycle states for a queued task."""

    PENDING = "pending"
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    """A single unit of work in the queue."""

    id: str
    state: TaskState
    payload: dict
    created_at: str
    claimed_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    worker_id: Optional[str] = field(default=None)

    def to_dict(self) -> dict:
        """Serialize the task to a plain dict for JSON storage."""
        return {
            "id": self.id,
            "state": self.state.value,
            "payload": self.payload,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
            "worker_id": self.worker_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """Deserialize a task from a plain dict."""
        return cls(
            id=data["id"],
            state=TaskState(data["state"]),
            payload=data["payload"],
            created_at=data["created_at"],
            claimed_at=data.get("claimed_at"),
            completed_at=data.get("completed_at"),
            result=data.get("result"),
            error=data.get("error"),
            worker_id=data.get("worker_id"),
        )


class TaskQueue:
    """File-based task queue backed by ./current_tasks/.

    Each task is a JSON file:
        current_tasks/<task_id>.pending.json   — awaiting pickup
        current_tasks/<task_id>.claimed.json   — being processed
        current_tasks/<task_id>.done.json      — finished
        current_tasks/<task_id>.failed.json    — errored

    Claiming is atomic: os.rename() on POSIX is atomic, so only one
    worker can claim a given task even under concurrent access.
    """

    def __init__(self, base_dir: str = "current_tasks") -> None:
        """Initialize the queue, creating the backing directory if needed."""
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(self, payload: dict) -> str:
        """Add a new task and return its ID."""
        task_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            state=TaskState.PENDING,
            payload=payload,
            created_at=datetime.utcnow().isoformat(),
        )
        path = self._path(task_id, TaskState.PENDING)
        path.write_text(json.dumps(task.to_dict(), indent=2))
        return task_id

    def claim_next(self, worker_id: Optional[str] = None) -> Optional[Task]:
        """Atomically claim the oldest pending task.

        Returns the Task on success, or None if the queue is empty.
        """
        pending_files = sorted(self.base_dir.glob("*.pending.json"))
        for pending_path in pending_files:
            task_id = pending_path.name.split(".")[0]
            claimed_path = self._path(task_id, TaskState.CLAIMED)
            try:
                os.rename(pending_path, claimed_path)
            except FileNotFoundError:
                # Another worker claimed it first — try the next one
                continue

            task = self._read(claimed_path)
            task.state = TaskState.CLAIMED
            task.claimed_at = datetime.utcnow().isoformat()
            task.worker_id = worker_id or f"worker-{os.getpid()}"
            claimed_path.write_text(json.dumps(task.to_dict(), indent=2))
            return task

        return None

    def complete(self, task_id: str, result: dict) -> None:
        """Mark a claimed task as successfully completed."""
        claimed_path = self._path(task_id, TaskState.CLAIMED)
        done_path = self._path(task_id, TaskState.DONE)

        task = self._read(claimed_path)
        task.state = TaskState.DONE
        task.completed_at = datetime.utcnow().isoformat()
        task.result = result

        claimed_path.write_text(json.dumps(task.to_dict(), indent=2))
        os.rename(claimed_path, done_path)

    def fail(self, task_id: str, error: str) -> None:
        """Mark a claimed task as failed."""
        claimed_path = self._path(task_id, TaskState.CLAIMED)
        failed_path = self._path(task_id, TaskState.FAILED)

        task = self._read(claimed_path)
        task.state = TaskState.FAILED
        task.completed_at = datetime.utcnow().isoformat()
        task.error = error

        claimed_path.write_text(json.dumps(task.to_dict(), indent=2))
        os.rename(claimed_path, failed_path)

    def list_tasks(self, state: Optional[TaskState] = None) -> list[Task]:
        """Return all tasks, optionally filtered by state."""
        pattern = f"*.{state.value}.json" if state else "*.json"
        return [
            self._read(p)
            for p in sorted(self.base_dir.glob(pattern))
        ]

    def get(self, task_id: str) -> Optional[Task]:
        """Retrieve a task by ID regardless of state."""
        for state in TaskState:
            path = self._path(task_id, state)
            if path.exists():
                return self._read(path)
        return None

    def _path(self, task_id: str, state: TaskState) -> Path:
        return self.base_dir / f"{task_id}.{state.value}.json"

    def _read(self, path: Path) -> Task:
        return Task.from_dict(json.loads(path.read_text()))
