"""
Quality scoring harness (P0-4).

Scores model outputs two ways:
- `exact_match`: normalized (lowercased, stripped, punctuation-tolerant)
  string comparison against `task['reference']`. Used for tasks with a single
  unambiguous correct answer (e.g. "extract the company name").
- `rubric_judge`: an LLM-as-judge score against `task['reference']` (used as
  a rubric, not a literal answer). When an ANTHROPIC_API_KEY is configured
  (and AWR_FORCE_MOCK is not set), this calls a REAL judge — claude-opus-4-8
  via the same Anthropic adapter + record/replay cache used for benchmark
  task completions (see `_real_rubric_judge` below). Otherwise it falls back
  to the OFFLINE MOCK judge — see QUALITY_PROFILE below. It is validated
  (eventually) against a small human-labeled subset per the PRD;
  `validate_judge_against_human()` is the seam for that.

Both the mock provider adapter (router.adapters.mock.MockAdapter) and this
module's mock judge read from the SAME QUALITY_PROFILE table, so simulated
adapter behavior and simulated judge scores never disagree with each other.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from typing import Any

from .adapters.base import Response
from .config import MODEL_TIER
from .secrets import force_mock, get_api_key

# ---------------------------------------------------------------------------
# SIMULATED quality profile — PLACEHOLDER DATA.
#
# This is not measured from any real model. It is a hand-authored, documented
# stand-in for "how good would a budget/mid/frontier model plausibly be at
# this task_type + difficulty combination", designed so that:
#   - all tiers score high on easy tasks (cheap models are "good enough" for
#     the easy work the router sends them),
#   - the budget and mid tiers degrade noticeably on medium/hard work, and
#     especially on `reasoning` (multi-step logic is where cheap models fall
#     over hardest),
#   - the frontier tier stays high across the board (that's what you're
#     paying the premium for).
# Replace this table with real LLM-judge scores (validated against human
# labels) once API keys are available — everything downstream (MockAdapter,
# rubric_judge, the benchmark report) reads from this one place, so swapping
# it out does not require touching call sites.
# ---------------------------------------------------------------------------
QUALITY_PROFILE: dict[str, dict[str, dict[str, float]]] = {
    "budget": {  # e.g. gpt-4o-mini
        "classification": {"easy": 0.94, "medium": 0.78, "hard": 0.50},
        "extraction": {"easy": 0.93, "medium": 0.75, "hard": 0.45},
        "short_generation": {"easy": 0.90, "medium": 0.68, "hard": 0.40},
        "reasoning": {"easy": 0.55, "medium": 0.35, "hard": 0.20},
    },
    "mid": {  # e.g. deepseek-chat
        "classification": {"easy": 0.95, "medium": 0.88, "hard": 0.70},
        "extraction": {"easy": 0.93, "medium": 0.85, "hard": 0.65},
        "short_generation": {"easy": 0.92, "medium": 0.82, "hard": 0.60},
        "reasoning": {"easy": 0.85, "medium": 0.68, "hard": 0.42},
    },
    "frontier": {  # e.g. claude-opus-4-8
        "classification": {"easy": 0.97, "medium": 0.95, "hard": 0.93},
        "extraction": {"easy": 0.97, "medium": 0.94, "hard": 0.90},
        "short_generation": {"easy": 0.95, "medium": 0.92, "hard": 0.88},
        "reasoning": {"easy": 0.96, "medium": 0.92, "hard": 0.90},
    },
}

DIFFICULTIES = ("easy", "medium", "hard")
TASK_TYPES = ("classification", "extraction", "short_generation", "reasoning")

# Max +/- spread applied by the deterministic per-task jitter below.
_JITTER_SPREAD = 0.06


def _hash_unit(*parts: str) -> float:
    """Deterministic pseudo-random float in [0, 1) derived from `parts`."""
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / 16**12


def _normalize(text: str) -> str:
    """Lowercase, strip, drop punctuation, collapse whitespace."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def exact_match(response_text: str, reference: str) -> float:
    """
    Tolerant exact-match comparison.

    Real chat models answer conversationally ("The sentiment is positive.")
    rather than emitting the bare label ("positive"), so a strict string
    equality check scores correct answers as wrong. We therefore accept the
    answer when the normalized reference appears as a whole-token span within
    the normalized response (or vice versa, for short references).

    Args:
        response_text: Model output text.
        reference: Expected answer.

    Returns:
        1.0 if the reference is found in the response, else 0.0.
    """
    resp = _normalize(response_text)
    ref = _normalize(reference)
    if not ref:
        return 0.0
    if resp == ref:
        return 1.0
    # Whole-token containment: the reference tokens appear contiguously in the
    # response. Guards against 'no' matching inside 'notable'.
    return 1.0 if re.search(rf"(?:^|\s){re.escape(ref)}(?:\s|$)", resp) else 0.0


def rubric_judge(task: dict[str, Any], response: Response) -> float:
    """
    OFFLINE MOCK judge (PLACEHOLDER — see QUALITY_PROFILE docstring above).

    Looks up the simulated base quality for (model tier, task_type,
    difficulty) and adds small deterministic, hash-based per-task variation
    so scores aren't all identical. Deliberately does NOT inspect
    `response.text` in any depth — that's the point of it being a mock: a
    real judge call (another LLM scoring the output against the rubric in
    `task['reference']`) is what replaces this function later, behind the
    same `score()` seam.

    Args:
        task: Task dict with at least id, task_type, difficulty.
        response: The Response to score (its `.model` selects the tier).

    Returns:
        Simulated quality score in [0.0, 1.0].
    """
    tier = MODEL_TIER.get(response.model, "budget")
    base = QUALITY_PROFILE[tier][task["task_type"]][task["difficulty"]]
    jitter = (_hash_unit(task["id"], response.model, "quality") - 0.5) * _JITTER_SPREAD
    return max(0.0, min(1.0, base + jitter))


# ---------------------------------------------------------------------------
# REAL judge — claude-opus-4-8 via the Anthropic adapter + record/replay
# cache (the SAME cache used for benchmark task completions, keyed on
# sha256(f"{model}\n{prompt}")). Used for `rubric_judge` tasks whenever an
# ANTHROPIC_API_KEY is configured and AWR_FORCE_MOCK is not set; the offline
# QUALITY_PROFILE judge above remains the fallback (no key, forced-mock
# tests, or a real judge call that fails after retries and degrades to the
# mock adapter — see router.adapters._FallbackToMockAdapter).
# ---------------------------------------------------------------------------

# Matches a float in [0.0, 1.0], e.g. "0.85", "0", "1.0". Deliberately
# permissive about surrounding text ("Score: 0.85." or "I'd give this 0.7")
# since the judge model isn't forced into structured output.
_JUDGE_SCORE_RE = re.compile(r"(?<![\d.])(0(?:\.\d+)?|1(?:\.0+)?)(?![\d])")

_JUDGE_PROMPT_TEMPLATE = """You are grading a candidate answer to a task.

Task prompt:
{task_prompt}

Rubric / reference (may be a literal expected answer or a description of \
what a correct answer looks like):
{reference}

Candidate answer:
{candidate}

Score the candidate answer from 0.0 (completely fails the rubric) to 1.0 \
(fully satisfies it). Respond with ONLY a single number between 0.0 and \
1.0 — no words, no explanation."""


def _parse_judge_score(text: str) -> float:
    """
    Robustly extract a float in [0.0, 1.0] from a judge model's raw text.

    Returns 0.5 (a neutral default) if no parseable number in range is found.
    """
    match = _JUDGE_SCORE_RE.search(text or "")
    if not match:
        return 0.5
    try:
        value = float(match.group(1))
    except ValueError:
        return 0.5
    return max(0.0, min(1.0, value))


def _real_judge_available() -> bool:
    """True if the real judge should be used: ANTHROPIC_API_KEY is
    configured and the test-suite mock-forcing escape hatch isn't set."""
    if force_mock():
        return False
    return bool(get_api_key("ANTHROPIC_API_KEY"))


def judge_score_with_model(
    task: dict[str, Any], response: Response, judge_model: str
) -> float:
    """
    Real LLM-as-judge score using `judge_model`, via the same adapter + cache
    path (`router.adapters.get_adapter`) as benchmark task completions.

    Factored out (rather than hardcoded to claude-opus-4-8) so the judge
    model is a parameter — `_real_rubric_judge` below calls this with the
    primary judge (FRONTIER_MODEL), and `router.judge_validation` reuses it
    to re-score the same candidate answers with a second, independent judge
    from a different vendor for inter-judge agreement checks.

    Args:
        task: Task dict with at least prompt, reference.
        response: The candidate Response to score.
        judge_model: Model name (key in router.config.MODELS) to judge with.

    Returns:
        Judge-assigned quality score in [0.0, 1.0]; 0.5 if the judge's
        output can't be parsed as a number in range.
    """
    # Local import to avoid a circular import at module load time:
    # router.adapters.mock imports QUALITY_PROFILE from this module, so this
    # module cannot import router.adapters at the top level.
    from .adapters import get_adapter

    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        task_prompt=task.get("prompt", ""),
        reference=task.get("reference", ""),
        candidate=response.text,
    )
    judge_adapter = get_adapter(judge_model)
    judge_response = judge_adapter.complete(prompt)
    return _parse_judge_score(judge_response.text)


def _real_rubric_judge(task: dict[str, Any], response: Response) -> float:
    """
    Real LLM-as-judge score using the primary judge model (claude-opus-4-8),
    via `judge_score_with_model`.

    Args:
        task: Task dict with at least prompt, reference.
        response: The candidate Response to score.

    Returns:
        Judge-assigned quality score in [0.0, 1.0]; 0.5 if the judge's
        output can't be parsed as a number in range.
    """
    from .config import FRONTIER_MODEL

    return judge_score_with_model(task, response, FRONTIER_MODEL)


def score(task: dict[str, Any], response: Response) -> float:
    """
    Score a response against a task using the method named in task['scoring'].

    For `rubric_judge` tasks, uses the real claude-opus-4-8 judge when
    ANTHROPIC_API_KEY is configured (and AWR_FORCE_MOCK is unset), otherwise
    falls back to the offline mock judge.

    Args:
        task: Task dict with reference/scoring fields.
        response: The Response to score.

    Returns:
        Quality score in [0.0, 1.0].

    Raises:
        ValueError: If task['scoring'] is not a supported method.
    """
    method = task.get("scoring")
    if method == "exact_match":
        return exact_match(response.text, task["reference"])
    if method == "rubric_judge":
        if _real_judge_available():
            return _real_rubric_judge(task, response)
        return rubric_judge(task, response)
    raise ValueError(f"Unsupported scoring method: {method!r}")


def validate_judge_against_human(
    pairs: list[tuple[float, float]],
    correlation_threshold: float = 0.80,
) -> dict[str, Any]:
    """
    Validate (mock, later real) judge scores against a small human-labeled subset.

    Args:
        pairs: List of (human_score, judge_score) tuples, one per labeled task.
        correlation_threshold: Minimum acceptable Pearson correlation.

    Returns:
        Dict with keys: correlation, mean_human, mean_judge, disagreements
        (pairs where |human - judge| > 0.2), and passed (bool).
    """
    if len(pairs) < 2:
        return {
            "correlation": None,
            "mean_human": None,
            "mean_judge": None,
            "disagreements": [],
            "passed": False,
        }
    humans = [h for h, _ in pairs]
    judges = [j for _, j in pairs]
    try:
        correlation = statistics.correlation(humans, judges)
    except statistics.StatisticsError:
        correlation = None
    disagreements = [(h, j) for h, j in pairs if abs(h - j) > 0.2]
    return {
        "correlation": correlation,
        "mean_human": statistics.mean(humans),
        "mean_judge": statistics.mean(judges),
        "disagreements": disagreements,
        "passed": correlation is not None and correlation >= correlation_threshold,
    }
