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
from router import learned  # noqa: E402
from router.adapters import get_adapter  # noqa: E402
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.cascade import run_cascade  # noqa: E402
from router.classifier import classify_task  # noqa: E402
from router.config import DEFAULT_ROSTER, EFFORT_STATES, ROSTERS, get_model, get_roster  # noqa: E402
from router.report import BASELINE_STRATEGY, build_report, format_report_markdown, print_report  # noqa: E402
from router.router import route_task  # noqa: E402
from router.scoring import score  # noqa: E402

# Experimental arms the benchmark can run against the frontier_only baseline.
EXPERIMENTAL_STRATEGIES = ("router", "cascade")


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


def _tasks_tag(tasks: Optional[str]) -> Optional[str]:
    """Short filename tag for a non-default task set (tasks_hard.json -> 'hard')."""
    if not tasks:
        return None
    stem = Path(tasks).stem
    if stem == "tasks":
        return None
    return stem[len("tasks_"):] if stem.startswith("tasks_") else stem


def _report_path(
    roster: str,
    all_real: bool,
    classify: bool,
    effort_policy: Optional[dict[str, Optional[str]]],
    strategy: str = "router",
    tasks: Optional[str] = None,
    verifier: Optional[str] = None,
    learned_flag: bool = False,
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

    `verifier` (cascade only) is likewise part of the run shape: a cascade run
    with an explicit `--verifier` overriding run_cascade's default is a
    DIFFERENT experiment from the same roster/strategy/tasks with the default
    verifier — same as --classify above, it must not silently overwrite the
    default-verifier report under the same filename. (This guard exists
    because a live `--verifier claude-haiku-4-5` run once did exactly that
    clobber before the tag was added — see git history / docs/V2_FINDINGS.md's
    verifier-economics note.)

    `learned_flag` (v3) is part of the run shape for the same reason: a
    `--learned` run consults outcome history and can route differently from
    the identical flags without it, so it must not overwrite the
    no-history report — and it must never claim the canonical v1 filename.
    """
    data_dir = Path(__file__).parent / "data"
    tag = _tasks_tag(tasks)
    if not all_real:
        suffix = f"_{tag}" if tag else ""
        return data_dir / f"benchmark_report_simulated_{roster}{suffix}.md"

    is_published_shape = (
        roster == DEFAULT_ROSTER
        and strategy == "router"
        and not classify
        and not effort_policy
        and tag is None
        and verifier is None
        and not learned_flag
    )
    if is_published_shape:
        return data_dir / "benchmark_report.md"

    parts = [roster]
    if strategy != "router":
        parts.append(strategy)
    if classify:
        parts.append("classified")
    if learned_flag:
        parts.append("learned")
    if effort_policy:
        parts.append("effort")
    if verifier:
        parts.append(f"verifier-{verifier}")
    if tag:
        parts.append(tag)
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
            "overhead rather than excluded from the savings figure. "
            "(--strategy router only.)"
        ),
    )
    parser.add_argument(
        "--learned",
        action="store_true",
        help=(
            "After classifying (predicted labels, or hand labels if "
            "--classify is omitted), consult data/runs.db for logged "
            "outcomes on tasks like this one and let history override the "
            "classifier's tier toward something cheaper — only under a "
            "strict evidence threshold (n>=5 similar prior outcomes at or "
            "above the 95%%-of-frontier quality bar), and it can also "
            "escalate the tier if recent evidence looks concerning. See "
            "router.learned and docs/V3_DESIGN.md. Costs nothing extra: "
            "the lookup is pure SQL/dict logic, so routing overhead is "
            "still just the classifier's own cost (0 if using hand labels). "
            "(--strategy router only.)"
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=EXPERIMENTAL_STRATEGIES,
        default="router",
        help=(
            "Experimental arm to compare against the frontier-only baseline. "
            "'router' predicts difficulty and routes once; 'cascade' tries the "
            "cheapest model, verifies the answer, and escalates only on a failed "
            "check. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--escalate-threshold",
        type=float,
        default=0.7,
        help=(
            "Cascade only: minimum verifier adequacy [0-1] to accept a cheap "
            "answer instead of escalating. Lower trusts cheap answers more "
            "(cheaper, riskier); higher escalates more readily. Default "
            "%(default)s."
        ),
    )
    parser.add_argument(
        "--tasks",
        default=None,
        help=(
            "Path to an alternate task file (e.g. data/tasks_hard.json). Defaults "
            "to the published data/tasks.json. The hard set exists to stress "
            "quality retention on tasks that actually separate the tiers."
        ),
    )
    parser.add_argument(
        "--verifier",
        default=None,
        help=(
            "Cascade only: model name to use as the quality-gate verifier "
            "(e.g. claude-haiku-4-5). Omit to use run_cascade's default (the "
            "roster's BUDGET tier — cheap but self-verifying; pass the "
            "roster's mid model here for an independent grader instead). See "
            "router.cascade.run_cascade's docstring for the cost-vs-"
            "independence trade-off this flag controls."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    """Run the full benchmark pipeline end to end and print/save the report."""
    args = _build_parser().parse_args(argv)
    effort_policy = _parse_effort_policy(args.effort_policy)
    roster = get_roster(args.roster)

    tasks = load_tasks(args.tasks)
    db.init_db()
    run_group = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    # The experimental arm (router OR cascade) plus the fixed frontier-only
    # baseline. Both experimental arms charge their decision cost to
    # routing_cost_usd, so the report's net-savings accounting is identical for
    # either — the classifier's prediction toll and the cascade's
    # verifier-plus-discarded-attempt overhead land in the same column.
    for strategy in (args.strategy, BASELINE_STRATEGY):
        for task in tasks:
            routing_cost = 0.0
            agreed: Optional[bool] = None
            effort: Optional[str] = None
            verifier_scores: Optional[list[float]] = None
            learned_evidence: Optional[dict] = None

            if strategy == "cascade":
                # React-to-failure: try cheap, verify, escalate only on a failed
                # check. cost_usd is the winning answer; overhead (verifier
                # calls + discarded cheap attempts) is the routing cost.
                result = run_cascade(
                    task,
                    roster_name=args.roster,
                    verifier_model=args.verifier,
                    escalate_threshold=args.escalate_threshold,
                )
                response = result.response
                model_name = response.model
                cost = result.answer_cost
                routing_cost = result.overhead_cost
                # Persist the per-tier verifier scores so escalate_threshold can
                # be re-tuned directly from the log (see tune_cascade.py).
                verifier_scores = result.verifier_scores
            elif strategy == "router":
                # Predict-then-route. The classifier runs on this arm only; the
                # baseline does not route, so charging it overhead would
                # understate the router's true cost.
                classification = classify_task(
                    task, roster_name=args.roster, use_labels=not args.classify
                )
                routing_cost = classification.cost_usd
                agreed = classification.agreed_with_label

                if args.learned:
                    # History gets a chance to override the classifier's
                    # tier — but only toward cheaper, and only under the
                    # evidence threshold; see router.learned. `run_group` is
                    # the chronological cutoff: this task can only see
                    # evidence strictly earlier than the run it's part of,
                    # which excludes both the future and its own siblings
                    # logged earlier in this same loop.
                    learned_decision = learned.evaluate(
                        task, classification.task_type, classification.difficulty,
                        roster_name=args.roster, before_run_group=run_group,
                    )
                    learned_evidence = learned_decision.to_log_dict()
                    decision = route_task(
                        task,
                        roster_name=args.roster,
                        effort_policy=effort_policy,
                        tier_override=learned_decision.chosen_tier,
                    )
                else:
                    decision = route_task(
                        task,
                        roster_name=args.roster,
                        effort_policy=effort_policy,
                        task_type=classification.task_type,
                        difficulty=classification.difficulty,
                    )
                model_name = decision.chosen_model
                effort = decision.effort
                response = get_adapter(model_name).complete(task["prompt"], effort=effort)
                cost = get_model(model_name).cost_for_tokens(
                    response.input_tokens, response.output_tokens
                )
            else:  # frontier_only baseline
                model_name = roster.frontier
                effort = (effort_policy or {}).get("frontier")
                response = get_adapter(model_name).complete(task["prompt"], effort=effort)
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
                verifier_scores=verifier_scores,
                learned_evidence=learned_evidence,
            )

    report = build_report(run_group, experimental_strategy=args.strategy)
    print_report(report)

    out_path = _report_path(
        args.roster, report.all_real, args.classify, effort_policy, args.strategy, args.tasks,
        args.verifier, args.learned,
    )
    out_path.write_text(format_report_markdown(report))
    print(f"\nFull report written to {out_path}")
    if not report.all_real:
        print(
            "NOTE: this run was partially simulated, so it was written to a "
            "`_simulated_` filename and did NOT touch the published report."
        )


if __name__ == "__main__":
    main()
