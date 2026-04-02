"""Coordinator tools — the spawn mechanism for Phase 3 multi-agent investigation.

SpawnWorkerTool is the only tool the coordinator loop has. When the coordinator
calls it three times in one turn, AgentLoop's asyncio.gather executes all three
workers concurrently — each in its own isolated AgentLoop with its own message
history and scoped tool list.

Worker types:
  log_worker    — fetches log sections; identifies the full error sequence
  source_worker — reads source files; identifies the buggy code
  diff_worker   — reads the commit diff; identifies what changed and why

Worker isolation guarantees:
  - Own message history (starts fresh, no coordinator context leaks in)
  - Scoped tool list (log_worker cannot read source files; diff_worker cannot
    fetch logs — focus prevents rabbit holes and keeps findings clean)
  - No SpawnWorkerTool (workers cannot recursively spawn more workers)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from agents.tools.fix_tools import GetRepoTreeTool
from agents.tools.triage_tools import GetCommitDiffTool, GetFileTool, GetMoreLogTool
from shared.agent_loop import AgentLoop, Permission, Tool, ToolContext, ToolResult

# ── Worker finding model ───────────────────────────────────────────────────────
# Workers don't produce full Triage — they produce focused findings.
# The coordinator synthesises these into the final Triage via post-loop extraction.

class WorkerFinding(BaseModel):
    """Structured output from a specialist worker's investigation."""

    summary: str = ""
    key_observations: list[str] = []
    confidence: str = "HIGH"


# ── Worker system prompts ──────────────────────────────────────────────────────
# Each prompt is narrow. Narrow prompts produce focused workers.
# Vague prompts produce workers that try to triage the whole failure themselves,
# defeating the purpose of specialisation and polluting the coordinator's synthesis.

LOG_WORKER_PROMPT = """You are a specialist log analyst investigating a CI failure.

Your job: fetch and read the relevant CI log sections.
Focus on:
  - When did the failure first appear in the log (not just the tail shown to you)?
  - What error messages or stack traces preceded the failure line?
  - Is this a cascading failure (something upstream caused it) or a direct failure?
  - Does the pattern suggest a flaky test or a genuine regression?

Use get_more_log to fetch earlier sections. Start from offset=0 to see the beginning.
Report only what you find in the logs — do not speculate about source code."""

SOURCE_WORKER_PROMPT = """You are a specialist source code analyst investigating a CI failure.

Your job: read the source file(s) at the lines mentioned in the failure.
Focus on:
  - What does the code at the failing line actually do?
  - What invariant does the failure suggest was violated?
  - What is the minimal change that would fix it?

Use get_file to read source. Use get_repo_tree if you need to discover the file path.
Report only what you find in the source — do not speculate about log causes."""

DIFF_WORKER_PROMPT = """You are a specialist diff analyst investigating a CI failure.

Your job: read the commit diff to understand what changed.
Focus on:
  - What was removed or modified in this commit?
  - Which specific change is the most likely cause of the failure?
  - Is the failure a direct consequence of a removed guard, changed signature, or renamed symbol?

Use get_commit_diff to see the actual +/- lines. Use get_file to read context around the change.
Report only what you find in the diff — do not speculate about runtime log causes."""


# ── SpawnWorkerTool ────────────────────────────────────────────────────────────

@dataclass
class WorkerLoop:
    """A named, pre-built worker ready to run investigations."""

    name: str
    loop: AgentLoop[WorkerFinding]


class SpawnWorkerTool(Tool):
    """Spawn a specialist worker to investigate one aspect of the CI failure.

    The coordinator calls this tool three times in the same turn (once per worker).
    AgentLoop.execute_tools_concurrent() runs all three via asyncio.gather — true
    parallel execution, not sequential. All three findings arrive back in one user
    message, which the coordinator then synthesises.

    Worker isolation is structural: each worker is a separate AgentLoop instance
    with its own message history and scoped tool list. The coordinator never sees
    raw tool outputs — only each worker's summarised findings.
    """

    def __init__(self, workers: dict[str, WorkerLoop]) -> None:
        self._workers = workers

    @property
    def name(self) -> str:
        return "spawn_worker"

    @property
    def description(self) -> str:
        names = ", ".join(sorted(self._workers.keys()))
        return (
            "Spawn a specialist worker to investigate one aspect of this CI failure. "
            f"Available workers: {names}. "
            "CRITICAL: Spawn ALL workers in the same turn to run them in parallel — "
            "calling them sequentially wastes turns and loses the parallelism benefit. "
            "Each worker runs an isolated investigation with scoped tools and returns a "
            "findings summary."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": (
                        "Which specialist to spawn. "
                        f"One of: {', '.join(sorted(self._workers.keys()))}."
                    ),
                    "enum": sorted(self._workers.keys()),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "Specific investigation task for this worker. Be concrete: "
                        "include the job name, relevant file paths, or commit SHA "
                        "from the failure context so the worker knows where to look."
                    ),
                },
            },
            "required": ["worker", "task"],
        }

    @property
    def permission(self) -> Permission:
        return Permission.READ_ONLY

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        worker_name = input.get("worker", "")
        task = input.get("task", "")
        worker = self._workers.get(worker_name)
        if worker is None:
            return ToolResult(
                f"Unknown worker '{worker_name}'. "
                f"Available: {sorted(self._workers.keys())}",
                is_error=True,
            )

        result = await worker.loop.run(
            messages=[{"role": "user", "content": task}],
            ctx=ctx,
        )
        summary = result.last_assistant_text or "(worker produced no findings)"
        return ToolResult(
            f"[{worker_name}] confidence={result.model_confidence}\n\n{summary}"
        )


# ── Worker factory ─────────────────────────────────────────────────────────────

def build_workers(
    backend: Any,
    model: str,
    worker_max_turns: int = 5,
) -> dict[str, WorkerLoop]:
    """Build the three standard specialist workers.

    Each worker gets only the tools relevant to its domain. This is the key
    isolation guarantee: log_worker cannot read source files (avoiding scope
    creep), source_worker cannot fetch logs, diff_worker stays focused on diffs.

    Args:
        backend:          LLM backend shared with the coordinator.
        model:            Model ID used for all workers.
        worker_max_turns: Turn limit per worker. Lower than the coordinator's
                          limit — workers should be targeted, not exploratory.

    Returns:
        Dict of worker name → WorkerLoop, ready to pass to SpawnWorkerTool.
    """

    def _make_loop(tools: list[Tool], prompt: str) -> AgentLoop[WorkerFinding]:
        return AgentLoop(
            tools=tools,
            backend=backend,
            domain_system_prompt=prompt,
            response_model=WorkerFinding,
            model=model,
            max_turns=worker_max_turns,
        )

    return {
        "log_worker": WorkerLoop(
            name="log_worker",
            loop=_make_loop([GetMoreLogTool(), GetFileTool()], LOG_WORKER_PROMPT),
        ),
        "source_worker": WorkerLoop(
            name="source_worker",
            loop=_make_loop([GetFileTool(), GetRepoTreeTool()], SOURCE_WORKER_PROMPT),
        ),
        "diff_worker": WorkerLoop(
            name="diff_worker",
            loop=_make_loop([GetCommitDiffTool(), GetFileTool()], DIFF_WORKER_PROMPT),
        ),
    }
