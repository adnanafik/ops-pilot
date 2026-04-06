"""TenantContext — bundles all per-deployment runtime state.

Constructed once at startup by make_tenant_context() and injected into
agents alongside LLMBackend. Agents hold a TenantContext; they do not
import individual subsystems directly.

Future DB upgrade: replace UsageTracker and RateLimiter implementations
inside make_tenant_context() only. Zero changes to agents or AgentLoop.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared.rate_limiter import RateLimiter
from shared.tool_permissions import ToolPermissions
from shared.usage_tracker import UsageTracker


@dataclass
class TenantContext:
    """Runtime state for one deployment instance.

    Attributes:
        tenant_id:     Identifier for this deployment — stamped on all records.
        permissions:   Tool allowlist — checked before every tool execution.
        usage_tracker: Daily usage counters — records tokens, API calls, incidents.
        rate_limiter:  Sliding-window rate limiter — prevents runaway LLM costs.
    """

    tenant_id: str
    permissions: ToolPermissions
    usage_tracker: UsageTracker
    rate_limiter: RateLimiter


def make_tenant_context(
    config: object,
    base_dir: Path | str = Path("usage"),
) -> TenantContext:
    """Construct a TenantContext from an OpsPilotConfig instance.

    This is the single wiring point — the only place that knows how to
    build all four subsystems from config. Replace implementations here
    for the future DB-backed upgrade.

    Args:
        config:   OpsPilotConfig instance (typed as object to avoid
                  a circular import — duck-typed access is safe here).
        base_dir: Base directory for usage files. Defaults to ./usage.

    Returns:
        Fully wired TenantContext ready for injection into agents.
    """
    base_dir = Path(base_dir)
    permissions = ToolPermissions(
        allowed_tools=config.permissions.allowed_tools,  # type: ignore[attr-defined]
    )
    usage_tracker = UsageTracker(base_dir=base_dir)
    rate_limiter = RateLimiter(
        max_api_calls_per_hour=config.rate_limits.max_api_calls_per_hour,  # type: ignore[attr-defined]
        max_tokens_per_hour=config.rate_limits.max_tokens_per_hour,  # type: ignore[attr-defined]
        base_dir=base_dir,
    )
    return TenantContext(
        tenant_id=config.tenant_id,  # type: ignore[attr-defined]
        permissions=permissions,
        usage_tracker=usage_tracker,
        rate_limiter=rate_limiter,
    )
