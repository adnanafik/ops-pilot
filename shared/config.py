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
from pydantic import BaseModel, Field, field_validator

from shared.models import Severity


class PipelineConfig(BaseModel):
    """Configuration for a single monitored repository."""

    repo: str = Field(..., description="owner/repo slug")
    slack_channel: str = Field(default="#platform-alerts")
    severity_threshold: Severity = Field(
        default=Severity.MEDIUM,
        description="Ignore failures below this severity level",
    )

    @field_validator("repo")
    @classmethod
    def repo_must_have_owner(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(f"repo must be 'owner/repo', got: {v!r}")
        return v


class OpsPilotConfig(BaseModel):
    """Top-level ops-pilot configuration."""

    # LLM
    anthropic_api_key: str = Field(default="")
    model: str = Field(default="claude-sonnet-4-6")

    # GitHub
    github_token: str = Field(default="")

    # Slack — bot token takes priority over webhook URL
    slack_bot_token: str = Field(default="")
    slack_webhook_url: str = Field(default="")

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
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)


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
        "github_token": os.environ.get("GITHUB_TOKEN", ""),
        "slack_bot_token": os.environ.get("SLACK_BOT_TOKEN", ""),
        "slack_webhook_url": os.environ.get("SLACK_WEBHOOK_URL", ""),
        "model": os.environ.get("CLAUDE_MODEL", ""),
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
