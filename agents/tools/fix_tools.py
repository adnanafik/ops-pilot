"""Fix tools — WRITE-tier tools for the ops-pilot tool registry.

These tools wrap the write-side of the CIProvider interface. They are not
currently used by FixAgent (which still orchestrates provider calls directly),
but are registered in the tool catalog so future loop-driven fix agents and
Phase 3 workers can discover and use them.

Permission levels:
  - GetRepoTreeTool : READ_ONLY  — safe listing, no side effects
  - CreateBranchTool: WRITE                — creates a git branch
  - UpdateFileTool  : REQUIRES_CONFIRMATION — commits a file change to a branch
  - OpenDraftPRTool : REQUIRES_CONFIRMATION — opens a draft PR/MR

All tools are stateless. Runtime dependencies arrive via ToolContext.
"""

from __future__ import annotations

from shared.agent_loop import Permission, Tool, ToolContext, ToolResult


class GetRepoTreeTool(Tool):
    """List the file paths present in the repository at a given ref.

    Use this when the triage output names an affected service but does not
    specify which file to edit, or when get_file returns FileNotFoundError
    and you need to discover the correct path. Returns a flat list of paths
    filtered by the extensions you request.
    """

    @property
    def name(self) -> str:
        return "get_repo_tree"

    @property
    def description(self) -> str:
        return (
            "List file paths in the repository at a given git ref. "
            "Use this to discover which files exist when the triage analysis "
            "references a service or module but not a specific file path. "
            "Filter by extension to limit output to relevant file types. "
            "Returns a sorted list of paths relative to the repo root."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Git ref (branch, tag, or commit SHA). Defaults to HEAD.",
                },
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "File extensions to include, e.g. [\".py\", \".toml\"]. "
                        "Omit to return all file types."
                    ),
                },
            },
            "required": [],
        }

    @property
    def permission(self) -> Permission:
        return Permission.READ_ONLY

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        if ctx.provider is None:
            return ToolResult("No provider available — cannot list repo tree.", is_error=True)

        ref = input.get("ref", "HEAD")
        raw_extensions = input.get("extensions")
        extensions: tuple[str, ...] | None = None
        if raw_extensions:
            extensions = tuple(str(e) for e in raw_extensions)

        repo = ctx.failure.pipeline.repo
        try:
            paths = ctx.provider.get_repo_tree(repo, ref=ref, extensions=extensions)
            if not paths:
                return ToolResult(
                    f"No files found in {repo} at {ref}"
                    + (f" with extensions {list(extensions)}" if extensions else "")
                    + "."
                )
            listing = "\n".join(paths)
            return ToolResult(
                f"Repository tree: {repo} @ {ref} ({len(paths)} files)\n\n{listing}"
            )
        except Exception as exc:
            return ToolResult(f"Failed to list repo tree: {exc}", is_error=True)


class CreateBranchTool(Tool):
    """Create a new git branch from an existing ref.

    Use this as the first step before committing any fix — all file changes
    must land on a dedicated ops-pilot branch, never directly on main.
    The operation is idempotent: calling it when the branch already exists
    is safe and returns a success result.
    """

    @property
    def name(self) -> str:
        return "create_branch"

    @property
    def description(self) -> str:
        return (
            "Create a new git branch from an existing branch or commit SHA. "
            "Always call this before update_file — never commit directly to main. "
            "Use the naming convention 'ops-pilot/fix-<short-sha>' for the branch "
            "name so fix PRs are easy to identify and filter. "
            "Idempotent: safe to call if the branch already exists."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": (
                        "New branch name. Convention: 'ops-pilot/fix-<7-char-sha>', "
                        "e.g. 'ops-pilot/fix-abc1234'."
                    ),
                },
                "from_ref": {
                    "type": "string",
                    "description": "Branch name or commit SHA to branch from (typically 'main').",
                },
            },
            "required": ["branch", "from_ref"],
        }

    @property
    def permission(self) -> Permission:
        return Permission.WRITE

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        if ctx.provider is None:
            return ToolResult("No provider available — cannot create branch.", is_error=True)

        repo = ctx.failure.pipeline.repo
        branch = input["branch"]
        from_ref = input["from_ref"]
        try:
            ctx.provider.create_branch(repo, branch, from_ref=from_ref)
            return ToolResult(f"Branch '{branch}' created from '{from_ref}' in {repo}.")
        except Exception as exc:
            return ToolResult(f"Failed to create branch '{branch}': {exc}", is_error=True)


class UpdateFileTool(Tool):
    """Commit updated file content to an existing branch.

    Writes the complete new file content to the specified path on the branch.
    You must have the blob_id from a prior get_file call — it is the conflict
    guard that prevents overwriting concurrent changes. For GitLab, pass an
    empty string for blob_id.
    """

    @property
    def name(self) -> str:
        return "update_file"

    @property
    def description(self) -> str:
        return (
            "Commit a new version of a file to an existing branch. "
            "You must call get_file first to obtain the blob_id (the conflict guard). "
            "Provide the complete new file content — not a diff. Make the minimal "
            "change that fixes the bug; do not reformat unrelated code. "
            "For GitLab providers, blob_id can be an empty string."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the repo root.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete new file content (not a diff).",
                },
                "blob_id": {
                    "type": "string",
                    "description": (
                        "Blob identifier from a prior get_file call. "
                        "Required for GitHub to prevent conflicts. "
                        "Pass empty string for GitLab."
                    ),
                },
                "branch": {
                    "type": "string",
                    "description": "Target branch to commit to (must already exist).",
                },
                "commit_message": {
                    "type": "string",
                    "description": "Commit message. Use imperative mood, e.g. 'fix: restore null guard'.",
                },
            },
            "required": ["path", "content", "branch", "commit_message"],
        }

    @property
    def permission(self) -> Permission:
        return Permission.REQUIRES_CONFIRMATION

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        if ctx.provider is None:
            return ToolResult("No provider available — cannot update file.", is_error=True)

        repo = ctx.failure.pipeline.repo
        path = input["path"]
        content = input["content"]
        blob_id = input.get("blob_id", "")
        branch = input["branch"]
        commit_message = input["commit_message"]
        try:
            ctx.provider.update_file(
                repo=repo,
                path=path,
                content=content,
                blob_id=blob_id,
                branch=branch,
                commit_message=commit_message,
            )
            return ToolResult(
                f"Committed '{path}' to branch '{branch}' in {repo}.\n"
                f"Message: {commit_message}"
            )
        except Exception as exc:
            return ToolResult(f"Failed to update '{path}': {exc}", is_error=True)


class OpenDraftPRTool(Tool):
    """Open a draft PR or MR targeting the base branch.

    Call this after all file changes have been committed to the fix branch.
    The operation is idempotent: if a PR for the same head branch already
    exists, the existing PR URL and number are returned instead of creating
    a duplicate.
    """

    @property
    def name(self) -> str:
        return "open_draft_pr"

    @property
    def description(self) -> str:
        return (
            "Open a draft pull request (GitHub) or merge request (GitLab) "
            "targeting the base branch. Call this after all file changes are "
            "committed to the fix branch. "
            "The PR title should be under 72 characters in imperative mood. "
            "The PR body should use sections: ## Problem / ## Root cause / ## Fix / ## Tests. "
            "Idempotent: returns the existing PR if one already exists for the head branch."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "PR title, imperative mood, under 72 characters.",
                },
                "body": {
                    "type": "string",
                    "description": "Full markdown PR body.",
                },
                "head": {
                    "type": "string",
                    "description": "Source branch (the ops-pilot fix branch).",
                },
                "base": {
                    "type": "string",
                    "description": "Target branch, typically 'main'.",
                },
            },
            "required": ["title", "body", "head", "base"],
        }

    @property
    def permission(self) -> Permission:
        return Permission.REQUIRES_CONFIRMATION

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        if ctx.provider is None:
            return ToolResult("No provider available — cannot open PR.", is_error=True)

        repo = ctx.failure.pipeline.repo
        title = input["title"]
        body = input["body"]
        head = input["head"]
        base = input["base"]
        try:
            url, number = ctx.provider.open_draft_pr(
                repo=repo,
                title=title,
                body=body,
                head=head,
                base=base,
            )
            return ToolResult(f"Draft PR #{number} opened: {url}")
        except Exception as exc:
            return ToolResult(f"Failed to open draft PR: {exc}", is_error=True)
