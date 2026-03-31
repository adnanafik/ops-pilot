"""Pydantic models shared across all ops-pilot agents.

All inter-agent communication uses these typed models — no raw dicts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    """Triage severity classification."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentStatus(StrEnum):
    """Execution status of an agent step."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class PipelineInfo(BaseModel):
    """Metadata about the CI/CD pipeline run that failed."""

    provider: str = Field(..., description="CI provider, e.g. 'github_actions'")
    repo: str = Field(..., description="owner/repo slug")
    workflow: str = Field(..., description="Workflow filename")
    run_id: str = Field(..., description="Provider-assigned run ID")
    branch: str
    commit: str = Field(..., description="Short SHA")
    commit_message: str
    author: str
    triggered_at: datetime
    failed_at: datetime
    duration_seconds: int


class FailureDetail(BaseModel):
    """Raw failure data extracted from CI logs."""

    job: str = Field(..., description="Job name that failed")
    step: str = Field(..., description="Step within the job")
    exit_code: int
    log_tail: list[str] = Field(..., description="Last N lines of the failing step log")


class DiffSummary(BaseModel):
    """Summary of code changes in the triggering commit."""

    files_changed: list[str]
    lines_added: int
    lines_removed: int
    key_change: str = Field(..., description="Human-readable description of the key diff")


class Failure(BaseModel):
    """Complete failure payload handed from MonitorAgent to TriageAgent."""

    id: str = Field(..., description="Unique failure ID, e.g. 'null_pointer_auth'")
    pipeline: PipelineInfo
    failure: FailureDetail
    diff_summary: DiffSummary


class Triage(BaseModel):
    """Root-cause analysis produced by TriageAgent."""

    failure_id: str
    output: str = Field(..., description="Narrative explanation of the root cause")
    severity: Severity
    affected_service: str
    regression_introduced_in: str = Field(..., description="Commit SHA where regression was introduced")
    production_impact: str | None = Field(None, description="Description of production impact, if any")
    fix_confidence: str = Field(..., description="HIGH / MEDIUM / LOW")
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Fix(BaseModel):
    """Fix suggestion and draft PR details produced by FixAgent."""

    failure_id: str
    output: str = Field(..., description="Summary of what was done")
    pr_title: str
    pr_body: str
    pr_url: str
    pr_number: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Alert(BaseModel):
    """Notification payload produced by NotifyAgent."""

    failure_id: str
    output: str = Field(..., description="Summary of notification sent")
    slack_message: str
    channel: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class AgentStep(BaseModel):
    """A single agent's recorded output — used in scenario files and streaming."""

    agent: str
    status: AgentStatus
    timestamp: datetime
    output: str
    # Optional fields that vary by agent
    severity: Severity | None = None
    affected_service: str | None = None
    regression_introduced_in: str | None = None
    production_impact: str | None = None
    pr_title: str | None = None
    pr_body: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    slack_message: str | None = None


class Scenario(BaseModel):
    """A complete pre-recorded or live scenario — the top-level data model."""

    id: str
    label: str
    pipeline: PipelineInfo
    failure: FailureDetail
    diff_summary: DiffSummary
    agents: list[AgentStep]
