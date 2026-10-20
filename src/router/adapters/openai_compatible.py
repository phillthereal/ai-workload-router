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
_MOONSHOT_EFFORTS = ("low", "high", "max")


class OpenAICompatibleAdapter(Adapter):
    """Real Adapter backed by an OpenAI-compatible chat/completions endpoint."""

    def __init__(
        self, model_name: str, api_key: str, base_url: str, provider: str = ""
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url

    def build_body(
        self, prompt: str, effort: Optional[str], max_tokens: Optional[int]
    ) -> dict:
        body = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens or _MAX_TOKENS,
        }
        # !! Moonshot kimi-k3 ALWAYS reasons. effort=None sends NO
        # reasoning_effort, and Moonshot then defaults to "max" -- the MOST
        # EXPENSIVE setting (reasoning is billed as output tokens). Pass an
        # explicit "low"/"high" to spend less. Never send Anthropic-style
        # thinking/output_config here.
        if self.provider == "moonshot" and effort in _MOONSHOT_EFFORTS:
            body["reasoning_effort"] = effort
        return body

    def complete(
        self,
        prompt: str,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Response:
        """
        Complete `prompt` against an OpenAI-compatible endpoint.

        `effort` is ignored except for provider "moonshot" (see build_body);
        elsewhere it is an Anthropic parameter with no equivalent here. Neither model in the cross-vendor roster that uses this
        adapter (gpt-4o-mini, deepseek-chat) supports it, so router.gates never
        constructs such a pairing — the parameter is in the signature only to
        satisfy the Adapter contract.
        """
        import httpx  # lazy import — keeps the offline path dependency-free

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = self.build_body(prompt, effort, max_tokens)

        last_latency_ms = 0.0
        last_error: Optional[str] = None
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
                    output_tokens=_billed_output_tokens(usage),
                    latency_ms=latency_ms,
                    model=self.model_name,
                    simulated=False,
                    success=True,
                )
            except Exception as exc:
                last_latency_ms = (time.perf_counter() - start) * 1000
                last_error = _safe_error(exc)
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
            error=last_error,
        )


def _safe_error(exc: Exception) -> str:
    """HTTP status + provider error *type* only — never the message body."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return type(exc).__name__
    try:
        etype = resp.json().get("error", {}).get("type")
    except Exception:  # noqa: BLE001 - non-JSON error body
        etype = None
    etype = etype if isinstance(etype, str) and etype.replace("_", "").isalnum() else None
    return f"HTTP {resp.status_code}" + (f" {etype}" if etype else "")


def _billed_output_tokens(usage: dict) -> int:
    """
    Output tokens to bill, reasoning included.

    ASSUMPTION (unverified until the first live Moonshot call): like OpenAI,
    `completion_tokens` already INCLUDES reasoning tokens
    (completion_tokens_details.reasoning_tokens is a subset). If a response
    instead reports reasoning separately so that reasoning_tokens >
    completion_tokens, it cannot be a subset, so we add it.
    """
    completion = usage.get("completion_tokens", 0) or 0
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    return completion + reasoning if reasoning > completion else completion
