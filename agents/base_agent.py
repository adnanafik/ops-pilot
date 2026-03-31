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
from typing import Generic, Optional, TypeVar

import anthropic

from shared.models import AgentStatus

logger = logging.getLogger(__name__)

# TypeVar for the structured output model each concrete agent returns
OutputT = TypeVar("OutputT")


class BaseAgent(abc.ABC, Generic[OutputT]):
    """Abstract base for all ops-pilot agents.

    Args:
        client: An ``anthropic.Anthropic`` client instance. If not provided,
                one is created from ``ANTHROPIC_API_KEY`` in the environment.
        model:  Claude model ID to use. Defaults to claude-sonnet-4-6.
    """

    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(
        self,
        client: Optional[anthropic.Anthropic] = None,
        model: Optional[str] = None,
    ) -> None:
        """Initialize the agent with an injected or auto-created LLM client.

        No business logic here — only dependency wiring.
        """
        self.client = client or anthropic.Anthropic(
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

        Args:
            system:     System prompt describing the agent's role.
            user:       User message containing the task details.
            max_tokens: Maximum tokens in the response.

        Returns:
            The text content of the model's first response block.
        """
        logger.debug("%s: calling %s", self.name, self.model)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r}, status={self._status.value!r})"
