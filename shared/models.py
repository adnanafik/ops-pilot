"""Pydantic models shared across all ops-pilot agents.

All inter-agent communication uses these typed models — no raw dicts.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Triage severity classification."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentStatus(str, Enum):
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
    production_impact: Optional[str] = Field(None, description="Description of production impact, if any")
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
    severity: Optional[Severity] = None
    affected_service: Optional[str] = None
    regression_introduced_in: Optional[str] = None
    production_impact: Optional[str] = None
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    slack_message: Optional[str] = None


class Scenario(BaseModel):
    """A complete pre-recorded or live scenario — the top-level data model."""

    id: str
    label: str
    pipeline: PipelineInfo
    failure: FailureDetail
    diff_summary: DiffSummary
    agents: list[AgentStep]
