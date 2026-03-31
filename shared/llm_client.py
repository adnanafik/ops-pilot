"""Re-export shim — superseded by shared.llm_backend.

All callers have been updated to import from shared.llm_backend directly.
"""
from shared.llm_backend import (  # noqa: F401
    AnthropicBackend,
    BedrockBackend,
    LLMBackend,
    VertexBackend,
    make_backend,
)

# Legacy aliases used by scripts/watch_and_fix.py before the Protocol refactor
LLMClient = LLMBackend
make_client = make_backend
