"""TrustContext — bundles audit log and explanation generator for Phase 7.

Constructed once at startup by make_trust_context() and injected into
AgentLoop alongside TenantContext. Follows the same injection pattern used
throughout Phases 5 and 6.

Phase 8 upgrade path: when human-in-the-loop approval is added, it plugs in
here — zero changes to AuditLog, ExplanationGenerator, or any tool definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from shared.audit_log import AuditLog
from shared.explanation_gen import ExplanationGenerator
from shared.llm_backend import LLMBackend

if TYPE_CHECKING:
    from shared.config import OpsPilotConfig


@dataclass
class TrustContext:
    """Runtime trust infrastructure for one agent loop.

    Attributes:
        audit_log:             Appends one record per tool call to the daily JSONL file.
        explanation_generator: Generates pre-action explanations for REQUIRES_CONFIRMATION tools.
    """

    audit_log: AuditLog
    explanation_generator: ExplanationGenerator


def make_trust_context(config: OpsPilotConfig, backend: LLMBackend) -> TrustContext:
    """Construct a TrustContext from an OpsPilotConfig and a shared backend.

    The backend is the same instance used by the calling agent — no extra
    authentication is needed, and test doubles are injected here.

    The explanation_model falls back to config.model when left empty in config,
    so operators can omit the field without losing functionality.

    Args:
        config:  OpsPilotConfig instance with optional trust.explanation_model.
        backend: LLM backend shared with the calling agent.

    Returns:
        Fully wired TrustContext ready for injection into AgentLoop.
    """
    explanation_model = config.trust.explanation_model or config.model  # type: ignore[attr-defined]
    return TrustContext(
        audit_log=AuditLog(base_dir=Path("audit")),
        explanation_generator=ExplanationGenerator(
            backend=backend,
            model=explanation_model,
        ),
    )
