"""Tests for NotifyAgent."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.notify_agent import NotifyAgent, SEVERITY_EMOJI
from shared.models import AgentStatus, Failure, Severity


class TestNotifyAgentDescribe:
    def test_describe_returns_string(self):
        agent = NotifyAgent(client=MagicMock())
        assert isinstance(agent.describe(), str)

    def test_name(self):
        agent = NotifyAgent(client=MagicMock())
        assert agent.name == "notify_agent"


class TestNotifyAgentRun:
    def _make_agent(self, mock_client, message="Team notified!"):
        mock_client.messages.create.return_value.content[0].text = message
        return NotifyAgent(client=mock_client, demo_mode=True)

    def test_run_returns_alert_model(
        self,
        sample_failure: Failure,
        sample_triage,
        sample_fix,
        mock_anthropic_client,
    ):
        agent = self._make_agent(mock_anthropic_client, ":red_circle: alert message")
        alert = agent.run(sample_failure, sample_triage, sample_fix)

        assert alert.failure_id == sample_failure.id
        assert ":red_circle:" in alert.slack_message or len(alert.slack_message) > 0

    def test_run_sets_status_complete(
        self,
        sample_failure,
        sample_triage,
        sample_fix,
        mock_anthropic_client,
    ):
        agent = self._make_agent(mock_anthropic_client)
        agent.run(sample_failure, sample_triage, sample_fix)
        assert agent.status == AgentStatus.COMPLETE

    def test_run_sets_status_failed_on_error(
        self,
        sample_failure,
        sample_triage,
        sample_fix,
        mock_anthropic_client,
    ):
        mock_anthropic_client.messages.create.side_effect = Exception("LLM error")
        agent = NotifyAgent(client=mock_anthropic_client, demo_mode=True)

        with pytest.raises(Exception, match="LLM error"):
            agent.run(sample_failure, sample_triage, sample_fix)

        assert agent.status == AgentStatus.FAILED

    def test_demo_mode_output_references_console(
        self,
        sample_failure,
        sample_triage,
        sample_fix,
        mock_anthropic_client,
    ):
        agent = self._make_agent(mock_anthropic_client)
        alert = agent.run(sample_failure, sample_triage, sample_fix)
        assert "console" in alert.output.lower() or "demo" in alert.output.lower()

    def test_run_stores_channel(
        self,
        sample_failure,
        sample_triage,
        sample_fix,
        mock_anthropic_client,
    ):
        mock_anthropic_client.messages.create.return_value.content[0].text = "msg"
        agent = NotifyAgent(
            client=mock_anthropic_client,
            demo_mode=True,
            channel="#my-channel",
        )
        alert = agent.run(sample_failure, sample_triage, sample_fix)
        assert alert.channel == "#my-channel"


class TestSeverityEmoji:
    def test_all_severities_have_emoji(self):
        for severity in Severity:
            assert severity in SEVERITY_EMOJI

    def test_high_is_red(self):
        assert "red" in SEVERITY_EMOJI[Severity.HIGH]

    def test_critical_is_rotating_light(self):
        assert "rotating" in SEVERITY_EMOJI[Severity.CRITICAL]
