"""
Record-and-replay cache for real provider calls (stdlib only).

Every real API response is cached to disk under `.cache/` (gitignored),
keyed on `sha256(f"{model}\\n{prompt}")` -> a JSON file storing text,
input_tokens, output_tokens, model, and latency_ms. On a cache hit, the
stored Response is replayed with no network call (free, reproducible,
deterministic re-runs). Real latency is measured on the network call that
wrote the cache entry; replays reuse that stored value rather than
measuring near-zero disk-read latency, per the task brief.

Only successful, real (non-simulated) responses are cached — a failed call
is never persisted, so a later run can retry it instead of being stuck
replaying an error forever.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from .base import Adapter, Response
from ..secrets import refresh_cache

CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache"


def _cache_key(model: str, prompt: str) -> str:
    return hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()


def _cache_path(model: str, prompt: str) -> Path:
    return CACHE_DIR / f"{_cache_key(model, prompt)}.json"


def load_cached(model: str, prompt: str) -> Optional[Response]:
    """Return the cached Response for (model, prompt), or None on a miss."""
    path = _cache_path(model, prompt)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return Response(
        text=data["text"],
        input_tokens=data["input_tokens"],
        output_tokens=data["output_tokens"],
        latency_ms=data["latency_ms"],
        model=data["model"],
        simulated=False,
        success=True,
    )


def save_cache(model: str, prompt: str, response: Response) -> None:
    """Persist a successful, real Response to disk keyed on (model, prompt)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(model, prompt)
    payload = {
        "text": response.text,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": response.latency_ms,
        "model": response.model,
    }
    path.write_text(json.dumps(payload, indent=2))


class CachedAdapter(Adapter):
    """Wraps a real Adapter with the record/replay disk cache."""

    def __init__(self, inner: Adapter, model_name: str) -> None:
        self.inner = inner
        self.model_name = model_name

    def complete(self, prompt: str) -> Response:
        if not refresh_cache():
            cached = load_cached(self.model_name, prompt)
            if cached is not None:
                return cached

        response = self.inner.complete(prompt)
        if response.success and not response.simulated:
            save_cache(self.model_name, prompt, response)
        return response


__all__ = ["CachedAdapter", "load_cached", "save_cache", "CACHE_DIR"]
