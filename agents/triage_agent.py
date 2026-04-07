"""TriageAgent — root cause analysis for CI/CD failures.

Phase 1 upgrade: now uses AgentLoop instead of a single LLM call. The model
can request additional context via three tools before concluding:
  - get_file:        read the source file at the offending line
  - get_more_log:    fetch earlier sections of the CI log
  - get_commit_diff: read the full unified diff, not just the summary

The loop exits when the model decides it has enough signal (end_turn with no
tool calls), or when the turn limit is reached (safety net). After exit, a
second extraction call converts the conversation history into a Triage model.

If the loop exits with LOW confidence, the result is still returned — but
the caller (watch loop) is responsible for routing it to escalation rather
than proceeding to FixAgent.

Async bridge note: run() uses asyncio.run() to bridge the synchronous watch
loop into the async AgentLoop. This works because the watch loop is a plain
Python script with no pre-existing event loop. Phase 3 will make the full
pipeline async and remove this bridge.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from agents.base_agent import BaseAgent
from agents.tools.triage_tools import GetCommitDiffTool, GetFileTool, GetMoreLogTool
from shared.agent_loop import AgentLoop, LoopOutcome, LoopResult, Permission, ToolContext
from shared.models import AgentStatus, Failure, Severity, Triage
from shared.tenant_context import TenantContext
from shared.tool_registry import ToolRegistry
from shared.trust_context import TrustContext

if TYPE_CHECKING:
    from providers.base import CIProvider
    from shared.context_budget import ContextBudget

logger = logging.getLogger(__name__)


# ── System prompt ──────────────────────────────────────────────────────────────
# This is the domain prompt — what the model is and what it's investigating.
# AgentLoop appends the loop mechanics footer (when to stop, extraction schema).
# Keep these concerns separate: this prompt should not mention end_turn or JSON.

SYSTEM_PROMPT = """You are an expert Site Reliability Engineer performing root cause analysis on CI/CD failures.

You have tools to gather evidence before concluding. Use them.

Your goal:
1. Identify the EXACT root cause — not just the symptom. If the log shows an
   AssertionError, find what changed to cause it.
2. Read the actual source file at the failing line, not just the filename.
3. Fetch earlier log sections if the tail only shows a symptom.
4. Look at the full commit diff if the failure looks regression-related.
5. Classify severity: critical (prod down), high (blocking deploy),
   medium (non-blocking), low (test-only).
6. State your fix confidence honestly: HIGH (certain of root cause),
   MEDIUM (likely but not proven), LOW (genuinely unclear).

Do not guess. Do not fabricate certainty. LOW confidence with a clear
explanation of what you couldn't determine is more useful than HIGH confidence
built on incomplete evidence."""


class TriageAgent(BaseAgent[Triage]):
    """Performs root cause analysis on CI failures using an agentic loop.

    The agent is given three tools and can call them in any order, any number
    of times, until it decides it has enough signal. This replaces the original
    single-call design where the model had to guess from a fixed log tail.

    Args:
        backend:  LLM backend (Anthropic, Bedrock, or Vertex). Must implement
                  both complete() and complete_with_tools().
        model:    Model ID in the appropriate format for the backend.
        provider: CIProvider for tool execution. Optional — if None, tools will
                  return "not available" and the model falls back to log tail only.
                  In production, always pass a provider.
        max_turns: Maximum tool-use turns before the loop exits with TURN_LIMIT.
                   10 is a reasonable default; increase if you see frequent limit hits.
    """

    def __init__(
        self,
        backend=None,
        model: str | None = None,
        provider: CIProvider | None = None,
        max_turns: int = 10,
        registry: ToolRegistry | None = None,
        context_budget: ContextBudget | None = None,
        tenant_context: TenantContext | None = None,
        trust_context: TrustContext | None = None,
    ) -> None:
        super().__init__(backend=backend, model=model)
        self._provider = provider
        self._max_turns = max_turns
        self._context_budget = context_budget
        self._tenant_context = tenant_context
        self._trust_context = trust_context
        if registry is None:
            registry = ToolRegistry()
            registry.register(GetFileTool())
            registry.register(GetMoreLogTool())
            registry.register(GetCommitDiffTool())
        self._registry = registry

    def describe(self) -> str:
        return "Analyses CI logs and diffs to identify root causes via agentic tool use"

    def run(self, failure: Failure) -> Triage:
        """Perform root cause analysis on a CI failure.

        Runs the AgentLoop synchronously via asyncio.run(). See module docstring
        for why this is safe in Phase 1 and when it will be removed.

        Args:
            failure: Complete failure payload from MonitorAgent.

        Returns:
            Triage with root cause, severity, and fix confidence.
            fix_confidence will be LOW if the loop hit its turn limit or if
            the model couldn't determine the root cause with confidence.
        """
        self._status = AgentStatus.RUNNING
        logger.info("TriageAgent: analysing failure %s", failure.id)

        try:
            result: LoopResult[Triage] = asyncio.run(self._run_loop(failure))
            triage = self._loop_result_to_triage(result, failure)

            self._status = AgentStatus.COMPLETE
            logger.info(
                "TriageAgent: complete — outcome=%s severity=%s confidence=%s turns=%d",
                result.outcome.value,
                triage.severity.value,
                triage.fix_confidence,
                result.turns_used,
            )
            if result.failed_tools:
                logger.warning("TriageAgent: tools that errored — %s", result.failed_tools)

            return triage

        except Exception as exc:
            self._status = AgentStatus.FAILED
            logger.error("TriageAgent: failed — %s", exc)
            raise

    async def _run_loop(self, failure: Failure) -> LoopResult[Triage]:
        """Build and run the AgentLoop for one failure."""
        loop: AgentLoop[Triage] = AgentLoop(
            tools=self._registry.get_tools(max_permission=Permission.READ_ONLY),
            backend=self.backend,
            domain_system_prompt=SYSTEM_PROMPT,
            response_model=Triage,
            model=self.model,
            max_turns=self._max_turns,
            context_budget=self._context_budget,
            tenant_context=self._tenant_context,
            trust_context=self._trust_context,
            actor="TriageAgent",
        )
        ctx = ToolContext(
            provider=self._provider,
            failure=failure,
            tenant_id=self._tenant_context.tenant_id if self._tenant_context else "",
        )
        messages = [{"role": "user", "content": self._build_initial_message(failure)}]
        return await loop.run(messages=messages, ctx=ctx)

    @staticmethod
    def _build_initial_message(failure: Failure) -> str:
        """Build the initial user message containing the failure context.

        This is the same structured prompt as before, now passed as the first
        message in a conversation rather than the single user turn. The agent
        can then call tools to get more information before concluding.
        """
        log_tail = "\n".join(failure.failure.log_tail)
        files_changed = ", ".join(failure.diff_summary.files_changed) or "(none listed)"

        return f"""## CI Failure to Investigate

**Failure ID:** {failure.id}
**Repository:** {failure.pipeline.repo}
**Branch:** {failure.pipeline.branch}
**Commit:** {failure.pipeline.commit} — "{failure.pipeline.commit_message}"
**Author:** {failure.pipeline.author}
**Job:** {failure.failure.job} / Step: {failure.failure.step}
**Exit code:** {failure.failure.exit_code}

### Log tail (last ~50 lines — use get_more_log to see earlier sections)
```
{log_tail}
```

### Diff summary (commit {failure.pipeline.commit})
Files changed: {files_changed}
Lines added: {failure.diff_summary.lines_added}, removed: {failure.diff_summary.lines_removed}
Key change: {failure.diff_summary.key_change}

Note: use get_commit_diff to see the actual code changes (+/- lines), and
get_file to read the source at any referenced path."""

    @staticmethod
    def _loop_result_to_triage(result: LoopResult[Triage], failure: Failure) -> Triage:
        """Convert a LoopResult into a Triage model.

        If extraction succeeded, use it. If it failed (None), build a minimal
        Triage from what we know — a failed extraction should never crash the
        pipeline, it should produce a LOW confidence triage for escalation.
        """
        if result.extracted is not None:
            # Extraction succeeded — use the model's output directly.
            # Re-stamp failure_id and timestamp since the extraction call
            # constructs a Triage from scratch and may leave these empty.
            triage = result.extracted
            triage.failure_id = failure.id
            triage.timestamp = datetime.utcnow()

            # Override confidence to LOW when the loop exited abnormally,
            # regardless of what the model self-reported. A model that ran
            # out of turns cannot legitimately claim HIGH confidence.
            if result.outcome != LoopOutcome.COMPLETED:
                triage.fix_confidence = "LOW"

            return triage

        # Extraction failed entirely — build a fallback Triage so the pipeline
        # can continue to the escalation path rather than crashing.
        escalation_reason = {
            LoopOutcome.TURN_LIMIT: (
                f"Investigation cut short after {result.turns_used} turns. "
                f"Partial findings: {result.last_assistant_text[:300] or '(none)'}"
            ),
            LoopOutcome.TOOL_FAILURE: (
                f"Tool errors blocked investigation. "
                f"Failed tools: {result.failed_tools}. "
                f"Partial findings: {result.last_assistant_text[:300] or '(none)'}"
            ),
            LoopOutcome.COMPLETED: (
                "Structured extraction failed despite successful investigation. "
                f"Partial findings: {result.last_assistant_text[:300] or '(none)'}"
            ),
        }
        return Triage(
            failure_id=failure.id,
            output=escalation_reason.get(result.outcome, "Triage failed."),
            severity=Severity.MEDIUM,
            affected_service=failure.failure.job,
            regression_introduced_in=failure.pipeline.commit,
            production_impact=None,
            fix_confidence="LOW",
            timestamp=datetime.utcnow(),
        )
