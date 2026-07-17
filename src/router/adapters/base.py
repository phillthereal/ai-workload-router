"""
Provider adapter layer (P0-1).

Normalizes calls across multiple LLM providers (Anthropic, OpenAI, Google,
DeepSeek, ...) into one interface: ``Adapter.complete(prompt) -> Response``.
Every adapter — real or mock — takes a raw prompt string and returns a
normalized Response with output text, token counts, latency, and the model
that produced it, so the rest of the pipeline (router, scoring, db, report)
never has to know which provider it's talking to.

``router.adapters.get_adapter()`` returns a real, cached adapter
(AnthropicAdapter / OpenAICompatibleAdapter) for any model whose provider
API key is configured in .env, and falls back to ``MockAdapter`` (see
mock.py) for any model without a key — or if the real call keeps failing —
so the benchmark always completes, offline or online.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Response:
    """Normalized response returned by any provider adapter."""

    text: str
    """The completion text."""

    input_tokens: int
    """Tokens consumed by the prompt."""

    output_tokens: int
    """Tokens produced in the completion."""

    latency_ms: float
    """Round-trip latency in milliseconds."""

    model: str
    """Name of the model that produced this response."""

    simulated: bool
    """True if this response was fabricated offline (MockAdapter) rather than
    coming from a real provider call."""

    success: bool = True
    """False if a real provider call failed (error, timeout, or a safety
    refusal) after retries. MockAdapter responses are always successful.
    A failed real-adapter Response has empty text and zero token counts;
    get_adapter()'s fallback wrapper turns a failed call into a simulated
    MockAdapter response so the benchmark run always completes."""


class Adapter(ABC):
    """Common interface every provider adapter implements."""

    @abstractmethod
    def complete(self, prompt: str) -> Response:
        """
        Send `prompt` to the underlying model and return a normalized Response.

        Args:
            prompt: The raw input text.

        Returns:
            Response with completion text, token counts, latency, and model name.
        """
        raise NotImplementedError


# Real adapters + record/replay cache keyed on (model, sha256(prompt)) are
# implemented in anthropic_adapter.py, openai_compatible.py, cache.py, and
# wired together in __init__.py:get_adapter(). See those modules for the
# fallback-to-mock and caching behavior.
