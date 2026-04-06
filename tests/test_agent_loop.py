"""Tests for AgentLoop — the core agentic execution engine.

Testing strategy: we never make real API calls. The backend is a MagicMock
whose complete_with_tools() return value we control turn by turn. Tools are
simple in-process classes that record their calls.

The tests assert on three levels:
  1. Outcome — did the loop exit for the right reason?
  2. History structure — is the conversation list shaped correctly?
     (The API will reject malformed history; we catch this in tests, not prod.)
  3. Behavior — were tools called? Were errors fed back? Was extraction called?

Key helper: SimpleNamespace lets us build fake Anthropic SDK response objects
that satisfy `block.type` attribute access without defining full dataclasses.
MagicMock would work but its auto-generated attributes make assertions harder
to read.
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from shared.agent_loop import (
    AgentLoop,
    LoopOutcome,
    Permission,
    Tool,
    ToolContext,
    ToolResult,
)
from shared.exceptions import RateLimitExceeded
from shared.models import DiffSummary, Failure, FailureDetail, PipelineInfo
from shared.rate_limiter import RateLimiter
from shared.tenant_context import TenantContext
from shared.tool_permissions import ToolPermissions
from shared.usage_tracker import UsageTracker

# ── Minimal Pydantic model used as response_model in tests ────────────────────
# Using a simple two-field model keeps tests focused on loop behaviour,
# not on the Triage extraction schema.

class Finding(BaseModel):
    """Minimal structured output for AgentLoop tests."""
    summary: str = ""
    confidence: str = "HIGH"


# ── Response builder helpers ───────────────────────────────────────────────────
# These build fake Anthropic SDK Message objects using SimpleNamespace.
#
# Why SimpleNamespace and not MagicMock?
#   AgentLoop._parse_response does `block.type` — attribute access.
#   MagicMock().type returns another MagicMock (truthy but not "text" or
#   "tool_use"), so the if/elif branches never match correctly.
#   SimpleNamespace gives us literal attribute values with zero boilerplate.

def _text_block(text: str = "I have enough information now.") -> types.SimpleNamespace:
    return types.SimpleNamespace(type="text", text=text)


def _tool_block(
    name: str,
    input_dict: dict,
    block_id: str = "toolu_01",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="tool_use", id=block_id, name=name, input=input_dict)


def _response(*blocks: types.SimpleNamespace) -> types.SimpleNamespace:
    """Wrap content blocks in a fake Anthropic Message object."""
    return types.SimpleNamespace(content=list(blocks))


def _end_turn(text: str = "Investigation complete.") -> types.SimpleNamespace:
    """A response with only a text block — model is done, no tool calls."""
    return _response(_text_block(text))


# ── Controllable test tools ────────────────────────────────────────────────────

class RecordingTool(Tool):
    """Tool that records calls and returns a canned response.

    Used to assert the loop called the right tool with the right input.
    """

    def __init__(
        self,
        tool_name: str = "test_tool",
        response: str = "tool result",
        is_error: bool = False,
    ) -> None:
        self._name = tool_name
        self._response = response
        self._is_error = is_error
        self.calls: list[dict] = []  # input dicts from each call

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"A test tool named {self._name}"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(input))
        return ToolResult(content=self._response, is_error=self._is_error)


class RaisingTool(Tool):
    """Tool that always raises an exception.

    Used to verify the loop catches tool errors and feeds them back to the
    model rather than crashing the whole investigation.
    """

    @property
    def name(self) -> str:
        return "raising_tool"

    @property
    def description(self) -> str:
        return "A tool that raises"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("simulated tool failure")


class HangingTool(Tool):
    """Tool that sleeps forever — used to test the per-tool timeout."""

    @property
    def name(self) -> str:
        return "hanging_tool"

    @property
    def description(self) -> str:
        return "A tool that hangs"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(999)
        return ToolResult("never reached")


# ── Shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def minimal_failure() -> Failure:
    """Minimal Failure for ToolContext — fields the tools read at runtime."""
    return Failure(
        id="test_failure_001",
        pipeline=PipelineInfo(
            provider="github_actions",
            repo="acme/backend",
            workflow="ci.yml",
            run_id="12345",
            branch="main",
            commit="abc1234",
            commit_message="test commit",
            author="dev@acme.com",
            triggered_at=datetime(2026, 1, 1),
            failed_at=datetime(2026, 1, 1),
            duration_seconds=60,
        ),
        failure=FailureDetail(
            job="test-job",
            step="pytest",
            exit_code=1,
            log_tail=["FAILED test_auth.py::test_login"],
        ),
        diff_summary=DiffSummary(
            files_changed=["auth.py"],
            lines_added=5,
            lines_removed=2,
            key_change="removed null guard",
        ),
    )


@pytest.fixture
def tool_ctx(minimal_failure: Failure) -> ToolContext:
    return ToolContext(provider=None, failure=minimal_failure)


def _make_backend(
    tool_responses: list[types.SimpleNamespace],
    extraction_json: str = '{"summary": "root cause found", "confidence": "HIGH"}',
) -> MagicMock:
    """Build a mock backend with pre-set tool response turns.

    complete_with_tools() returns tool_responses[0] on the first call,
    tool_responses[1] on the second, etc. complete() (used for extraction)
    returns extraction_json.
    """
    backend = MagicMock()
    backend.complete_with_tools.side_effect = tool_responses
    backend.complete.return_value = extraction_json
    return backend


def _make_loop(
    tools: list[Tool] | None = None,
    backend: MagicMock | None = None,
    max_turns: int = 5,
    tool_timeout: float = 5.0,
) -> AgentLoop[Finding]:
    if tools is None:
        tools = [RecordingTool()]
    if backend is None:
        backend = _make_backend([_end_turn()])
    return AgentLoop(
        tools=tools,
        backend=backend,
        domain_system_prompt="You are a test agent.",
        response_model=Finding,
        model="claude-sonnet-4-6",
        max_turns=max_turns,
        tool_timeout=tool_timeout,
    )


# ── Tests: happy path ──────────────────────────────────────────────────────────

class TestHappyPath:
    async def test_immediate_end_turn(self, tool_ctx: ToolContext) -> None:
        """Loop exits COMPLETED on the first turn when model returns no tool calls."""
        loop = _make_loop(backend=_make_backend([_end_turn()]))
        result = await loop.run(messages=[{"role": "user", "content": "analyse this"}], ctx=tool_ctx)

        assert result.outcome == LoopOutcome.COMPLETED
        assert result.turns_used == 1
        assert result.failed_tools == []

    async def test_two_tool_calls_then_end_turn(self, tool_ctx: ToolContext) -> None:
        """Happy path: model calls a tool twice, then ends. COMPLETED after 3 turns."""
        tool = RecordingTool("get_file", response="file contents here")
        backend = _make_backend([
            _response(_tool_block("get_file", {"path": "auth.py"}, "t1")),
            _response(_tool_block("get_file", {"path": "token.py"}, "t2")),
            _end_turn("Found root cause in auth.py line 42."),
        ])
        loop = _make_loop(tools=[tool], backend=backend)

        result = await loop.run(
            messages=[{"role": "user", "content": "investigate"}],
            ctx=tool_ctx,
        )

        assert result.outcome == LoopOutcome.COMPLETED
        assert result.turns_used == 3
        # Tool was called twice with the right inputs
        assert len(tool.calls) == 2
        assert tool.calls[0] == {"path": "auth.py"}
        assert tool.calls[1] == {"path": "token.py"}

    async def test_last_assistant_text_captured(self, tool_ctx: ToolContext) -> None:
        """last_assistant_text carries the model's final reasoning note."""
        final_text = "Root cause: null guard removed in auth.py line 42."
        loop = _make_loop(backend=_make_backend([_end_turn(final_text)]))
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert final_text in result.last_assistant_text

    async def test_extraction_result_populated(self, tool_ctx: ToolContext) -> None:
        """extracted field is populated from the post-loop extraction call."""
        extraction_json = '{"summary": "null guard removed", "confidence": "HIGH"}'
        loop = _make_loop(
            backend=_make_backend([_end_turn()], extraction_json=extraction_json)
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert result.extracted is not None
        assert result.extracted.summary == "null guard removed"
        assert result.extracted.confidence == "HIGH"


# ── Tests: history structure ───────────────────────────────────────────────────

class TestHistoryStructure:
    """The most important tests in the file.

    If history is malformed, the Anthropic API will reject the next call with
    a cryptic error. We assert the exact shape here so we catch violations at
    test time, not in production.
    """

    async def test_tool_call_produces_paired_messages(self, tool_ctx: ToolContext) -> None:
        """One tool call produces exactly two new history entries: assistant + user."""
        initial_messages = [{"role": "user", "content": "investigate"}]
        tool = RecordingTool("get_file")
        backend = _make_backend([
            _response(_tool_block("get_file", {"path": "auth.py"}, "t1")),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        await loop.run(messages=initial_messages, ctx=tool_ctx)

        # Access internal history via the backend call args
        # On the second complete_with_tools call, history has 3 entries:
        #   [0] original user message
        #   [1] assistant: [tool_use block]
        #   [2] user: [tool_result block]
        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        assert len(second_call_messages) == 3

        assistant_msg = second_call_messages[1]
        assert assistant_msg["role"] == "assistant"
        assert any(b["type"] == "tool_use" for b in assistant_msg["content"])

        user_msg = second_call_messages[2]
        assert user_msg["role"] == "user"
        assert any(b["type"] == "tool_result" for b in user_msg["content"])

    async def test_tool_use_id_matches_tool_result_id(self, tool_ctx: ToolContext) -> None:
        """tool_result.tool_use_id must match the tool_use.id — API requirement."""
        tool = RecordingTool("get_file")
        backend = _make_backend([
            _response(_tool_block("get_file", {"path": "auth.py"}, block_id="toolu_unique_99")),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        await loop.run(messages=[{"role": "user", "content": "go"}], ctx=tool_ctx)

        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        tool_result_block = second_call_messages[2]["content"][0]
        assert tool_result_block["tool_use_id"] == "toolu_unique_99"

    async def test_two_tools_in_one_turn_produce_one_user_message(
        self, tool_ctx: ToolContext
    ) -> None:
        """Two tool calls in one response must produce ONE user message with two results.

        The API requires all tool_results for a turn in a single user message.
        Two separate user messages with one result each is an API error.
        """
        tool = RecordingTool("get_file")
        backend = _make_backend([
            _response(
                _tool_block("get_file", {"path": "auth.py"}, "t1"),
                _tool_block("get_file", {"path": "token.py"}, "t2"),
            ),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        await loop.run(messages=[{"role": "user", "content": "go"}], ctx=tool_ctx)

        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        # History: [user, assistant, user]
        # The last user message must contain BOTH tool results
        user_msg = second_call_messages[2]
        assert user_msg["role"] == "user"
        tool_results = [b for b in user_msg["content"] if b["type"] == "tool_result"]
        assert len(tool_results) == 2

    async def test_tool_result_order_matches_tool_use_order(
        self, tool_ctx: ToolContext
    ) -> None:
        """Results must be in the same order as tool_use blocks, not completion order.

        Even if t2 completes before t1, results must appear as [t1_result, t2_result].
        The API pairs results to calls by tool_use_id — position order is also expected.
        """
        tool = RecordingTool("get_file")
        backend = _make_backend([
            _response(
                _tool_block("get_file", {"path": "first.py"}, "t1"),
                _tool_block("get_file", {"path": "second.py"}, "t2"),
            ),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        await loop.run(messages=[{"role": "user", "content": "go"}], ctx=tool_ctx)

        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        user_msg = second_call_messages[2]
        results = [b for b in user_msg["content"] if b["type"] == "tool_result"]
        assert results[0]["tool_use_id"] == "t1"
        assert results[1]["tool_use_id"] == "t2"

    async def test_text_block_preserved_in_assistant_message(
        self, tool_ctx: ToolContext
    ) -> None:
        """When a response has both text and tool_use, both go in the assistant message.

        Stripping the text block is wrong — it makes the history look like the
        model jumped straight to a tool call without any reasoning, which the
        API may reject or misinterpret.
        """
        tool = RecordingTool("get_file")
        backend = _make_backend([
            _response(
                _text_block("I need to check the source file first."),
                _tool_block("get_file", {"path": "auth.py"}, "t1"),
            ),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        await loop.run(messages=[{"role": "user", "content": "go"}], ctx=tool_ctx)

        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        assistant_content = second_call_messages[1]["content"]
        block_types = [b["type"] for b in assistant_content]
        assert "text" in block_types
        assert "tool_use" in block_types


# ── Tests: error handling ──────────────────────────────────────────────────────

class TestErrorHandling:
    async def test_tool_exception_fed_back_not_raised(self, tool_ctx: ToolContext) -> None:
        """A tool that raises should produce an is_error tool_result, not crash the loop.

        The model receives the error message and can adapt — try a different
        tool, try different arguments, or proceed with partial evidence.
        """
        tool = RaisingTool()
        backend = _make_backend([
            _response(_tool_block("raising_tool", {}, "t1")),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        # Loop should complete, not raise
        assert result.outcome == LoopOutcome.COMPLETED
        # Failed tool is recorded
        assert "raising_tool" in result.failed_tools

        # The tool_result in history should have is_error=True
        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        tool_result = second_call_messages[2]["content"][0]
        assert tool_result.get("is_error") is True
        assert "simulated tool failure" in tool_result["content"]

    async def test_tool_timeout_fed_back_not_raised(self, tool_ctx: ToolContext) -> None:
        """A tool that times out should produce an error result, not hang the loop."""
        tool = HangingTool()
        backend = _make_backend([
            _response(_tool_block("hanging_tool", {}, "t1")),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend, tool_timeout=0.05)
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert result.outcome == LoopOutcome.COMPLETED
        assert "hanging_tool" in result.failed_tools

        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        tool_result = second_call_messages[2]["content"][0]
        assert tool_result.get("is_error") is True
        assert "timed out" in tool_result["content"]

    async def test_unknown_tool_name_returns_error_result(self, tool_ctx: ToolContext) -> None:
        """If the model calls a tool that doesn't exist, return an error result
        listing the available tools. Do not crash.
        """
        loop = _make_loop(
            tools=[RecordingTool("real_tool")],
            backend=_make_backend([
                _response(_tool_block("nonexistent_tool", {}, "t1")),
                _end_turn(),
            ]),
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert result.outcome == LoopOutcome.COMPLETED
        assert "nonexistent_tool" in result.failed_tools

    async def test_extraction_failure_returns_none_not_crash(
        self, tool_ctx: ToolContext
    ) -> None:
        """If the extraction call returns invalid JSON, extracted is None — not a crash.

        The caller (_loop_result_to_triage) handles None by building a fallback.
        """
        backend = _make_backend([_end_turn()], extraction_json="not valid json {{")
        loop = _make_loop(backend=backend)
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert result.extracted is None
        # Outcome and other fields still populated
        assert result.outcome == LoopOutcome.COMPLETED
        assert result.turns_used == 1


# ── Tests: turn limit and outcomes ────────────────────────────────────────────

class TestTurnLimitAndOutcomes:
    async def test_turn_limit_exits_with_turn_limit_outcome(
        self, tool_ctx: ToolContext
    ) -> None:
        """When the model never stops calling tools, outcome is TURN_LIMIT."""
        # Always return a tool call — never end_turn
        tool = RecordingTool("get_file")
        never_ending = [
            _response(_tool_block("get_file", {"path": f"file{i}.py"}, f"t{i}"))
            for i in range(20)  # more responses than max_turns
        ]
        backend = _make_backend(never_ending)
        loop = _make_loop(tools=[tool], backend=backend, max_turns=3)

        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert result.outcome == LoopOutcome.TURN_LIMIT
        assert result.turns_used == 3
        assert result.model_confidence == "LOW"

    async def test_turn_limit_still_runs_extraction(self, tool_ctx: ToolContext) -> None:
        """Even when hitting the turn limit, extraction still runs.

        Partial findings have value for escalation — don't discard them.
        """
        extraction_json = '{"summary": "partial analysis", "confidence": "LOW"}'
        never_ending = [
            _response(_tool_block("test_tool", {}, f"t{i}")) for i in range(10)
        ]
        backend = _make_backend(never_ending, extraction_json=extraction_json)
        loop = _make_loop(backend=backend, max_turns=2)

        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        # extraction was called despite hitting the turn limit
        backend.complete.assert_called_once()
        assert result.extracted is not None
        assert result.extracted.summary == "partial analysis"

    async def test_failed_tools_accumulate_across_turns(
        self, tool_ctx: ToolContext
    ) -> None:
        """Every tool error across all turns is recorded in failed_tools."""
        tool = RaisingTool()
        backend = _make_backend([
            _response(_tool_block("raising_tool", {}, "t1")),
            _response(_tool_block("raising_tool", {}, "t2")),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        # Two errors, both recorded
        assert result.failed_tools.count("raising_tool") == 2


# ── Tests: tool execution ─────────────────────────────────────────────────────

class TestToolExecution:
    async def test_tool_receives_correct_input(self, tool_ctx: ToolContext) -> None:
        """The input dict the model sends is passed unchanged to tool.execute()."""
        tool = RecordingTool("get_file")
        expected_input = {"path": "src/auth.py", "ref": "abc1234"}
        backend = _make_backend([
            _response(_tool_block("get_file", expected_input, "t1")),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        await loop.run(messages=[{"role": "user", "content": "go"}], ctx=tool_ctx)

        assert tool.calls[0] == expected_input

    async def test_tool_success_result_in_history(self, tool_ctx: ToolContext) -> None:
        """A successful tool result appears in history without is_error flag."""
        tool = RecordingTool("get_file", response="file content here")
        backend = _make_backend([
            _response(_tool_block("get_file", {"path": "auth.py"}, "t1")),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        await loop.run(messages=[{"role": "user", "content": "go"}], ctx=tool_ctx)

        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        tool_result = second_call_messages[2]["content"][0]
        assert tool_result.get("is_error") is not True
        assert "file content here" in tool_result["content"]

    async def test_error_tool_result_has_is_error_flag(self, tool_ctx: ToolContext) -> None:
        """A tool that returns is_error=True has that flag set in the history entry."""
        tool = RecordingTool("get_file", response="file not found", is_error=True)
        backend = _make_backend([
            _response(_tool_block("get_file", {"path": "missing.py"}, "t1")),
            _end_turn(),
        ])
        loop = _make_loop(tools=[tool], backend=backend)
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert "get_file" in result.failed_tools
        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        tool_result = second_call_messages[2]["content"][0]
        assert tool_result.get("is_error") is True


# ── Tests: confirmation hook ───────────────────────────────────────────────────

class ConfirmationTool(Tool):
    """A tool that requires confirmation before it executes."""

    def __init__(self) -> None:
        self.executed = False

    @property
    def name(self) -> str:
        return "confirm_action"

    @property
    def description(self) -> str:
        return "A tool that requires confirmation"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def permission(self) -> Permission:
        return Permission.REQUIRES_CONFIRMATION

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        self.executed = True
        return ToolResult("action executed")


class TestConfirmHook:
    async def test_requires_confirmation_blocked_when_no_hook(
        self, tool_ctx: ToolContext
    ) -> None:
        """REQUIRES_CONFIRMATION tool is blocked by default (confirm=None → deny)."""
        tool = ConfirmationTool()
        backend = _make_backend([
            _response(_tool_block("confirm_action", {}, "t1")),
            _end_turn(),
        ])
        # No confirm hook — fail-safe
        loop = _make_loop(tools=[tool], backend=backend)
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        # Tool must NOT have executed
        assert not tool.executed
        # Must still produce an error tool_result (never skip the pair)
        assert "confirm_action" in result.failed_tools
        second_call_messages = backend.complete_with_tools.call_args_list[1][1]["messages"]
        tool_result = second_call_messages[2]["content"][0]
        assert tool_result.get("is_error") is True
        assert "confirmation" in tool_result["content"].lower()

    async def test_requires_confirmation_executes_when_hook_approves(
        self, tool_ctx: ToolContext
    ) -> None:
        """REQUIRES_CONFIRMATION tool runs when confirm hook returns True."""
        tool = ConfirmationTool()
        backend = _make_backend([
            _response(_tool_block("confirm_action", {}, "t1")),
            _end_turn(),
        ])

        async def auto_approve(t: Tool, inp: dict) -> bool:
            return True

        loop = AgentLoop(
            tools=[tool],
            backend=backend,
            domain_system_prompt="test",
            response_model=Finding,
            model="claude-sonnet-4-6",
            max_turns=5,
            confirm=auto_approve,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert tool.executed
        assert "confirm_action" not in result.failed_tools

    async def test_requires_confirmation_blocked_when_hook_denies(
        self, tool_ctx: ToolContext
    ) -> None:
        """REQUIRES_CONFIRMATION tool is blocked when confirm hook returns False."""
        tool = ConfirmationTool()
        backend = _make_backend([
            _response(_tool_block("confirm_action", {}, "t1")),
            _end_turn(),
        ])

        async def auto_deny(t: Tool, inp: dict) -> bool:
            return False

        loop = AgentLoop(
            tools=[tool],
            backend=backend,
            domain_system_prompt="test",
            response_model=Finding,
            model="claude-sonnet-4-6",
            max_turns=5,
            confirm=auto_deny,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert not tool.executed
        assert "confirm_action" in result.failed_tools

    async def test_read_only_tool_not_gated_by_confirm(
        self, tool_ctx: ToolContext
    ) -> None:
        """READ_ONLY tools execute without needing a confirm hook."""
        tool = RecordingTool("safe_tool")
        backend = _make_backend([
            _response(_tool_block("safe_tool", {}, "t1")),
            _end_turn(),
        ])
        # No confirm hook — should not affect read-only tools
        loop = _make_loop(tools=[tool], backend=backend)
        result = await loop.run(
            messages=[{"role": "user", "content": "go"}],
            ctx=tool_ctx,
        )

        assert len(tool.calls) == 1
        assert "safe_tool" not in result.failed_tools


# ── Tests: TenantContext integration ──────────────────────────────────────────


def _make_tenant_context(
    allowed_tools: list[str] | None = None,
    raise_rate_limit: bool = False,
) -> TenantContext:
    permissions = ToolPermissions(allowed_tools=allowed_tools or [])
    usage_tracker = MagicMock(spec=UsageTracker)
    rate_limiter = MagicMock(spec=RateLimiter)
    if raise_rate_limit:
        rate_limiter.check_and_consume.side_effect = RateLimitExceeded("limit reached")
    return TenantContext(
        tenant_id="test-tenant",
        permissions=permissions,
        usage_tracker=usage_tracker,
        rate_limiter=rate_limiter,
    )


def _make_loop_with_tenant(
    tenant_context: TenantContext,
    responses: list[types.SimpleNamespace],
    tools: list[Tool] | None = None,
) -> AgentLoop[Finding]:
    if tools is None:
        tools = [RecordingTool()]
    return AgentLoop(
        tools=tools,
        backend=_make_backend(responses),
        domain_system_prompt="You are a test agent.",
        response_model=Finding,
        model="claude-sonnet-4-6",
        max_turns=5,
        tenant_context=tenant_context,
    )


class TestTenantContext:
    async def test_rate_limit_exceeded_stops_loop_gracefully(
        self, tool_ctx: ToolContext
    ) -> None:
        """When RateLimitExceeded is raised, loop exits with TURN_LIMIT outcome."""
        tenant_ctx = _make_tenant_context(raise_rate_limit=True)
        loop = _make_loop_with_tenant(tenant_ctx, responses=[_end_turn()])
        result = await loop.run(
            messages=[{"role": "user", "content": "investigate"}],
            ctx=tool_ctx,
        )
        assert result.outcome == LoopOutcome.TURN_LIMIT
        assert "rate limit" in result.last_assistant_text.lower()

    async def test_usage_tracker_records_api_call(
        self, tool_ctx: ToolContext
    ) -> None:
        """Usage tracker records an API call after each LLM call."""
        tenant_ctx = _make_tenant_context()
        loop = _make_loop_with_tenant(tenant_ctx, responses=[_end_turn()])
        await loop.run(
            messages=[{"role": "user", "content": "investigate"}],
            ctx=tool_ctx,
        )
        tenant_ctx.usage_tracker.record_api_call.assert_called()

    async def test_denied_tool_returns_error_result(
        self, tool_ctx: ToolContext
    ) -> None:
        """A tool not in the allowlist returns an is_error tool result."""
        # Only "other_tool" is allowed, but we call "record" which is blocked
        tenant_ctx = _make_tenant_context(allowed_tools=["other_tool"])
        tool = RecordingTool("record")
        backend = _make_backend([
            _response(_tool_block("record", {}, "t1")),
            _end_turn(),
        ])
        loop = AgentLoop(
            tools=[tool],
            backend=backend,
            domain_system_prompt="You are a test agent.",
            response_model=Finding,
            model="claude-sonnet-4-6",
            max_turns=5,
            tenant_context=tenant_ctx,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "investigate"}],
            ctx=tool_ctx,
        )
        # Tool should not have executed
        assert len(tool.calls) == 0
        assert "record" in result.failed_tools

    async def test_allowed_tool_executes_normally(
        self, tool_ctx: ToolContext
    ) -> None:
        """A tool in the allowlist executes without restriction."""
        tenant_ctx = _make_tenant_context(allowed_tools=["record"])
        tool = RecordingTool("record")
        backend = _make_backend([
            _response(_tool_block("record", {}, "t1")),
            _end_turn(),
        ])
        loop = AgentLoop(
            tools=[tool],
            backend=backend,
            domain_system_prompt="You are a test agent.",
            response_model=Finding,
            model="claude-sonnet-4-6",
            max_turns=5,
            tenant_context=tenant_ctx,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "investigate"}],
            ctx=tool_ctx,
        )
        assert len(tool.calls) == 1
        assert "record" not in result.failed_tools
