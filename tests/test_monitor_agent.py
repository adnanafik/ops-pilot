"""Tests for MonitorAgent."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.monitor_agent import MonitorAgent
from shared.models import AgentStatus, Failure
from shared.task_queue import TaskQueue


@pytest.fixture
def scenarios_dir(tmp_path):
    """A temp dir with two minimal scenario files."""
    s1 = {
        "id": "scenario_a",
        "label": "Scenario A",
        "pipeline": {
            "provider": "github_actions",
            "repo": "org/repo",
            "workflow": "ci.yml",
            "run_id": "123",
            "branch": "main",
            "commit": "abc1234",
            "commit_message": "fix: something",
            "author": "dev@example.com",
            "triggered_at": "2026-03-01T10:00:00Z",
            "failed_at": "2026-03-01T10:05:00Z",
            "duration_seconds": 300,
        },
        "failure": {
            "job": "test",
            "step": "run tests",
            "exit_code": 1,
            "log_tail": ["FAILED test_foo"],
        },
        "diff_summary": {
            "files_changed": ["foo.py"],
            "lines_added": 5,
            "lines_removed": 2,
            "key_change": "Changed foo function",
        },
        "agents": [],
    }
    s2 = {**s1, "id": "scenario_b", "label": "Scenario B"}
    (tmp_path / "a.json").write_text(json.dumps(s1))
    (tmp_path / "b.json").write_text(json.dumps(s2))
    return tmp_path


@pytest.fixture
def tmp_queue(tmp_path):
    return TaskQueue(base_dir=str(tmp_path / "tasks"))


class TestMonitorAgentDescribe:
    def test_describe_returns_string(self):
        agent = MonitorAgent(backend=MagicMock())
        assert isinstance(agent.describe(), str)

    def test_name(self):
        agent = MonitorAgent(backend=MagicMock())
        assert agent.name == "monitor_agent"


class TestMonitorAgentDemoMode:
    def test_loads_scenarios(self, scenarios_dir, tmp_queue):
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=True,
            task_queue=tmp_queue,
            scenarios_dir=str(scenarios_dir),
        )
        failures = agent.run()
        assert len(failures) == 2

    def test_returns_failure_models(self, scenarios_dir, tmp_queue):
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=True,
            task_queue=tmp_queue,
            scenarios_dir=str(scenarios_dir),
        )
        failures = agent.run()
        assert all(isinstance(f, Failure) for f in failures)

    def test_enqueues_tasks(self, scenarios_dir, tmp_queue):
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=True,
            task_queue=tmp_queue,
            scenarios_dir=str(scenarios_dir),
        )
        agent.run()
        tasks = tmp_queue.list_tasks()
        assert len(tasks) == 2

    def test_status_complete_after_run(self, scenarios_dir, tmp_queue):
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=True,
            task_queue=tmp_queue,
            scenarios_dir=str(scenarios_dir),
        )
        agent.run()
        assert agent.status == AgentStatus.COMPLETE

    def test_skips_malformed_json(self, tmp_path, tmp_queue):
        """Malformed scenario files should be skipped with a warning."""
        (tmp_path / "bad.json").write_text("not json {{{")
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=True,
            task_queue=tmp_queue,
            scenarios_dir=str(tmp_path),
        )
        failures = agent.run()
        assert len(failures) == 0

    def test_empty_scenarios_dir(self, tmp_path, tmp_queue):
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=True,
            task_queue=tmp_queue,
            scenarios_dir=str(tmp_path),
        )
        failures = agent.run()
        assert failures == []
        assert agent.status == AgentStatus.COMPLETE


class TestMonitorAgentLiveMode:
    def test_raises_without_repo(self, tmp_queue):
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=False,
            task_queue=tmp_queue,
        )
        with pytest.raises(ValueError, match="repo"):
            agent.run()

    def test_raises_without_token(self, tmp_queue):
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=False,
            repo="org/repo",
            github_token="",
            task_queue=tmp_queue,
        )
        with pytest.raises(ValueError, match="GITHUB_TOKEN"):
            agent.run()

    def test_status_failed_on_error(self, tmp_queue):
        agent = MonitorAgent(
            backend=MagicMock(),
            demo_mode=False,
            task_queue=tmp_queue,
        )
        with pytest.raises(Exception, match=""):
            agent.run()
        assert agent.status == AgentStatus.FAILED
