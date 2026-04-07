"""LLM-backed pre-action explanation generator for REQUIRES_CONFIRMATION tools.

Called once before each REQUIRES_CONFIRMATION tool execution when a
TrustContext is present. Returns a single plain-English sentence describing
what the agent is about to do and why.

This is observability infrastructure — it must never block tool execution.
Backend failures return "" and the tool runs regardless.
"""

from __future__ import annotations

import json
import logging

from shared.llm_backend import LLMBackend

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are explaining what an automated CI agent is about to do to an engineering team. "
    "Write exactly one plain-English sentence describing the action and the reason for it. "
    "Be specific: include the file path or PR title if present. No markdown, no lists."
)


class ExplanationGenerator:
    """Generates a plain-English explanation before a REQUIRES_CONFIRMATION tool executes.

    Args:
        backend: LLM backend instance (same interface as agents use).
        model:   Model identifier to use for explanation calls. Should be a
                 cheap, fast model (e.g. Haiku) since this runs before every
                 dangerous tool call.
    """

    def __init__(self, backend: LLMBackend, model: str) -> None:
        self._backend = backend
        self._model = model

    def generate(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        context_summary: str,
    ) -> str:
        """Generate a plain-English explanation for an imminent tool call.

        Args:
            tool_name:       Registered tool name, e.g. "update_file".
            tool_input:      The input dict the model is about to send.
            context_summary: Last assistant text from the investigation
                             (2-3 sentences of what was found so far).

        Returns:
            A single plain-English sentence. Returns "" on backend failure.
        """
        sanitized_input = json.dumps(tool_input, default=str)
        user = (
            f"The agent is about to call tool '{tool_name}' with these arguments:\n"
            f"{sanitized_input}\n\n"
            f"Investigation context:\n{context_summary}\n\n"
            f"Write one sentence explaining what will happen and why."
        )
        try:
            return self._backend.complete(
                system=_SYSTEM,
                user=user,
                model=self._model,
                max_tokens=128,
            ).strip()
        except Exception as exc:
            logger.warning("ExplanationGenerator: backend call failed — %s", exc)
            return ""
