"""Shared pytest fixtures for ops-pilot tests."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from shared.models import (
    DiffSummary,
    Failure,
    FailureDetail,
    Fix,
    PipelineInfo,
    Severity,
    Triage,
)


@pytest.fixture
def sample_failure() -> Failure:
    """A realistic Failure fixture based on the null_pointer_auth scenario."""
    return Failure(
        id="test_null_pointer",
        pipeline=PipelineInfo(
            provider="github_actions",
            repo="acme-corp/platform",
            workflow="ci.yml",
            run_id="8821043901",
            branch="feature/oauth-refresh-tokens",
            commit="a3f21b7",
            commit_message="feat: add refresh token rotation for OAuth sessions",
            author="dev@acme-corp.com",
            triggered_at=datetime(2026, 3, 28, 14, 22, 11),
            failed_at=datetime(2026, 3, 28, 14, 26, 43),
            duration_seconds=272,
        ),
        failure=FailureDetail(
            job="test-auth-service",
            step="Run pytest",
            exit_code=1,
            log_tail=[
                "FAILED tests/auth/test_token_refresh.py::test_refresh_token_rotation - NullPointerException",
                "AttributeError: 'NoneType' object has no attribute 'rotate_token'",
                "auth/session_manager.py:87: AttributeError",
                "ERROR: Redis connection returned None for key session:usr_abc123",
                "2 failed, 47 passed in 18.34s",
            ],
        ),
        diff_summary=DiffSummary(
            files_changed=["auth/session_manager.py", "auth/token_store.py"],
            lines_added=84,
            lines_removed=12,
            key_change="Removed null-guard on Redis hydration in SessionManager.__init__",
        ),
    )


@pytest.fixture
def sample_triage(sample_failure: Failure) -> Triage:
    """A realistic Triage fixture."""
    return Triage(
        failure_id=sample_failure.id,
        output=(
            "Root cause: commit a3f21b7 removed a null-guard in SessionManager.__init__. "
            "Redis returns None for uncached sessions — None now propagates to rotate_token() "
            "causing AttributeError."
        ),
        severity=Severity.HIGH,
        affected_service="auth-service",
        regression_introduced_in="a3f21b7",
        production_impact="none",
        fix_confidence="HIGH",
        timestamp=datetime(2026, 3, 28, 14, 26, 52),
    )


@pytest.fixture
def sample_fix(sample_failure: Failure) -> Fix:
    """A realistic Fix fixture."""
    return Fix(
        failure_id=sample_failure.id,
        output="Fix generated and draft PR opened.",
        pr_title="fix(auth): restore null-guard in SessionManager Redis hydration",
        pr_body="## Problem\nNull guard removed.\n\n## Fix\nRestore it.\n\n---\n*ops-pilot*",
        pr_url="https://github.com/acme-corp/platform/pull/1847",
        pr_number=1847,
        timestamp=datetime(2026, 3, 28, 14, 27, 8),
    )


@pytest.fixture
def mock_anthropic_client() -> MagicMock:
    """A mock Anthropic client that returns canned responses."""
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text='{"output": "test", "severity": "high", "affected_service": "auth", "regression_introduced_in": "a3f21b7", "production_impact": "none", "fix_confidence": "HIGH"}')]
    client.messages.create.return_value = response
    return client
