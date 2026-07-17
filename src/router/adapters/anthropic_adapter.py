"""
Real Anthropic provider adapter.

Uses the `anthropic` SDK (imported lazily, inside complete(), so importing
this module — or the package — never requires the SDK to be installed on
the offline mock-only path). Also used as the LLM-as-judge model
(claude-opus-4-8) by router.scoring._real_rubric_judge, via the exact same
adapter + cache path used for benchmark task completions.

EFFORT AND THINKING — THE THREE STATES, AND WHY THERE ARE THREE:

`effort` is tri-state, not a simple Optional, because omitting the thinking
parameter does NOT mean the same thing on every current Anthropic model:

  - Opus 4.8 with no `thinking` field runs WITHOUT thinking.
  - Sonnet 5 with no `thinking` field runs WITH adaptive thinking.

So a naive "effort=None means send nothing" would have Sonnet 5 silently
thinking while Haiku and Opus did not — inflating Sonnet's cost AND its quality
in the same run, and confounding the entire (model x effort) comparison. The
three states disambiguate:

  effort=None       -> send no thinking/output_config at all. Byte-identical to
                       the request shape the published v1 benchmark used, which
                       is what keeps the v1 result reproducible and its 123
                       cached responses valid.
  effort="off"      -> send thinking={"type": "disabled"} explicitly. The
                       apples-to-apples no-thinking baseline for the v2 grid,
                       identical across every model in the Claude ladder.
  effort=<level>    -> send thinking={"type": "adaptive"} + output_config
                       {"effort": level}.

Haiku 4.5 supports none of this (it predates output_config.effort and 400s on
it); router.gates.effort_supported() is what stops those pairings ever being
constructed, so this adapter only ever sees valid combinations.

NOT SENT: temperature / top_p / top_k. Opus 4.8 and Sonnet 5 reject them
outright with a 400.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..config import EFFORT_OFF, get_model
from .base import Adapter, Response

_MAX_TOKENS = 1024
"""Default output ceiling, unchanged from v1 so the published run reproduces."""

_MAX_TOKENS_WITH_THINKING = 8192
"""Ceiling used whenever adaptive thinking is on. Thinking tokens are billed as
output AND count against max_tokens, so leaving this at 1024 would let a
high-effort run spend its whole budget reasoning and return a truncated answer —
which scores as a quality failure that is really a config bug. See
Response.truncated."""

_MAX_ATTEMPTS = 2


class AnthropicAdapter(Adapter):
    """Real Adapter backed by the Anthropic Messages API."""

    def __init__(self, model_name: str, api_key: str) -> None:
        self.model_name = model_name
        self.api_key = api_key

    def _request_kwargs(
        self, effort: Optional[str], max_tokens: Optional[int]
    ) -> dict[str, Any]:
        """Build the model-specific request parameters for this effort state."""
        thinking_on = effort is not None and effort != EFFORT_OFF
        kwargs: dict[str, Any] = {
            "max_tokens": max_tokens
            or (_MAX_TOKENS_WITH_THINKING if thinking_on else _MAX_TOKENS),
        }

        if effort is None:
            return kwargs  # v1 request shape — send nothing extra

        if not get_model(self.model_name).supports_effort:
            # Should be unreachable: router.gates rejects this pairing before a
            # request is ever built. Degrade to the v1 shape rather than send a
            # parameter we know the API will 400 on.
            return kwargs

        if effort == EFFORT_OFF:
            kwargs["thinking"] = {"type": "disabled"}
            return kwargs

        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort}
        return kwargs

    def complete(
        self,
        prompt: str,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Response:
        import anthropic  # lazy import — keeps the offline path dependency-free

        client = anthropic.Anthropic(api_key=self.api_key)
        request_kwargs = self._request_kwargs(effort, max_tokens)

        last_latency_ms = 0.0
        for _attempt in range(_MAX_ATTEMPTS):
            start = time.perf_counter()
            try:
                message = client.messages.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    **request_kwargs,
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
                    effort=effort,
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
                effort=effort,
                truncated=message.stop_reason == "max_tokens",
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
            effort=effort,
        )
