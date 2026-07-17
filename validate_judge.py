#!/usr/bin/env python3
"""
Runnable entry point: cross-check the primary rubric_judge scores (from
claude-opus-4-8, the frontier/judge model) against an independent second
judge (gpt-4o-mini, a different vendor) on the most recently benchmarked
run, then write a human label sheet for a third, manual check.

Usage:
    python validate_judge.py

WARNING — this makes REAL gpt-4o-mini API calls when run for real (gated
behind OPENAI_API_KEY exactly like router.scoring's primary judge; see
router.adapters.get_adapter). Do NOT run it live casually. Run with
AWR_FORCE_MOCK=1 to exercise it fully offline — under that flag,
get_adapter() returns the offline MockAdapter for the second judge too, so
this script is safe to run with no API keys and no network access at all.

See src/router/judge_validation.py for the underlying functions.
"""

import sys
from pathlib import Path

# Add src to path so we can import the router package — same pattern as
# run_benchmark.py.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from router import judge_validation  # noqa: E402


def main() -> None:
    """Run inter-judge agreement on the latest benchmark run, print the
    summary, and export a human label sheet for that same run_group."""
    try:
        result = judge_validation.run_inter_judge_agreement()
    except judge_validation.JudgeValidationError as exc:
        print(f"Judge validation failed: {exc}")
        return

    judge_validation.print_agreement_summary(result)

    sheet_path = judge_validation.export_human_label_sheet(run_group=result["run_group"])
    print(f"\nHuman label sheet written to {sheet_path}")
    print(
        "Fill in the human_score column, then call "
        "router.judge_validation.score_human_agreement() to compute "
        "judge-vs-human agreement."
    )


if __name__ == "__main__":
    main()
