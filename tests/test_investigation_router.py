"""Tests for InvestigationRouter — heuristic routing to fast vs. deep path.

Testing strategy: construct Failure objects with controlled diff/log parameters
and assert on the route returned. The router is pure Python with no LLM calls,
so tests are fast and fully deterministic.
"""

from __future__ import annotations

from datetime import datetime

from agents.investigation_router import InvestigationRouter
from shared.models import DiffSummary, Failure, FailureDetail, PipelineInfo

# ── Failure builder ────────────────────────────────────────────────────────────

def _make_failure(
    files_changed: list[str] | None = None,
    lines_added: int = 5,
    lines_removed: int = 5,
    log_tail_length: int = 10,
) -> Failure:
    """Build a minimal Failure with controlled routing-relevant parameters."""
    return Failure(
        id="router_test",
        pipeline=PipelineInfo(
            provider="github_actions",
            repo="acme/backend",
            workflow="ci.yml",
            run_id="99001",
            branch="main",
            commit="abc1234",
            commit_message="test commit",
            author="dev@acme.com",
            triggered_at=datetime(2026, 1, 1),
            failed_at=datetime(2026, 1, 1),
            duration_seconds=30,
        ),
        failure=FailureDetail(
            job="test-job",
            step="pytest",
            exit_code=1,
            log_tail=[f"log line {i}" for i in range(log_tail_length)],
        ),
        diff_summary=DiffSummary(
            files_changed=files_changed or ["auth.py"],
            lines_added=lines_added,
            lines_removed=lines_removed,
            key_change="test change",
        ),
    )


# ── Default thresholds ─────────────────────────────────────────────────────────

class TestDefaultThresholds:
    def test_single_file_small_diff_routes_fast(self) -> None:
        router = InvestigationRouter()
        assert router.route(_make_failure(files_changed=["auth.py"])) == "fast"

    def test_three_files_routes_deep(self) -> None:
        """Exactly at the files threshold → deep."""
        router = InvestigationRouter()
        failure = _make_failure(files_changed=["a.py", "b.py", "c.py"])
        assert router.route(failure) == "deep"

    def test_two_files_routes_fast(self) -> None:
        """One below the files threshold → fast."""
        router = InvestigationRouter()
        failure = _make_failure(files_changed=["a.py", "b.py"])
        assert router.route(failure) == "fast"

    def test_large_diff_routes_deep(self) -> None:
        """lines_added + lines_removed >= 100 → deep."""
        router = InvestigationRouter()
        failure = _make_failure(lines_added=60, lines_removed=40)  # 100 total
        assert router.route(failure) == "deep"

    def test_just_below_diff_threshold_routes_fast(self) -> None:
        router = InvestigationRouter()
        failure = _make_failure(lines_added=50, lines_removed=49)  # 99 total
        assert router.route(failure) == "fast"

    def test_long_log_tail_routes_deep(self) -> None:
        """log_tail_length >= 40 → deep."""
        router = InvestigationRouter()
        failure = _make_failure(log_tail_length=40)
        assert router.route(failure) == "deep"

    def test_just_below_log_threshold_routes_fast(self) -> None:
        router = InvestigationRouter()
        failure = _make_failure(log_tail_length=39)
        assert router.route(failure) == "fast"

    def test_empty_files_list_routes_fast(self) -> None:
        """No files in diff → fast (zero is below threshold)."""
        router = InvestigationRouter()
        failure = _make_failure(files_changed=[])
        assert router.route(failure) == "fast"


# ── Threshold precedence ───────────────────────────────────────────────────────

class TestThresholdPrecedence:
    def test_files_takes_priority_over_diff(self) -> None:
        """Files threshold checked first — both would trigger deep."""
        router = InvestigationRouter()
        failure = _make_failure(
            files_changed=["a.py", "b.py", "c.py"],
            lines_added=200,
        )
        assert router.route(failure) == "deep"

    def test_diff_size_checked_when_files_below_threshold(self) -> None:
        """When files < threshold, diff size is still evaluated."""
        router = InvestigationRouter()
        failure = _make_failure(
            files_changed=["a.py"],      # below files threshold
            lines_added=100,             # at diff threshold
        )
        assert router.route(failure) == "deep"

    def test_log_length_checked_as_last_resort(self) -> None:
        """Log length triggers deep even when files and diff are small."""
        router = InvestigationRouter()
        failure = _make_failure(
            files_changed=["a.py"],
            lines_added=10,
            log_tail_length=40,
        )
        assert router.route(failure) == "deep"


# ── Custom thresholds ──────────────────────────────────────────────────────────

class TestCustomThresholds:
    def test_custom_files_threshold(self) -> None:
        router = InvestigationRouter(files_threshold=5)
        four_files = _make_failure(files_changed=["a.py", "b.py", "c.py", "d.py"])
        assert router.route(four_files) == "fast"

        five_files = _make_failure(files_changed=["a.py", "b.py", "c.py", "d.py", "e.py"])
        assert router.route(five_files) == "deep"

    def test_custom_diff_threshold(self) -> None:
        router = InvestigationRouter(diff_lines_threshold=50)
        # Use lines_removed=0 to isolate lines_added as the only diff signal
        assert router.route(_make_failure(lines_added=50, lines_removed=0)) == "deep"
        assert router.route(_make_failure(lines_added=49, lines_removed=0)) == "fast"

    def test_custom_log_threshold(self) -> None:
        router = InvestigationRouter(log_lines_threshold=10)
        assert router.route(_make_failure(log_tail_length=10)) == "deep"
        assert router.route(_make_failure(log_tail_length=9)) == "fast"

    def test_zero_thresholds_always_deep(self) -> None:
        """Setting all thresholds to 0 means every failure routes deep."""
        router = InvestigationRouter(
            files_threshold=0,
            diff_lines_threshold=0,
            log_lines_threshold=0,
        )
        assert router.route(_make_failure()) == "deep"
