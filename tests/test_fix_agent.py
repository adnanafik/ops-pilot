"""Tests for FixAgent."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.fix_agent import FixAgent
from shared.models import AgentStatus, Failure, Triage


class TestFixAgentDescribe:
    def test_describe_returns_string(self):
        agent = FixAgent(client=MagicMock())
        assert isinstance(agent.describe(), str)

    def test_name_is_snake_case(self):
        agent = FixAgent(client=MagicMock())
        assert agent.name == "fix_agent"


class TestFixAgentRun:
    def _make_agent(self, mock_client, pr_response: dict | None = None) -> FixAgent:
        if pr_response is None:
            pr_response = {
                "pr_title": "fix(auth): restore null-guard",
                "pr_body": "## Problem\nNull guard removed.\n\n## Fix\nRestore it.\n\n---\n*ops-pilot*",
                "summary": "Restores null-guard in SessionManager.",
            }
        mock_client.messages.create.return_value.content[0].text = json.dumps(pr_response)
        return FixAgent(client=mock_client, demo_mode=True)

    def test_run_returns_fix_model(
        self,
        sample_failure: Failure,
        sample_triage: Triage,
        mock_anthropic_client,
    ):
        """FixAgent.run() should return a Fix model with PR details."""
        agent = self._make_agent(mock_anthropic_client)
        fix = agent.run(sample_failure, sample_triage)

        assert fix.failure_id == sample_failure.id
        assert fix.pr_title == "fix(auth): restore null-guard"
        assert fix.pr_number > 0
        assert "github.com" in fix.pr_url

    def test_run_sets_status_complete(
        self,
        sample_failure: Failure,
        sample_triage: Triage,
        mock_anthropic_client,
    ):
        agent = self._make_agent(mock_anthropic_client)
        agent.run(sample_failure, sample_triage)
        assert agent.status == AgentStatus.COMPLETE

    def test_run_sets_status_failed_on_error(
        self,
        sample_failure: Failure,
        sample_triage: Triage,
        mock_anthropic_client,
    ):
        mock_anthropic_client.messages.create.side_effect = Exception("LLM error")
        agent = FixAgent(client=mock_anthropic_client, demo_mode=True)

        with pytest.raises(Exception, match="LLM error"):
            agent.run(sample_failure, sample_triage)

        assert agent.status == AgentStatus.FAILED

    def test_demo_mode_uses_mock_pr_url(
        self,
        sample_failure: Failure,
        sample_triage: Triage,
        mock_anthropic_client,
    ):
        """In demo mode, the PR URL should reference the failure's repo."""
        agent = self._make_agent(mock_anthropic_client)
        fix = agent.run(sample_failure, sample_triage)

        assert sample_failure.pipeline.repo in fix.pr_url

    def test_run_raises_on_invalid_json_response(
        self,
        sample_failure: Failure,
        sample_triage: Triage,
        mock_anthropic_client,
    ):
        mock_anthropic_client.messages.create.return_value.content[0].text = "plain text, not json"
        agent = FixAgent(client=mock_anthropic_client, demo_mode=True)

        with pytest.raises(ValueError, match="non-JSON"):
            agent.run(sample_failure, sample_triage)


class TestFixAgentParseResponse:
    def test_valid_json(self):
        agent = FixAgent(client=MagicMock())
        data = agent._parse_response('{"pr_title": "fix: thing"}')
        assert data == {"pr_title": "fix: thing"}

    def test_strips_code_fences(self):
        agent = FixAgent(client=MagicMock())
        fenced = "```json\n{\"pr_title\": \"fix: thing\"}\n```"
        data = agent._parse_response(fenced)
        assert data["pr_title"] == "fix: thing"

    def test_raises_on_bad_json(self):
        agent = FixAgent(client=MagicMock())
        with pytest.raises(ValueError):
            agent._parse_response("not valid json")


class TestMockPrUrl:
    def test_mock_pr_url_contains_repo(self, sample_failure: Failure):
        agent = FixAgent(client=MagicMock())
        url, number = agent._mock_pr_url(sample_failure)
        assert sample_failure.pipeline.repo in url
        assert isinstance(number, int)
        assert number > 0
