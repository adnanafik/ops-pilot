# tests/test_tenant_config.py
from shared.config import OpsPilotConfig


def test_default_tenant_id():
    config = OpsPilotConfig()
    assert config.tenant_id == "default"


def test_tenant_id_from_dict():
    config = OpsPilotConfig(tenant_id="acme-corp")
    assert config.tenant_id == "acme-corp"


def test_default_permissions_allow_all():
    config = OpsPilotConfig()
    assert config.permissions.allowed_tools == []


def test_permissions_from_dict():
    config = OpsPilotConfig(permissions={"allowed_tools": ["get_file", "get_more_log"]})
    assert config.permissions.allowed_tools == ["get_file", "get_more_log"]


def test_default_rate_limits_are_zero():
    config = OpsPilotConfig()
    assert config.rate_limits.max_api_calls_per_hour == 0
    assert config.rate_limits.max_tokens_per_hour == 0


def test_rate_limits_from_dict():
    config = OpsPilotConfig(rate_limits={"max_api_calls_per_hour": 100, "max_tokens_per_hour": 500000})
    assert config.rate_limits.max_api_calls_per_hour == 100
    assert config.rate_limits.max_tokens_per_hour == 500000
