"""LLM backend abstraction for ops-pilot.

Defines the ``LLMBackend`` Protocol — any object with a ``complete()`` method
satisfies it, including mocks in tests.

Three concrete implementations ship out of the box:

  ``AnthropicBackend``  — direct Anthropic API (ANTHROPIC_API_KEY)
  ``BedrockBackend``    — AWS Bedrock (boto3 credential chain: env vars,
                          ~/.aws/credentials, or IAM instance role)
  ``VertexBackend``     — Google Cloud Vertex AI (Application Default
                          Credentials: GOOGLE_APPLICATION_CREDENTIALS,
                          gcloud auth, or service account on GCP compute)

Adding a new cloud provider (Azure, etc.) means writing one new class that
satisfies the Protocol — no changes to BaseAgent or any agent are needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import anthropic

if TYPE_CHECKING:
    from shared.config import OpsPilotConfig


@runtime_checkable
class LLMBackend(Protocol):
    """Minimal interface every LLM backend must satisfy.

    The single ``complete()`` method maps a single-turn conversation
    (system prompt + user message) to a text response. Keeping the surface
    area to one method means any cloud provider or mock can satisfy it.
    """

    def complete(self, system: str, user: str, model: str, max_tokens: int) -> str:
        """Send a single-turn prompt and return the response text.

        Args:
            system:     System prompt describing the agent's role.
            user:       User message containing the task and context.
            model:      Model identifier (format varies per backend).
            max_tokens: Maximum tokens to generate.

        Returns:
            The model's text response.
        """
        ...


# ── Concrete backends ─────────────────────────────────────────────────────────


class AnthropicBackend:
    """Direct Anthropic API backend.

    Credentials: ``ANTHROPIC_API_KEY`` environment variable or explicit
    ``api_key`` constructor argument.
    """

    def __init__(self, api_key: str = "") -> None:
        self._client = anthropic.Anthropic(api_key=api_key or None)

    def complete(self, system: str, user: str, model: str, max_tokens: int) -> str:
        """Call the Anthropic Messages API and return response text."""
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text


class BedrockBackend:
    """AWS Bedrock backend.

    Credentials resolved by boto3 in order:
      1. ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` env vars
      2. ``AWS_PROFILE`` named profile from ``~/.aws/credentials``
      3. EC2 / ECS / Lambda IAM instance role (automatic on AWS compute)

    Model IDs use the Bedrock format, e.g.:
      ``anthropic.claude-sonnet-4-5-20251001-v1:0``
    or cross-region inference IDs:
      ``us.anthropic.claude-sonnet-4-5-20251001-v1:0``
    """

    def __init__(self, aws_region: str = "") -> None:
        kwargs: dict = {}
        if aws_region:
            kwargs["aws_region"] = aws_region
        self._client = anthropic.AnthropicBedrock(**kwargs)

    def complete(self, system: str, user: str, model: str, max_tokens: int) -> str:
        """Call the Bedrock Messages API and return response text."""
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text


class VertexBackend:
    """Google Cloud Vertex AI backend.

    Credentials resolved by Application Default Credentials (ADC) in order:
      1. ``GOOGLE_APPLICATION_CREDENTIALS`` env var pointing to a service
         account JSON key file
      2. ``gcloud auth application-default login`` on a developer machine
      3. Service account attached to the GCP compute instance (automatic
         on GCE / GKE / Cloud Run)

    Model IDs use the Vertex format, e.g.:
      ``claude-sonnet-4-5-20251001``  (no ``anthropic.`` prefix on Vertex)
    """

    def __init__(self, project_id: str, region: str = "us-east5") -> None:
        self._client = anthropic.AnthropicVertex(region=region, project_id=project_id)

    def complete(self, system: str, user: str, model: str, max_tokens: int) -> str:
        """Call the Vertex AI Messages API and return response text."""
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text


# ── Factory ───────────────────────────────────────────────────────────────────


def make_backend(cfg: OpsPilotConfig) -> LLMBackend:
    """Return the appropriate ``LLMBackend`` based on ``cfg.llm_provider``.

    Args:
        cfg: Loaded ``OpsPilotConfig`` instance.

    Returns:
        ``AnthropicBackend``  when ``llm_provider`` is ``"anthropic"`` (default).
        ``BedrockBackend``    when ``llm_provider`` is ``"bedrock"``.
        ``VertexBackend``     when ``llm_provider`` is ``"vertex_ai"``.
    """
    if cfg.llm_provider == "bedrock":
        return BedrockBackend(aws_region=cfg.aws_region)
    if cfg.llm_provider == "vertex_ai":
        return VertexBackend(
            project_id=cfg.gcp_project,
            region=cfg.gcp_region or "us-east5",
        )
    return AnthropicBackend(api_key=cfg.anthropic_api_key)
