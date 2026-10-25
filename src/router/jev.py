"""Jev as a router policy: a cheap DECISION model answers when sure, an LLM answers when not.

    Jev decides -> top prob >= threshold and choice not protected? -> handled
                                                                   -> else escalate: fallback LLM decides
                                                                      with the SAME rulebook rendered as a prompt

JevClassifierPolicy drives a label decision (e.g. email triage). JevTierPolicy is the extension point
for the router's own tier choice: same pattern, options are the roster tiers, answer comes from the
chosen tier's model. Jev failure or an unparseable fallback reply fails toward quality (escalate).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from .adapters.base import Response
from .adapters.typesafe import Decision, JevClient, MODEL
from .api import Result
from .rulebook import Rulebook
from .untrusted import fence


@dataclass
class JevResult(Result):
    choice: Optional[str] = None
    jev_probabilities: Optional[dict] = None
    escalated: bool = False
    rulebook_hash: Optional[str] = None


class JevClassifierPolicy:
    name = "jev_classifier"

    def __init__(self, rulebook: Rulebook, threshold: float = 0.9, protected: Sequence[str] = (),
                 fallback_model: str = "claude-haiku-4-5", client: Optional[JevClient] = None,
                 guard: Optional[Callable[[str], bool]] = None, fence_state: bool = False) -> None:
        # guard(state) True -> skip Jev, go straight to the fallback (e.g. email_guards.injection_hit).
        # fence_state: wrap the email in <email> tags for Jev (X3: no coverage loss, small DoS gain).
        self.guard, self.fence_state = guard, fence_state
        self.rulebook, self.threshold = rulebook, threshold
        self.protected, self.fallback_model = tuple(protected), fallback_model
        self.client = client or JevClient()

    def report_tags(self) -> list[str]:
        return [f"jev-{self.threshold}"]

    def before_run_group(self, run_group: str) -> None:
        pass

    def sure(self, d) -> bool:
        return d.success and d.top_prob >= self.threshold and d.choice not in self.protected

    def run(self, prompt: str, task: dict[str, Any], ctx) -> JevResult:
        state = task.get("state") or prompt
        rb = self.rulebook
        if self.guard and self.guard(state):
            d = Decision(None, success=False, error="guard")
        else:
            d = self.client.decide(fence(state, "email") if self.fence_state else state,
                                   rb.render_instructions(), rb.criteria)
        common = dict(jev_probabilities=d.probabilities, rulebook_hash=rb.content_hash(),
                      routing_cost_usd=d.cost_usd)
        why = (f"jev {d.choice} p={d.top_prob:.2f}" if d.success else f"jev failed ({d.error})")
        if self.sure(d):
            resp = Response(d.choice, d.input_tokens, 0, d.latency_ms, MODEL, d.simulated)
            return JevResult(text=d.choice, model=MODEL, answer_cost_usd=0.0, simulated=d.simulated,
                             trace=[f"{why} -> handled"], response=resp, choice=d.choice, **common)
        f = ctx.call(self.fallback_model, rb.render_prompt(state), max_tokens=16)
        choice = rb.parse_choice(f.text) or d.choice
        return JevResult(text=choice or f.text, model=f.model, answer_cost_usd=ctx.cost(f),
                         simulated=f.simulated or d.simulated, response=f, escalated=True, choice=choice,
                         trace=[f"{why} -> escalate to {f.model}"], **common)


TIER_RULEBOOK = Rulebook(1, "Pick the cheapest model tier that can answer the request correctly.", {
    "budget": "Short, well-specified lookups, extraction or labelling with one clear answer.",
    "mid": "Multi-step but routine work: summaries, light analysis, standard code changes.",
    "frontier": "Ambiguous, high-stakes or deep multi-step reasoning where a mistake is costly.",
})


class JevTierPolicy:
    """Extension point (minimal): Jev picks the tier; unsure -> frontier. Answer comes from that tier."""
    name = "jev_tier"

    def __init__(self, rulebook: Rulebook = TIER_RULEBOOK, threshold: float = 0.9,
                 client: Optional[JevClient] = None) -> None:
        self.rulebook, self.threshold, self.client = rulebook, threshold, client or JevClient()

    def report_tags(self) -> list[str]:
        return [f"jevtier-{self.threshold}"]

    def before_run_group(self, run_group: str) -> None:
        pass

    def run(self, prompt: str, task: dict[str, Any], ctx) -> JevResult:
        rb = self.rulebook
        d = self.client.decide(prompt, rb.render_instructions(), rb.criteria)
        sure = d.success and d.top_prob >= self.threshold and d.choice in ("budget", "mid", "frontier")
        tier = d.choice if sure else "frontier"
        r = ctx.call(ctx.roster.model_for_tier(tier), prompt)
        return JevResult(text=r.text, model=r.model, answer_cost_usd=ctx.cost(r), routing_cost_usd=d.cost_usd,
                         simulated=r.simulated or d.simulated, response=r, choice=tier, escalated=not sure,
                         jev_probabilities=d.probabilities, rulebook_hash=rb.content_hash(),
                         trace=[f"jev tier {d.choice} p={d.top_prob:.2f} -> {tier}"])
