"""
Real Anthropic provider adapter.

Uses the `anthropic` SDK (imported lazily, inside complete(), so importing
this module — or the package — never requires the SDK to be installed on
the offline mock-only path). Also used as the LLM-as-judge model
(claude-opus-4-8) by router.scoring._real_rubric_judge, via the exact same
adapter + cache path used for benchmark task completions.
"""

from __future__ import annotations

import time

from .base import Adapter, Response

_MAX_TOKENS = 1024
_MAX_ATTEMPTS = 2


class AnthropicAdapter(Adapter):
    """Real Adapter backed by the Anthropic Messages API."""

    def __init__(self, model_name: str, api_key: str) -> None:
        self.model_name = model_name
        self.api_key = api_key

    def complete(self, prompt: str) -> Response:
        import anthropic  # lazy import — keeps the offline path dependency-free

        client = anthropic.Anthropic(api_key=self.api_key)

        last_latency_ms = 0.0
        for attempt in range(_MAX_ATTEMPTS):
            start = time.perf_counter()
            try:
                message = client.messages.create(
                    model=self.model_name,
                    max_tokens=_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
            except Exception:
                last_latency_ms = (time.perf_counter() - start) * 1000
                continue

            latency_ms = (time.perf_counter() - start) * 1000

            # A safety refusal is a legitimate model decision, not a
            # transient error — don't retry it, just record the failure.
            if message.stop_reason == "refusal":
                return Response(
                    text="",
                    input_tokens=message.usage.input_tokens,
                    output_tokens=message.usage.output_tokens,
                    latency_ms=latency_ms,
                    model=self.model_name,
                    simulated=False,
                    success=False,
                )

            text = "".join(
                block.text for block in message.content if block.type == "text"
            )
            return Response(
                text=text,
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
                latency_ms=latency_ms,
                model=self.model_name,
                simulated=False,
                success=True,
            )

        # Every attempt raised — surface a failed-but-non-crashing Response
        # so get_adapter()'s fallback wrapper can degrade to the mock.
        return Response(
            text="",
            input_tokens=0,
            output_tokens=0,
            latency_ms=last_latency_ms,
            model=self.model_name,
            simulated=False,
            success=False,
        )
