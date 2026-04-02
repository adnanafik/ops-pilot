"""Tests for ToolRegistry — permission-tier filtering and registration.

Testing strategy: use lightweight in-process Tool subclasses rather than the
real tools. This keeps tests fast and isolated from provider concerns. We test
the registry in isolation; tests for how agents use the registry live in the
agent test files.
"""

from __future__ import annotations

import pytest

from shared.agent_loop import Permission, Tool, ToolContext, ToolResult  # noqa: F401
from shared.tool_registry import ToolRegistry

# ── Minimal test tools at each permission tier ────────────────────────────────

def _make_tool(name: str, perm: Permission) -> Tool:
    """Factory: returns a minimal Tool instance at the given permission tier."""

    class _T(Tool):
        @property
        def name(self) -> str:
            return name

        @property
        def description(self) -> str:
            return f"Test tool {name}"

        @property
        def input_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        @property
        def permission(self) -> Permission:
            return perm

        async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(f"{name} executed")

    return _T()


@pytest.fixture
def populated_registry() -> ToolRegistry:
    """Registry with one tool at every permission tier."""
    reg = ToolRegistry()
    reg.register(_make_tool("read_tool", Permission.READ_ONLY))
    reg.register(_make_tool("write_tool", Permission.WRITE))
    reg.register(_make_tool("dangerous_tool", Permission.DANGEROUS))
    reg.register(_make_tool("confirm_tool", Permission.REQUIRES_CONFIRMATION))
    return reg


# ── Registration ──────────────────────────────────────────────────────────────

class TestRegistration:
    def test_register_adds_tool(self) -> None:
        reg = ToolRegistry()
        reg.register(_make_tool("my_tool", Permission.READ_ONLY))
        assert "my_tool" in reg.all_tool_names()

    def test_duplicate_name_raises(self) -> None:
        reg = ToolRegistry()
        reg.register(_make_tool("dup", Permission.READ_ONLY))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_make_tool("dup", Permission.WRITE))

    def test_len_reflects_registrations(self) -> None:
        reg = ToolRegistry()
        assert len(reg) == 0
        reg.register(_make_tool("a", Permission.READ_ONLY))
        reg.register(_make_tool("b", Permission.WRITE))
        assert len(reg) == 2

    def test_registration_order_preserved(self) -> None:
        reg = ToolRegistry()
        names = ["first", "second", "third"]
        for n in names:
            reg.register(_make_tool(n, Permission.READ_ONLY))
        result_names = [t.name for t in reg.get_tools(max_permission=Permission.READ_ONLY)]
        assert result_names == names


# ── Watermark filtering ───────────────────────────────────────────────────────

class TestWatermarkFiltering:
    def test_read_only_watermark_returns_only_read_tools(
        self, populated_registry: ToolRegistry
    ) -> None:
        tools = populated_registry.get_tools(max_permission=Permission.READ_ONLY)
        names = {t.name for t in tools}
        assert names == {"read_tool"}

    def test_write_watermark_returns_read_and_write_tools(
        self, populated_registry: ToolRegistry
    ) -> None:
        tools = populated_registry.get_tools(max_permission=Permission.WRITE)
        names = {t.name for t in tools}
        assert names == {"read_tool", "write_tool"}

    def test_dangerous_and_confirm_excluded_by_default(
        self, populated_registry: ToolRegistry
    ) -> None:
        """DANGEROUS and REQUIRES_CONFIRMATION never appear in watermark queries."""
        for max_perm in (Permission.READ_ONLY, Permission.WRITE):
            tools = populated_registry.get_tools(max_permission=max_perm)
            names = {t.name for t in tools}
            assert "dangerous_tool" not in names
            assert "confirm_tool" not in names

    def test_include_dangerous_adds_non_tiered_tools(
        self, populated_registry: ToolRegistry
    ) -> None:
        tools = populated_registry.get_tools(
            max_permission=Permission.READ_ONLY,
            include_dangerous=True,
        )
        names = {t.name for t in tools}
        assert "dangerous_tool" in names
        assert "confirm_tool" in names
        # Watermark still applies to tiered tools
        assert "write_tool" not in names

    def test_include_dangerous_with_write_watermark(
        self, populated_registry: ToolRegistry
    ) -> None:
        tools = populated_registry.get_tools(
            max_permission=Permission.WRITE,
            include_dangerous=True,
        )
        names = {t.name for t in tools}
        assert names == {"read_tool", "write_tool", "dangerous_tool", "confirm_tool"}

    def test_empty_registry_returns_empty_list(self) -> None:
        reg = ToolRegistry()
        assert reg.get_tools(max_permission=Permission.WRITE) == []

    def test_returns_list_not_dict(self, populated_registry: ToolRegistry) -> None:
        result = populated_registry.get_tools()
        assert isinstance(result, list)

    def test_default_max_permission_is_read_only(
        self, populated_registry: ToolRegistry
    ) -> None:
        """Calling get_tools() with no args defaults to READ_ONLY watermark."""
        default_result = populated_registry.get_tools()
        explicit_result = populated_registry.get_tools(max_permission=Permission.READ_ONLY)
        assert {t.name for t in default_result} == {t.name for t in explicit_result}


# ── all_tool_names ─────────────────────────────────────────────────────────────

class TestAllToolNames:
    def test_returns_all_registered_names(
        self, populated_registry: ToolRegistry
    ) -> None:
        names = set(populated_registry.all_tool_names())
        assert names == {"read_tool", "write_tool", "dangerous_tool", "confirm_tool"}

    def test_empty_registry_returns_empty_list(self) -> None:
        assert ToolRegistry().all_tool_names() == []
