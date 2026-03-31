"""Tests for NotifyAgent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.notify_agent import SEVERITY_EMOJI, NotifyAgent
from shared.models import AgentStatus, Failure, Severity


class TestNotifyAgentDescribe:
    def test_describe_returns_string(self):
        agent = NotifyAgent(backend=MagicMock())
        assert isinstance(agent.describe(), str)

    def test_name(self):
        agent = NotifyAgent(backend=MagicMock())
        assert agent.name == "notify_agent"


class TestNotifyAgentRun:
    def _make_agent(self, mock_backend, message="Team notified!"):
        mock_backend.complete.return_value = message
        return NotifyAgent(backend=mock_backend, demo_mode=True)

    def test_run_returns_alert_model(
        self,
        sample_failure: Failure,
        sample_triage,
        sample_fix,
        mock_backend,
    ):
        agent = self._make_agent(mock_backend, ":red_circle: alert message")
        alert = agent.run(sample_failure, sample_triage, sample_fix)

        assert alert.failure_id == sample_failure.id
        assert ":red_circle:" in alert.slack_message or len(alert.slack_message) > 0

    def test_run_sets_status_complete(
        self,
        sample_failure,
        sample_triage,
        sample_fix,
        mock_backend,
    ):
        agent = self._make_agent(mock_backend)
        agent.run(sample_failure, sample_triage, sample_fix)
        assert agent.status == AgentStatus.COMPLETE

    def test_run_sets_status_failed_on_error(
        self,
        sample_failure,
        sample_triage,
        sample_fix,
        mock_backend,
    ):
        mock_backend.complete.side_effect = Exception("LLM error")
        agent = NotifyAgent(backend=mock_backend, demo_mode=True)

        with pytest.raises(Exception, match="LLM error"):
            agent.run(sample_failure, sample_triage, sample_fix)

        assert agent.status == AgentStatus.FAILED

    def test_demo_mode_output_references_console(
        self,
        sample_failure,
        sample_triage,
        sample_fix,
        mock_backend,
    ):
        agent = self._make_agent(mock_backend)
        alert = agent.run(sample_failure, sample_triage, sample_fix)
        assert "console" in alert.output.lower() or "demo" in alert.output.lower()

    def test_run_stores_channel(
        self,
        sample_failure,
        sample_triage,
        sample_fix,
        mock_backend,
    ):
        mock_backend.complete.return_value = "msg"
        agent = NotifyAgent(
            backend=mock_backend,
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
