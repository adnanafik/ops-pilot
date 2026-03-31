"""Tests for TriageAgent."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.triage_agent import TriageAgent
from shared.models import AgentStatus, Failure, Severity


class TestTriageAgentDescribe:
    def test_describe_returns_string(self):
        agent = TriageAgent(client=MagicMock())
        assert isinstance(agent.describe(), str)
        assert len(agent.describe()) > 10

    def test_name_is_snake_case(self):
        agent = TriageAgent(client=MagicMock())
        assert agent.name == "triage_agent"


class TestTriageAgentRun:
    def test_run_returns_triage_model(self, sample_failure: Failure, mock_anthropic_client):
        """TriageAgent.run() should return a valid Triage model."""
        mock_anthropic_client.messages.create.return_value.content[0].text = json.dumps({
            "output": "Redis null guard removed causing AttributeError.",
            "severity": "high",
            "affected_service": "auth-service",
            "regression_introduced_in": "a3f21b7",
            "production_impact": "none",
            "fix_confidence": "HIGH",
        })

        agent = TriageAgent(client=mock_anthropic_client)
        triage = agent.run(sample_failure)

        assert triage.failure_id == sample_failure.id
        assert triage.severity == Severity.HIGH
        assert triage.fix_confidence == "HIGH"
        assert triage.affected_service == "auth-service"
        assert triage.regression_introduced_in == "a3f21b7"

    def test_run_sets_status_complete(self, sample_failure: Failure, mock_anthropic_client):
        """Status should be COMPLETE after a successful run."""
        mock_anthropic_client.messages.create.return_value.content[0].text = json.dumps({
            "output": "Root cause found.",
            "severity": "medium",
            "affected_service": "auth",
            "regression_introduced_in": "a3f21b7",
            "production_impact": "none",
            "fix_confidence": "HIGH",
        })

        agent = TriageAgent(client=mock_anthropic_client)
        agent.run(sample_failure)

        assert agent.status == AgentStatus.COMPLETE

    def test_run_handles_markdown_fenced_json(self, sample_failure: Failure, mock_anthropic_client):
        """TriageAgent should strip ```json fences from LLM response."""
        raw_with_fences = "```json\n" + json.dumps({
            "output": "Root cause.",
            "severity": "low",
            "affected_service": "auth",
            "regression_introduced_in": "a3f21b7",
            "production_impact": "none",
            "fix_confidence": "MEDIUM",
        }) + "\n```"

        mock_anthropic_client.messages.create.return_value.content[0].text = raw_with_fences
        agent = TriageAgent(client=mock_anthropic_client)
        triage = agent.run(sample_failure)

        assert triage.severity == Severity.LOW

    def test_run_raises_on_invalid_json(self, sample_failure: Failure, mock_anthropic_client):
        """TriageAgent.run() should raise ValueError on non-JSON LLM response."""
        mock_anthropic_client.messages.create.return_value.content[0].text = "I cannot determine the root cause."

        agent = TriageAgent(client=mock_anthropic_client)
        with pytest.raises(ValueError, match="non-JSON"):
            agent.run(sample_failure)

    def test_run_sets_status_failed_on_error(self, sample_failure: Failure, mock_anthropic_client):
        """Status should be FAILED when the run raises an exception."""
        mock_anthropic_client.messages.create.side_effect = Exception("API error")

        agent = TriageAgent(client=mock_anthropic_client)
        with pytest.raises(Exception, match="API error"):
            agent.run(sample_failure)

        assert agent.status == AgentStatus.FAILED

    def test_build_prompt_includes_key_fields(self, sample_failure: Failure):
        """The prompt should include repo, commit, and log tail content."""
        agent = TriageAgent(client=MagicMock())
        prompt = agent._build_prompt(sample_failure)

        assert sample_failure.pipeline.repo in prompt
        assert sample_failure.pipeline.commit in prompt
        assert sample_failure.diff_summary.key_change in prompt

    def test_all_severity_levels_accepted(self, sample_failure: Failure, mock_anthropic_client):
        """TriageAgent should correctly map all four severity levels."""
        for level in ["low", "medium", "high", "critical"]:
            mock_anthropic_client.messages.create.return_value.content[0].text = json.dumps({
                "output": "Root cause.",
                "severity": level,
                "affected_service": "svc",
                "regression_introduced_in": "abc1234",
                "production_impact": "none",
                "fix_confidence": "HIGH",
            })
            agent = TriageAgent(client=mock_anthropic_client)
            triage = agent.run(sample_failure)
            assert triage.severity.value == level


class TestTriageAgentParseResponse:
    def test_valid_json(self):
        agent = TriageAgent(client=MagicMock())
        data = agent._parse_response('{"key": "value"}')
        assert data == {"key": "value"}

    def test_strips_code_fences(self):
        agent = TriageAgent(client=MagicMock())
        fenced = "```json\n{\"key\": \"value\"}\n```"
        data = agent._parse_response(fenced)
        assert data == {"key": "value"}

    def test_raises_on_invalid(self):
        agent = TriageAgent(client=MagicMock())
        with pytest.raises(ValueError):
            agent._parse_response("not json at all")
