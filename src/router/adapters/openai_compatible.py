"""
Real adapter for OpenAI-compatible chat/completions APIs (plain HTTP via
httpx — no OpenAI SDK). Used for both OpenAI (gpt-4o-mini) and DeepSeek
(deepseek-chat), which expose the same request/response shape at different
base URLs.

httpx is imported lazily, inside complete(), so importing this module never
requires httpx to be installed on the offline mock-only path (even though
httpx is already a project dependency, this keeps the same discipline as
the Anthropic adapter and the base.py contract).
"""

from __future__ import annotations

import time
from typing import Optional

from .base import Adapter, Response

_MAX_TOKENS = 1024
_MAX_ATTEMPTS = 2
_TIMEOUT_S = 60.0


class OpenAICompatibleAdapter(Adapter):
    """Real Adapter backed by an OpenAI-compatible chat/completions endpoint."""

    def __init__(self, model_name: str, api_key: str, base_url: str) -> None:
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url

    def complete(
        self,
        prompt: str,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Response:
        """
        Complete `prompt` against an OpenAI-compatible endpoint.

        `effort` is accepted and ignored: it is an Anthropic parameter with no
        equivalent here. Neither model in the cross-vendor roster that uses this
        adapter (gpt-4o-mini, deepseek-chat) supports it, so router.gates never
        constructs such a pairing — the parameter is in the signature only to
        satisfy the Adapter contract.
        """
        import httpx  # lazy import — keeps the offline path dependency-free

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens or _MAX_TOKENS,
        }

        last_latency_ms = 0.0
        for attempt in range(_MAX_ATTEMPTS):
            start = time.perf_counter()
            try:
                http_response = httpx.post(
                    self.base_url, headers=headers, json=body, timeout=_TIMEOUT_S
                )
                http_response.raise_for_status()
                data = http_response.json()
                text = data["choices"][0]["message"]["content"] or ""
                usage = data.get("usage", {})
                latency_ms = (time.perf_counter() - start) * 1000
                return Response(
                    text=text,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    latency_ms=latency_ms,
                    model=self.model_name,
                    simulated=False,
                    success=True,
                )
            except Exception:
                last_latency_ms = (time.perf_counter() - start) * 1000
                continue

        # Every attempt raised (HTTP error, timeout, malformed JSON, ...) —
        # surface a failed-but-non-crashing Response so get_adapter()'s
        # fallback wrapper can degrade to the mock rather than blocking the
        # whole benchmark run.
        return Response(
            text="",
            input_tokens=0,
            output_tokens=0,
            latency_ms=last_latency_ms,
            model=self.model_name,
            simulated=False,
            success=False,
        )
