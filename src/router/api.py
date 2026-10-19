"""
Usable router entry point: ``Router(policy).complete(prompt) -> Result``.

Every routing strategy (frontier-only, rules, learned, cascade) implements the
same one-method ``Policy`` protocol, so the benchmark and a real caller drive
them identically. Policies WRAP the existing logic (classify_task, route_task,
learned.evaluate, run_cascade); they do not reimplement it.

FAILOVER SEAM. All provider calls a policy makes go through ``Ctx.call``.
``Router(allow_mock_fallback=False)`` (the default for real callers) asks
``get_adapter`` for the unwrapped real adapter, so a provider failure surfaces
as an exception / ``Response(success=False)`` instead of being silently turned
into a simulated mock answer. ``Ctx.call`` then retries on the next tier up the
roster ladder (budget -> mid -> frontier), records each hop in the trace, and
raises ``ProviderError`` only if the frontier tier fails too. The benchmark
passes ``allow_mock_fallback=True``, which keeps the historical offline
behaviour (the mock fallback lives in ``get_adapter``, so no failover fires).
AWR_FORCE_MOCK always yields mock answers, flagged ``Result.simulated``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from . import gates, learned
from .adapters import get_adapter
from .adapters.base import Response
from .cascade import run_cascade
from .classifier import classify_task
from .config import MODEL_TIER, TIER_ORDER, get_model, get_roster
from .router import route_task


class ProviderError(RuntimeError):
    """Every tier (including the frontier) failed to answer."""


@dataclass
class Result:
    text: str
    model: str
    answer_cost_usd: float
    routing_cost_usd: float
    simulated: bool
    trace: list[str] = field(default_factory=list)
    """Why each choice was made, in order (routing reason, failovers, ...)."""
    response: Optional[Response] = None
    effort: Optional[str] = None
    classifier_agreed: Optional[bool] = None
    verifier_scores: Optional[list[float]] = None
    learned_evidence: Optional[dict] = None

    @property
    def total_cost_usd(self) -> float:
        return self.answer_cost_usd + self.routing_cost_usd


class Ctx:
    """What a policy may use: roster, effort policy, and the failover-aware call."""

    def __init__(self, router: "Router") -> None:
        self.router = router
        self.roster_name = router.roster.name
        self.roster = router.roster
        self.effort_policy = router.effort_policy
        self.events: list[str] = []

    def call(
        self, model: str, prompt: str, effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Response:
        """Complete `prompt` on `model`; on provider failure climb the ladder.

        The returned Response's `.model` is the model that actually answered.
        """
        candidates = [model]
        if not self.router.allow_mock_fallback:
            tier = MODEL_TIER.get(model, "budget")
            above = TIER_ORDER[TIER_ORDER.index(tier) + 1:]
            candidates += [self.roster.model_for_tier(t) for t in above]
        last_error = "unknown"
        for i, m in enumerate(candidates):
            use_effort = effort if gates.effort_supported(m, effort) else None
            try:
                response = get_adapter(m, self.router.allow_mock_fallback).complete(
                    prompt, effort=use_effort, max_tokens=max_tokens
                )
                if response.success:
                    if i:
                        self.events.append(f"failover: answered by {m}")
                    return response
                last_error = "unsuccessful response (error/refusal)"
            except Exception as exc:  # noqa: BLE001 - any provider/adapter failure
                last_error = f"{type(exc).__name__}: {exc}"
            self.events.append(f"provider error on {m} ({last_error})")
        raise ProviderError(f"all tiers failed, last error: {last_error}")

    def cost(self, response: Response) -> float:
        return get_model(response.model).cost_for_tokens(
            response.input_tokens, response.output_tokens
        )


class Policy(Protocol):
    name: str
    """Strategy name used for run logging (db `strategy`) and reports."""

    def run(self, prompt: str, task: dict[str, Any], ctx: Ctx) -> Result: ...

    def report_tags(self) -> list[str]:
        """Filename tags this policy's config adds to a report (in order)."""
        ...

    def before_run_group(self, run_group: str) -> None:
        """Called once per benchmark run (chronological cutoff for learned)."""
        ...


def _answer(prompt, model, effort, ctx, routing_cost=0.0, trace=None, **extra) -> Result:
    resp = ctx.call(model, prompt, effort)
    return Result(
        text=resp.text, model=resp.model, answer_cost_usd=ctx.cost(resp),
        routing_cost_usd=routing_cost, simulated=resp.simulated,
        trace=list(trace or []), response=resp, effort=resp.effort, **extra,
    )


class FrontierOnlyPolicy:
    name = "frontier_only"

    def report_tags(self) -> list[str]:
        return []

    def run(self, prompt, task, ctx) -> Result:
        model = ctx.roster.frontier
        effort = (ctx.effort_policy or {}).get("frontier")
        return _answer(prompt, model, effort, ctx,
                       trace=[f"frontier-only baseline -> {model}"])

    def before_run_group(self, run_group: str) -> None:
        pass


class RulesPolicy:
    """Classify (hand labels or classifier model), then route by the rules table."""

    name = "router"

    def __init__(self, use_labels: bool = True) -> None:
        self.use_labels = use_labels

    def report_tags(self) -> list[str]:
        return [] if self.use_labels else ["classified"]

    def before_run_group(self, run_group: str) -> None:
        pass

    def _classify(self, task, ctx):
        # Labels only exist for benchmark tasks; a bare prompt must be classified.
        labelled = bool(task.get("task_type") and task.get("difficulty"))
        return classify_task(
            task, roster_name=ctx.roster_name, use_labels=self.use_labels and labelled
        )

    def run(self, prompt, task, ctx) -> Result:
        c = self._classify(task, ctx)
        d = route_task(task, roster_name=ctx.roster_name, effort_policy=ctx.effort_policy,
                       task_type=c.task_type, difficulty=c.difficulty)
        return _answer(
            prompt, d.chosen_model, d.effort, ctx, routing_cost=c.cost_usd,
            trace=[f"classified {c.task_type}/{c.difficulty} (model={c.model}, "
                   f"agreed_with_label={c.agreed_with_label})", d.reason],
            classifier_agreed=c.agreed_with_label,
        )


class LearnedPolicy(RulesPolicy):
    """RulesPolicy's classification, then outcome history may override the tier."""

    def __init__(self, use_labels: bool = True, before_run_group: Optional[str] = None) -> None:
        super().__init__(use_labels)
        self._cutoff = before_run_group

    def report_tags(self) -> list[str]:
        return super().report_tags() + ["learned"]

    def before_run_group(self, run_group: str) -> None:
        """Chronological cutoff: only evidence from strictly earlier run groups."""
        self._cutoff = run_group

    def run(self, prompt, task, ctx) -> Result:
        c = self._classify(task, ctx)
        ld = learned.evaluate(task, c.task_type, c.difficulty,
                              roster_name=ctx.roster_name, before_run_group=self._cutoff)
        d = route_task(task, roster_name=ctx.roster_name, effort_policy=ctx.effort_policy,
                       tier_override=ld.chosen_tier)
        return _answer(
            prompt, d.chosen_model, d.effort, ctx, routing_cost=c.cost_usd,
            trace=[f"classified {c.task_type}/{c.difficulty} (model={c.model})",
                   f"learned: {ld.direction} {ld.classifier_tier}->{ld.chosen_tier}: {ld.reason}",
                   d.reason],
            classifier_agreed=c.agreed_with_label, learned_evidence=ld.to_log_dict(),
        )


class CascadePolicy:
    name = "cascade"

    def __init__(self, verifier_model: Optional[str] = None, escalate_threshold: float = 0.7):
        self.verifier_model = verifier_model
        self.escalate_threshold = escalate_threshold

    def report_tags(self) -> list[str]:
        return [f"verifier-{self.verifier_model}"] if self.verifier_model else []

    def before_run_group(self, run_group: str) -> None:
        pass

    def run(self, prompt, task, ctx) -> Result:
        r = run_cascade(
            task, roster_name=ctx.roster_name, verifier_model=self.verifier_model,
            escalate_threshold=self.escalate_threshold, complete=ctx.call,
        )
        trace = [f"cascade tried {' -> '.join(r.tiers_used)}; verifier scores "
                 f"{r.verifier_scores} (threshold {self.escalate_threshold})"]
        return Result(
            text=r.response.text, model=r.response.model, answer_cost_usd=r.answer_cost,
            routing_cost_usd=r.overhead_cost, simulated=r.response.simulated,
            trace=trace, response=r.response, effort=r.response.effort,
            verifier_scores=r.verifier_scores,
        )


class Router:
    def __init__(
        self, policy: Policy, roster_name: Optional[str] = None,
        effort_policy: Optional[dict[str, Optional[str]]] = None,
        allow_mock_fallback: bool = False,
    ) -> None:
        self.policy = policy
        self.roster = get_roster(roster_name)
        self.effort_policy = effort_policy
        self.allow_mock_fallback = allow_mock_fallback

    def before_run_group(self, run_group: str) -> None:
        """Forward the benchmark's chronological cutoff to the policy."""
        self.policy.before_run_group(run_group)

    def complete(self, prompt: str, task: Optional[dict[str, Any]] = None) -> Result:
        task = {"id": "adhoc", **(task or {}), "prompt": prompt}
        ctx = Ctx(self)
        result = self.policy.run(prompt, task, ctx)
        result.trace.extend(ctx.events)
        return result
