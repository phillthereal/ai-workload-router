"""
Model registry and configuration.

Defines the available models, their providers, and pricing. Pricing below is
real (publicly listed) provider pricing as of this writing, not a
placeholder — see the per-model comments for source/caveats.
"""

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class ModelConfig:
    """Configuration for a single model."""

    name: str
    """Model identifier (e.g., 'claude-opus-4-8')."""

    provider: Literal["anthropic", "openai", "google", "deepseek"]
    """LLM provider."""

    display_name: str
    """Human-readable name for reports."""

    cost_per_1m_input_tokens: float
    """Cost in USD per 1 million input tokens. See the per-model comments in
    MODELS below for source/caveats on each rate."""

    cost_per_1m_output_tokens: float
    """Cost in USD per 1 million output tokens. See the per-model comments in
    MODELS below for source/caveats on each rate."""

    context_window_tokens: int = 128_000
    """Maximum input context this model accepts. This is a HARD capability
    constraint, not a quality heuristic: a task whose prompt exceeds it cannot
    be routed here at any difficulty. See router.gates."""

    supports_effort: bool = False
    """True if this model accepts `output_config.effort`. Anthropic's Sonnet 5
    and Opus 4.8 do; Haiku 4.5 does not (it predates the parameter and returns
    a 400). This asymmetry is why the (model x effort) grid has holes — see
    EFFORT_LEVELS and router.gates.effort_supported()."""

    def cost_for_tokens(self, input_tokens: int, output_tokens: int) -> float:
        """
        Calculate cost for a given token count.

        Args:
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            Cost in USD.
        """
        return (input_tokens / 1e6) * self.cost_per_1m_input_tokens + (
            output_tokens / 1e6
        ) * self.cost_per_1m_output_tokens


# Registry of available models.
#
# v1 uses a genuine 3-vendor roster (OpenAI, DeepSeek, Anthropic) spanning a
# real ~80x price range budget -> frontier, so the benchmark's cost claims
# hold up across providers rather than just across one vendor's tiers. Each
# model's adapter (see router.adapters.get_adapter) already routes by
# `provider` and falls back to the offline mock if that provider's API key
# isn't configured. Prices are the current published per-1M-token rates
# except where noted.
MODELS: dict[str, ModelConfig] = {
    "gpt-4o-mini": ModelConfig(
        name="gpt-4o-mini",
        provider="openai",
        display_name="GPT-4o mini",
        cost_per_1m_input_tokens=0.15,
        cost_per_1m_output_tokens=0.60,
        context_window_tokens=128_000,
    ),
    "deepseek-chat": ModelConfig(
        name="deepseek-chat",
        provider="deepseek",
        display_name="DeepSeek Chat",
        # Approximate public rates.
        cost_per_1m_input_tokens=0.27,
        cost_per_1m_output_tokens=1.10,
        context_window_tokens=64_000,
    ),
    "gemini-2.0-flash": ModelConfig(
        name="gemini-2.0-flash",
        provider="google",
        display_name="Gemini 2.0 Flash",
        # Approximate public rates — verify against current Google pricing.
        cost_per_1m_input_tokens=0.10,
        cost_per_1m_output_tokens=0.40,
        context_window_tokens=1_000_000,
        supports_effort=False,
    ),
    # --- Anthropic tier ladder (v2 `claude_tiers` roster) --------------------
    # Haiku 4.5's 200K context is 5x smaller than its stablemates' 1M. That is
    # the single most consequential line in this file for routing: it makes
    # "does this prompt fit" a real, deterministic gate rather than a quality
    # judgement call. See router.gates.
    "claude-haiku-4-5": ModelConfig(
        name="claude-haiku-4-5",
        provider="anthropic",
        display_name="Claude Haiku 4.5",
        cost_per_1m_input_tokens=1.00,
        cost_per_1m_output_tokens=5.00,
        context_window_tokens=200_000,
        supports_effort=False,  # predates output_config.effort; 400s if sent
    ),
    # LIST PRICE, deliberately. Sonnet 5 carries an introductory $2/$10 rate
    # that expires 2026-08-31. Costing the benchmark at the promo rate would
    # publish a savings number that silently becomes wrong weeks later, so the
    # model is priced at list and the promo is a footnote in the report.
    "claude-sonnet-5": ModelConfig(
        name="claude-sonnet-5",
        provider="anthropic",
        display_name="Claude Sonnet 5",
        cost_per_1m_input_tokens=3.00,
        cost_per_1m_output_tokens=15.00,
        context_window_tokens=1_000_000,
        supports_effort=True,
    ),
    "claude-opus-4-8": ModelConfig(
        name="claude-opus-4-8",
        provider="anthropic",
        display_name="Claude Opus 4.8",
        cost_per_1m_input_tokens=5.00,
        cost_per_1m_output_tokens=25.00,
        context_window_tokens=1_000_000,
        supports_effort=True,
    ),
}


# Ordered cheapest -> most capable. Used to compare/escalate tiers (e.g. for
# quality_floor overrides in the router).
TIER_ORDER: list[str] = ["budget", "mid", "frontier"]


@dataclass(frozen=True)
class Roster:
    """One budget/mid/frontier ladder the router can route across.

    A roster is the unit the v1 result is a claim ABOUT: "route across these
    three models and you save X%". Swapping the ladder changes the price range
    the router has to work with, which (per the v1 finding that the savings
    ceiling is workload composition x price range) changes the achievable
    ceiling. Making the roster selectable rather than hardcoded is what lets
    v2 test that prediction without invalidating the published v1 run.
    """

    name: str
    budget: str
    mid: str
    frontier: str

    def model_for_tier(self, tier: str) -> str:
        return {"budget": self.budget, "mid": self.mid, "frontier": self.frontier}[tier]

    def price_range(self) -> float:
        """Ratio of frontier to budget OUTPUT price — the roster's headroom.

        Output price (not input) because output dominates spend on these
        workloads. This is the number that caps how much routing can ever
        save: a task moved from frontier to budget saves at most
        (1 - 1/price_range) of its cost.
        """
        return (
            MODELS[self.frontier].cost_per_1m_output_tokens
            / MODELS[self.budget].cost_per_1m_output_tokens
        )


ROSTERS: dict[str, Roster] = {
    # v1, published: ~41x output price range across three vendors. Achieved
    # 53.6% cost reduction at 100% quality retention (run_group 20260717T082024).
    "cross_vendor": Roster(
        name="cross_vendor",
        budget="gpt-4o-mini",
        mid="deepseek-chat",
        frontier="claude-opus-4-8",
    ),
    # v2: single vendor, 5x output price range. Predicted ceiling is materially
    # lower for exactly the reason v1 established. The interesting question is
    # not "is it lower" (it must be) but how much of the gap the effort dial —
    # which only exists within a vendor — can close back.
    "claude_tiers": Roster(
        name="claude_tiers",
        budget="claude-haiku-4-5",
        mid="claude-sonnet-5",
        frontier="claude-opus-4-8",
    ),
    # v2: 4th vendor (Google) swapped in at budget, widening the output price
    # range beyond "cross_vendor"'s ~41x. Tests whether cross-vendor savings
    # climb past the v1/v2 ceiling or plateau once price range is no longer
    # the binding constraint.
    "cross_vendor_4": Roster(
        name="cross_vendor_4",
        budget="gemini-2.0-flash",
        mid="deepseek-chat",
        frontier="claude-opus-4-8",
    ),
}

DEFAULT_ROSTER = "cross_vendor"


def get_roster(name: Optional[str] = None) -> Roster:
    """Look up a roster by name, defaulting to the published v1 ladder."""
    return ROSTERS[name or DEFAULT_ROSTER]


# Module-level tier constants remain bound to the DEFAULT (v1) roster so every
# existing caller, test, and the published benchmark keep their exact behavior.
# Roster-aware code should call get_roster(...).model_for_tier(...) instead.
BUDGET_MODEL = ROSTERS[DEFAULT_ROSTER].budget
MID_MODEL = ROSTERS[DEFAULT_ROSTER].mid
FRONTIER_MODEL = ROSTERS[DEFAULT_ROSTER].frontier

# Tier labels used by both the router (routing policy) and the scoring /
# mock-adapter quality profile (see router.scoring.QUALITY_PROFILE), so the
# routing policy, the mock adapter, and the mock judge all agree on which
# model belongs to which capability/price tier. claude-opus-4-8 also doubles
# as the primary LLM-as-judge model (see router.scoring._real_rubric_judge);
# router.judge_validation cross-checks it against a second, independent
# judge (gpt-4o-mini) from a different vendor.
#
# Every model maps to exactly one tier across ALL rosters (opus is the frontier
# of both), so this stays a flat model -> tier dict rather than needing to be
# roster-scoped.
MODEL_TIER: dict[str, str] = {
    "gpt-4o-mini": "budget",
    "deepseek-chat": "mid",
    "claude-haiku-4-5": "budget",
    "claude-sonnet-5": "mid",
    "claude-opus-4-8": "frontier",
    "gemini-2.0-flash": "budget",
}

# Valid values for output_config.effort on models where supports_effort is True.
# Ordered cheapest -> most thorough. Effort is the second routing dial and the
# one that only exists inside a single vendor's ladder: it trades thinking
# tokens (i.e. output tokens, i.e. cost) against quality WITHOUT changing model.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

EFFORT_OFF = "off"
"""Sentinel meaning "explicitly disable thinking", as distinct from None
("send no thinking config at all").

The distinction is load-bearing and non-obvious: omitting the thinking
parameter runs Opus 4.8 WITHOUT thinking but runs Sonnet 5 WITH adaptive
thinking. So None is not a neutral baseline across the Claude ladder — it would
have Sonnet silently thinking while its stablemates did not, inflating Sonnet's
cost and quality together and confounding the whole grid. EFFORT_OFF is the
neutral baseline; None is the v1-compatibility shape. See
router.adapters.anthropic_adapter."""

EFFORT_STATES: tuple[str, ...] = (EFFORT_OFF,) + EFFORT_LEVELS
"""Every non-None effort value the adapter layer accepts."""


def get_model(name: str) -> ModelConfig:
    """
    Retrieve model config by name.

    Args:
        name: Model name (key in MODELS dict).

    Returns:
        ModelConfig for the model.

    Raises:
        KeyError: If model not found.
    """
    return MODELS[name]
