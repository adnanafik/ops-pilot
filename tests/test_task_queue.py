"""Tests for the file-based TaskQueue."""

from __future__ import annotations

import pytest

from shared.task_queue import Task, TaskQueue, TaskState


@pytest.fixture
def tmp_queue(tmp_path):
    """A TaskQueue backed by a temp directory."""
    return TaskQueue(base_dir=str(tmp_path / "tasks"))


class TestEnqueue:
    def test_enqueue_returns_id(self, tmp_queue):
        task_id = tmp_queue.enqueue({"key": "value"})
        assert isinstance(task_id, str)
        assert len(task_id) > 0

    def test_enqueue_creates_pending_file(self, tmp_queue):
        task_id = tmp_queue.enqueue({"key": "value"})
        pending_files = list(tmp_queue.base_dir.glob("*.pending.json"))
        assert len(pending_files) == 1
        assert task_id in pending_files[0].name

    def test_enqueued_task_has_correct_payload(self, tmp_queue):
        payload = {"failure_id": "test_123", "repo": "acme/platform"}
        task_id = tmp_queue.enqueue(payload)
        task = tmp_queue.get(task_id)
        assert task is not None
        assert task.payload == payload


class TestClaimNext:
    def test_claim_returns_task(self, tmp_queue):
        tmp_queue.enqueue({"x": 1})
        task = tmp_queue.claim_next()
        assert task is not None
        assert task.state == TaskState.CLAIMED

    def test_claim_returns_none_when_empty(self, tmp_queue):
        task = tmp_queue.claim_next()
        assert task is None

    def test_claim_moves_file_to_claimed(self, tmp_queue):
        tmp_queue.enqueue({"x": 1})
        task = tmp_queue.claim_next()
        assert task is not None
        claimed_files = list(tmp_queue.base_dir.glob("*.claimed.json"))
        pending_files = list(tmp_queue.base_dir.glob("*.pending.json"))
        assert len(claimed_files) == 1
        assert len(pending_files) == 0

    def test_claim_sets_worker_id(self, tmp_queue):
        tmp_queue.enqueue({"x": 1})
        task = tmp_queue.claim_next(worker_id="worker-test")
        assert task is not None
        assert task.worker_id == "worker-test"

    def test_claim_only_claims_one_task(self, tmp_queue):
        tmp_queue.enqueue({"x": 1})
        tmp_queue.enqueue({"x": 2})
        task = tmp_queue.claim_next()
        assert task is not None
        # Second claim should still find a task
        task2 = tmp_queue.claim_next()
        assert task2 is not None
        assert task.id != task2.id


class TestComplete:
    def test_complete_moves_to_done(self, tmp_queue):
        task_id = tmp_queue.enqueue({"x": 1})
        tmp_queue.claim_next()
        tmp_queue.complete(task_id, {"result": "ok"})

        done_files = list(tmp_queue.base_dir.glob("*.done.json"))
        assert len(done_files) == 1

    def test_complete_stores_result(self, tmp_queue):
        task_id = tmp_queue.enqueue({"x": 1})
        tmp_queue.claim_next()
        tmp_queue.complete(task_id, {"output": "analysis done"})

        task = tmp_queue.get(task_id)
        assert task is not None
        assert task.result == {"output": "analysis done"}
        assert task.state == TaskState.DONE


class TestFail:
    def test_fail_moves_to_failed(self, tmp_queue):
        task_id = tmp_queue.enqueue({"x": 1})
        tmp_queue.claim_next()
        tmp_queue.fail(task_id, "something went wrong")

        failed_files = list(tmp_queue.base_dir.glob("*.failed.json"))
        assert len(failed_files) == 1

    def test_fail_stores_error(self, tmp_queue):
        task_id = tmp_queue.enqueue({"x": 1})
        tmp_queue.claim_next()
        tmp_queue.fail(task_id, "LLM timeout")

        task = tmp_queue.get(task_id)
        assert task is not None
        assert task.error == "LLM timeout"
        assert task.state == TaskState.FAILED


class TestListTasks:
    def test_list_all_tasks(self, tmp_queue):
        tmp_queue.enqueue({"x": 1})
        tmp_queue.enqueue({"x": 2})
        tasks = tmp_queue.list_tasks()
        assert len(tasks) == 2

    def test_list_filtered_by_state(self, tmp_queue):
        tmp_queue.enqueue({"x": 1})
        tmp_queue.enqueue({"x": 2})
        tmp_queue.claim_next()

        pending = tmp_queue.list_tasks(state=TaskState.PENDING)
        claimed = tmp_queue.list_tasks(state=TaskState.CLAIMED)
        assert len(pending) == 1
        assert len(claimed) == 1


class TestGet:
    def test_get_returns_none_for_unknown_id(self, tmp_queue):
        assert tmp_queue.get("nonexistent-id") is None

    def test_get_finds_task_in_any_state(self, tmp_queue):
        task_id = tmp_queue.enqueue({"x": 1})
        task = tmp_queue.get(task_id)
        assert task is not None
        assert task.id == task_id


class TestTaskSerialization:
    def test_roundtrip(self):
        task = Task(
            id="abc",
            state=TaskState.PENDING,
            payload={"k": "v"},
            created_at="2026-01-01T00:00:00",
        )
        restored = Task.from_dict(task.to_dict())
        assert restored.id == task.id
        assert restored.state == task.state
        assert restored.payload == task.payload
