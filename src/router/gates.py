"""
Capability gates (v2-1).

Hard, deterministic constraints applied BEFORE any quality-based routing
decision. A gate answers "can this model physically do this task at all?" —
never "would it do it well?". That distinction is the whole point of the
module:

  - A quality judgement is probabilistic, needs a classifier or a benchmark to
    justify, and can be wrong in a way that costs quality.
  - A gate is arithmetic. A 300K-token prompt does not fit in Haiku's 200K
    context. There is no difficulty level, quality floor, or effort setting at
    which it fits. Sending it is a guaranteed API error, not a bad bet.

Gates run first and for free (no network call, no model call), narrowing the
candidate set that the routing policy then picks from. If gates eliminate every
model in a tier, the router escalates rather than failing — see
router.router.route_task.

TOKEN ESTIMATION IS DELIBERATELY APPROXIMATE. Counting tokens exactly means
calling the provider's tokenizer endpoint, which is a network round-trip per
task — that would make the "free" gate the most expensive step in the router
and defeat the purpose. We use a conservative chars-per-token estimate with a
safety margin instead, and accept that the gate is slightly pessimistic (it
will occasionally disqualify a model that would in fact have fit). Being
pessimistic is the correct failure direction: the cost of a needless escalation
is a few cents; the cost of a context-overflow error is a failed task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import MODELS, get_model

# Conservative chars-per-token ratio. Real English averages ~4 chars/token;
# code, JSON, and non-English text tokenize denser (fewer chars per token, so
# MORE tokens for the same string). Estimating LOW on chars-per-token estimates
# HIGH on token count, which is the safe direction for a fit check.
_CHARS_PER_TOKEN = 3.5

# Fraction of the context window we allow the input to occupy. The rest is
# headroom for the model's own output (and, when effort/thinking is on, its
# thinking tokens, which are billed and counted as output). A prompt that
# technically fits but leaves no room to answer is not a usable route.
_INPUT_BUDGET_FRACTION = 0.70


@dataclass(frozen=True)
class GateResult:
    """Why a model was or wasn't eligible for a task."""

    model: str
    eligible: bool
    reason: str


def estimate_prompt_tokens(prompt: str) -> int:
    """
    Cheap, deliberately-pessimistic token estimate for `prompt`.

    Args:
        prompt: Raw prompt text.

    Returns:
        Estimated token count, biased high. See the module docstring for why
        this is an estimate rather than a real tokenizer call.
    """
    return int(len(prompt) / _CHARS_PER_TOKEN) + 1


def fits_context(model_name: str, prompt: str) -> bool:
    """
    True if `prompt` fits in `model_name`'s context with room to answer.

    Args:
        model_name: Model identifier (key in router.config.MODELS).
        prompt: Raw prompt text.

    Returns:
        Whether the estimated prompt size is within the model's usable input
        budget (_INPUT_BUDGET_FRACTION of its context window).
    """
    budget = get_model(model_name).context_window_tokens * _INPUT_BUDGET_FRACTION
    return estimate_prompt_tokens(prompt) <= budget


def effort_supported(model_name: str, effort: Optional[str]) -> bool:
    """
    True if `model_name` accepts the requested `effort` level.

    `effort=None` is always supported — it means "send no thinking/effort
    config at all", which every model accepts and which is exactly what the
    published v1 benchmark did.

    Args:
        model_name: Model identifier.
        effort: Requested effort level, or None for "don't send one".

    Returns:
        Whether the pairing is valid. Haiku 4.5 returns False for any non-None
        effort — it predates the parameter and the API rejects it.
    """
    if effort is None:
        return True
    return get_model(model_name).supports_effort


def check(model_name: str, prompt: str, effort: Optional[str] = None) -> GateResult:
    """
    Run every gate for one (model, prompt, effort) combination.

    Args:
        model_name: Model identifier.
        prompt: Raw prompt text.
        effort: Requested effort level, or None.

    Returns:
        A GateResult carrying eligibility and a human-readable reason. The
        reason is propagated into the routing decision's `reason` string so
        every escalation in the benchmark report is explainable.
    """
    if not fits_context(model_name, prompt):
        estimated = estimate_prompt_tokens(prompt)
        window = get_model(model_name).context_window_tokens
        return GateResult(
            model=model_name,
            eligible=False,
            reason=(
                f"prompt ~{estimated} tokens exceeds {model_name}'s usable input "
                f"budget ({int(window * _INPUT_BUDGET_FRACTION)} of {window})"
            ),
        )
    if not effort_supported(model_name, effort):
        return GateResult(
            model=model_name,
            eligible=False,
            reason=f"{model_name} does not support effort={effort!r}",
        )
    return GateResult(model=model_name, eligible=True, reason="all gates passed")


def eligible_models(
    prompt: str, effort: Optional[str] = None, candidates: Optional[list[str]] = None
) -> list[str]:
    """
    Filter `candidates` down to models that pass every gate for this task.

    Args:
        prompt: Raw prompt text.
        effort: Requested effort level, or None.
        candidates: Model names to consider. Defaults to the whole registry.

    Returns:
        The eligible subset, preserving input order.
    """
    names = candidates if candidates is not None else list(MODELS)
    return [name for name in names if check(name, prompt, effort).eligible]
