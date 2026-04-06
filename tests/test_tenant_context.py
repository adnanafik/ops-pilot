# tests/test_tenant_context.py
from pathlib import Path

from shared.config import OpsPilotConfig
from shared.rate_limiter import RateLimiter
from shared.tenant_context import TenantContext, make_tenant_context
from shared.tool_permissions import ToolPermissions
from shared.usage_tracker import UsageTracker


def test_make_tenant_context_wires_tenant_id(tmp_path: Path):
    config = OpsPilotConfig(tenant_id="acme-corp")
    ctx = make_tenant_context(config, base_dir=tmp_path)
    assert ctx.tenant_id == "acme-corp"


def test_make_tenant_context_creates_permissions(tmp_path: Path):
    config = OpsPilotConfig(permissions={"allowed_tools": ["get_file"]})
    ctx = make_tenant_context(config, base_dir=tmp_path)
    assert isinstance(ctx.permissions, ToolPermissions)
    assert ctx.permissions.is_allowed("get_file") is True
    assert ctx.permissions.is_allowed("create_pr") is False


def test_make_tenant_context_creates_usage_tracker(tmp_path: Path):
    config = OpsPilotConfig()
    ctx = make_tenant_context(config, base_dir=tmp_path)
    assert isinstance(ctx.usage_tracker, UsageTracker)


def test_make_tenant_context_creates_rate_limiter(tmp_path: Path):
    config = OpsPilotConfig(rate_limits={"max_api_calls_per_hour": 50, "max_tokens_per_hour": 0})
    ctx = make_tenant_context(config, base_dir=tmp_path)
    assert isinstance(ctx.rate_limiter, RateLimiter)


def test_make_tenant_context_default_open_permissions(tmp_path: Path):
    """No allowed_tools configured → all tools permitted."""
    config = OpsPilotConfig()
    ctx = make_tenant_context(config, base_dir=tmp_path)
    assert ctx.permissions.is_allowed("any_tool") is True


def test_tenant_context_is_dataclass(tmp_path: Path):
    config = OpsPilotConfig(tenant_id="test")
    ctx = make_tenant_context(config, base_dir=tmp_path)
    assert isinstance(ctx, TenantContext)
    assert ctx.tenant_id == "test"
