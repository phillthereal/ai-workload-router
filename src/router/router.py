"""
Rules-based router (P0-3).

Classifies each incoming task by (task_type, difficulty) and picks a model
from a documented default mapping. Respects an optional `quality_floor` that
escalates the choice to a stronger tier regardless of what the default policy
would have picked, and records a human-readable reason for every decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .config import BUDGET_MODEL, FRONTIER_MODEL, MID_MODEL, MODEL_TIER, TIER_ORDER


@dataclass
class RoutingDecision:
    """Result of a routing decision."""

    task_id: str
    """Task identifier."""

    chosen_model: str
    """Name of the model this task is routed to."""

    reason: str
    """Human-readable explanation of why this model was chosen."""

    quality_floor_applied: bool
    """True if a quality floor override forced a stronger model."""


# Documented default routing policy: (task_type, difficulty) -> model.
#
#   - easy classification/extraction/short_generation -> cheapest (budget) model
#   - medium classification/extraction/short_generation -> mid-tier model
#   - hard classification/extraction/short_generation -> frontier model
#   - ANY reasoning task, regardless of difficulty, -> frontier model, because
#     multi-step logic errors are costlier than the savings from a cheaper
#     model and cheap/mid models degrade sharply on reasoning (see
#     router.scoring.QUALITY_PROFILE).
DEFAULT_ROUTING_RULES: dict[str, dict[str, str]] = {
    "classification": {"easy": BUDGET_MODEL, "medium": MID_MODEL, "hard": FRONTIER_MODEL},
    "extraction": {"easy": BUDGET_MODEL, "medium": MID_MODEL, "hard": FRONTIER_MODEL},
    "short_generation": {"easy": BUDGET_MODEL, "medium": MID_MODEL, "hard": FRONTIER_MODEL},
    "reasoning": {"easy": FRONTIER_MODEL, "medium": FRONTIER_MODEL, "hard": FRONTIER_MODEL},
}

# quality_floor overrides: the first (floor, tier) whose floor the given
# quality_floor meets or exceeds sets the *minimum* tier the router may use,
# regardless of what the default policy above would have picked.
_FLOOR_MIN_TIER: list[tuple[float, str]] = [(0.95, "frontier"), (0.85, "mid")]


def _tier_of(model: str) -> str:
    return MODEL_TIER.get(model, "budget")


def _model_for_tier(tier: str) -> str:
    for name, t in MODEL_TIER.items():
        if t == tier:
            return name
    raise KeyError(f"No model registered for tier={tier!r}")


def _escalate_for_floor(model: str, quality_floor: float) -> str:
    """Escalate `model` to the minimum tier required by `quality_floor`, if any."""
    min_tier = next((tier for floor, tier in _FLOOR_MIN_TIER if quality_floor >= floor), None)
    if min_tier is None:
        return model
    if TIER_ORDER.index(_tier_of(model)) >= TIER_ORDER.index(min_tier):
        return model
    return _model_for_tier(min_tier)


def route_task(
    task: dict[str, Any],
    quality_floor: Optional[float] = None,
    routing_rules: Optional[dict[str, dict[str, str]]] = None,
) -> RoutingDecision:
    """
    Route a task to a model based on task_type + difficulty, with an optional
    quality_floor override.

    Args:
        task: Task dict with at least id, task_type, difficulty.
        quality_floor: If set, forces at least the mid tier (>=0.85) or the
                       frontier tier (>=0.95) regardless of the default policy.
        routing_rules: Nested {task_type: {difficulty: model}} mapping.
                       Defaults to DEFAULT_ROUTING_RULES.

    Returns:
        RoutingDecision with chosen_model, reason, and quality_floor_applied.

    Raises:
        KeyError: If task_type/difficulty has no matching rule.
    """
    rules = routing_rules or DEFAULT_ROUTING_RULES
    task_type = task["task_type"]
    difficulty = task["difficulty"]

    if task_type not in rules:
        raise KeyError(f"No routing rule for task_type={task_type!r}")
    type_rules = rules[task_type]
    if difficulty not in type_rules:
        raise KeyError(f"No routing rule for task_type={task_type!r}, difficulty={difficulty!r}")

    model = type_rules[difficulty]
    reason = f"{task_type}/{difficulty} -> {model} per default routing policy"
    quality_floor_applied = False

    if quality_floor is not None:
        escalated = _escalate_for_floor(model, quality_floor)
        if escalated != model:
            reason = (
                f"{task_type}/{difficulty} would default to {model}, but "
                f"quality_floor={quality_floor} requires at least the "
                f"{_tier_of(escalated)} tier -> escalated to {escalated}"
            )
            model = escalated
            quality_floor_applied = True

    return RoutingDecision(
        task_id=task["id"],
        chosen_model=model,
        reason=reason,
        quality_floor_applied=quality_floor_applied,
    )


def get_routing_rules() -> dict[str, dict[str, str]]:
    """
    Retrieve the current default routing rules.

    Returns:
        Nested dict mapping task_type -> difficulty -> model name.
    """
    return DEFAULT_ROUTING_RULES
