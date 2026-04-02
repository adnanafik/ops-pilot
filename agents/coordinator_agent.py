"""CoordinatorAgent — multi-agent orchestration for complex CI failures.

Replaces TriageAgent in the pipeline for failures that need deep investigation
(as determined by InvestigationRouter). Returns the same Triage type as
TriageAgent, so the pipeline slot is unchanged.

How it works:
  1. Builds three specialist workers (LogWorker, SourceWorker, DiffWorker),
     each with its own AgentLoop and scoped tool list.
  2. Runs a coordinator AgentLoop with a single tool: SpawnWorkerTool.
  3. The coordinator spawns all three workers in one turn. AgentLoop executes
     them concurrently via asyncio.gather.
  4. Each worker returns a findings summary. The coordinator synthesises them.
  5. Post-loop extraction produces a Triage from the synthesis.

For simple failures, use TriageAgent (single loop, faster, cheaper).
InvestigationRouter makes the routing decision before this agent is constructed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from agents.base_agent import BaseAgent
from agents.tools.coordinator_tools import SpawnWorkerTool, build_workers
from shared.agent_loop import AgentLoop, LoopOutcome, LoopResult, ToolContext
from shared.models import AgentStatus, Failure, Severity, Triage

if TYPE_CHECKING:
    from providers.base import CIProvider

logger = logging.getLogger(__name__)


# ── Coordinator system prompt ──────────────────────────────────────────────────
# The coordinator's job is narrow: dispatch workers, then synthesise.
# It should NOT do its own investigation — that's what workers are for.
# Long coordinator prompts produce coordinators that try to do everything
# themselves (wasting the parallel architecture).

COORDINATOR_SYSTEM_PROMPT = """You are a senior SRE coordinating a parallel CI failure investigation.

You have three specialist workers available via spawn_worker:
  - log_worker:    fetches CI log sections — finds the full error sequence
  - source_worker: reads source files at failing lines — identifies the buggy code
  - diff_worker:   reads the commit diff — identifies what changed and why

Investigation procedure:

TURN 1 — Spawn all three workers simultaneously with targeted tasks.
  Include concrete details from the failure context in each task:
    - log_worker: specify the job name and ask it to fetch from offset=0
    - source_worker: specify the file path and failing line number if known
    - diff_worker: specify the commit SHA
  All three must be spawned in the SAME turn to run in parallel.

TURN 2 — Synthesise worker findings into a root cause.
  - What is the exact root cause? Cite the specific evidence.
  - Which worker's findings are most conclusive?
  - Do findings agree? If not, note the conflict and how it affects confidence.
  - State fix_confidence honestly: HIGH only if the cause is unambiguous
    across multiple workers' findings."""


class CoordinatorAgent(BaseAgent[Triage]):
    """Coordinates parallel specialist workers for deep CI failure investigation.

    Same interface as TriageAgent — both accept Failure, both return Triage.
    The pipeline can swap between them based on InvestigationRouter's decision
    without knowing which strategy is running.

    Args:
        backend:          LLM backend. Shared across coordinator and all workers.
        model:            Model ID. Same model used for coordinator and workers.
        provider:         CIProvider for worker tool execution.
        max_turns:        Coordinator turn limit. Default 4: spawn turn + synthesis
                          turn + 2 buffer turns for clarification or retry.
        worker_max_turns: Per-worker turn limit. Keep ≤ 5 to contain costs.
                          Workers are targeted; they should not explore broadly.
    """

    def __init__(
        self,
        backend=None,
        model: str | None = None,
        provider: CIProvider | None = None,
        max_turns: int = 4,
        worker_max_turns: int = 5,
    ) -> None:
        super().__init__(backend=backend, model=model)
        self._provider = provider
        self._max_turns = max_turns
        self._worker_max_turns = worker_max_turns

    def describe(self) -> str:
        return (
            "Coordinates parallel specialist workers (log, source, diff) "
            "to investigate complex CI failures, then synthesises findings into a Triage"
        )

    def run(self, failure: Failure) -> Triage:
        """Coordinate parallel investigation and return a Triage result.

        Synchronous entry point — bridges into async coordinator loop via
        asyncio.run(). Same bridge pattern as TriageAgent.

        Args:
            failure: CI failure payload from MonitorAgent.

        Returns:
            Triage with root cause synthesised from all worker findings.
            fix_confidence is LOW if coordinator hit its turn limit or workers
            returned conflicting findings.
        """
        self._status = AgentStatus.RUNNING
        logger.info("CoordinatorAgent: starting deep investigation for %s", failure.id)

        try:
            result: LoopResult[Triage] = asyncio.run(self._run_coordinator(failure))
            triage = self._loop_result_to_triage(result, failure)

            self._status = AgentStatus.COMPLETE
            logger.info(
                "CoordinatorAgent: complete — outcome=%s severity=%s confidence=%s turns=%d",
                result.outcome.value,
                triage.severity.value,
                triage.fix_confidence,
                result.turns_used,
            )
            if result.failed_tools:
                logger.warning(
                    "CoordinatorAgent: tools that errored — %s", result.failed_tools
                )
            return triage

        except Exception as exc:
            self._status = AgentStatus.FAILED
            logger.error("CoordinatorAgent: failed — %s", exc)
            raise

    async def _run_coordinator(self, failure: Failure) -> LoopResult[Triage]:
        """Build workers and run the coordinator loop."""
        workers = build_workers(self.backend, self.model, self._worker_max_turns)
        spawn_tool = SpawnWorkerTool(workers)

        loop: AgentLoop[Triage] = AgentLoop(
            tools=[spawn_tool],
            backend=self.backend,
            domain_system_prompt=COORDINATOR_SYSTEM_PROMPT,
            response_model=Triage,
            model=self.model,
            max_turns=self._max_turns,
        )
        ctx = ToolContext(
            provider=self._provider,
            failure=failure,
        )
        messages = [{"role": "user", "content": self._build_initial_message(failure)}]
        return await loop.run(messages=messages, ctx=ctx)

    @staticmethod
    def _build_initial_message(failure: Failure) -> str:
        """Build the coordinator's initial user message with full failure context."""
        log_tail = "\n".join(failure.failure.log_tail)
        files_changed = ", ".join(failure.diff_summary.files_changed) or "(none listed)"

        return f"""## CI Failure requiring deep investigation

**Failure ID:** {failure.id}
**Repository:** {failure.pipeline.repo}
**Branch:** {failure.pipeline.branch}
**Commit:** {failure.pipeline.commit} — "{failure.pipeline.commit_message}"
**Author:** {failure.pipeline.author}
**Job:** {failure.failure.job} / Step: {failure.failure.step}
**Exit code:** {failure.failure.exit_code}

### Log tail (last ~50 lines)
```
{log_tail}
```

### Diff summary
Files changed: {files_changed}
Lines added: {failure.diff_summary.lines_added}, removed: {failure.diff_summary.lines_removed}
Key change: {failure.diff_summary.key_change}

Spawn all three workers now with concrete tasks based on the above context."""

    @staticmethod
    def _loop_result_to_triage(result: LoopResult[Triage], failure: Failure) -> Triage:
        """Convert LoopResult to Triage. Same pattern as TriageAgent."""
        if result.extracted is not None:
            triage = result.extracted
            triage.failure_id = failure.id
            triage.timestamp = datetime.utcnow()
            if result.outcome != LoopOutcome.COMPLETED:
                triage.fix_confidence = "LOW"
            return triage

        escalation_reason = {
            LoopOutcome.TURN_LIMIT: (
                f"Deep investigation cut short after {result.turns_used} turns. "
                f"Partial findings: {result.last_assistant_text[:300] or '(none)'}"
            ),
            LoopOutcome.TOOL_FAILURE: (
                "Worker failures blocked deep investigation. "
                f"Failed tools: {result.failed_tools}. "
                f"Partial findings: {result.last_assistant_text[:300] or '(none)'}"
            ),
            LoopOutcome.COMPLETED: (
                "Coordinator completed but structured extraction failed. "
                f"Partial findings: {result.last_assistant_text[:300] or '(none)'}"
            ),
        }
        return Triage(
            failure_id=failure.id,
            output=escalation_reason.get(result.outcome, "Coordinator failed."),
            severity=Severity.MEDIUM,
            affected_service=failure.failure.job,
            regression_introduced_in=failure.pipeline.commit,
            production_impact=None,
            fix_confidence="LOW",
            timestamp=datetime.utcnow(),
        )
