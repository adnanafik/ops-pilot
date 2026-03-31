"""Abstract base class for all ops-pilot agents.

Every agent extends BaseAgent and implements:
- ``run()``       — execute the agent's logic and return a structured output
- ``describe()``  — return a one-line human-readable description of what this
                    agent does (used in logs and the demo UI)

The LLM client is injected via the constructor so agents are testable without
network access and the provider can be swapped without touching agent logic.
"""

from __future__ import annotations

import abc
import logging
import os
from typing import Generic, TypeVar

from shared.llm_backend import AnthropicBackend, LLMBackend
from shared.models import AgentStatus

logger = logging.getLogger(__name__)

# TypeVar for the structured output model each concrete agent returns
OutputT = TypeVar("OutputT")


class BaseAgent(abc.ABC, Generic[OutputT]):
    """Abstract base for all ops-pilot agents.

    Args:
        backend: An ``LLMBackend`` instance. If not provided, an
                 ``AnthropicBackend`` is created from ``ANTHROPIC_API_KEY``
                 in the environment. Use ``shared.llm_backend.make_backend(cfg)``
                 to select the right backend (Anthropic, Bedrock, Vertex AI)
                 based on your config.
        model:   Model ID to use. Format varies per backend:
                 - Anthropic direct: ``claude-sonnet-4-6``
                 - Bedrock: ``anthropic.claude-sonnet-4-5-20251001-v1:0``
                 - Vertex AI: ``claude-sonnet-4-5-20251001``
    """

    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(
        self,
        backend: LLMBackend | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the agent with an injected or auto-created LLM backend.

        No business logic here — only dependency wiring.
        """
        self.backend: LLMBackend = backend or AnthropicBackend(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self.model = model or self.DEFAULT_MODEL
        self._status: AgentStatus = AgentStatus.PENDING

    @abc.abstractmethod
    def run(self, *args, **kwargs) -> OutputT:
        """Execute the agent's primary logic.

        Subclasses must implement this method. It should:
        1. Accept typed Pydantic models as inputs (no raw dicts).
        2. Return a typed Pydantic model as output.
        3. Update ``self._status`` to RUNNING at the start and COMPLETE/FAILED
           at the end.
        """

    @abc.abstractmethod
    def describe(self) -> str:
        """Return a one-line description of this agent's responsibility."""

    @property
    def name(self) -> str:
        """Snake-case name derived from the class name, e.g. 'triage_agent'."""
        cls = type(self).__name__
        # Convert CamelCase to snake_case
        import re
        return re.sub(r"(?<!^)(?=[A-Z])", "_", cls).lower()

    @property
    def status(self) -> AgentStatus:
        """Current execution status of this agent instance."""
        return self._status

    def _call_llm(self, system: str, user: str, max_tokens: int = 2048) -> str:
        """Make a single-turn LLM call and return the text response.

        Delegates to ``self.backend.complete()`` — the backend handles all
        cloud-provider-specific details (auth, SDK, model ID format).

        Args:
            system:     System prompt describing the agent's role.
            user:       User message containing the task details.
            max_tokens: Maximum tokens in the response.

        Returns:
            The model's text response.
        """
        logger.debug("%s: calling %s via %s", self.name, self.model, type(self.backend).__name__)
        return self.backend.complete(
            system=system,
            user=user,
            model=self.model,
            max_tokens=max_tokens,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r}, status={self._status.value!r})"
