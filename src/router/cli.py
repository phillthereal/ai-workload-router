"""`awr` command line. One subcommand: `awr bench tasks.jsonl` (docs/BYO_WORKLOAD.md).

This module only parses arguments; loading, estimating, running and formatting
live in router.benchmark.byo.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .api import ProviderError
from .benchmark import byo
from .benchmark.guards import RunStopped, _spend_cap
from .config import DEFAULT_ROSTER, ROSTERS
from .scoring import JudgeError
from .secrets import force_mock

NEITHER_MODE = ("Nothing was run. Set AWR_FORCE_MOCK=1 for a free simulated run, "
                "or pass --max-spend USD for a live run.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="awr", description="AI Workload Router command line.")
    sub = parser.add_subparsers(dest="command", required=True)
    bench = sub.add_parser(
        "bench",
        help="Run your own JSONL prompts through the default rule and frontier-only.",
        description=(
            "Answer every row of a JSONL file with the default rule and with frontier-only, and "
            "report cost cut and quality kept with 99%% ranges. A dry run with the list-price "
            "worst case is always printed first. Mock mode: AWR_FORCE_MOCK=1. Live mode: --max-spend."
        ),
    )
    bench.add_argument("tasks", help="JSONL file, one {\"prompt\": ...} object per line (see docs/BYO_WORKLOAD.md).")
    bench.add_argument(
        "--max-spend", type=_spend_cap, default=None, metavar="USD",
        help=("Live run: refused before any call if the worst case is above USD; tests each model "
              "first and stops on any simulated answer, judge fallback or provider failure. "
              "A stopped run writes no report. Not with AWR_FORCE_MOCK."),
    )
    bench.add_argument(
        "--roster", choices=sorted(ROSTERS), default=DEFAULT_ROSTER,
        help=("Swaps the budget, mid and frontier models. Haiku 5.5 and Sonnet 5.5 stay fixed in "
              "the default rule. Default %(default)s."),
    )
    bench.add_argument(
        "--workers", type=int, default=1,
        help=("Answer this many rows at a time. Faster, but reported latency then includes "
              "load from the other rows."),
    )
    bench.add_argument("--out", default=None, metavar="PATH",
                       help="Report path: must end in .md, must not exist, and not inside data/ except data/byo/.")
    bench.add_argument("--dry-run", action="store_true", help="Print the dry run and exit, with no calls.")
    return parser


def _bench(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    mock = force_mock()
    if args.max_spend is not None and mock:
        parser.error("--max-spend is for live runs; unset AWR_FORCE_MOCK")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    try:
        out = byo.check_out_path(args.out) if args.out else None
        tasks = byo.load_rows(args.tasks, args.roster)
    except byo.BYOError as exc:
        parser.error(str(exc))
    input_name = Path(args.tasks).name
    est = byo.estimate(tasks, args.roster)
    print(byo.format_dry_run(est, input_name, mock, args.workers))
    if args.dry_run:
        print(byo.STUBBED_ONLY_NOTE)
        return 0
    live = args.max_spend is not None
    if not (mock or live):
        print(NEITHER_MODE, file=sys.stderr)
        return 2
    if live:
        need = est.required_cap(args.workers)
        if need > args.max_spend:
            print(f"REFUSED: this run needs --max-spend of at least ${need:.4f} (worst case "
                  f"${est.total_usd:.4f} + reserve ${est.reserve_usd:.4f} x {args.workers} workers + "
                  f"preflight ${est.preflight_usd:.4f}); you gave ${args.max_spend:.2f}. Nothing was called.",
                  file=sys.stderr)
            return 1
    # Claim the report path before any call: an unwritable path stops the run while it is free.
    run_group = byo.new_run_group()
    path = out or byo.default_report_path(byo.report_stem(args.tasks), run_group, simulated=not live)
    try:
        byo.claim_report_path(path)
    except byo.BYOError as exc:
        parser.error(str(exc))
    print()
    try:
        result = byo.run(tasks, roster=args.roster, input_name=input_name, live=live,
                         max_spend=args.max_spend, est=est, workers=args.workers, run_group=run_group)
    except (RunStopped, JudgeError, ProviderError) as exc:
        byo.release_report_path(path)
        raise SystemExit(f"STOPPED: {exc}. Real spend ${byo.spent():.4f}. No report written.") from None
    except BaseException:
        byo.release_report_path(path)
        raise
    text = byo.write_report(result, path)
    print(text.rstrip("\n").removesuffix(byo.STUBBED_ONLY_NOTE).rstrip("\n"))
    print(f"Report written to {path}")
    print(f"Real spend ${byo.spent():.4f}")
    print(byo.STUBBED_ONLY_NOTE)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the `awr` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "bench":
        return _bench(parser, args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
