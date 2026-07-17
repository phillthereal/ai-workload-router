"""
Offline mock provider adapter (P0-1 default runnable path).

PLACEHOLDER BEHAVIOR — this whole module exists to be deleted (or rather,
demoted to a fallback) once real provider adapters land. See the record/
replay cache TODO in base.py for the intended real-adapter design.

MockAdapter never makes a network call. It fabricates a deterministic,
plausible-looking response and token counts from the prompt text alone —
exactly what a real Adapter.complete(prompt) sees, nothing more (a real
provider API doesn't get task metadata either, just a prompt string). Its
simulated competence (how often it gets classification/extraction prompts
right, how long/thorough its answers are) is driven by the SAME quality
profile the offline mock judge uses (`router.scoring.QUALITY_PROFILE`),
keyed by model tier plus a difficulty proxy inferred from the prompt, so the
mock adapter and the mock judge always agree with each other about how good
a given model is at a given kind of task.
"""

from __future__ import annotations

import hashlib
import re
import time

from ..config import MODEL_TIER
from ..scoring import QUALITY_PROFILE
from .base import Adapter, Response

_POSITIVE_WORDS = {
    "amazing", "great", "excellent", "love", "best", "fantastic", "good",
    "wonderful", "happy", "satisfied", "impressed", "perfect",
}
_NEGATIVE_WORDS = {
    "bad", "terrible", "awful", "hate", "worst", "slow", "broken",
    "disappointed", "frustrat", "issue", "problem", "complaint", "wrong",
}

_COMPANY_SUFFIXES = r"(Inc\.|Corp\.|LLC|Ltd\.|Co\.|Company|Corporation)"
_COMPANY_RE = re.compile(
    r"([A-Z][A-Za-z&]*(?:\s+[A-Z][A-Za-z&]*)*\s+" + _COMPANY_SUFFIXES + r")"
)
_QUOTED_RE = re.compile(r"'([^']+)'")

_FILLER_BASE_WORDS = {"budget": 15, "mid": 25, "frontier": 40}
_FILLER_DIFFICULTY_BUMP = {"easy": 0, "medium": 15, "hard": 30}
_LATENCY_BASE_MS = {"budget": 250.0, "mid": 450.0, "frontier": 900.0}
_LEAD_IN = {
    "budget": "Here's a quick answer:",
    "mid": "Based on the input, here's my response:",
    "frontier": "Here is a thorough, carefully reasoned response:",
}


def _hash_float(*parts: str) -> float:
    """Deterministic pseudo-random float in [0, 1) derived from `parts`."""
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / 16**12


def _infer_task_type(prompt: str) -> str:
    """Best-effort guess at task_type from prompt text alone (mock-only heuristic)."""
    p = prompt.lower()
    if "extract" in p:
        return "extraction"
    if any(k in p for k in ("positive or negative", "classify", "intent", "complaint, inquiry")):
        return "classification"
    if any(k in p for k in ("rewrite", "summarize", "summarise")):
        return "short_generation"
    if any(k in p for k in ("explain your reasoning", "show your reasoning")):
        return "reasoning"
    return "short_generation"


def _infer_difficulty(prompt: str) -> str:
    """Crude length-based proxy for difficulty (mock-only heuristic)."""
    length = len(prompt)
    if length < 120:
        return "easy"
    if length < 220:
        return "medium"
    return "hard"


def _guess_sentiment(prompt: str) -> str:
    p = prompt.lower()
    pos = sum(1 for w in _POSITIVE_WORDS if w in p)
    neg = sum(1 for w in _NEGATIVE_WORDS if w in p)
    return "positive" if pos >= neg else "negative"


def _guess_company(prompt: str) -> str:
    match = _COMPANY_RE.search(prompt)
    if match:
        return match.group(1)
    quoted = _QUOTED_RE.search(prompt)
    if quoted:
        words = quoted.group(1).split()
        return " ".join(words[:2]) if words else "unknown"
    return "unknown"


def _filler_text(prompt: str, tier: str, difficulty: str) -> str:
    """Generic plausible prose; length scales with model tier and difficulty."""
    n_words = _FILLER_BASE_WORDS[tier] + _FILLER_DIFFICULTY_BUMP[difficulty]
    words = prompt.split() or ["input"]
    body = (words * ((n_words // max(len(words), 1)) + 1))[:n_words]
    return f"{_LEAD_IN[tier]} " + " ".join(body)


class MockAdapter(Adapter):
    """Deterministic, offline stand-in for a real provider adapter."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.tier = MODEL_TIER.get(model_name, "mid")

    def complete(self, prompt: str) -> Response:
        """Fabricate a deterministic Response for `prompt` (no network call)."""
        start = time.perf_counter()

        task_type = _infer_task_type(prompt)
        difficulty = _infer_difficulty(prompt)
        base_quality = QUALITY_PROFILE[self.tier][task_type][difficulty]
        confident = base_quality >= 0.5

        p_lower = prompt.lower()
        if task_type == "classification" and "positive or negative" in p_lower:
            guess = _guess_sentiment(prompt)
            text = guess if confident else ("negative" if guess == "positive" else "positive")
        elif task_type == "extraction" and "company name" in p_lower:
            guess = _guess_company(prompt)
            text = guess if confident else "unknown"
        else:
            text = _filler_text(prompt, self.tier, difficulty)

        input_tokens = max(1, len(prompt) // 4)
        output_tokens = max(1, len(text) // 4)

        latency_ms = _LATENCY_BASE_MS[self.tier] + _hash_float(self.model_name, prompt, "latency") * 200
        latency_ms += (time.perf_counter() - start) * 1000

        return Response(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            model=self.model_name,
            simulated=True,
        )
