"""Escalation summary generation for LOW-confidence triage results.

When TriageAgent returns fix_confidence: LOW, FixAgent is skipped entirely.
This module generates a structured summary for the on-call engineer instead,
explaining what was investigated, what remained inconclusive, and what to do next.

On backend failure, a minimal EscalationSummary is constructed directly from
triage.output so the pipeline never crashes.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from shared.llm_backend import LLMBackend
from shared.models import Failure, Triage

logger = logging.getLogger(__name__)

_SYSTEM = """You are generating an escalation summary for an on-call engineer.
The automated agent investigated a CI failure but could not determine the root cause
with sufficient confidence to apply a fix automatically.

Output valid JSON only — no markdown fences. Schema:
{
  "failure_id": "<string>",
  "tenant_id": "<string or null>",
  "what_was_investigated": "<1-2 sentences: what the agent looked at>",
  "what_was_inconclusive": "<1 sentence: what remained unclear>",
  "recommended_next_step": "<1 sentence: what the engineer should do first>"
}"""


class EscalationSummary(BaseModel):
    """Structured escalation summary for on-call engineers.

    Generated when TriageAgent returns fix_confidence: LOW. Sent to the
    engineer via NotifyAgent instead of a fix PR.
    """

    failure_id: str
    tenant_id: str | None
    what_was_investigated: str
    what_was_inconclusive: str
    recommended_next_step: str


def generate_escalation_summary(
    failure: Failure,
    triage: Triage,
    backend: LLMBackend,
    model: str,
) -> EscalationSummary:
    """Generate a structured escalation summary from a LOW-confidence triage.

    Makes one backend.complete() call to synthesize the escalation content.
    On any failure, returns a minimal EscalationSummary built directly from
    triage.output — the pipeline never crashes.

    Args:
        failure: Original CI failure payload.
        triage:  Triage result with fix_confidence == "LOW".
        backend: LLM backend for the summary call.
        model:   Model identifier.

    Returns:
        EscalationSummary with investigation context for the engineer.
    """
    user = (
        f"CI failure in {failure.pipeline.repo} — job '{failure.failure.job}', "
        f"step '{failure.failure.step}', commit {failure.pipeline.commit}.\n\n"
        f"Triage findings:\n{triage.output}\n\n"
        f"Severity: {triage.severity.value.upper()}\n"
        f"Affected service: {triage.affected_service}\n"
        f"Fix confidence: {triage.fix_confidence}\n\n"
        f"Generate the escalation summary JSON."
    )
    fallback = EscalationSummary(
        failure_id=failure.id,
        tenant_id=None,
        what_was_investigated=triage.output,
        what_was_inconclusive="Root cause could not be determined with sufficient confidence.",
        recommended_next_step=(
            f"Manually inspect the '{failure.failure.job}' job logs for commit "
            f"{failure.pipeline.commit} in {failure.pipeline.repo}."
        ),
    )
    try:
        raw = backend.complete(
            system=_SYSTEM,
            user=user,
            model=model,
            max_tokens=512,
        ).strip()
        if raw.startswith("```"):
            raw = "\n".join(
                line for line in raw.splitlines() if not line.startswith("```")
            ).strip()
        data = json.loads(raw)
        data.setdefault("failure_id", failure.id)
        data.setdefault("tenant_id", None)
        return EscalationSummary(**data)
    except Exception as exc:
        logger.error("generate_escalation_summary: failed — %s", exc)
        return fallback
