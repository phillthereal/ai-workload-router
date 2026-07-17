"""
Prompt classifier (v2-2).

Closes the biggest honesty gap in v1's design.

v1's router routes on `(task_type, difficulty)` — but both of those fields are
hand-authored in data/tasks.json. That is fine for a benchmark and indefensible
as a product claim: a production router receives a prompt, not a label. "How do
you know it's hard before you run it?" had exactly one answer, and it was "I
told it".

This module supplies the missing step: predict (task_type, difficulty) from the
prompt text alone, using the cheapest model in the roster, so the router's input
is something a real deployment actually has.

TWO THINGS THIS MODULE MAKES TRUE THAT WEREN'T BEFORE:

1. Routing becomes probabilistic. The classifier can be wrong, and a
   misclassified reasoning task routed to the budget tier is a quality
   regression the deterministic v1 router could not produce. `Classification`
   therefore carries `agreed_with_label` so the benchmark can report classifier
   accuracy alongside savings, instead of quietly absorbing its errors.

2. Routing stops being free. Every classified task costs one extra budget-model
   call. `Classification.cost_usd` carries that, and the report subtracts it
   from gross savings rather than hiding it. "The router costs 1.8% of the
   savings it generates" is a claim with a denominator; "53.6% cheaper" with an
   uncounted classifier call is not.

The offline path (no API key, or AWR_FORCE_MOCK=1) falls back to a free
keyword/length heuristic — see `heuristic_classify`. It is genuinely worse than
the model classifier, which is the point: the heuristic is the heuristic, and
the gap between them is itself a result worth reporting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .config import get_model, get_roster
from .scoring import DIFFICULTIES, TASK_TYPES

_CLASSIFIER_MAX_TOKENS = 32
"""The classifier answers with one short line. Capping output hard is what
keeps its cost a rounding error rather than a second full inference."""

_CLASSIFIER_PROMPT_TEMPLATE = """Classify the following task prompt.

Task prompt:
{task_prompt}

Answer with exactly one line in the form:
task_type=<one of: {task_types}> difficulty=<one of: {difficulties}>

Definitions:
- classification: assign the input to one of a fixed set of labels.
- extraction: pull specific literal fields or spans out of the input.
- short_generation: write or rewrite prose (summarize, rephrase, draft).
- reasoning: multi-step logic, arithmetic, deduction, or constraint solving.

Difficulty is about how much of the work is ambiguous or multi-step, not how \
long the input is. Respond with ONLY that one line — no explanation."""

_TASK_TYPE_RE = re.compile(r"task_type\s*=\s*([a-z_]+)", re.IGNORECASE)
_DIFFICULTY_RE = re.compile(r"difficulty\s*=\s*([a-z]+)", re.IGNORECASE)

# --- Free heuristic fallback ------------------------------------------------
# Keyword signals, checked most-specific first. `reasoning` leads because a
# reasoning prompt often also contains an extraction or classification verb
# ("work out which category...") and the reasoning demand is what dominates
# the routing decision.
_REASONING_MARKERS = (
    "explain your reasoning", "show your reasoning", "step by step",
    "how many", "who owns", "can both", "if all", "deduce", "work out",
)
_EXTRACTION_MARKERS = ("extract", "pull out", "list all", "find the")
_CLASSIFICATION_MARKERS = (
    "classify", "positive or negative", "spam or not", "categor",
    "what category", "intent", "sentiment", "priority", "tone of",
)
_GENERATION_MARKERS = ("rewrite", "summarize", "summarise", "write a", "draft")


@dataclass(frozen=True)
class Classification:
    """A predicted (task_type, difficulty) plus the cost of predicting it."""

    task_type: str
    difficulty: str

    cost_usd: float
    """What this prediction cost. Zero for the heuristic path. Non-zero for the
    model path — and it is the number that makes the router's savings claim
    net rather than gross."""

    model: Optional[str]
    """Model that produced the prediction, or None for the heuristic path."""

    simulated: bool
    """True if the prediction came from the offline heuristic or a mock
    adapter rather than a real provider call."""

    agreed_with_label: Optional[bool] = None
    """Whether this prediction matched the task's hand-authored label, when one
    is available. None when the task carries no label to compare against.

    NOTE ON WHAT THIS MEASURES: agreement is scored against labels authored by
    one person for 25 tasks. It is a sanity check, not an accuracy benchmark —
    a disagreement means the classifier and the author differ, not necessarily
    that the classifier is wrong. Report it as such."""


def _coerce(value: str, allowed: tuple[str, ...], default: str) -> str:
    """Map a raw model token onto an allowed value, falling back to `default`."""
    cleaned = value.strip().lower()
    return cleaned if cleaned in allowed else default


def heuristic_classify(prompt: str) -> Classification:
    """
    Free, deterministic classification from keywords and length. No model call.

    This is the fallback when no API key is configured (and the path the
    offline test suite exercises). It is also a legitimate baseline in its own
    right: if it routes nearly as well as the model classifier, the model
    classifier is not worth its cost.

    Args:
        prompt: Raw prompt text.

    Returns:
        A Classification with cost_usd=0.0 and simulated=True.
    """
    p = prompt.lower()
    if any(marker in p for marker in _REASONING_MARKERS):
        task_type = "reasoning"
    elif any(marker in p for marker in _EXTRACTION_MARKERS):
        task_type = "extraction"
    elif any(marker in p for marker in _CLASSIFICATION_MARKERS):
        task_type = "classification"
    elif any(marker in p for marker in _GENERATION_MARKERS):
        task_type = "short_generation"
    else:
        task_type = "short_generation"

    # Length is a weak proxy for difficulty and we do not pretend otherwise;
    # it is here so the free path produces a full (type, difficulty) pair.
    length = len(prompt)
    if length < 120:
        difficulty = "easy"
    elif length < 220:
        difficulty = "medium"
    else:
        difficulty = "hard"

    return Classification(
        task_type=task_type,
        difficulty=difficulty,
        cost_usd=0.0,
        model=None,
        simulated=True,
    )


def classify(
    prompt: str,
    roster_name: Optional[str] = None,
    classifier_model: Optional[str] = None,
) -> Classification:
    """
    Predict (task_type, difficulty) for `prompt` using the roster's budget model.

    The budget model classifies for the same reason it answers easy tasks: it
    is the cheapest thing in the roster that can do the job. Routing overhead
    scaling with the CHEAPEST tier rather than the frontier tier is what keeps
    the overhead a rounding error.

    Falls back to `heuristic_classify` whenever the adapter layer hands back a
    simulated response (no API key, forced mock, or a real call that failed and
    degraded) — a mock adapter's fabricated prose cannot be parsed as a label,
    so trusting it would silently inject noise into the routing decision.

    Args:
        prompt: Raw prompt text.
        roster_name: Roster whose budget model should classify. Defaults to the
            v1 roster.
        classifier_model: Explicit override for the classifying model.

    Returns:
        A Classification. `cost_usd` is the real cost of this prediction.
    """
    from .adapters import get_adapter  # local import: mirrors scoring.py, avoids cycles

    model_name = classifier_model or get_roster(roster_name).budget
    adapter = get_adapter(model_name)
    response = adapter.complete(
        _CLASSIFIER_PROMPT_TEMPLATE.format(
            task_prompt=prompt,
            task_types=", ".join(TASK_TYPES),
            difficulties=", ".join(DIFFICULTIES),
        ),
        max_tokens=_CLASSIFIER_MAX_TOKENS,
    )

    if response.simulated or not response.success:
        return heuristic_classify(prompt)

    type_match = _TASK_TYPE_RE.search(response.text or "")
    difficulty_match = _DIFFICULTY_RE.search(response.text or "")

    # An unparseable answer routes to the safest available assumption rather
    # than a cheap one: unknown work is treated as hard reasoning, which
    # escalates to the frontier tier. A classifier that fails should cost
    # money, not quality.
    task_type = (
        _coerce(type_match.group(1), TASK_TYPES, "reasoning")
        if type_match
        else "reasoning"
    )
    difficulty = (
        _coerce(difficulty_match.group(1), DIFFICULTIES, "hard")
        if difficulty_match
        else "hard"
    )

    return Classification(
        task_type=task_type,
        difficulty=difficulty,
        cost_usd=get_model(model_name).cost_for_tokens(
            response.input_tokens, response.output_tokens
        ),
        model=model_name,
        simulated=False,
    )


def classify_task(
    task: dict[str, Any],
    roster_name: Optional[str] = None,
    classifier_model: Optional[str] = None,
    use_labels: bool = False,
) -> Classification:
    """
    Classify a benchmark task, scoring the prediction against its hand label.

    Args:
        task: Task dict with at least `prompt`; `task_type`/`difficulty` are
            used as the comparison label when present.
        roster_name: Roster whose budget model classifies.
        classifier_model: Explicit classifying-model override.
        use_labels: If True, skip the classifier entirely and return the task's
            hand-authored labels at zero cost. This reproduces v1's exact
            routing behavior and is the control arm the classified arm is
            measured against.

    Returns:
        A Classification with `agreed_with_label` populated when the task
        carries labels.
    """
    labelled_type = task.get("task_type")
    labelled_difficulty = task.get("difficulty")

    if use_labels:
        return Classification(
            task_type=labelled_type,
            difficulty=labelled_difficulty,
            cost_usd=0.0,
            model=None,
            simulated=False,
            agreed_with_label=True,
        )

    result = classify(task["prompt"], roster_name, classifier_model)
    if labelled_type is None or labelled_difficulty is None:
        return result
    return Classification(
        task_type=result.task_type,
        difficulty=result.difficulty,
        cost_usd=result.cost_usd,
        model=result.model,
        simulated=result.simulated,
        agreed_with_label=(
            result.task_type == labelled_type
            and result.difficulty == labelled_difficulty
        ),
    )
