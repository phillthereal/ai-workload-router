"""
Cascade / escalate-on-failure routing (v2 — the react-to-failure alternative).

The upfront classifier (router.classifier) PREDICTS difficulty from the prompt
and routes once. The cascade does the opposite: it DISCOVERS difficulty by
trying the cheapest model, cheaply checking the answer, and escalating only when
the check fails. Two philosophies for the same problem — "guess before" vs
"react after" — and this module is the second one, so the benchmark can compare
them head to head.

The economics differ in a way that is the whole point of comparing them:

  classifier: pays a fixed toll up front (one budget-model prediction per task),
              then makes exactly one answer call. Overhead is constant.
  cascade:    pays no prediction toll, but pays a verifier check between tiers
              AND pays for a discarded cheap attempt every time it escalates.
              Overhead is zero on easy tasks and largest on the hard ones — it
              scales with how often it is wrong to be optimistic.

THE VERIFIER IS REFERENCE-FREE, DELIBERATELY. A production system does not have
the answer key at request time — if it did, it would not need the model. So the
verifier sees only the prompt and the candidate answer and estimates adequacy on
the answer's own merits. (The final quality number the benchmark reports comes
from the separate, reference-USING Opus judge in router.scoring — that is the
offline ground truth, not something a live deployment has. Keeping the in-loop
verifier and the ground-truth judge distinct is what stops the cascade from
grading its own homework.)

THE VERIFIER CAN BE WRONG, and that is the cascade's version of the classifier's
misclassification risk: a lenient verifier waves through weak budget answers (a
quality leak), a strict one escalates answers that were already fine (a cost
leak). `escalate_threshold` is the single knob that trades those against each
other, and the benchmark reports both sides so the knob is not hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .adapters import get_adapter
from .adapters.base import Response
from .config import get_model, get_roster

# The verifier answers with a bare number, so its output is tiny and its cost is
# a fraction of a real answer call — which is the premise that makes checking
# cheaper than always escalating. Cap it hard.
_VERIFIER_MAX_TOKENS = 8

_VERIFIER_PROMPT_TEMPLATE = """You are a quality gate deciding whether a \
candidate answer is good enough to return to a user, or whether the question \
should be escalated to a more capable model.

You do NOT have an answer key. Judge the candidate on its own merits: is it \
correct, complete, and responsive to what was asked?

Question:
{prompt}

Candidate answer:
{answer}

Respond with ONLY a single number from 0.0 (clearly inadequate — escalate) to \
1.0 (clearly adequate — return it). No words."""

# Same permissive float-in-[0,1] parse the scoring judge uses; the verifier is
# not forced into structured output either.
_SCORE_RE = re.compile(r"(?<![\d.])(0(?:\.\d+)?|1(?:\.0+)?)(?![\d])")

# When the verifier's output can't be parsed as a number, ASSUME INADEQUATE and
# escalate. An unreadable quality gate must fail toward quality, not toward cost
# — the same fail-safe direction the classifier uses for unparseable output.
_UNPARSEABLE_ADEQUACY = 0.0


@dataclass
class CascadeResult:
    """Outcome of running one task through the cascade."""

    response: Response
    """The winning response — the first that passed the verifier, or the
    frontier model's response if nothing did."""

    answer_cost: float
    """Cost of the winning answer alone. Logged as the run's cost_usd, so it is
    directly comparable to the single call the classifier arm makes."""

    overhead_cost: float
    """Everything the cascade spent that was NOT the winning answer: every
    verifier check, plus every cheaper attempt it tried and then discarded on
    the way up. Logged as routing_cost_usd, so the report charges it against
    savings exactly like the classifier's prediction toll."""

    tiers_used: list[str] = field(default_factory=list)
    """Models tried, in order. Length 1 = the cheapest model sufficed."""

    verifier_scores: list[float] = field(default_factory=list)
    """The verifier's adequacy score at each non-final tier, in order."""

    @property
    def escalated(self) -> bool:
        """True if the cascade climbed past the first model."""
        return len(self.tiers_used) > 1

    @property
    def total_cost(self) -> float:
        return self.answer_cost + self.overhead_cost


def _parse_adequacy(text: str) -> float:
    """Extract a [0,1] adequacy score; unparseable -> escalate (0.0)."""
    match = _SCORE_RE.search(text or "")
    if not match:
        return _UNPARSEABLE_ADEQUACY
    try:
        return max(0.0, min(1.0, float(match.group(1))))
    except ValueError:
        return _UNPARSEABLE_ADEQUACY


def verify_adequacy(
    task: dict[str, Any], response: Response, verifier_model: str
) -> tuple[float, float]:
    """
    Reference-free adequacy check of `response` for `task`.

    Args:
        task: Task dict with at least `prompt`.
        response: The candidate answer to assess.
        verifier_model: Model to run the check (the cheap quality gate).

    Returns:
        (adequacy_score, verifier_cost_usd). The cost is returned alongside the
        score because it is real spend the cascade must account for — the check
        is cheap, not free.
    """
    prompt = _VERIFIER_PROMPT_TEMPLATE.format(
        prompt=task.get("prompt", ""), answer=response.text
    )
    check = get_adapter(verifier_model).complete(prompt, max_tokens=_VERIFIER_MAX_TOKENS)
    cost = get_model(verifier_model).cost_for_tokens(
        check.input_tokens, check.output_tokens
    )
    return _parse_adequacy(check.text), cost


def run_cascade(
    task: dict[str, Any],
    roster_name: Optional[str] = None,
    verifier_model: Optional[str] = None,
    escalate_threshold: float = 0.7,
    ladder: Optional[list[str]] = None,
) -> CascadeResult:
    """
    Run `task` through the cascade, escalating on a failed verifier check.

    Args:
        task: Task dict with at least `prompt`.
        roster_name: Roster to draw the ladder and default verifier from.
        verifier_model: The quality-gate model. Defaults to the roster's MID
            tier — cheap relative to the frontier (or the check would cost as
            much as just escalating) yet stronger than the budget answer it is
            vetting.
        escalate_threshold: Minimum adequacy to accept an answer and stop. Lower
            = trust cheap answers more (cheaper, riskier); higher = escalate
            more readily (pricier, safer).
        ladder: Models to climb, cheapest first. Defaults to the canonical
            two-tier budget -> frontier: try the cheapest, fall back to the
            strongest. A three-tier [budget, mid, frontier] ladder is supported
            too; note that with the default mid verifier the mid tier then vets
            its own answer, a mild circularity the two-tier default avoids.

    Returns:
        A CascadeResult with the winning response and split answer/overhead cost.
    """
    roster = get_roster(roster_name)
    verifier = verifier_model or roster.mid
    climb = ladder if ladder is not None else [roster.budget, roster.frontier]

    answer_cost = 0.0
    overhead_cost = 0.0
    tiers_used: list[str] = []
    verifier_scores: list[float] = []
    response: Optional[Response] = None

    for index, model in enumerate(climb):
        response = get_adapter(model).complete(task["prompt"])
        this_cost = get_model(model).cost_for_tokens(
            response.input_tokens, response.output_tokens
        )
        tiers_used.append(model)

        is_top = index == len(climb) - 1
        if is_top:
            # Top of the ladder: this is the answer, nothing to escalate to.
            # Any earlier attempts were discarded overhead; this one is the cost.
            answer_cost = this_cost
            break

        adequacy, verify_cost = verify_adequacy(task, response, verifier)
        overhead_cost += verify_cost
        verifier_scores.append(adequacy)

        if adequacy >= escalate_threshold:
            answer_cost = this_cost  # accepted — this is the winning answer
            break

        # Rejected: this attempt's cost becomes sunk overhead, and we climb.
        overhead_cost += this_cost

    assert response is not None  # climb is always non-empty
    return CascadeResult(
        response=response,
        answer_cost=answer_cost,
        overhead_cost=overhead_cost,
        tiers_used=tiers_used,
        verifier_scores=verifier_scores,
    )
