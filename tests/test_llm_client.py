"""Tests for the LLM backend abstraction (shared/llm_backend.py)."""

from __future__ import annotations

import pytest

from shared.config import OpsPilotConfig
from shared.llm_backend import (
    AnthropicBackend,
    BedrockBackend,
    LLMBackend,
    VertexBackend,
    make_backend,
)


def _cfg(**kwargs) -> OpsPilotConfig:
    """Build a minimal OpsPilotConfig with overrides."""
    base: dict = {"pipelines": []}
    base.update(kwargs)
    return OpsPilotConfig(**base)


class TestLLMBackendProtocol:
    def test_anthropic_backend_satisfies_protocol(self) -> None:
        backend = AnthropicBackend(api_key="sk-ant-test")
        assert isinstance(backend, LLMBackend)

    def test_bedrock_backend_satisfies_protocol(self) -> None:
        backend = BedrockBackend(aws_region="us-east-1")
        assert isinstance(backend, LLMBackend)

    def test_vertex_backend_satisfies_protocol(self) -> None:
        backend = VertexBackend(project_id="my-project", region="us-east5")
        assert isinstance(backend, LLMBackend)


class TestMakeBackend:
    def test_default_returns_anthropic_backend(self) -> None:
        cfg = _cfg(anthropic_api_key="sk-ant-test")
        backend = make_backend(cfg)
        assert isinstance(backend, AnthropicBackend)

    def test_explicit_anthropic_provider(self) -> None:
        cfg = _cfg(llm_provider="anthropic", anthropic_api_key="sk-ant-test")
        backend = make_backend(cfg)
        assert isinstance(backend, AnthropicBackend)

    def test_bedrock_provider_returns_bedrock_backend(self) -> None:
        cfg = _cfg(llm_provider="bedrock", aws_region="us-east-1")
        backend = make_backend(cfg)
        assert isinstance(backend, BedrockBackend)

    def test_bedrock_without_region(self) -> None:
        cfg = _cfg(llm_provider="bedrock")
        backend = make_backend(cfg)
        assert isinstance(backend, BedrockBackend)

    def test_vertex_provider_returns_vertex_backend(self) -> None:
        cfg = _cfg(llm_provider="vertex_ai", gcp_project="my-project", gcp_region="us-east5")
        backend = make_backend(cfg)
        assert isinstance(backend, VertexBackend)

    def test_invalid_llm_provider_raises_on_config(self) -> None:
        with pytest.raises(ValueError, match="llm_provider"):
            _cfg(llm_provider="openai")


class TestOpsPilotConfigCloudFields:
    def test_has_bedrock_false_by_default(self) -> None:
        assert _cfg().has_bedrock is False

    def test_has_bedrock_true_when_provider_set(self) -> None:
        assert _cfg(llm_provider="bedrock").has_bedrock is True

    def test_has_vertex_false_by_default(self) -> None:
        assert _cfg().has_vertex is False

    def test_has_vertex_true_when_provider_set(self) -> None:
        assert _cfg(llm_provider="vertex_ai").has_vertex is True

    def test_aws_region_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        from shared.config import load_config
        cfg = load_config()
        assert cfg.aws_region == "eu-west-1"

    def test_gcp_project_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCP_PROJECT", "my-gcp-project")
        from shared.config import load_config
        cfg = load_config()
        assert cfg.gcp_project == "my-gcp-project"

    def test_gcp_region_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCP_REGION", "us-central1")
        from shared.config import load_config
        cfg = load_config()
        assert cfg.gcp_region == "us-central1"

    def test_llm_provider_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "bedrock")
        from shared.config import load_config
        cfg = load_config()
        assert cfg.llm_provider == "bedrock"

    def test_invalid_log_level_raises(self) -> None:
        with pytest.raises(ValueError, match="log_level"):
            _cfg(log_level="VERBOSE")

    def test_log_level_normalised_to_uppercase(self) -> None:
        cfg = _cfg(log_level="debug")
        assert cfg.log_level == "DEBUG"

    def test_has_slack_bot_token(self) -> None:
        assert _cfg(slack_bot_token="xoxb-test").has_slack is True

    def test_has_slack_webhook(self) -> None:
        assert _cfg(slack_webhook_url="https://hooks.slack.com/test").has_slack is True

    def test_has_slack_false_by_default(self) -> None:
        assert _cfg().has_slack is False

    def test_pipeline_invalid_provider_raises(self) -> None:
        from shared.config import PipelineConfig
        with pytest.raises(ValueError, match="provider"):
            PipelineConfig(repo="org/repo", provider="circleci")

    def test_pipeline_repo_must_have_owner(self) -> None:
        from shared.config import PipelineConfig
        with pytest.raises(ValueError, match="owner/repo"):
            PipelineConfig(repo="noslash")

    def test_jenkins_requires_code_host(self) -> None:
        from shared.config import PipelineConfig
        with pytest.raises(ValueError, match="code_host"):
            PipelineConfig(repo="org/repo", provider="jenkins", jenkins_url="https://ci.example.com")
