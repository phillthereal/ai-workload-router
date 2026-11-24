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
from router.adapters import SpendMeter, get_adapter  # noqa: E402
from router.api import (  # noqa: E402
    CascadePolicy, FrontierOnlyPolicy, JuryPolicy, LearnedPolicy, ProviderError,
    Router, RulesPolicy, default_policy, jev_log_fields,
)
from router.benchmark import guards  # noqa: E402
from router.benchmark.guards import (  # noqa: E402,F401  (re-exported: tests reach them through this module)
    _TASK_RESERVE_USD, RunStopped, _check_real, _spend_cap,
)
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.lookup import public_lookup  # noqa: E402
from router.config import DEFAULT_ROSTER, EFFORT_STATES, ROSTERS  # noqa: E402
from router.report import build_report, format_report_markdown, print_report  # noqa: E402
from router.scoring import JudgeError, score  # noqa: E402
from router.secrets import force_mock  # noqa: E402

# Experimental arms the benchmark can run against the frontier_only baseline.
EXPERIMENTAL_STRATEGIES = ("router", "cascade", "jury")


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
    tags: list[str],
    effort_policy: Optional[dict[str, Optional[str]]] = None,
    strategy: str = "router",
    tasks: Optional[str] = None,
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
    filename would overwrite the published 53.6% labels result. So the guard
    keys on the full run shape, not just the roster.

    `tags` is the run shape: the policy's own report_tags(), in the order the
    policy gives them. The filename is built from those tags, so a tag that no
    code knows about still lands in the name, and a new experiment cannot reuse
    an old filename by accident. A policy's own name is dropped from its tags
    ("jury-majority" under strategy "jury" becomes "majority"), which keeps the
    published jury filenames unchanged.

    A partially-simulated run can only ever write to a `_simulated_` name, so
    fabricated mock numbers can never land on any published path.
    """
    data_dir = Path(__file__).parent / "data"
    tasks_tag = _tasks_tag(tasks)
    if not all_real:
        suffix = f"_{tasks_tag}" if tasks_tag else ""
        return data_dir / f"benchmark_report_simulated_{roster}{suffix}.md"

    is_published_shape = (
        roster == DEFAULT_ROSTER
        and strategy == "router"
        and not tags
        and not effort_policy
        and tasks_tag is None
    )
    if is_published_shape:
        return data_dir / "benchmark_report.md"

    parts = [roster]
    if strategy != "router":
        parts.append(strategy)
    own_prefix = f"{strategy}-"
    for tag in tags:
        parts.append(tag[len(own_prefix):] if tag.startswith(own_prefix) else tag)
    # Effort is not a policy tag, so it always comes after the policy tags.
    if effort_policy:
        parts.append("effort")
    if tasks_tag:
        parts.append(tasks_tag)
    return data_dir / f"benchmark_report_{'_'.join(parts)}.md"


# The benchmark replays PUBLISHED cached results, which were recorded with the
# pre-hardening ("v2") meta-prompts; API/server callers get the hardened default.
_PV = "v2"


# The guard rails (_TASK_RESERVE_USD, _spend_cap, RunStopped, _check_real and the two
# below) live in router.benchmark.guards so `awr bench` can share them. These two thin
# wrappers pass this module's `score` and `get_adapter`, so patching either name here
# still reaches the run, as it did before the move.
def _run_tasks(router, tasks, workers: int, max_spend: Optional[float]):
    """Yield (task, result, quality) in task order, answering `workers` tasks at a time.

    The spend check runs before each batch and reserves room for the whole
    batch, so a parallel run stops under the cap just as a serial one does.
    """
    return guards._run_tasks(router, tasks, workers, max_spend, scorer=score)


def _preflight(roster_name: str) -> None:
    """One tiny real call per roster model, so a bad key or empty account stops the run first."""
    guards._preflight(roster_name, adapter_factory=get_adapter)


def _is_default_run(args: argparse.Namespace) -> bool:
    """A plain run gets the router's default. Any routing flag keeps that flag's own meaning."""
    return (args.strategy == "router" and args.roster == DEFAULT_ROSTER and not args.effort_policy
            and not (args.classic or args.classify or args.learned or args.lookup
                     or args.agree_on or args.frontier_on or args.consensus_on))


def _build_policy(args: argparse.Namespace, tasks: list[dict]):
    """Map CLI flags to the experimental Policy. The lookup never sees the tasks under test."""
    if args.strategy == "cascade":
        return CascadePolicy(args.verifier, args.escalate_threshold, prompt_version=_PV)
    if args.strategy == "jury":
        return JuryPolicy(mode=args.jury_mode, prompt_version=_PV)
    if args.learned:
        return LearnedPolicy(use_labels=not args.classify, prompt_version=_PV)
    if _is_default_run(args):
        return default_policy(_PV, public_lookup(t["prompt"] for t in tasks))
    return RulesPolicy(use_labels=not args.classify, prompt_version=_PV,
                       agree_on=tuple(args.agree_on), frontier_on=tuple(args.frontier_on),
                       consensus_on=tuple(args.consensus_on),
                       lookup=public_lookup(t["prompt"] for t in tasks) if args.lookup else None)


def _policy_report_path(roster, all_real, policy, effort_policy, tasks) -> Path:
    """_report_path with the run-shape tags taken from the policy's report_tags()."""
    return _report_path(roster, all_real, policy.report_tags(), effort_policy, policy.name, tasks)


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
        "--agree-on",
        action="append",
        default=[],
        metavar="TASK_TYPE",
        help=(
            "For this task type, ask the budget and mid models and keep the answer "
            "if they agree; if not, ask the frontier model. Repeatable. "
            "(--strategy router, not with --learned.)"
        ),
    )
    parser.add_argument(
        "--classic",
        action="store_true",
        help="Hand labels and the rules table only: the v1 setup behind data/benchmark_report.md.",
    )
    parser.add_argument(
        "--consensus-on",
        action="append",
        default=[],
        metavar="TASK_TYPE",
        help=(
            "For this task type, ask Haiku 5.5 and the mid model and keep the mid answer "
            "if they agree. If not, reasoning gets a vote from the budget model, then "
            "Sonnet 5.5 decides. Repeatable. (As --agree-on.)"
        ),
    )
    parser.add_argument(
        "--lookup",
        action="store_true",
        help="Predict type and difficulty free from the most similar public tasks, "
             "leaving out the tasks under test. (As --agree-on, replaces --classify.)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Answer this many tasks at a time. Faster, and logged in the same order, "
            "but reported latency then includes load from the other tasks. "
            "Not with --learned, which depends on task order."
        ),
    )
    parser.add_argument(
        "--frontier-on",
        action="append",
        default=[],
        metavar="TASK_TYPE",
        help="Always answer this task type on the frontier model. Repeatable. (As --agree-on.)",
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
        "--jury-mode", choices=("unanimous", "majority"), default="unanimous",
        help="Jury only: escalate unless all jurors agree (unanimous) or a majority does.",
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
    parser.add_argument(
        "--max-spend",
        type=_spend_cap,
        default=None,
        metavar="USD",
        help=(
            "Live run with guard rails: test each model first, stop before real "
            "spend passes USD, and stop on any simulated answer, classifier or "
            "judge fallback, or provider failure. A stopped run writes no report."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    """Run the benchmark, turning a guard-rail stop into a clear exit."""
    try:
        _run(argv)
    except (RunStopped, JudgeError, ProviderError) as exc:
        raise SystemExit(f"STOPPED: {exc}. Real spend ${SpendMeter.usd:.4f}. No report written.") from None


def _run(argv: Optional[list[str]] = None) -> None:
    """Run the full benchmark pipeline end to end and print/save the report."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.workers < 1 or (args.workers > 1 and args.learned):
        parser.error("--workers must be at least 1, and 1 with --learned")
    if args.max_spend is not None and force_mock():
        parser.error("--max-spend is for live runs; unset AWR_FORCE_MOCK")
    effort_policy = _parse_effort_policy(args.effort_policy)

    tasks = load_tasks(args.tasks)
    db.init_db()
    run_group = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    # The experimental policy plus the fixed frontier-only baseline. Both
    # experimental policies charge their decision cost to routing_cost_usd, so
    # the report's net-savings accounting is identical for either. The benchmark
    # opts into the offline mock fallback (allow_mock_fallback=True) so a run
    # always completes; real callers use router.api.Router's strict default.
    policy = _build_policy(args, tasks)
    capped = args.max_spend is not None
    if capped:
        _preflight(args.roster)
    fallbacks = truncated = 0
    for pol in (policy, FrontierOnlyPolicy()):
        router = Router(
            pol, roster_name=args.roster, effort_policy=effort_policy,
            allow_mock_fallback=not capped,
        )
        # `run_group` is the chronological cutoff for the learned policy: a
        # task only sees evidence strictly earlier than the run it is part of.
        router.before_run_group(run_group)
        for task, result, quality in _run_tasks(router, tasks, args.workers, args.max_spend):
            response = result.response
            fallbacks += result.classifier_fell_back
            truncated += response.truncated

            db.log_run(
                run_group=run_group,
                strategy=pol.name,
                task_id=task["id"],
                # The task's TRUE labels are logged, not the predicted ones, so
                # the report's per-type/per-difficulty breakdowns stay anchored
                # to ground truth even when the router acted on a wrong guess.
                task_type=task["task_type"],
                difficulty=task["difficulty"],
                model=result.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=result.answer_cost_usd,
                latency_ms=result.latency_ms or response.latency_ms,
                quality_score=quality,
                success=response.success,
                simulated=response.simulated,
                response_text=response.text,
                effort=response.effort,
                roster=args.roster,
                routing_cost_usd=result.routing_cost_usd,
                classifier_agreed=result.classifier_agreed,
                verifier_scores=result.verifier_scores,
                learned_evidence=result.learned_evidence,
                **jev_log_fields(result),
            )

    report = build_report(run_group, experimental_strategy=args.strategy)
    print_report(report)

    out_path = _policy_report_path(args.roster, report.all_real, policy, effort_policy, args.tasks)
    out_path.write_text(format_report_markdown(report))
    print(f"\nFull report written to {out_path}")
    print(f"Real spend ${SpendMeter.usd:.4f}, classifier fallbacks {fallbacks}, truncated answers {truncated}")
    if not report.all_real:
        print(
            "NOTE: this run was partially simulated, so it was written to a "
            "`_simulated_` filename and did NOT touch the published report."
        )


if __name__ == "__main__":
    main()
