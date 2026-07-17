#!/usr/bin/env python3
"""
Entry point for the AI Workload Router benchmark.

This script orchestrates the full pipeline:
1. Load the task set from data/tasks.json
2. Initialize the database
3. Route each task according to routing rules (router strategy) or always to
   the frontier model (frontier_only strategy, the baseline)
4. Call the (mock) provider adapter to get a response
5. Score outputs using the quality harness
6. Log metrics (cost, latency, quality) for each run
7. Generate a benchmark report comparing router vs frontier-only strategies
8. Print the report and test the hypothesis: >=40% cost reduction, >=95%
   quality retention

Usage:
    python run_benchmark.py

The report will show:
- Total cost and mean quality under router strategy
- Total cost and mean quality under frontier-only (baseline) strategy
- % cost reduction and % quality retention
- Whether the core hypothesis (40% cost, 95% quality) passed
- Breakdowns by task type and difficulty

LIVE vs SIMULATED: router.adapters.get_adapter() uses real provider calls
(Anthropic/OpenAI/DeepSeek) for any model whose API key is configured in
.env, cached to disk under .cache/ so re-runs are free; it falls back to
the offline MockAdapter for any model without a key, or if a real call
keeps failing. The report is stamped "LIVE RESULTS" only if every model in
the run was real — otherwise "PARTIALLY SIMULATED" with a per-model
breakdown. See src/router/adapters/ and src/router/scoring.py.
"""

import argparse
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# Add src to path so we can import the router package.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from router import db  # noqa: E402
from router.adapters import get_adapter  # noqa: E402
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.classifier import classify_task  # noqa: E402
from router.config import DEFAULT_ROSTER, EFFORT_STATES, ROSTERS, get_model, get_roster  # noqa: E402
from router.report import build_report, format_report_markdown, print_report  # noqa: E402
from router.router import route_task  # noqa: E402
from router.scoring import score  # noqa: E402

STRATEGIES = ("router", "frontier_only")


def _parse_effort_policy(raw: Optional[str]) -> Optional[dict[str, Optional[str]]]:
    """
    Parse a `tier=effort,...` string into an effort policy dict.

    Example: "budget=off,mid=low,frontier=medium". Omitting the flag entirely
    yields None, which sends no effort/thinking config at all and reproduces
    the published v1 request shape exactly.
    """
    if not raw:
        return None
    policy: dict[str, Optional[str]] = {}
    for pair in raw.split(","):
        tier, _, effort = pair.partition("=")
        tier, effort = tier.strip(), effort.strip()
        if tier not in ("budget", "mid", "frontier"):
            raise SystemExit(f"Unknown tier {tier!r} in --effort-policy")
        if effort not in EFFORT_STATES:
            raise SystemExit(
                f"Unknown effort {effort!r} in --effort-policy; "
                f"expected one of {', '.join(EFFORT_STATES)}"
            )
        policy[tier] = effort
    return policy


def _report_path(
    roster: str,
    all_real: bool,
    classify: bool,
    effort_policy: Optional[dict[str, Optional[str]]],
) -> Path:
    """
    Pick an output path that cannot clobber a published result.

    data/benchmark_report.md is a COMMITTED artifact holding ONE specific live
    run: the published v1 result — cross-vendor roster, hand-authored labels,
    no effort dial. That exact configuration is the only thing allowed to write
    there. Every other run gets a descriptive filename.

    This is deliberately strict. It would be easy to reserve the canonical file
    for "the default roster, live" — but a `cross_vendor --classify` run is the
    default roster and live, and it is a DIFFERENT experiment (it pays for a
    classifier and reports a different number). Letting it claim the canonical
    filename would overwrite the published 53.6% labels result with a
    same-roster-but-not-the-same-experiment number. So the guard keys on the
    full run shape, not just the roster.

    A partially-simulated run can only ever write to a `_simulated_` name, so
    fabricated mock numbers can never land on any published path.
    """
    data_dir = Path(__file__).parent / "data"
    if not all_real:
        return data_dir / f"benchmark_report_simulated_{roster}.md"

    is_published_shape = (
        roster == DEFAULT_ROSTER and not classify and not effort_policy
    )
    if is_published_shape:
        return data_dir / "benchmark_report.md"

    parts = [roster]
    if classify:
        parts.append("classified")
    if effort_policy:
        parts.append("effort")
    return data_dir / f"benchmark_report_{'_'.join(parts)}.md"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--roster",
        choices=sorted(ROSTERS),
        default=DEFAULT_ROSTER,
        help=(
            "Model ladder to route across. Default %(default)s is the published "
            "v1 roster; claude_tiers is the single-vendor v2 ladder."
        ),
    )
    parser.add_argument(
        "--effort-policy",
        default=None,
        help=(
            "Per-tier effort, e.g. 'budget=off,mid=low,frontier=medium'. "
            "Omit to send no effort/thinking config (the v1 request shape)."
        ),
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help=(
            "Predict each task's (task_type, difficulty) from its prompt using "
            "the roster's budget model, instead of reading the hand-authored "
            "labels. This is what a real deployment has. Costs one budget-model "
            "call per task; that cost is logged and reported as routing "
            "overhead rather than excluded from the savings figure."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    """Run the full benchmark pipeline end to end and print/save the report."""
    args = _build_parser().parse_args(argv)
    effort_policy = _parse_effort_policy(args.effort_policy)
    roster = get_roster(args.roster)

    tasks = load_tasks()
    db.init_db()
    run_group = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    for strategy in STRATEGIES:
        for task in tasks:
            routing_cost = 0.0
            agreed: Optional[bool] = None

            if strategy == "router":
                # The classifier runs on the ROUTER arm only. The frontier-only
                # baseline does not route, so it has no routing overhead to pay
                # — charging it one would understate the router's true cost.
                classification = classify_task(
                    task, roster_name=args.roster, use_labels=not args.classify
                )
                routing_cost = classification.cost_usd
                agreed = classification.agreed_with_label
                decision = route_task(
                    task,
                    roster_name=args.roster,
                    effort_policy=effort_policy,
                    task_type=classification.task_type,
                    difficulty=classification.difficulty,
                )
                model_name = decision.chosen_model
                effort = decision.effort
            else:
                model_name = roster.frontier
                effort = (effort_policy or {}).get("frontier")

            adapter = get_adapter(model_name)
            response = adapter.complete(task["prompt"], effort=effort)
            cost = get_model(model_name).cost_for_tokens(
                response.input_tokens, response.output_tokens
            )
            quality = score(task, response)

            db.log_run(
                run_group=run_group,
                strategy=strategy,
                task_id=task["id"],
                # The task's TRUE labels are logged, not the predicted ones, so
                # the report's per-type/per-difficulty breakdowns stay anchored
                # to ground truth even when the router acted on a wrong guess.
                task_type=task["task_type"],
                difficulty=task["difficulty"],
                model=model_name,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=cost,
                latency_ms=response.latency_ms,
                quality_score=quality,
                success=response.success,
                simulated=response.simulated,
                response_text=response.text,
                effort=response.effort,
                roster=args.roster,
                routing_cost_usd=routing_cost,
                classifier_agreed=agreed,
            )

    report = build_report(run_group)
    print_report(report)

    out_path = _report_path(args.roster, report.all_real, args.classify, effort_policy)
    out_path.write_text(format_report_markdown(report))
    print(f"\nFull report written to {out_path}")
    if not report.all_real:
        print(
            "NOTE: this run was partially simulated, so it was written to a "
            "`_simulated_` filename and did NOT touch the published report."
        )


if __name__ == "__main__":
    main()
