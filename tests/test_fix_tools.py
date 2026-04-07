"""Tests for fix tools — the WRITE-tier tools in the tool registry.

Testing strategy: each test provides a mock CIProvider via ToolContext.
We assert on ToolResult.content and ToolResult.is_error, and verify the
correct provider method was called with the correct arguments. Tools are
stateless, so each test constructs a fresh instance.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agents.tools.fix_tools import (
    CreateBranchTool,
    GetRepoTreeTool,
    OpenDraftPRTool,
    UpdateFileTool,
)
from shared.agent_loop import Permission, ToolContext
from shared.models import DiffSummary, Failure, FailureDetail, PipelineInfo

# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def failure() -> Failure:
    return Failure(
        id="fix_test_001",
        pipeline=PipelineInfo(
            provider="github_actions",
            repo="acme/backend",
            workflow="ci.yml",
            run_id="99001",
            branch="main",
            commit="def5678",
            commit_message="chore: bump deps",
            author="dev@acme.com",
            triggered_at=datetime(2026, 1, 1),
            failed_at=datetime(2026, 1, 1),
            duration_seconds=30,
        ),
        failure=FailureDetail(
            job="test-job",
            step="pytest",
            exit_code=1,
            log_tail=["FAILED test_auth.py::test_login"],
        ),
        diff_summary=DiffSummary(
            files_changed=["auth.py"],
            lines_added=1,
            lines_removed=1,
            key_change="removed null guard",
        ),
    )


@pytest.fixture
def mock_provider() -> MagicMock:
    """Mock CIProvider with stubs for all methods used by fix tools."""
    p = MagicMock()
    p.get_repo_tree.return_value = ["auth.py", "models.py", "tests/test_auth.py"]
    p.create_branch.return_value = None
    p.update_file.return_value = None
    p.open_draft_pr.return_value = ("https://github.com/acme/backend/pull/42", 42)
    return p


@pytest.fixture
def ctx(failure: Failure, mock_provider: MagicMock) -> ToolContext:
    return ToolContext(provider=mock_provider, failure=failure)


@pytest.fixture
def ctx_no_provider(failure: Failure) -> ToolContext:
    return ToolContext(provider=None, failure=failure)


# ── GetRepoTreeTool ────────────────────────────────────────────────────────────

class TestGetRepoTreeTool:
    def test_permission_is_read_only(self) -> None:
        assert GetRepoTreeTool().permission == Permission.READ_ONLY

    async def test_returns_file_listing(self, ctx: ToolContext) -> None:
        result = await GetRepoTreeTool().execute({}, ctx)
        assert not result.is_error
        assert "auth.py" in result.content
        assert "models.py" in result.content

    async def test_passes_ref_to_provider(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        await GetRepoTreeTool().execute({"ref": "abc1234"}, ctx)
        mock_provider.get_repo_tree.assert_called_once_with(
            "acme/backend", ref="abc1234", extensions=None
        )

    async def test_passes_extensions_to_provider(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        await GetRepoTreeTool().execute({"extensions": [".py", ".toml"]}, ctx)
        _, kwargs = mock_provider.get_repo_tree.call_args
        assert kwargs["extensions"] == (".py", ".toml")

    async def test_default_ref_is_head(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        await GetRepoTreeTool().execute({}, ctx)
        _, kwargs = mock_provider.get_repo_tree.call_args
        assert kwargs["ref"] == "HEAD"

    async def test_empty_tree_returns_success_message(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        mock_provider.get_repo_tree.return_value = []
        result = await GetRepoTreeTool().execute({}, ctx)
        assert not result.is_error
        assert "No files found" in result.content

    async def test_no_provider_returns_error(self, ctx_no_provider: ToolContext) -> None:
        result = await GetRepoTreeTool().execute({}, ctx_no_provider)
        assert result.is_error

    async def test_provider_exception_returns_error(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        mock_provider.get_repo_tree.side_effect = RuntimeError("API down")
        result = await GetRepoTreeTool().execute({}, ctx)
        assert result.is_error
        assert "API down" in result.content


# ── CreateBranchTool ──────────────────────────────────────────────────────────

class TestCreateBranchTool:
    def test_permission_is_write(self) -> None:
        assert CreateBranchTool().permission == Permission.WRITE

    async def test_creates_branch_via_provider(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        result = await CreateBranchTool().execute(
            {"branch": "ops-pilot/fix-def5678", "from_ref": "main"}, ctx
        )
        assert not result.is_error
        mock_provider.create_branch.assert_called_once_with(
            "acme/backend", "ops-pilot/fix-def5678", from_ref="main"
        )

    async def test_success_message_contains_branch_name(
        self, ctx: ToolContext
    ) -> None:
        result = await CreateBranchTool().execute(
            {"branch": "ops-pilot/fix-def5678", "from_ref": "main"}, ctx
        )
        assert "ops-pilot/fix-def5678" in result.content

    async def test_no_provider_returns_error(self, ctx_no_provider: ToolContext) -> None:
        result = await CreateBranchTool().execute(
            {"branch": "ops-pilot/fix-def5678", "from_ref": "main"}, ctx_no_provider
        )
        assert result.is_error

    async def test_provider_exception_returns_error(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        mock_provider.create_branch.side_effect = RuntimeError("permission denied")
        result = await CreateBranchTool().execute(
            {"branch": "ops-pilot/fix-def5678", "from_ref": "main"}, ctx
        )
        assert result.is_error
        assert "permission denied" in result.content


# ── UpdateFileTool ─────────────────────────────────────────────────────────────

class TestUpdateFileTool:
    def test_permission_is_requires_confirmation(self) -> None:
        assert UpdateFileTool().permission == Permission.REQUIRES_CONFIRMATION

    async def test_commits_file_via_provider(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        result = await UpdateFileTool().execute(
            {
                "path": "auth.py",
                "content": "def login(): return True",
                "blob_id": "abc123blob",
                "branch": "ops-pilot/fix-def5678",
                "commit_message": "fix: restore null guard",
            },
            ctx,
        )
        assert not result.is_error
        mock_provider.update_file.assert_called_once_with(
            repo="acme/backend",
            path="auth.py",
            content="def login(): return True",
            blob_id="abc123blob",
            branch="ops-pilot/fix-def5678",
            commit_message="fix: restore null guard",
        )

    async def test_blob_id_defaults_to_empty_string(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        """blob_id is optional — GitLab providers accept empty string."""
        await UpdateFileTool().execute(
            {
                "path": "auth.py",
                "content": "fixed content",
                "branch": "ops-pilot/fix-def5678",
                "commit_message": "fix: test",
            },
            ctx,
        )
        _, kwargs = mock_provider.update_file.call_args
        assert kwargs["blob_id"] == ""

    async def test_success_message_contains_path_and_branch(
        self, ctx: ToolContext
    ) -> None:
        result = await UpdateFileTool().execute(
            {
                "path": "auth.py",
                "content": "x",
                "branch": "ops-pilot/fix-def5678",
                "commit_message": "fix: test",
            },
            ctx,
        )
        assert "auth.py" in result.content
        assert "ops-pilot/fix-def5678" in result.content

    async def test_no_provider_returns_error(self, ctx_no_provider: ToolContext) -> None:
        result = await UpdateFileTool().execute(
            {"path": "auth.py", "content": "x", "branch": "b", "commit_message": "m"},
            ctx_no_provider,
        )
        assert result.is_error

    async def test_provider_exception_returns_error(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        mock_provider.update_file.side_effect = RuntimeError("conflict")
        result = await UpdateFileTool().execute(
            {"path": "auth.py", "content": "x", "branch": "b", "commit_message": "m"},
            ctx,
        )
        assert result.is_error
        assert "conflict" in result.content


# ── OpenDraftPRTool ────────────────────────────────────────────────────────────

class TestOpenDraftPRTool:
    def test_permission_is_requires_confirmation(self) -> None:
        assert OpenDraftPRTool().permission == Permission.REQUIRES_CONFIRMATION

    async def test_opens_pr_via_provider(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        result = await OpenDraftPRTool().execute(
            {
                "title": "fix(auth): restore null guard",
                "body": "## Problem\nNull guard removed.\n",
                "head": "ops-pilot/fix-def5678",
                "base": "main",
            },
            ctx,
        )
        assert not result.is_error
        mock_provider.open_draft_pr.assert_called_once_with(
            repo="acme/backend",
            title="fix(auth): restore null guard",
            body="## Problem\nNull guard removed.\n",
            head="ops-pilot/fix-def5678",
            base="main",
        )

    async def test_result_contains_pr_url_and_number(
        self, ctx: ToolContext
    ) -> None:
        result = await OpenDraftPRTool().execute(
            {
                "title": "fix: test",
                "body": "body",
                "head": "ops-pilot/fix-def5678",
                "base": "main",
            },
            ctx,
        )
        assert "42" in result.content
        assert "https://github.com/acme/backend/pull/42" in result.content

    async def test_no_provider_returns_error(self, ctx_no_provider: ToolContext) -> None:
        result = await OpenDraftPRTool().execute(
            {"title": "t", "body": "b", "head": "h", "base": "main"},
            ctx_no_provider,
        )
        assert result.is_error

    async def test_provider_exception_returns_error(
        self, ctx: ToolContext, mock_provider: MagicMock
    ) -> None:
        mock_provider.open_draft_pr.side_effect = RuntimeError("rate limited")
        result = await OpenDraftPRTool().execute(
            {"title": "t", "body": "b", "head": "h", "base": "main"},
            ctx,
        )
        assert result.is_error
        assert "rate limited" in result.content
