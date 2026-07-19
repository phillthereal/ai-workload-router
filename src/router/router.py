"""
Rules-based router (P0-3, extended in v2).

Classifies each incoming task by (task_type, difficulty) and picks a model from
a documented default mapping. Respects an optional `quality_floor` that
escalates the choice to a stronger tier regardless of what the default policy
would have picked, and records a human-readable reason for every decision.

v2 adds three things, in the order they now execute:

  1. ROSTER (router.config.Roster) — which budget/mid/frontier ladder to route
     across. The published v1 result routes across three vendors; `claude_tiers`
     routes across one vendor's ladder. Selectable, not hardcoded, so testing
     the second does not invalidate the first.

  2. GATES (router.gates) — hard capability constraints, applied BEFORE the
     quality policy. A model that cannot physically take the prompt (context
     overflow) or the requested effort is removed from consideration, and the
     router escalates to the next tier up rather than failing. Gates are
     arithmetic, not judgement: they cost nothing and cannot be wrong.

  3. EFFORT (router.config.EFFORT_LEVELS) — the second dial. Effort trades
     thinking tokens (billed as output, so: cost) against quality WITHOUT
     changing model. It exists only within a single vendor's ladder, which is
     what makes the `claude_tiers` roster a genuinely different experiment
     rather than a rerun of v1 with a smaller price range.

The (task_type, difficulty) inputs may come from a task's hand-authored labels
(v1 behavior, and the control arm) or from router.classifier (v2, and what a
real deployment would actually have). The router itself does not care which —
it takes the pair and routes. See run_benchmark.py for how the arms are wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import gates
from .config import EFFORT_STATES, MODEL_TIER, TIER_ORDER, Roster, get_roster


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

    effort: Optional[str] = None
    """Effort level to request from the chosen model, or None to send no
    thinking/effort config at all (the v1 request shape). See
    router.config.EFFORT_OFF for why None and "off" are different."""

    roster: str = "cross_vendor"
    """Name of the roster this decision was made within."""

    gated_models: list[str] = field(default_factory=list)
    """Models a capability gate removed from consideration for this task.
    Empty on the overwhelming majority of tasks — populated only when a prompt
    genuinely does not fit somewhere, which is the point: gates should be
    invisible until they matter, and auditable when they do."""


# Documented default routing policy: (task_type, difficulty) -> tier.
#
#   - easy classification/extraction/short_generation -> cheapest (budget) tier
#   - medium classification/extraction/short_generation -> mid tier
#   - hard classification/extraction/short_generation -> frontier tier
#   - ANY reasoning task, regardless of difficulty, -> frontier tier, because
#     multi-step logic errors are costlier than the savings from a cheaper
#     model and cheap/mid models degrade sharply on reasoning (see
#     router.scoring.QUALITY_PROFILE).
#
# NOTE: this maps to TIERS, not to model names. v1 mapped straight to model
# names, which silently welded the policy to one roster. The indirection is what
# lets the same documented policy be evaluated against either ladder.
DEFAULT_ROUTING_RULES: dict[str, dict[str, str]] = {
    "classification": {"easy": "budget", "medium": "mid", "hard": "frontier"},
    "extraction": {"easy": "budget", "medium": "mid", "hard": "frontier"},
    "short_generation": {"easy": "budget", "medium": "mid", "hard": "frontier"},
    "reasoning": {"easy": "frontier", "medium": "frontier", "hard": "frontier"},
}

# quality_floor overrides: the first (floor, tier) whose floor the given
# quality_floor meets or exceeds sets the *minimum* tier the router may use,
# regardless of what the default policy above would have picked.
_FLOOR_MIN_TIER: list[tuple[float, str]] = [(0.95, "frontier"), (0.85, "mid")]


def _tier_of(model: str) -> str:
    return MODEL_TIER.get(model, "budget")


def _escalate_tier_for_floor(tier: str, quality_floor: float) -> str:
    """Raise `tier` to the minimum required by `quality_floor`, if any."""
    min_tier = next((t for floor, t in _FLOOR_MIN_TIER if quality_floor >= floor), None)
    if min_tier is None:
        return tier
    if TIER_ORDER.index(tier) >= TIER_ORDER.index(min_tier):
        return tier
    return min_tier


def _effort_for_tier(
    tier: str, effort_policy: Optional[dict[str, Optional[str]]]
) -> Optional[str]:
    """Look up the effort level this policy assigns to `tier`."""
    if not effort_policy:
        return None
    return effort_policy.get(tier)


def _apply_gates(
    roster: Roster, tier: str, prompt: str, effort: Optional[str]
) -> tuple[str, Optional[str], list[str], list[str]]:
    """
    Escalate past any tier whose model fails a capability gate.

    Walks up the tier ladder from `tier` until it finds a model that can
    physically take this prompt at this effort. If the requested effort is what
    blocks an otherwise-viable model, the effort is dropped for that model
    rather than escalating to a pricier one — a Haiku answer with no thinking
    is cheaper than a Sonnet answer with thinking, and effort is a preference
    where context is a hard limit.

    Returns:
        (final_tier, final_effort, gated_model_names, gate_reasons)
    """
    gated: list[str] = []
    reasons: list[str] = []

    for candidate_tier in TIER_ORDER[TIER_ORDER.index(tier):]:
        model = roster.model_for_tier(candidate_tier)

        if not gates.fits_context(model, prompt):
            gated.append(model)
            reasons.append(gates.check(model, prompt, effort).reason)
            continue

        if not gates.effort_supported(model, effort):
            # Model fits; only the effort is unsupported. Keep the model, drop
            # the dial.
            reasons.append(
                f"{model} does not support effort={effort!r} — routing it with "
                f"no effort config rather than escalating tier"
            )
            return candidate_tier, None, gated, reasons

        return candidate_tier, effort, gated, reasons

    # Every tier gated out. Return the frontier anyway: the frontier model is
    # the largest context in the roster, so if the prompt does not fit there it
    # does not fit anywhere, and failing loudly at the API is more honest than
    # this function inventing a route that cannot work.
    reasons.append(
        "every tier failed a capability gate — falling through to frontier; "
        "this task is expected to fail at the provider"
    )
    return "frontier", effort, gated, reasons


def route_task(
    task: dict[str, Any],
    quality_floor: Optional[float] = None,
    routing_rules: Optional[dict[str, dict[str, str]]] = None,
    roster_name: Optional[str] = None,
    effort_policy: Optional[dict[str, Optional[str]]] = None,
    task_type: Optional[str] = None,
    difficulty: Optional[str] = None,
    tier_override: Optional[str] = None,
) -> RoutingDecision:
    """
    Route a task to a (model, effort) pair.

    Args:
        task: Task dict with at least `id`. `prompt` is required for capability
            gates; `task_type`/`difficulty` are read from here unless
            overridden by the arguments below.
        quality_floor: If set, forces at least the mid tier (>=0.85) or the
            frontier tier (>=0.95) regardless of the default policy.
        routing_rules: Nested {task_type: {difficulty: tier}} mapping.
            Defaults to DEFAULT_ROUTING_RULES.
        roster_name: Which ladder to route across. Defaults to the published v1
            roster (`cross_vendor`), so existing callers are unaffected.
        effort_policy: {tier: effort_level_or_None}. Absent or None means send
            no effort/thinking config, reproducing v1's request shape exactly.
        task_type: Overrides `task['task_type']`. This is the seam
            router.classifier plugs into — a predicted label routes through the
            identical policy as a hand-authored one, which is what makes the
            classified and labelled arms comparable.
        difficulty: Overrides `task['difficulty']`.
        tier_override: Skip the (task_type, difficulty) -> tier policy lookup
            entirely and start from this tier instead. This is the seam
            router.learned plugs into: its evidence-based decision already
            resolved a TIER (not a relabelled task_type/difficulty pair), so
            re-deriving one just to look the tier back up through the policy
            table would mean inventing a fake label. Gates and quality_floor
            still apply on top of the override — history can pick a cheaper
            tier, but it does not get to bypass "does this prompt even fit".

    Returns:
        RoutingDecision with chosen_model, effort, reason, and any gate hits.

    Raises:
        KeyError: If task_type/difficulty has no matching rule, or roster_name
            is not a known roster.
        ValueError: If effort_policy names an unknown effort level, or
            tier_override names an unknown tier.
    """
    rules = routing_rules or DEFAULT_ROUTING_RULES
    roster = get_roster(roster_name)
    resolved_type = task_type if task_type is not None else task.get("task_type")
    resolved_difficulty = (
        difficulty if difficulty is not None else task.get("difficulty")
    )

    if effort_policy:
        for value in effort_policy.values():
            if value is not None and value not in EFFORT_STATES:
                raise ValueError(
                    f"Unknown effort level {value!r}; expected None or one of "
                    f"{EFFORT_STATES}"
                )

    if tier_override is not None:
        if tier_override not in TIER_ORDER:
            raise ValueError(f"Unknown tier {tier_override!r}; expected one of {TIER_ORDER}")
        tier = tier_override
        reason_parts = [f"tier={tier_override} (explicit override, e.g. from router.learned)"]
    else:
        if resolved_type not in rules:
            raise KeyError(f"No routing rule for task_type={resolved_type!r}")
        type_rules = rules[resolved_type]
        if resolved_difficulty not in type_rules:
            raise KeyError(
                f"No routing rule for task_type={resolved_type!r}, "
                f"difficulty={resolved_difficulty!r}"
            )
        tier = type_rules[resolved_difficulty]
        reason_parts = [f"{resolved_type}/{resolved_difficulty} -> {tier} tier per default policy"]

    quality_floor_applied = False

    if quality_floor is not None:
        escalated = _escalate_tier_for_floor(tier, quality_floor)
        if escalated != tier:
            reason_parts.append(
                f"quality_floor={quality_floor} requires at least the "
                f"{escalated} tier -> escalated from {tier}"
            )
            tier = escalated
            quality_floor_applied = True

    effort = _effort_for_tier(tier, effort_policy)

    prompt = task.get("prompt", "")
    tier, effort, gated, gate_reasons = _apply_gates(roster, tier, prompt, effort)
    reason_parts.extend(gate_reasons)

    model = roster.model_for_tier(tier)
    reason_parts.append(
        f"chose {model} (roster={roster.name}, effort={effort!r})"
    )

    return RoutingDecision(
        task_id=task["id"],
        chosen_model=model,
        reason="; ".join(reason_parts),
        quality_floor_applied=quality_floor_applied,
        effort=effort,
        roster=roster.name,
        gated_models=gated,
    )


def get_routing_rules() -> dict[str, dict[str, str]]:
    """
    Retrieve the current default routing rules.

    Returns:
        Nested dict mapping task_type -> difficulty -> tier name.
    """
    return DEFAULT_ROUTING_RULES
