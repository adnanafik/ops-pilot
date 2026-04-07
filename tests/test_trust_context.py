"""Tests for TrustContext and make_trust_context."""

from __future__ import annotations

from unittest.mock import MagicMock

from shared.audit_log import AuditLog
from shared.config import OpsPilotConfig, TrustConfig
from shared.explanation_gen import ExplanationGenerator
from shared.trust_context import TrustContext, make_trust_context


class TestTrustConfig:
    def test_default_explanation_model_is_empty_string(self) -> None:
        cfg = TrustConfig()
        assert cfg.explanation_model == ""

    def test_explanation_model_can_be_set(self) -> None:
        cfg = TrustConfig(explanation_model="claude-haiku-4-5-20251001")
        assert cfg.explanation_model == "claude-haiku-4-5-20251001"


class TestOpsPilotConfigTrustField:
    def test_ops_pilot_config_has_trust_field(self) -> None:
        cfg = OpsPilotConfig()
        assert hasattr(cfg, "trust")
        assert isinstance(cfg.trust, TrustConfig)


class TestMakeTrustContext:
    def test_returns_trust_context(self) -> None:
        config = OpsPilotConfig()
        backend = MagicMock()
        ctx = make_trust_context(config, backend)
        assert isinstance(ctx, TrustContext)

    def test_audit_log_is_audit_log_instance(self) -> None:
        config = OpsPilotConfig()
        backend = MagicMock()
        ctx = make_trust_context(config, backend)
        assert isinstance(ctx.audit_log, AuditLog)

    def test_explanation_generator_is_explanation_generator_instance(self) -> None:
        config = OpsPilotConfig()
        backend = MagicMock()
        ctx = make_trust_context(config, backend)
        assert isinstance(ctx.explanation_generator, ExplanationGenerator)

    def test_empty_explanation_model_resolves_to_config_model(self) -> None:
        config = OpsPilotConfig(model="claude-sonnet-4-6")
        backend = MagicMock()
        ctx = make_trust_context(config, backend)
        assert ctx.explanation_generator._model == "claude-sonnet-4-6"

    def test_explicit_explanation_model_used_when_set(self) -> None:
        config = OpsPilotConfig(
            model="claude-sonnet-4-6",
            trust=TrustConfig(explanation_model="claude-haiku-4-5-20251001"),
        )
        backend = MagicMock()
        ctx = make_trust_context(config, backend)
        assert ctx.explanation_generator._model == "claude-haiku-4-5-20251001"

    def test_backend_reference_passed_to_explanation_generator(self) -> None:
        config = OpsPilotConfig()
        backend = MagicMock()
        ctx = make_trust_context(config, backend)
        assert ctx.explanation_generator._backend is backend
