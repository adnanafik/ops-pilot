"""ops-pilot configuration loader.

Reads ops-pilot.yml (or ops-pilot.yaml), substitutes ${ENV_VAR} references,
and validates the result with Pydantic. Environment variables always win over
file values — set them in .env or Docker environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from shared.models import Severity

_KNOWN_PROVIDERS = {"github_actions", "gitlab_ci", "jenkins"}
_KNOWN_CODE_HOSTS = {"github", "gitlab"}


class PipelineConfig(BaseModel):
    """Configuration for a single monitored repository."""

    repo: str = Field(..., description="owner/repo slug (GitHub/GitLab) or code-host repo for Jenkins")
    slack_channel: str = Field(default="#platform-alerts")
    severity_threshold: Severity = Field(
        default=Severity.MEDIUM,
        description="Ignore failures below this severity level",
    )

    # CI provider
    provider: str = Field(
        default="github_actions",
        description="CI system to poll: github_actions | gitlab_ci | jenkins",
    )
    base_branch: str = Field(
        default="main",
        description="Branch that fix PRs/MRs are opened against",
    )

    # Per-pipeline credential overrides (fall back to global config when empty)
    github_token: str = Field(default="", description="Override global GITHUB_TOKEN")
    gitlab_token: str = Field(default="", description="Override global GITLAB_TOKEN")

    # GitLab-specific
    gitlab_url: Optional[str] = Field(
        default=None,
        description="GitLab base URL — omit for gitlab.com, set for self-hosted instances",
    )

    # Jenkins-specific
    jenkins_url: Optional[str] = Field(
        default=None,
        description="Jenkins server base URL, e.g. 'https://ci.example.com'",
    )
    jenkins_job: Optional[str] = Field(
        default=None,
        description="Jenkins job path, e.g. 'folder/my-job'. Defaults to repo value.",
    )
    code_host: Optional[str] = Field(
        default=None,
        description="For Jenkins: code hosting provider for git/PR ops — github | gitlab",
    )

    @field_validator("repo")
    @classmethod
    def repo_must_have_owner(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(f"repo must be 'owner/repo', got: {v!r}")
        return v

    @field_validator("provider")
    @classmethod
    def provider_must_be_known(cls, v: str) -> str:
        if v not in _KNOWN_PROVIDERS:
            raise ValueError(f"provider must be one of {_KNOWN_PROVIDERS}, got: {v!r}")
        return v

    @model_validator(mode="after")
    def jenkins_requires_code_host(self) -> "PipelineConfig":
        if self.provider == "jenkins" and not self.code_host:
            raise ValueError("'code_host' is required when provider is 'jenkins' (github or gitlab)")
        if self.code_host and self.code_host not in _KNOWN_CODE_HOSTS:
            raise ValueError(f"code_host must be one of {_KNOWN_CODE_HOSTS}, got: {self.code_host!r}")
        return self


_KNOWN_LLM_PROVIDERS = {"anthropic", "bedrock", "vertex_ai"}


class OpsPilotConfig(BaseModel):
    """Top-level ops-pilot configuration."""

    # LLM provider selection
    llm_provider: str = Field(
        default="anthropic",
        description="LLM backend: 'anthropic' (direct API key) or 'bedrock' (AWS Bedrock)",
    )

    # Anthropic direct API
    anthropic_api_key: str = Field(default="")

    # Model — for Bedrock use the full model ID e.g.
    # 'anthropic.claude-sonnet-4-5-20251001-v1:0' or a cross-region
    # inference ID like 'us.anthropic.claude-sonnet-4-5-20251001-v1:0'
    model: str = Field(default="claude-sonnet-4-6")

    # AWS Bedrock
    aws_region: str = Field(
        default="",
        description="AWS region for Bedrock, e.g. 'us-east-1'. Falls back to AWS_DEFAULT_REGION.",
    )

    # Google Cloud Vertex AI
    gcp_project: str = Field(
        default="",
        description="GCP project ID for Vertex AI, e.g. 'my-project-123'.",
    )
    gcp_region: str = Field(
        default="",
        description="GCP region for Vertex AI, e.g. 'us-east5'. Defaults to 'us-east5'.",
    )

    @field_validator("log_level")
    @classmethod
    def log_level_must_be_valid(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}, got: {v!r}")
        return upper

    @field_validator("llm_provider")
    @classmethod
    def llm_provider_must_be_known(cls, v: str) -> str:
        if v not in _KNOWN_LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {_KNOWN_LLM_PROVIDERS}, got: {v!r}")
        return v

    # GitHub
    github_token: str = Field(default="")

    # GitLab
    gitlab_token: str = Field(default="")

    # Jenkins
    jenkins_user: str = Field(default="")
    jenkins_token: str = Field(default="", description="Jenkins API token (not password)")

    # Slack — bot token takes priority over webhook URL
    slack_bot_token: str = Field(default="")
    slack_webhook_url: str = Field(default="")

    # Logging
    log_level: str = Field(
        default="WARNING",
        description="Python log level for all agents: DEBUG | INFO | WARNING | ERROR",
    )

    # Watcher
    poll_interval_seconds: int = Field(default=30, ge=10)
    state_file: str = Field(default="ops_pilot_state.json")

    # Pipelines
    pipelines: list[PipelineConfig] = Field(default_factory=list)

    @property
    def has_slack(self) -> bool:
        return bool(self.slack_bot_token or self.slack_webhook_url)

    @property
    def has_github(self) -> bool:
        return bool(self.github_token)

    @property
    def has_gitlab(self) -> bool:
        return bool(self.gitlab_token)

    @property
    def has_jenkins(self) -> bool:
        return bool(self.jenkins_user and self.jenkins_token)

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_bedrock(self) -> bool:
        return self.llm_provider == "bedrock"

    @property
    def has_vertex(self) -> bool:
        return self.llm_provider == "vertex_ai"


def _substitute_env(value: object) -> object:
    """Recursively substitute ${VAR} references with environment variable values."""
    if isinstance(value, str):
        def replacer(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return re.sub(r"\$\{(\w+)\}", replacer, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(item) for item in value]
    return value


def load_config(path: Optional[str] = None) -> OpsPilotConfig:
    """Load and validate config from file + environment.

    Search order for config file:
      1. ``path`` argument if provided
      2. ``OPS_PILOT_CONFIG`` environment variable
      3. ``ops-pilot.yml`` in the current directory
      4. ``ops-pilot.yaml`` in the current directory
      5. Empty config (env vars only)

    Environment variables always override file values.
    """
    config_path = _find_config_file(path)
    raw: dict = {}

    if config_path:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        raw = _substitute_env(raw)  # type: ignore[assignment]

    # Environment variables override everything
    env_overrides = {
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "llm_provider":      os.environ.get("LLM_PROVIDER", ""),
        "aws_region":        os.environ.get("AWS_REGION", "") or os.environ.get("AWS_DEFAULT_REGION", ""),
        "gcp_project":       os.environ.get("GCP_PROJECT", ""),
        "gcp_region":        os.environ.get("GCP_REGION", ""),
        "github_token":      os.environ.get("GITHUB_TOKEN", ""),
        "gitlab_token":      os.environ.get("GITLAB_TOKEN", ""),
        "jenkins_user":      os.environ.get("JENKINS_USER", ""),
        "jenkins_token":     os.environ.get("JENKINS_TOKEN", ""),
        "slack_bot_token":   os.environ.get("SLACK_BOT_TOKEN", ""),
        "slack_webhook_url": os.environ.get("SLACK_WEBHOOK_URL", ""),
        "model":             os.environ.get("CLAUDE_MODEL", ""),
        "log_level":         os.environ.get("LOG_LEVEL", ""),
    }
    for key, val in env_overrides.items():
        if val:
            raw[key] = val

    return OpsPilotConfig(**raw)


def _find_config_file(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        return Path(explicit)
    env_path = os.environ.get("OPS_PILOT_CONFIG")
    if env_path:
        return Path(env_path)
    for name in ("ops-pilot.yml", "ops-pilot.yaml"):
        p = Path(name)
        if p.exists():
            return p
    return None
