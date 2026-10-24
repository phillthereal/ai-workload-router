"""Client for TypeSafe Jev (POST /v1/systemone): a DECISION model, not a text model.

It picks one option per question and returns probabilities; it never writes an answer, so it is
not a roster tier and does not implement `Adapter`. Docs: https://docs.typesafe.ai/api
AWR_FORCE_MOCK returns a deterministic fake decision. Errors carry a safe reason only (status + type).
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from ..secrets import force_mock, get_api_key

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
COST_PER_1M_INPUT = 0.042  # USD; output is free. Not a ModelConfig (provider Literal is for text models).
_TIMEOUT_S = 60.0


@dataclass
class Decision:
    choice: Optional[str]
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    input_tokens: int = 0
    latency_ms: float = 0.0
    simulated: bool = False
    success: bool = True
    error: Optional[str] = None

    @property
    def top_prob(self) -> float:
        return max(self.probabilities.values(), default=0.0)

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1e6 * COST_PER_1M_INPUT


def build_body(state: str, instructions: str, criteria: dict[str, str], name: str = "answer") -> dict:
    return {"state": state, "model": MODEL, "questions": {name: {
        "type": "choice", "instructions": instructions, "criteria": criteria}}}


def _safe_error(exc: Exception) -> str:
    """HTTP status + provider error *type* only, never the body (it may echo the input)."""
    if not isinstance(exc, urllib.error.HTTPError):
        return type(exc).__name__
    try:
        etype = json.load(exc).get("error", {}).get("type")
    except Exception:  # noqa: BLE001 - non-JSON body
        etype = None
    etype = etype if isinstance(etype, str) and etype.replace("_", "").isalnum() else None
    return f"HTTP {exc.code}" + (f" {etype}" if etype else "")


def mock_decision(state: str, criteria: dict[str, str]) -> Decision:
    """Deterministic from the inputs: same (state, options) -> same decision. Top prob in [0.50, 0.99]."""
    opts = list(criteria)
    h = hashlib.sha256((state + "\n" + "|".join(opts)).encode()).digest()
    top = opts[h[0] % len(opts)]
    p = 0.5 + (h[1] % 50) / 100
    rest = [o for o in opts if o != top]
    probs = {top: p, **{o: (1 - p) / len(rest) for o in rest}} if rest else {top: 1.0}
    return Decision(top, p, probs, input_tokens=len(state) // 4 + 100, latency_ms=1.0, simulated=True)


class JevClient:
    def __init__(self, api_key: Optional[str] = None, url: str = URL) -> None:
        self.api_key, self.url = api_key, url

    def decide(self, state: str, instructions: str, criteria: dict[str, str]) -> Decision:
        if force_mock():
            return mock_decision(state, criteria)
        key = self.api_key or get_api_key("TYPESAFE_API_KEY")
        if not key:
            return Decision(None, success=False, error="no_api_key")
        req = urllib.request.Request(
            self.url, json.dumps(build_body(state, instructions, criteria)).encode(),
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        t = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
                out = json.load(r)
            a = out["answers"]["answer"]
            return Decision(a["choice"], float(a.get("confidence", 0)), dict(a["probabilities"]),
                            int(out["usage"]["input_tokens"]), (time.perf_counter() - t) * 1000)
        except Exception as exc:  # noqa: BLE001
            return Decision(None, latency_ms=(time.perf_counter() - t) * 1000, success=False,
                            error=_safe_error(exc))
