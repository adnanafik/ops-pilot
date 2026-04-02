"""Tests for TriageAgent.

These tests cover the TriageAgent's public interface and the helpers it still
owns. The core loop logic is tested separately in test_agent_loop.py — there
is no reason to duplicate those assertions here.

Phase 1 changes: TriageAgent now uses AgentLoop internally. Tests that called
private methods (_build_prompt, _parse_response) have been updated since those
methods moved into the loop infrastructure. The public interface (run() returns
a Triage, status is updated) is unchanged.
"""

from __future__ import annotations

import json
import types
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agents.triage_agent import TriageAgent
from shared.models import AgentStatus, Failure, Severity


# ── Mock backend helpers ───────────────────────────────────────────────────────

def _end_turn_response(text: str = "Investigation complete.") -> types.SimpleNamespace:
    """A complete_with_tools response with no tool calls — loop exits immediately."""
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)]
    )


def _make_backend(
    extraction_json: str | None = None,
    tool_responses: list | None = None,
) -> MagicMock:
    """Build a mock backend suitable for TriageAgent tests.

    complete_with_tools() returns an end_turn response (no tool calls) so
    the loop exits after one turn. complete() returns extraction_json so
    the structured extraction succeeds.
    """
    backend = MagicMock()
    responses = tool_responses or [_end_turn_response()]
    backend.complete_with_tools.side_effect = responses
    if extraction_json is not None:
        backend.complete.return_value = extraction_json
    return backend


def _valid_extraction(
    severity: str = "high",
    confidence: str = "HIGH",
    service: str = "auth-service",
) -> str:
    return json.dumps({
        "failure_id": "test_null_pointer",
        "output": "Redis null guard removed causing AttributeError.",
        "severity": severity,
        "affected_service": service,
        "regression_introduced_in": "a3f21b7",
        "production_impact": "none",
        "fix_confidence": confidence,
        "timestamp": "2026-01-01T00:00:00",
    })


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestTriageAgentDescribe:
    def test_describe_returns_string(self) -> None:
        agent = TriageAgent(backend=MagicMock())
        assert isinstance(agent.describe(), str)
        assert len(agent.describe()) > 10

    def test_name_is_snake_case(self) -> None:
        agent = TriageAgent(backend=MagicMock())
        assert agent.name == "triage_agent"


class TestTriageAgentRun:
    def test_run_returns_triage_model(self, sample_failure: Failure) -> None:
        """run() should return a valid Triage with correct fields."""
        backend = _make_backend(extraction_json=_valid_extraction())
        agent = TriageAgent(backend=backend)
        triage = agent.run(sample_failure)

        assert triage.failure_id == sample_failure.id
        assert triage.severity == Severity.HIGH
        assert triage.fix_confidence == "HIGH"
        assert triage.affected_service == "auth-service"

    def test_run_sets_status_complete(self, sample_failure: Failure) -> None:
        """Status should be COMPLETE after a successful run."""
        backend = _make_backend(extraction_json=_valid_extraction())
        agent = TriageAgent(backend=backend)
        agent.run(sample_failure)

        assert agent.status == AgentStatus.COMPLETE

    def test_run_sets_status_failed_on_backend_error(self, sample_failure: Failure) -> None:
        """Status should be FAILED when the backend raises."""
        backend = MagicMock()
        backend.complete_with_tools.side_effect = Exception("API down")
        agent = TriageAgent(backend=backend)

        with pytest.raises(Exception, match="API down"):
            agent.run(sample_failure)

        assert agent.status == AgentStatus.FAILED

    def test_all_severity_levels_accepted(self, sample_failure: Failure) -> None:
        """TriageAgent should correctly map all four severity levels."""
        for level in ["low", "medium", "high", "critical"]:
            backend = _make_backend(extraction_json=_valid_extraction(severity=level))
            agent = TriageAgent(backend=backend)
            triage = agent.run(sample_failure)
            assert triage.severity.value == level

    def test_low_confidence_returned_when_extraction_fails(
        self, sample_failure: Failure
    ) -> None:
        """If extraction produces invalid JSON, run() returns a LOW confidence fallback.

        The pipeline should route this to escalation rather than crashing.
        """
        backend = _make_backend(extraction_json="not valid json {{")
        agent = TriageAgent(backend=backend)
        triage = agent.run(sample_failure)

        assert triage.fix_confidence == "LOW"
        assert triage.failure_id == sample_failure.id


class TestBuildInitialMessage:
    def test_includes_key_failure_fields(self, sample_failure: Failure) -> None:
        """The initial message should contain repo, commit, and log tail."""
        msg = TriageAgent._build_initial_message(sample_failure)

        assert sample_failure.pipeline.repo in msg
        assert sample_failure.pipeline.commit in msg
        assert sample_failure.diff_summary.key_change in msg

    def test_includes_failure_id(self, sample_failure: Failure) -> None:
        """failure_id must be in the initial message so extraction can populate it."""
        msg = TriageAgent._build_initial_message(sample_failure)
        assert sample_failure.id in msg

    def test_includes_log_tail(self, sample_failure: Failure) -> None:
        """Log tail lines should appear in the initial message."""
        msg = TriageAgent._build_initial_message(sample_failure)
        for line in sample_failure.failure.log_tail:
            assert line in msg
