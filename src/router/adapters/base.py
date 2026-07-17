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
from typing import Optional


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

    effort: Optional[str] = None
    """The effort level this response was produced at, or None if no effort /
    thinking config was sent. Echoed back on the Response (rather than only
    living on the request) so the run log can attribute cost to an
    (model, effort) pair — effort changes output-token count, which is where
    the money is, so a cost figure without it is unattributable."""

    truncated: bool = False
    """True if the model hit max_tokens before finishing (stop_reason ==
    'max_tokens'). Worth surfacing because thinking tokens count against
    max_tokens: raising effort without raising max_tokens produces a response
    that is mostly reasoning and a cut-off answer, which scores as a quality
    failure when it is really a configuration error."""


class Adapter(ABC):
    """Common interface every provider adapter implements."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Response:
        """
        Send `prompt` to the underlying model and return a normalized Response.

        Args:
            prompt: The raw input text.
            effort: Thinking/effort level to request, or None to send no
                effort or thinking config at all. None is the default and
                reproduces the published v1 benchmark's exact request shape —
                important, because turning thinking on changes both cost and
                quality, so it must be opt-in rather than an accidental
                consequence of upgrading the adapter. Adapters whose provider
                does not support effort ignore this.
            max_tokens: Output token ceiling, or None for the adapter default.

        Returns:
            Response with completion text, token counts, latency, and model name.
        """
        raise NotImplementedError


# Real adapters + record/replay cache keyed on (model, sha256(prompt)) are
# implemented in anthropic_adapter.py, openai_compatible.py, cache.py, and
# wired together in __init__.py:get_adapter(). See those modules for the
# fallback-to-mock and caching behavior.
