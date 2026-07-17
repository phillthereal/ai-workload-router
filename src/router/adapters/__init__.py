"""
Provider adapter layer (P0-1).

Normalizes calls across multiple LLM providers (Anthropic, OpenAI,
DeepSeek, ...) into a unified interface. Each adapter handles provider-
specific API details, token counting, and error handling, returning
normalized output and metadata (tokens, latency).

`get_adapter()` is the single factory seam every caller should go through
(never instantiate an adapter class directly). For a given model it:

  1. Returns MockAdapter immediately if AWR_FORCE_MOCK is set (test suite
     escape hatch — guarantees zero network calls regardless of configured
     keys) or if the model's provider has no API key configured in .env.
  2. Otherwise wraps the real, key-backed adapter in the record/replay disk
     cache (CachedAdapter), then in a fallback wrapper that transparently
     degrades to MockAdapter (clearly marking the Response `simulated=True`)
     if the real call fails after retries — so a missing key, a bad key, or
     a flaky provider never blocks the benchmark from completing.
"""

from __future__ import annotations

from typing import Optional

from ..config import get_model
from ..secrets import force_mock, get_api_key
from .base import Adapter, Response
from .cache import CachedAdapter
from .mock import MockAdapter

_PROVIDER_ENV_VAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GOOGLE_API_KEY",
}

_PROVIDER_BASE_URL: dict[str, str] = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}


class _FallbackToMockAdapter(Adapter):
    """Runs `primary`; on a failed real call, transparently replays MockAdapter."""

    def __init__(self, primary: Adapter, model_name: str) -> None:
        self.primary = primary
        self.model_name = model_name

    def complete(
        self,
        prompt: str,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Response:
        response = self.primary.complete(prompt, effort, max_tokens)
        if response.success:
            return response
        return MockAdapter(self.model_name).complete(prompt, effort, max_tokens)


def _build_real_adapter(model_name: str, provider: str, api_key: str) -> Adapter:
    if provider == "anthropic":
        from .anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(model_name, api_key)

    from .openai_compatible import OpenAICompatibleAdapter

    return OpenAICompatibleAdapter(model_name, api_key, _PROVIDER_BASE_URL[provider])


def get_adapter(model_name: str) -> Adapter:
    """
    Factory: return an Adapter instance for the given model name.

    Args:
        model_name: Model identifier (key in router.config.MODELS).

    Returns:
        A real, cached, fallback-wrapped Adapter if the model's provider key
        is configured (and AWR_FORCE_MOCK is not set); otherwise MockAdapter.
    """
    if force_mock():
        return MockAdapter(model_name)

    model = get_model(model_name)
    provider_supported = model.provider == "anthropic" or model.provider in _PROVIDER_BASE_URL
    env_var = _PROVIDER_ENV_VAR.get(model.provider, "")
    api_key = get_api_key(env_var)
    if not provider_supported or not api_key:
        return MockAdapter(model_name)

    real = _build_real_adapter(model_name, model.provider, api_key)
    cached = CachedAdapter(real, model_name)
    return _FallbackToMockAdapter(cached, model_name)


__all__ = ["Adapter", "Response", "MockAdapter", "get_adapter"]
