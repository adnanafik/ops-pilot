"""TriageAgent — root cause analysis for CI/CD failures.

Given a Failure (pipeline metadata + log tail + diff summary), this agent:
1. Analyses the log output to identify the failing assertion or exception.
2. Cross-references the diff summary to pinpoint which commit introduced
   the regression.
3. Classifies severity and production impact.
4. Returns a structured Triage model with high confidence explanation.

This is the most analytically complex agent in the system — its output
directly determines the quality of the fix suggestion.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from agents.base_agent import BaseAgent
from shared.models import Failure, Severity, Triage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Site Reliability Engineer performing root cause analysis on CI/CD failures.

Your job:
1. Identify the EXACT root cause from the log output and diff summary.
2. Explain clearly which line/function/change caused the failure.
3. Classify severity: critical (production down), high (blocking deploy), medium (non-blocking), low (test-only).
4. Assess production impact: none, low, medium, high.
5. State your fix confidence: HIGH (certain), MEDIUM (likely), LOW (unclear).

Be concise, technical, and precise. Avoid filler phrases. Output valid JSON matching the schema."""

SCHEMA = """{
  "output": "<narrative root cause explanation, 2-4 sentences>",
  "severity": "low|medium|high|critical",
  "affected_service": "<service or component name>",
  "regression_introduced_in": "<commit SHA>",
  "production_impact": "<none|brief description>",
  "fix_confidence": "HIGH|MEDIUM|LOW"
}"""


class TriageAgent(BaseAgent[Triage]):
    """Performs root cause analysis on CI failures using LLM reasoning.

    Input:  ``Failure`` — pipeline info, log tail, diff summary
    Output: ``Triage``  — structured root cause analysis
    """

    def describe(self) -> str:
        """Return a one-line description of this agent's responsibility."""
        return "Analyses CI logs and diffs to identify root causes and classify severity"

    def run(self, failure: Failure) -> Triage:
        """Perform root cause analysis on a CI failure.

        Args:
            failure: Complete failure payload from MonitorAgent.

        Returns:
            Triage model with root cause, severity, and fix confidence.
        """
        from shared.models import AgentStatus

        self._status = AgentStatus.RUNNING
        logger.info("TriageAgent: analysing failure %s", failure.id)

        user_message = self._build_prompt(failure)

        try:
            raw = self._call_llm(
                system=SYSTEM_PROMPT,
                user=user_message,
                max_tokens=1024,
            )
            data = self._parse_response(raw)
            triage = Triage(
                failure_id=failure.id,
                output=data["output"],
                severity=Severity(data["severity"]),
                affected_service=data["affected_service"],
                regression_introduced_in=data.get(
                    "regression_introduced_in", failure.pipeline.commit
                ),
                production_impact=data.get("production_impact"),
                fix_confidence=data.get("fix_confidence", "MEDIUM"),
                timestamp=datetime.utcnow(),
            )
            self._status = AgentStatus.COMPLETE
            logger.info(
                "TriageAgent: complete — severity=%s confidence=%s",
                triage.severity,
                triage.fix_confidence,
            )
            return triage

        except Exception as exc:
            self._status = AgentStatus.FAILED
            logger.error("TriageAgent: failed — %s", exc)
            raise

    def _build_prompt(self, failure: Failure) -> str:
        """Build the user prompt from the failure data."""
        log_tail = "\n".join(failure.failure.log_tail)
        files_changed = ", ".join(failure.diff_summary.files_changed)

        return f"""## CI Failure to Triage

**Repository:** {failure.pipeline.repo}
**Branch:** {failure.pipeline.branch}
**Commit:** {failure.pipeline.commit} — "{failure.pipeline.commit_message}"
**Author:** {failure.pipeline.author}
**Job:** {failure.failure.job} / Step: {failure.failure.step}
**Exit code:** {failure.failure.exit_code}

### Failing log output
```
{log_tail}
```

### Diff summary (commit {failure.pipeline.commit})
- Files changed: {files_changed}
- Lines added: {failure.diff_summary.lines_added}, removed: {failure.diff_summary.lines_removed}
- Key change: {failure.diff_summary.key_change}

### Expected JSON output schema
{SCHEMA}

Respond with ONLY the JSON object. No markdown fences, no explanation outside the JSON."""

    def _parse_response(self, raw: str) -> dict:
        """Extract and parse the JSON from the LLM response.

        The model is instructed to return only JSON, but we strip any
        accidental markdown fences as a safety measure.
        """
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop opening ```json and closing ```
            text = "\n".join(
                line for line in lines
                if not line.startswith("```")
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error("TriageAgent: failed to parse JSON response: %s\n%s", exc, raw)
            raise ValueError(f"LLM returned non-JSON response: {exc}") from exc
