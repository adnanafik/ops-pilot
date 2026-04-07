"""Tests for escalation — LOW-confidence escalation summary generation."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from shared.escalation import EscalationSummary, generate_escalation_summary
from shared.models import DiffSummary, Failure, FailureDetail, PipelineInfo, Severity, Triage


@pytest.fixture
def failure() -> Failure:
    return Failure(
        id="esc_test_001",
        pipeline=PipelineInfo(
            provider="github_actions",
            repo="acme/backend",
            workflow="ci.yml",
            run_id="12345",
            branch="main",
            commit="abc1234",
            commit_message="chore: bump deps",
            author="dev@acme.com",
            triggered_at=datetime(2026, 4, 6),
            failed_at=datetime(2026, 4, 6),
            duration_seconds=60,
        ),
        failure=FailureDetail(
            job="test-auth",
            step="pytest",
            exit_code=1,
            log_tail=["FAILED test_auth.py::test_login"],
        ),
        diff_summary=DiffSummary(
            files_changed=["auth.py"],
            lines_added=5,
            lines_removed=2,
            key_change="removed null guard",
        ),
    )


@pytest.fixture
def low_confidence_triage(failure: Failure) -> Triage:
    return Triage(
        failure_id=failure.id,
        output="Unable to determine root cause — Redis connection state unclear.",
        severity=Severity.HIGH,
        affected_service="auth-service",
        regression_introduced_in="abc1234",
        production_impact=None,
        fix_confidence="LOW",
        timestamp=datetime(2026, 4, 6),
    )


class TestEscalationSummary:
    def test_is_pydantic_model(self) -> None:
        from pydantic import BaseModel
        assert issubclass(EscalationSummary, BaseModel)

    def test_has_required_fields(self) -> None:
        s = EscalationSummary(
            failure_id="f1",
            tenant_id="acme",
            what_was_investigated="Auth failures",
            what_was_inconclusive="Redis state",
            recommended_next_step="Check Redis logs manually",
        )
        assert s.failure_id == "f1"
        assert s.tenant_id == "acme"


class TestGenerateEscalationSummary:
    def test_calls_backend_once(
        self, failure: Failure, low_confidence_triage: Triage
    ) -> None:
        backend = MagicMock()
        backend.complete.return_value = (
            '{"failure_id": "esc_test_001", "tenant_id": null, '
            '"what_was_investigated": "Auth failures", '
            '"what_was_inconclusive": "Redis state", '
            '"recommended_next_step": "Check Redis logs"}'
        )
        result = generate_escalation_summary(
            failure=failure,
            triage=low_confidence_triage,
            backend=backend,
            model="claude-haiku-4-5-20251001",
        )
        backend.complete.assert_called_once()
        assert isinstance(result, EscalationSummary)

    def test_returns_escalation_summary_with_correct_failure_id(
        self, failure: Failure, low_confidence_triage: Triage
    ) -> None:
        backend = MagicMock()
        backend.complete.return_value = (
            '{"failure_id": "esc_test_001", "tenant_id": null, '
            '"what_was_investigated": "Auth failures in test-auth step", '
            '"what_was_inconclusive": "Redis connection state", '
            '"recommended_next_step": "Check Redis logs"}'
        )
        result = generate_escalation_summary(
            failure=failure,
            triage=low_confidence_triage,
            backend=backend,
            model="claude-haiku-4-5-20251001",
        )
        assert result.failure_id == "esc_test_001"

    def test_backend_failure_returns_minimal_summary(
        self, failure: Failure, low_confidence_triage: Triage
    ) -> None:
        backend = MagicMock()
        backend.complete.side_effect = RuntimeError("API down")
        result = generate_escalation_summary(
            failure=failure,
            triage=low_confidence_triage,
            backend=backend,
            model="claude-haiku-4-5-20251001",
        )
        assert isinstance(result, EscalationSummary)
        assert result.failure_id == failure.id
        assert low_confidence_triage.output in result.what_was_investigated

    def test_invalid_json_from_backend_returns_minimal_summary(
        self, failure: Failure, low_confidence_triage: Triage
    ) -> None:
        backend = MagicMock()
        backend.complete.return_value = "not valid json {{"
        result = generate_escalation_summary(
            failure=failure,
            triage=low_confidence_triage,
            backend=backend,
            model="claude-haiku-4-5-20251001",
        )
        assert isinstance(result, EscalationSummary)
        assert result.failure_id == failure.id
