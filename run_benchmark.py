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

import sys
import time
import uuid
from pathlib import Path

# Add src to path so we can import the router package.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from router import db  # noqa: E402
from router.adapters import get_adapter  # noqa: E402
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.config import FRONTIER_MODEL, get_model  # noqa: E402
from router.report import build_report, format_report_markdown, print_report  # noqa: E402
from router.router import route_task  # noqa: E402
from router.scoring import score  # noqa: E402

STRATEGIES = ("router", "frontier_only")


def main() -> None:
    """Run the full benchmark pipeline end to end and print/save the report."""
    tasks = load_tasks()
    db.init_db()
    run_group = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    for strategy in STRATEGIES:
        for task in tasks:
            if strategy == "router":
                model_name = route_task(task).chosen_model
            else:
                model_name = FRONTIER_MODEL

            adapter = get_adapter(model_name)
            response = adapter.complete(task["prompt"])
            cost = get_model(model_name).cost_for_tokens(
                response.input_tokens, response.output_tokens
            )
            quality = score(task, response)

            db.log_run(
                run_group=run_group,
                strategy=strategy,
                task_id=task["id"],
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
            )

    report = build_report(run_group)
    print_report(report)

    out_path = Path(__file__).parent / "data" / "benchmark_report.md"
    out_path.write_text(format_report_markdown(report))
    print(f"\nFull report written to {out_path}")


if __name__ == "__main__":
    main()
