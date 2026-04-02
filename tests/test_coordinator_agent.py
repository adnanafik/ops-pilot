"""Tests for CoordinatorAgent and SpawnWorkerTool.

Testing strategy:
  - CoordinatorAgent tests follow the same mock-backend pattern as TriageAgent.
  - SpawnWorkerTool tests use a simple inner-loop mock to verify the worker
    dispatch mechanism without making real LLM calls.
  - Worker isolation is verified by checking that workers have no SpawnWorkerTool.

Key constraint: CoordinatorAgent.run() calls asyncio.run() internally.
The test framework (pytest-asyncio in AUTO mode) runs each test in its own
event loop, so we call run() synchronously — same pattern as TriageAgent tests.
"""

from __future__ import annotations

import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.coordinator_agent import CoordinatorAgent
from agents.tools.coordinator_tools import (
    SpawnWorkerTool,
    WorkerFinding,
    WorkerLoop,
    build_workers,
)
from shared.agent_loop import AgentLoop, LoopOutcome, LoopResult, ToolContext
from shared.models import DiffSummary, Failure, FailureDetail, PipelineInfo, Triage

# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_failure() -> Failure:
    return Failure(
        id="coord_test_001",
        pipeline=PipelineInfo(
            provider="github_actions",
            repo="acme/backend",
            workflow="ci.yml",
            run_id="55001",
            branch="main",
            commit="abc1234",
            commit_message="feat: add refresh tokens",
            author="dev@acme.com",
            triggered_at=datetime(2026, 1, 1),
            failed_at=datetime(2026, 1, 1),
            duration_seconds=60,
        ),
        failure=FailureDetail(
            job="test-auth",
            step="pytest",
            exit_code=1,
            log_tail=["FAILED test_auth.py::test_refresh - AttributeError"],
        ),
        diff_summary=DiffSummary(
            files_changed=["auth.py", "tokens.py", "session.py"],
            lines_added=50,
            lines_removed=20,
            key_change="removed null guard",
        ),
    )


def _make_triage_json() -> str:
    return (
        '{"output": "Null guard removed", "severity": "high",'
        ' "affected_service": "auth", "regression_introduced_in": "abc1234",'
        ' "production_impact": "none", "fix_confidence": "HIGH"}'
    )


def _text_block(text: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="text", text=text)


def _tool_block(
    name: str, input_dict: dict, block_id: str = "toolu_01"
) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="tool_use", id=block_id, name=name, input=input_dict)


def _response(*blocks: types.SimpleNamespace) -> types.SimpleNamespace:
    return types.SimpleNamespace(content=list(blocks))


def _end_turn(text: str = "Root cause: null guard removed.") -> types.SimpleNamespace:
    return _response(_text_block(text))


def _make_backend(
    tool_responses: list[types.SimpleNamespace],
    extraction_json: str | None = None,
) -> MagicMock:
    backend = MagicMock()
    backend.complete_with_tools.side_effect = tool_responses
    backend.complete.return_value = extraction_json or _make_triage_json()
    return backend


# ── CoordinatorAgent basic behaviour ──────────────────────────────────────────

class TestCoordinatorAgentDescribe:
    def test_describe_returns_string(self) -> None:
        agent = CoordinatorAgent()
        assert isinstance(agent.describe(), str)
        assert len(agent.describe()) > 0

    def test_name_is_snake_case(self) -> None:
        assert CoordinatorAgent().name == "coordinator_agent"


class TestCoordinatorAgentRun:
    def _mock_workers(self) -> dict[str, WorkerLoop]:
        """Build worker dict with AsyncMock loops — no real LLM calls."""
        mock_result = LoopResult(
            outcome=LoopOutcome.COMPLETED,
            model_confidence="HIGH",
            extracted=WorkerFinding(summary="found root cause", key_observations=[], confidence="HIGH"),
            turns_used=1,
            failed_tools=[],
            last_assistant_text="null guard removed at line 42",
        )
        return {
            name: WorkerLoop(name=name, loop=MagicMock(run=AsyncMock(return_value=mock_result)))
            for name in ["log_worker", "source_worker", "diff_worker"]
        }

    def test_run_returns_triage_model(self, sample_failure: Failure) -> None:
        """Coordinator spawns three workers (mocked), synthesises, returns Triage."""
        backend = _make_backend(
            tool_responses=[
                _response(
                    _tool_block("spawn_worker", {"worker": "log_worker", "task": "check logs"}, "t1"),
                    _tool_block("spawn_worker", {"worker": "source_worker", "task": "read source"}, "t2"),
                    _tool_block("spawn_worker", {"worker": "diff_worker", "task": "read diff"}, "t3"),
                ),
                _end_turn("Root cause: null guard removed in auth.py line 42."),
            ],
        )
        agent = CoordinatorAgent(backend=backend, model="claude-sonnet-4-6")

        # patch in coordinator_agent's namespace (where build_workers was imported)
        with patch("agents.coordinator_agent.build_workers", return_value=self._mock_workers()):
            result = agent.run(sample_failure)

        assert isinstance(result, Triage)
        assert result.failure_id == sample_failure.id

    def test_run_sets_status_complete(self, sample_failure: Failure) -> None:
        backend = _make_backend(tool_responses=[_end_turn()])
        agent = CoordinatorAgent(backend=backend, model="claude-sonnet-4-6")

        with patch("agents.coordinator_agent.build_workers", return_value=self._mock_workers()):
            from shared.models import AgentStatus
            agent.run(sample_failure)
            assert agent.status == AgentStatus.COMPLETE

    def test_run_sets_status_failed_on_error(self, sample_failure: Failure) -> None:
        backend = MagicMock()
        backend.complete_with_tools.side_effect = RuntimeError("API down")
        agent = CoordinatorAgent(backend=backend, model="claude-sonnet-4-6")

        with patch("agents.coordinator_agent.build_workers", return_value=self._mock_workers()):
            from shared.models import AgentStatus
            with pytest.raises(RuntimeError):
                agent.run(sample_failure)
            assert agent.status == AgentStatus.FAILED


# ── SpawnWorkerTool ────────────────────────────────────────────────────────────

class TestSpawnWorkerTool:
    @pytest.fixture
    def mock_ctx(self, sample_failure: Failure) -> ToolContext:
        return ToolContext(provider=None, failure=sample_failure)

    def _make_worker_loop(self, finding_text: str = "found root cause") -> WorkerLoop:
        """Build a WorkerLoop backed by a mock inner loop."""
        inner_backend = MagicMock()
        inner_backend.complete_with_tools.side_effect = [_end_turn(finding_text)]
        inner_backend.complete.return_value = (
            '{"summary": "' + finding_text + '", "key_observations": [], "confidence": "HIGH"}'
        )
        loop = AgentLoop(
            tools=[],
            backend=inner_backend,
            domain_system_prompt="test worker",
            response_model=WorkerFinding,
            model="claude-sonnet-4-6",
            max_turns=3,
        )
        return WorkerLoop(name="test_worker", loop=loop)

    async def test_returns_worker_findings(self, mock_ctx: ToolContext) -> None:
        worker = self._make_worker_loop("null guard removed at line 42")
        tool = SpawnWorkerTool({"log_worker": worker})
        result = await tool.execute({"worker": "log_worker", "task": "check logs"}, mock_ctx)

        assert not result.is_error
        assert "null guard removed at line 42" in result.content
        assert "log_worker" in result.content

    async def test_unknown_worker_returns_error(self, mock_ctx: ToolContext) -> None:
        tool = SpawnWorkerTool({"log_worker": self._make_worker_loop()})
        result = await tool.execute({"worker": "nonexistent", "task": "go"}, mock_ctx)

        assert result.is_error
        assert "nonexistent" in result.content
        assert "log_worker" in result.content  # lists available workers

    async def test_result_includes_confidence(self, mock_ctx: ToolContext) -> None:
        worker = self._make_worker_loop()
        tool = SpawnWorkerTool({"diff_worker": worker})
        result = await tool.execute({"worker": "diff_worker", "task": "read diff"}, mock_ctx)

        assert "confidence=" in result.content

    def test_schema_enum_matches_registered_workers(self) -> None:
        tool = SpawnWorkerTool({"log_worker": MagicMock(), "diff_worker": MagicMock()})
        schema = tool.input_schema
        assert set(schema["properties"]["worker"]["enum"]) == {"diff_worker", "log_worker"}


# ── Worker isolation ───────────────────────────────────────────────────────────

class TestWorkerIsolation:
    def test_workers_do_not_have_spawn_worker_tool(self) -> None:
        """Workers must not be able to recursively spawn more workers."""
        backend = MagicMock()
        workers = build_workers(backend, "claude-sonnet-4-6", worker_max_turns=3)

        for name, worker in workers.items():
            tool_names = {t.name for t in worker.loop._tools.values()}
            assert "spawn_worker" not in tool_names, (
                f"Worker '{name}' has spawn_worker — workers must not be able to "
                "recursively spawn workers"
            )

    def test_log_worker_has_log_and_file_tools(self) -> None:
        workers = build_workers(MagicMock(), "claude-sonnet-4-6")
        tool_names = set(workers["log_worker"].loop._tools.keys())
        assert "get_more_log" in tool_names
        assert "get_file" in tool_names

    def test_source_worker_has_file_and_tree_tools(self) -> None:
        workers = build_workers(MagicMock(), "claude-sonnet-4-6")
        tool_names = set(workers["source_worker"].loop._tools.keys())
        assert "get_file" in tool_names
        assert "get_repo_tree" in tool_names

    def test_diff_worker_has_diff_and_file_tools(self) -> None:
        workers = build_workers(MagicMock(), "claude-sonnet-4-6")
        tool_names = set(workers["diff_worker"].loop._tools.keys())
        assert "get_commit_diff" in tool_names
        assert "get_file" in tool_names

    def test_log_worker_cannot_access_diff_tool(self) -> None:
        """log_worker must not have get_commit_diff — stays focused on logs."""
        workers = build_workers(MagicMock(), "claude-sonnet-4-6")
        tool_names = set(workers["log_worker"].loop._tools.keys())
        assert "get_commit_diff" not in tool_names

    def test_diff_worker_cannot_access_log_tool(self) -> None:
        """diff_worker must not have get_more_log — stays focused on diffs."""
        workers = build_workers(MagicMock(), "claude-sonnet-4-6")
        tool_names = set(workers["diff_worker"].loop._tools.keys())
        assert "get_more_log" not in tool_names

    def test_build_workers_returns_all_three(self) -> None:
        workers = build_workers(MagicMock(), "claude-sonnet-4-6")
        assert set(workers.keys()) == {"log_worker", "source_worker", "diff_worker"}
