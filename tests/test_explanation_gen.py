"""Tests for ExplanationGenerator — LLM-backed pre-action explanations."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from shared.explanation_gen import ExplanationGenerator


@pytest.fixture
def backend() -> MagicMock:
    b = MagicMock()
    b.complete.return_value = "Patching null-check at line 47 to restore the guard."
    return b


class TestExplanationGenerator:
    def test_returns_string(self, backend: MagicMock) -> None:
        gen = ExplanationGenerator(backend=backend, model="claude-haiku-4-5-20251001")
        result = gen.generate(
            tool_name="update_file",
            tool_input={"path": "auth.py", "content": "fixed content"},
            context_summary="Found null pointer at line 47 in auth.py.",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_calls_backend_once(self, backend: MagicMock) -> None:
        gen = ExplanationGenerator(backend=backend, model="claude-haiku-4-5-20251001")
        gen.generate(
            tool_name="update_file",
            tool_input={"path": "auth.py"},
            context_summary="context",
        )
        backend.complete.assert_called_once()

    def test_prompt_includes_tool_name(self, backend: MagicMock) -> None:
        gen = ExplanationGenerator(backend=backend, model="claude-haiku-4-5-20251001")
        gen.generate(
            tool_name="open_draft_pr",
            tool_input={"title": "fix: restore guard"},
            context_summary="context",
        )
        call_kwargs = backend.complete.call_args[1]
        assert "open_draft_pr" in call_kwargs["user"]

    def test_prompt_includes_context_summary(self, backend: MagicMock) -> None:
        gen = ExplanationGenerator(backend=backend, model="claude-haiku-4-5-20251001")
        gen.generate(
            tool_name="update_file",
            tool_input={},
            context_summary="Found null pointer at line 47.",
        )
        call_kwargs = backend.complete.call_args[1]
        assert "Found null pointer at line 47." in call_kwargs["user"]

    def test_backend_failure_returns_empty_string(self) -> None:
        bad_backend = MagicMock()
        bad_backend.complete.side_effect = RuntimeError("API down")
        gen = ExplanationGenerator(backend=bad_backend, model="claude-haiku-4-5-20251001")
        result = gen.generate(
            tool_name="update_file",
            tool_input={},
            context_summary="context",
        )
        assert result == ""

    def test_uses_model_passed_to_constructor(self, backend: MagicMock) -> None:
        gen = ExplanationGenerator(backend=backend, model="claude-haiku-4-5-20251001")
        gen.generate(tool_name="update_file", tool_input={}, context_summary="ctx")
        call_kwargs = backend.complete.call_args[1]
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
