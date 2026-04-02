"""Triage tools for the agentic investigation loop.

These are the three tools identified as missing from the original TriageAgent:
  GetFileTool       — read source at the offending line (model only had filename+lineno)
  GetMoreLogTool    — fetch earlier log sections (root cause often 50-100 lines above tail)
  GetCommitDiffTool — full unified diff, not the DiffSummary abstraction

Each tool is a stateless class. Runtime dependencies (provider, failure context)
come through ToolContext at execution time — never stored on the instance.

Description quality matters as much as implementation quality. The model
decides which tool to call based on descriptions alone. These are written
for the model, not for engineers.
"""

from __future__ import annotations

from shared.agent_loop import Permission, Tool, ToolContext, ToolResult


class GetFileTool(Tool):
    """Let the agent read the actual source file at the offending line.

    Why this matters: the log tail gives a filename and line number, but the
    model has to reason about a file it cannot see. The triage schema asked for
    root cause but provided only symptoms. This tool closes that gap.
    """

    @property
    def name(self) -> str:
        return "get_file"

    @property
    def description(self) -> str:
        return (
            "Read a source file from the repository at a specific git ref. "
            "Use this when the log references a specific file and line number — "
            "reading the actual code at that location is almost always more "
            "informative than reasoning from the filename alone. "
            "Returns the full file content. For large files, consider whether "
            "you need the full file or just context around a specific line."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the repo root, e.g. 'src/auth/validator.py'",
                },
                "ref": {
                    "type": "string",
                    "description": (
                        "Git ref to read from — branch name, tag, or commit SHA. "
                        "Use the failing commit SHA to see exactly what was deployed."
                    ),
                },
            },
            "required": ["path"],
        }

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        if ctx.provider is None:
            return ToolResult("No provider available — cannot read files.", is_error=True)

        path = input["path"]
        ref = input.get("ref", ctx.failure.pipeline.commit)
        repo = ctx.failure.pipeline.repo

        try:
            content, _ = ctx.provider.get_file(repo, path, ref)
            # Add line numbers so the model can reference specific lines
            numbered = "\n".join(
                f"{i + 1:4d} | {line}"
                for i, line in enumerate(content.splitlines())
            )
            return ToolResult(f"File: {path} @ {ref}\n\n{numbered}")
        except FileNotFoundError:
            return ToolResult(
                f"File '{path}' not found at ref '{ref}' in {repo}. "
                f"Check the path spelling or try a different ref.",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(f"Failed to read {path}: {exc}", is_error=True)


class GetMoreLogTool(Tool):
    """Fetch an earlier section of the CI job log by line offset.

    Why this matters: the original TriageAgent only had the last 50 lines.
    Stack traces and cascading failures often have the real root cause 50-100
    lines above the failure message. This tool lets the model navigate upward
    in the log to find it.
    """

    @property
    def name(self) -> str:
        return "get_more_log"

    @property
    def description(self) -> str:
        return (
            "Fetch a section of the CI job log by line offset. "
            "Use this when the visible log tail shows a symptom (e.g. an assertion "
            "failure or ImportError) but not the underlying cause — which is often "
            "50-100 lines earlier in the log. "
            "Start with offset=0 to see the beginning, or use a negative offset "
            "pattern: if you have the last 50 lines, try offset=0 to see the first "
            "100 lines and look for the original error or stack trace root."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "job_name": {
                    "type": "string",
                    "description": "Exact name of the CI job whose logs to fetch.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start from (0 = beginning of log).",
                    "default": 0,
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Number of lines to return. Keep under 150 to stay within context budget.",
                    "default": 100,
                },
            },
            "required": ["job_name"],
        }

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        if ctx.provider is None:
            return ToolResult("No provider available — cannot fetch logs.", is_error=True)

        job_name = input["job_name"]
        offset = int(input.get("offset", 0))
        max_lines = min(int(input.get("max_lines", 100)), 200)  # hard cap to protect context
        run_id = ctx.failure.pipeline.run_id
        repo = ctx.failure.pipeline.repo

        lines = ctx.provider.get_job_logs(repo, run_id, job_name, offset, max_lines)

        if not lines:
            return ToolResult(
                f"No log lines returned for job '{job_name}' at offset {offset}. "
                f"The log may be shorter than expected.",
                is_error=True,
            )

        # Show the user which section they're looking at
        header = f"Log: {job_name} | lines {offset}–{offset + len(lines)} | run {run_id}\n"
        body = "\n".join(f"{offset + i + 1:4d} | {line}" for i, line in enumerate(lines))
        return ToolResult(header + body)


class GetCommitDiffTool(Tool):
    """Fetch the full unified diff for the commit that triggered this failure.

    Why this matters: the original DiffSummary only contained files_changed,
    lines_added, lines_removed, and a human-written key_change string. Without
    the actual diff hunks, the model cannot tell what changed in the code —
    only that it changed. This tool returns the raw +/- lines.
    """

    @property
    def name(self) -> str:
        return "get_commit_diff"

    @property
    def description(self) -> str:
        return (
            "Fetch the full unified diff (+/- lines) for a specific commit. "
            "Use this when the failure is likely regression-related and you need "
            "to see exactly what changed in the code — not just which files changed. "
            "The diff summary in the failure report only contains file names and "
            "line counts; this tool returns the actual hunks. "
            "Use the failing commit SHA from the failure context by default."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "sha": {
                    "type": "string",
                    "description": (
                        "Commit SHA to diff. Defaults to the failing commit. "
                        "Use the full or 7-char short SHA."
                    ),
                },
            },
            "required": [],
        }

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        if ctx.provider is None:
            return ToolResult("No provider available — cannot fetch diff.", is_error=True)

        sha = input.get("sha", ctx.failure.pipeline.commit)
        repo = ctx.failure.pipeline.repo

        diff = ctx.provider.get_commit_diff(repo, sha)

        if not diff or diff.startswith("get_commit_diff not implemented"):
            return ToolResult(
                f"Diff not available for {sha}. "
                f"Try get_file to read the changed files directly.",
                is_error=True,
            )

        # Truncate very large diffs to stay within context budget
        max_chars = 8000
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n\n... (diff truncated at {max_chars} chars)"

        return ToolResult(f"Diff for {sha} in {repo}:\n\n{diff}")
