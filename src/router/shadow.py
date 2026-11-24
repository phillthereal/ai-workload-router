"""Shadow mode: answer on the frontier, log what the router WOULD have chosen and cost.

``ShadowComparePolicy`` lets a team keep sending everything to the frontier while
the run log collects the router's would-be decision for each request. The
router's own answer is never produced: ``RulesPolicy.dry_run`` works out the
models it would call without calling them, and their cost is estimated at list
prices on the frontier answer's token counts, in the router's best case (the
cheap models agree). See docs/SHADOW_MODE.md for what that estimate does and
does not prove.
"""

from __future__ import annotations

from typing import Any, Optional

from .api import Ctx, Result, RulesPolicy, _answer, default_policy
from .config import get_model

BASIS = "list prices on the frontier answer's token counts; cheap models assumed to agree (router best case)"
"""Stored with every estimate, so a logged row says how its number was made."""


class ShadowComparePolicy:
    """Answer on roster.frontier; record the inner RulesPolicy's best-case plan and its estimated cost."""

    name = "shadow"

    def __init__(self, inner: Optional[RulesPolicy] = None) -> None:
        self.inner = inner if inner is not None else default_policy()

    def report_tags(self) -> list[str]:
        """Tag reports 'shadow-compare'."""
        return ["shadow-compare"]

    def before_run_group(self, run_group: str) -> None:
        """No-op: this policy has no per-run state."""
        pass

    def run(self, prompt: str, task: dict[str, Any], ctx: Ctx) -> Result:
        """Answer on the frontier (as FrontierOnlyPolicy does), then attach the shadow estimate."""
        model = ctx.roster.frontier
        effort = (ctx.effort_policy or {}).get("frontier")
        result = _answer(prompt, model, effort, ctx, trace=[f"shadow mode: answered on {model}"])
        resp = result.response
        try:
            if resp is None:  # _answer always sets it; keeps the estimate honest if that changes
                raise ValueError("no frontier response to price")
            plan = self.inner.dry_run(prompt, task, ctx)
            estimated = sum(get_model(m).cost_for_tokens(resp.input_tokens, resp.output_tokens)
                            for m in plan.would_call)
        except Exception as exc:  # noqa: BLE001 - the frontier answer must still be returned
            result.trace.append(f"shadow: no estimate ({type(exc).__name__})")
            return result
        frontier_cost = ctx.cost(resp)
        result.task_type, result.difficulty = plan.task_type, plan.difficulty  # labels the logged row
        result.shadow = {
            "task_type": plan.task_type, "difficulty": plan.difficulty,
            "would_call": list(plan.would_call), "would_keep": plan.would_keep,
            "estimated_cost_usd": estimated, "frontier_cost_usd": frontier_cost,
            "saving_usd": frontier_cost - estimated, "basis": BASIS,
        }
        result.trace.append(f"shadow: router would call {', '.join(plan.would_call)}, "
                            f"est. ${estimated:.6f} vs frontier ${frontier_cost:.6f}")
        return result
