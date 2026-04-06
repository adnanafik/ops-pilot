# tests/test_usage_tracker.py
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shared.usage_tracker import UsageTracker


@pytest.fixture
def tracker(tmp_path: Path) -> UsageTracker:
    return UsageTracker(base_dir=tmp_path)


def test_record_tokens_creates_file(tracker: UsageTracker, tmp_path: Path):
    tracker.record_tokens(500)
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (tmp_path / f"{date_str}.json").exists()


def test_record_tokens_accumulates(tracker: UsageTracker, tmp_path: Path):
    tracker.record_tokens(500)
    tracker.record_tokens(300)
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    data = json.loads((tmp_path / f"{date_str}.json").read_text())
    assert data["tokens_consumed"] == 800


def test_record_api_call_accumulates(tracker: UsageTracker, tmp_path: Path):
    tracker.record_api_call()
    tracker.record_api_call()
    tracker.record_api_call()
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    data = json.loads((tmp_path / f"{date_str}.json").read_text())
    assert data["api_calls"] == 3


def test_record_incident_accumulates(tracker: UsageTracker, tmp_path: Path):
    tracker.record_incident()
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    data = json.loads((tmp_path / f"{date_str}.json").read_text())
    assert data["incidents_resolved"] == 1


def test_counters_start_at_zero_for_new_day(tracker: UsageTracker, tmp_path: Path):
    """A missing file means zero counters — no cross-day contamination."""
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    assert not (tmp_path / f"{date_str}.json").exists()
    tracker.record_tokens(0)  # trigger write
    data = json.loads((tmp_path / f"{date_str}.json").read_text())
    assert data["tokens_consumed"] == 0
    assert data["api_calls"] == 0
    assert data["incidents_resolved"] == 0


def test_corrupted_file_resets_to_zero(tracker: UsageTracker, tmp_path: Path):
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    (tmp_path / f"{date_str}.json").write_text("not valid json{{{")
    tracker.record_tokens(100)  # should not raise
    data = json.loads((tmp_path / f"{date_str}.json").read_text())
    assert data["tokens_consumed"] == 100
