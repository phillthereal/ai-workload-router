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


def _cache_key(
    model: str,
    prompt: str,
    effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """
    Content-addressed key for one cacheable request.

    THE KEY MUST COVER EVERY REQUEST FIELD THAT CHANGES THE RESPONSE. `effort`
    and `max_tokens` both do — effort changes how much the model thinks (and so
    what it answers and what it costs), max_tokens changes where it stops. A key
    that omitted them would happily replay a high-effort answer for a low-effort
    request, silently fabricating the exact comparison the v2 grid exists to
    measure. That would not error; it would just quietly produce a wrong result.

    BACKWARD COMPATIBILITY: when effort and max_tokens are both None — the
    default, and precisely the request shape the published v1 benchmark used —
    the key degrades to v1's original `sha256(model\\nprompt)`. That keeps every
    previously-recorded response valid instead of invalidating the whole cache
    and forcing a paid re-run of a result that is already published.
    """
    if effort is None and max_tokens is None:
        payload = f"{model}\n{prompt}"
    else:
        payload = f"{model}\neffort={effort}\nmax_tokens={max_tokens}\n{prompt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(
    model: str,
    prompt: str,
    effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> Path:
    return CACHE_DIR / f"{_cache_key(model, prompt, effort, max_tokens)}.json"


def load_cached(
    model: str,
    prompt: str,
    effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> Optional[Response]:
    """Return the cached Response for this exact request, or None on a miss."""
    path = _cache_path(model, prompt, effort, max_tokens)
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
        # Absent from v1-era cache files; default keeps those entries loadable.
        effort=data.get("effort"),
        truncated=data.get("truncated", False),
    )


def save_cache(
    model: str,
    prompt: str,
    response: Response,
    effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> None:
    """Persist a successful, real Response to disk keyed on the full request."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(model, prompt, effort, max_tokens)
    payload = {
        "text": response.text,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": response.latency_ms,
        "model": response.model,
        "effort": response.effort,
        "truncated": response.truncated,
    }
    path.write_text(json.dumps(payload, indent=2))


class CachedAdapter(Adapter):
    """Wraps a real Adapter with the record/replay disk cache."""

    def __init__(self, inner: Adapter, model_name: str) -> None:
        self.inner = inner
        self.model_name = model_name

    def complete(
        self,
        prompt: str,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Response:
        if not refresh_cache():
            cached = load_cached(self.model_name, prompt, effort, max_tokens)
            if cached is not None:
                return cached

        response = self.inner.complete(prompt, effort, max_tokens)
        if response.success and not response.simulated:
            save_cache(self.model_name, prompt, response, effort, max_tokens)
        return response


__all__ = ["CachedAdapter", "load_cached", "save_cache", "CACHE_DIR"]
