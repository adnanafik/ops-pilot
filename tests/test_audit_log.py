"""Tests for AuditLog — structured per-day JSONL audit trail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.audit_log import AuditLog


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    return tmp_path / "audit"


class TestAuditLogRecord:
    def test_creates_daily_file_on_first_write(self, audit_dir: Path) -> None:
        log = AuditLog(base_dir=audit_dir)
        log.record(
            tenant_id="acme",
            actor="TriageAgent",
            tool_name="get_file",
            tool_input={"path": "auth.py"},
            tool_result="file contents",
            is_error=False,
            explanation=None,
        )
        daily_files = list(audit_dir.glob("*.jsonl"))
        assert len(daily_files) == 1

    def test_record_is_valid_json_line(self, audit_dir: Path) -> None:
        log = AuditLog(base_dir=audit_dir)
        log.record(
            tenant_id="acme",
            actor="FixAgent",
            tool_name="update_file",
            tool_input={"path": "auth.py", "content": "fixed"},
            tool_result="File updated successfully",
            is_error=False,
            explanation="Patching null-check at line 47.",
        )
        daily_file = next(audit_dir.glob("*.jsonl"))
        lines = [line for line in daily_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["tenant_id"] == "acme"
        assert entry["actor"] == "FixAgent"
        assert entry["tool_name"] == "update_file"
        assert entry["tool_input"] == {"path": "auth.py", "content": "fixed"}
        assert entry["tool_result"] == "File updated successfully"
        assert entry["is_error"] is False
        assert entry["explanation"] == "Patching null-check at line 47."
        assert "ts" in entry

    def test_explanation_none_for_read_only_tools(self, audit_dir: Path) -> None:
        log = AuditLog(base_dir=audit_dir)
        log.record(
            tenant_id="t1",
            actor="TriageAgent",
            tool_name="get_file",
            tool_input={},
            tool_result="content",
            is_error=False,
            explanation=None,
        )
        entry = json.loads(next(audit_dir.glob("*.jsonl")).read_text().strip())
        assert entry["explanation"] is None

    def test_multiple_records_append_to_same_daily_file(self, audit_dir: Path) -> None:
        log = AuditLog(base_dir=audit_dir)
        for i in range(3):
            log.record(
                tenant_id="t1",
                actor="TriageAgent",
                tool_name=f"tool_{i}",
                tool_input={},
                tool_result="ok",
                is_error=False,
                explanation=None,
            )
        daily_file = next(audit_dir.glob("*.jsonl"))
        lines = [line for line in daily_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 3
        for line in lines:
            json.loads(line)  # every line must be valid JSON

    def test_is_error_true_recorded(self, audit_dir: Path) -> None:
        log = AuditLog(base_dir=audit_dir)
        log.record(
            tenant_id="t1",
            actor="TriageAgent",
            tool_name="get_file",
            tool_input={},
            tool_result="Error: not found",
            is_error=True,
            explanation=None,
        )
        entry = json.loads(next(audit_dir.glob("*.jsonl")).read_text().strip())
        assert entry["is_error"] is True

    def test_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        # Point to a path where we cannot write (a file, not a dir)
        blocker = tmp_path / "audit"
        blocker.write_text("I am a file, not a directory")
        log = AuditLog(base_dir=blocker / "subdir")
        # Should not raise
        log.record(
            tenant_id="t1",
            actor="agent",
            tool_name="get_file",
            tool_input={},
            tool_result="ok",
            is_error=False,
            explanation=None,
        )
