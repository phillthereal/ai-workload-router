"""
Benchmark report generation (P0-6).

Turns the runs logged by db.py for one run_group into the headline
comparison the PRD's success metrics are built on: cost reduction and
quality retention of the "router" strategy vs. the "frontier_only" baseline.

LIVE vs SIMULATED: every logged run carries a `simulated` flag (see
router.db / router.adapters). The report aggregates that per model — a
model is "simulated" for this run_group if ANY of its logged runs came
from MockAdapter (no configured key, or a real call that failed and
degraded to the mock) — and stamps the report "LIVE RESULTS" only if every
model used was real, else "PARTIALLY SIMULATED" with a per-model
breakdown. See router.adapters.get_adapter and router.scoring for the
real-vs-mock fallback logic.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from . import db

ROUTER_STRATEGY = "router"
BASELINE_STRATEGY = "frontier_only"

COST_REDUCTION_TARGET_PCT = 40.0
QUALITY_RETENTION_TARGET_PCT = 95.0

# Monthly request volumes the at-scale cost projection is shown at.
PROJECTION_VOLUMES: tuple[int, ...] = (100_000, 1_000_000)


@dataclass
class BenchmarkReport:
    """The benchmark report: headline metrics + breakdowns."""

    run_group: str
    router_cost: float
    router_quality: float
    router_n: int
    frontier_cost: float
    frontier_quality: float
    frontier_n: int
    cost_reduction_pct: float
    quality_retention_pct: float
    hypothesis_passed: bool
    results_by_task_type: dict[str, dict[str, Any]]
    results_by_difficulty: dict[str, dict[str, Any]]
    model_simulated: dict[str, bool]
    """model name -> True if ANY run for that model in this run_group was
    simulated (MockAdapter), False if every run was a real provider call."""
    all_real: bool
    """True if every model used in this run_group was real (no simulation)."""
    latency_by_strategy: dict[str, dict[str, float]] = field(default_factory=dict)
    """strategy -> {"mean_ms", "median_ms", "n"} latency stats across that
    strategy's runs. Latency is real wall-clock time on live calls and the
    stored value on cache replays (see router.adapters.cache)."""
    latency_delta_pct: float = 0.0
    """Router mean latency vs the frontier_only baseline's mean latency:
    positive = router is that % faster on average, negative = that % slower."""
    cost_per_task_router: float = 0.0
    """router total_cost / router n — used as the per-task rate for the
    at-scale cost projection below."""
    cost_per_task_frontier: float = 0.0
    """frontier_only total_cost / frontier_only n — the baseline per-task rate."""
    projections: list[dict[str, Any]] = field(default_factory=list)
    """At-scale monthly cost projection, one dict per PROJECTION_VOLUMES
    entry: {"volume", "baseline_monthly", "router_monthly",
    "savings_monthly"}, extrapolated from this run's per-task cost. Assumes
    production traffic resembles the benchmark's task mix — see the caveat
    printed alongside this section in the report."""
    routing_cost: float = 0.0
    """Total cost of the classifier calls that produced this run's routing
    decisions. Zero when the router read hand-authored labels (the v1 arm).

    ROUTING IS NOT FREE. A router that decides where to send a task by asking a
    model has to pay for asking. Reporting a savings figure that excludes this
    is reporting a number the deployment will not see."""
    net_router_cost: float = 0.0
    """router_cost + routing_cost — what the router arm ACTUALLY costs."""
    net_cost_reduction_pct: float = 0.0
    """Cost reduction computed against net_router_cost. This is the headline:
    `cost_reduction_pct` is retained alongside it as the gross figure, so the
    overhead is visible as the gap between the two rather than hidden.

    For any run that used labels (routing_cost == 0) net and gross are equal by
    construction — which is why adopting the net figure as the headline does
    not restate the published v1 result."""
    routing_overhead_pct_of_savings: float = 0.0
    """routing_cost as a percentage of the gross savings it generated. The
    number that answers "is the router worth its own overhead?" — and the one
    worth leading with, because it has a denominator."""
    classifier_agreement_pct: Optional[float] = None
    """Percentage of tasks where the classifier's predicted (task_type,
    difficulty) matched the hand-authored label. None when labels were used
    directly (nothing was predicted, so there is nothing to agree).

    THIS IS NOT AN ACCURACY SCORE. The labels are one person's judgement on 25
    self-authored tasks. Disagreement means the classifier and the author
    differ; it does not establish which is right."""


def build_report(
    run_group: str, db_path: Optional[Union[str, Path]] = None
) -> BenchmarkReport:
    """
    Query logged runs for `run_group` and compute the router vs
    frontier_only comparison.

    Args:
        run_group: The run_group to report on.
        db_path: Optional db path override (for tests).

    Returns:
        BenchmarkReport with headline metrics and breakdowns.
    """
    summary = db.summary_by_strategy(run_group, db_path=db_path)
    router = summary.get(ROUTER_STRATEGY, {"total_cost": 0.0, "mean_quality": 0.0, "n": 0})
    frontier = summary.get(BASELINE_STRATEGY, {"total_cost": 0.0, "mean_quality": 0.0, "n": 0})

    rows = db.fetch_runs(run_group, db_path=db_path)
    model_simulated = _model_simulated_map(rows)

    # Routing overhead is charged to the router arm only: the frontier-only
    # baseline does not route, so it has no classifier call to pay for.
    router_rows = [r for r in rows if r["strategy"] == ROUTER_STRATEGY]
    routing_cost = sum(r.get("routing_cost_usd") or 0.0 for r in router_rows)
    net_router_cost = router["total_cost"] + routing_cost

    cost_reduction_pct = (
        (frontier["total_cost"] - router["total_cost"]) / frontier["total_cost"] * 100
        if frontier["total_cost"]
        else 0.0
    )
    net_cost_reduction_pct = (
        (frontier["total_cost"] - net_router_cost) / frontier["total_cost"] * 100
        if frontier["total_cost"]
        else 0.0
    )
    gross_savings = frontier["total_cost"] - router["total_cost"]
    routing_overhead_pct_of_savings = (
        routing_cost / gross_savings * 100 if gross_savings > 0 else 0.0
    )

    agreements = [
        r["classifier_agreed"] for r in router_rows if r.get("classifier_agreed") is not None
    ]
    classifier_agreement_pct = (
        sum(agreements) / len(agreements) * 100 if agreements else None
    )

    quality_retention_pct = (
        router["mean_quality"] / frontier["mean_quality"] * 100
        if frontier["mean_quality"]
        else 0.0
    )
    # Judged on the NET figure. For label-driven runs routing_cost is 0, so this
    # is identical to the gross test and the published v1 verdict is unchanged.
    hypothesis_passed = (
        net_cost_reduction_pct >= COST_REDUCTION_TARGET_PCT
        and quality_retention_pct >= QUALITY_RETENTION_TARGET_PCT
    )

    latency_raw = db.latency_by_strategy(run_group, db_path=db_path)
    latency_stats = {strat: _latency_stats(values) for strat, values in latency_raw.items()}
    router_latency = latency_stats.get(ROUTER_STRATEGY, _latency_stats([]))
    frontier_latency = latency_stats.get(BASELINE_STRATEGY, _latency_stats([]))
    latency_delta_pct = (
        (frontier_latency["mean_ms"] - router_latency["mean_ms"]) / frontier_latency["mean_ms"] * 100
        if frontier_latency["mean_ms"]
        else 0.0
    )

    # Projections extrapolate the NET per-task rate — at 1M requests/month the
    # routing overhead is 1M classifier calls, so excluding it would overstate
    # the saving by exactly the amount that grows fastest with volume.
    cost_per_task_router = net_router_cost / router["n"] if router["n"] else 0.0
    cost_per_task_frontier = frontier["total_cost"] / frontier["n"] if frontier["n"] else 0.0
    projections = [
        {
            "volume": volume,
            "baseline_monthly": cost_per_task_frontier * volume,
            "router_monthly": cost_per_task_router * volume,
            "savings_monthly": (cost_per_task_frontier - cost_per_task_router) * volume,
        }
        for volume in PROJECTION_VOLUMES
    ]

    return BenchmarkReport(
        run_group=run_group,
        router_cost=router["total_cost"],
        router_quality=router["mean_quality"],
        router_n=router["n"],
        frontier_cost=frontier["total_cost"],
        frontier_quality=frontier["mean_quality"],
        frontier_n=frontier["n"],
        cost_reduction_pct=cost_reduction_pct,
        quality_retention_pct=quality_retention_pct,
        hypothesis_passed=hypothesis_passed,
        results_by_task_type=_breakdown(rows, "task_type"),
        results_by_difficulty=_breakdown(rows, "difficulty"),
        model_simulated=model_simulated,
        all_real=bool(rows) and not any(model_simulated.values()),
        latency_by_strategy=latency_stats,
        latency_delta_pct=latency_delta_pct,
        cost_per_task_router=cost_per_task_router,
        cost_per_task_frontier=cost_per_task_frontier,
        projections=projections,
        routing_cost=routing_cost,
        net_router_cost=net_router_cost,
        net_cost_reduction_pct=net_cost_reduction_pct,
        routing_overhead_pct_of_savings=routing_overhead_pct_of_savings,
        classifier_agreement_pct=classifier_agreement_pct,
    )


def _latency_stats(values: list[float]) -> dict[str, float]:
    """Mean/median/count for a list of latency_ms values (0s if empty)."""
    if not values:
        return {"mean_ms": 0.0, "median_ms": 0.0, "n": 0}
    return {
        "mean_ms": statistics.mean(values),
        "median_ms": statistics.median(values),
        "n": len(values),
    }


def _model_simulated_map(rows: list[dict[str, Any]]) -> dict[str, bool]:
    """model -> True if any logged row for that model was simulated."""
    result: dict[str, bool] = {}
    for row in rows:
        model = row["model"]
        simulated = bool(row["simulated"])
        result[model] = result.get(model, False) or simulated
    return result


def _breakdown(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Group logged rows by `key` (task_type or difficulty), then by strategy."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = grouped.setdefault(row[key], {})
        s = bucket.setdefault(row["strategy"], {"cost": 0.0, "_quality_sum": 0.0, "n": 0})
        s["cost"] += row["cost_usd"]
        s["_quality_sum"] += row["quality_score"]
        s["n"] += 1
    for strat_map in grouped.values():
        for s in strat_map.values():
            s["mean_quality"] = s["_quality_sum"] / s["n"] if s["n"] else 0.0
            del s["_quality_sum"]
    return grouped


def format_report_markdown(report: BenchmarkReport) -> str:
    """
    Render a BenchmarkReport as a markdown string.

    Args:
        report: The report to render.

    Returns:
        Markdown text suitable for printing or saving to a file.
    """
    lines: list[str] = []
    lines.append("# AI Workload Router — Benchmark Report")
    lines.append("")
    if report.all_real:
        lines.append(
            "**LIVE RESULTS** — every model in this run made real provider "
            "API calls (Anthropic/OpenAI/DeepSeek), cached to disk under "
            "`.cache/` for free, reproducible re-runs. The `rubric_judge` "
            "scores came from the real claude-opus-4-8 judge."
        )
    else:
        lines.append(
            "**PARTIALLY SIMULATED** — at least one model in this run had "
            "no configured API key (or its real call failed and degraded), "
            "and fell back to the offline `MockAdapter` / mock judge. "
            "Per-model status:"
        )
        lines.append("")
        for model, simulated in sorted(report.model_simulated.items()):
            status = "SIMULATED (mock)" if simulated else "REAL (live API)"
            lines.append(f"- `{model}`: {status}")
    lines.append("")
    lines.append(f"Run group: `{report.run_group}`")
    lines.append("")
    lines.append("## Strategy comparison")
    lines.append("")
    lines.append("| Strategy | Total cost (USD) | Mean quality | N |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| router | ${report.router_cost:.6f} | {report.router_quality:.3f} | {report.router_n} |"
    )
    lines.append(
        f"| frontier_only (baseline) | ${report.frontier_cost:.6f} | "
        f"{report.frontier_quality:.3f} | {report.frontier_n} |"
    )
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(
        f"- **Cost reduction vs baseline:** {report.net_cost_reduction_pct:.1f}% "
        f"(net of routing overhead)"
    )
    lines.append(f"- **Quality retention:** {report.quality_retention_pct:.1f}% of baseline")
    verdict = "PASSED" if report.hypothesis_passed else "NOT MET"
    lines.append(
        f"- **Hypothesis (>= {COST_REDUCTION_TARGET_PCT:.0f}% cost reduction, "
        f">= {QUALITY_RETENTION_TARGET_PCT:.0f}% quality retention): {verdict}**"
    )
    lines.append("")
    lines.append("## Routing overhead")
    lines.append("")
    if report.routing_cost > 0:
        lines.append(
            "This run predicted each task's `(task_type, difficulty)` from the "
            "prompt using the roster's budget model, rather than reading a "
            "hand-authored label. That is what a real deployment has — and it "
            "means the router costs money to run. That cost is charged against "
            "the savings below, not excluded from them."
        )
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Router model spend | ${report.router_cost:.6f} |")
        lines.append(f"| Routing (classifier) spend | ${report.routing_cost:.6f} |")
        lines.append(f"| **Router total, net** | **${report.net_router_cost:.6f}** |")
        lines.append(f"| Cost reduction, gross | {report.cost_reduction_pct:.1f}% |")
        lines.append(f"| **Cost reduction, net** | **{report.net_cost_reduction_pct:.1f}%** |")
        lines.append(
            f"| **Routing overhead as % of savings** | "
            f"**{report.routing_overhead_pct_of_savings:.1f}%** |"
        )
        if report.classifier_agreement_pct is not None:
            lines.append(
                f"| Classifier agreement with hand labels | "
                f"{report.classifier_agreement_pct:.1f}% |"
            )
        lines.append("")
        if report.classifier_agreement_pct is not None:
            lines.append(
                "*Agreement is measured against labels authored by one person "
                "for this task set. It is a sanity check, not an accuracy "
                "benchmark: a disagreement means the classifier and the author "
                "differ, not that the classifier is wrong.*"
            )
    else:
        lines.append(
            "This run routed on the task set's hand-authored "
            "`(task_type, difficulty)` labels, so routing cost nothing and the "
            "net and gross savings figures are identical. Note that a real "
            "deployment does not have those labels — it has a prompt. Re-run "
            "with `--classify` to pay for predicting them and see the net "
            "figure move."
        )
    lines.append("")
    lines.append("## Latency")
    lines.append("")
    lines.append(
        "Wall-clock latency per run — real on live calls, the stored value "
        "on cache replays (see router.adapters.cache)."
    )
    lines.append("")
    lines.append("| Strategy | Mean latency (ms) | Median latency (ms) | N |")
    lines.append("|---|---|---|---|")
    for strat in (ROUTER_STRATEGY, BASELINE_STRATEGY):
        stats = report.latency_by_strategy.get(strat, {"mean_ms": 0.0, "median_ms": 0.0, "n": 0})
        label = "router" if strat == ROUTER_STRATEGY else "frontier_only (baseline)"
        lines.append(
            f"| {label} | {stats['mean_ms']:.1f} | {stats['median_ms']:.1f} | {stats['n']} |"
        )
    lines.append("")
    direction = "faster" if report.latency_delta_pct >= 0 else "slower"
    lines.append(
        f"- **Router is {abs(report.latency_delta_pct):.1f}% {direction}** than the "
        "frontier_only baseline on average (mean latency)."
    )
    lines.append("")
    lines.append("## At-scale cost projection")
    lines.append("")
    lines.append(
        f"Cost per task in this run: router ${report.cost_per_task_router:.6f}, "
        f"frontier_only (baseline) ${report.cost_per_task_frontier:.6f}. Projected "
        "monthly cost at scale, extrapolating linearly from that per-task rate:"
    )
    lines.append("")
    lines.append(
        "| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |"
    )
    lines.append("|---|---|---|---|")
    for p in report.projections:
        lines.append(
            f"| {p['volume']:,} | ${p['baseline_monthly']:,.2f} | "
            f"${p['router_monthly']:,.2f} | ${p['savings_monthly']:,.2f} |"
        )
    lines.append("")
    lines.append(
        "*Caveat: this projection assumes production traffic resembles this "
        "benchmark's task-type/difficulty mix — real traffic will differ, so "
        "treat it as directional, not a committed forecast.*"
    )
    lines.append("")
    lines.append("## By task type")
    lines.append("")
    lines.append("| task_type | strategy | cost | mean_quality | n |")
    lines.append("|---|---|---|---|---|")
    for task_type, strat_map in sorted(report.results_by_task_type.items()):
        for strat, s in sorted(strat_map.items()):
            lines.append(
                f"| {task_type} | {strat} | ${s['cost']:.6f} | {s['mean_quality']:.3f} | {s['n']} |"
            )
    lines.append("")
    lines.append("## By difficulty")
    lines.append("")
    lines.append("| difficulty | strategy | cost | mean_quality | n |")
    lines.append("|---|---|---|---|---|")
    for difficulty, strat_map in sorted(report.results_by_difficulty.items()):
        for strat, s in sorted(strat_map.items()):
            lines.append(
                f"| {difficulty} | {strat} | ${s['cost']:.6f} | {s['mean_quality']:.3f} | {s['n']} |"
            )
    lines.append("")
    return "\n".join(lines)


def print_report(report: BenchmarkReport) -> None:
    """Print the report to stdout."""
    print(format_report_markdown(report))
