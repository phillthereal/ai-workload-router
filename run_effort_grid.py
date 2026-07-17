#!/usr/bin/env python3
"""
Effort-grid benchmark: the (model x effort) cost/quality frontier.

Separate from run_benchmark.py because it answers a different question. The main
benchmark asks "does routing across a ladder save money?"; this asks "for a
single model, what does the effort dial buy?" — the dial that trades thinking
tokens (billed as output) for quality WITHOUT changing the model, and that only
exists inside one vendor.

It sweeps every task at each (model, effort) cell and reports total cost and
mean quality (real Opus judge), writing a markdown table to
data/effort_grid.md. Haiku 4.5 has no effort dial (one row); Sonnet 5 and
Opus 4.8 sweep off/low/high — the meaningful range, since xhigh/max are the
flat, wasteful end of the curve.

Every call is content-addressed in .cache/, so re-running this after the first
live sweep is free and deterministic.

Usage:
    python run_effort_grid.py
"""

from __future__ import annotations  # 3.9-safe: stringizes str|None annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "src"))

from router.adapters import get_adapter  # noqa: E402
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.config import get_model  # noqa: E402
from router.scoring import score  # noqa: E402

# (model, effort) cells. None = no effort/thinking config (Haiku's only option);
# "off" = thinking explicitly disabled; "low"/"high" = adaptive thinking.
GRID: list[tuple[str, Optional[str]]] = [
    ("claude-haiku-4-5", None),
    ("claude-sonnet-5", "off"),
    ("claude-sonnet-5", "low"),
    ("claude-sonnet-5", "high"),
    ("claude-opus-4-8", "off"),
    ("claude-opus-4-8", "low"),
    ("claude-opus-4-8", "high"),
]


def run_cell(model: str, effort: Optional[str]) -> dict:
    """Run every task once at one (model, effort) cell; aggregate cost/quality."""
    tasks = load_tasks()
    adapter = get_adapter(model)
    total_cost = 0.0
    quality_sum = 0.0
    truncations = 0
    simulated = False
    for task in tasks:
        resp = adapter.complete(task["prompt"], effort=effort)
        simulated = simulated or resp.simulated
        total_cost += get_model(model).cost_for_tokens(resp.input_tokens, resp.output_tokens)
        quality_sum += score(task, resp)
        truncations += int(resp.truncated)
    return {
        "model": model,
        "effort": effort or "—",
        "cost": total_cost,
        "quality": quality_sum / len(tasks),
        "truncated": truncations,
        "simulated": simulated,
    }


def format_markdown(rows: list[dict]) -> str:
    """Render the grid + the key frontier comparisons as markdown."""
    all_real = not any(r["simulated"] for r in rows)
    out = ["# Effort grid — (model × effort) cost/quality frontier", ""]
    out.append(
        "**LIVE RESULTS** — real provider calls, real Opus judge."
        if all_real
        else "**PARTIALLY SIMULATED** — at least one cell fell back to the offline mock; "
        "its numbers are fabricated and must not be reported."
    )
    out += ["", "| Model | Effort | Total cost | Mean quality | Truncated |",
            "|---|---|---|---|---|"]
    for r in rows:
        out.append(
            f"| {r['model']} | {r['effort']} | ${r['cost']:.5f} | "
            f"{r['quality']:.3f} | {r['truncated']} |"
        )

    def find(model, effort):
        return next((r for r in rows if r["model"] == model and r["effort"] == effort), None)

    def cmp_line(a, la, b, lb):
        if not a or not b:
            return None
        dc = (a["cost"] - b["cost"]) / b["cost"] * 100
        dq = a["quality"] - b["quality"]
        return f"| {la} vs {lb} | {dc:+.1f}% | {dq:+.3f} |"

    out += ["", "## Key comparisons", "",
            "| Comparison | Cost Δ | Quality Δ |", "|---|---|---|"]
    for line in [
        cmp_line(find("claude-sonnet-5", "low"), "Sonnet@low",
                 find("claude-opus-4-8", "off"), "Opus@off"),
        cmp_line(find("claude-opus-4-8", "high"), "Opus@high",
                 find("claude-opus-4-8", "off"), "Opus@off"),
        cmp_line(find("claude-sonnet-5", "low"), "Sonnet@low",
                 find("claude-haiku-4-5", "—"), "Haiku"),
    ]:
        if line:
            out.append(line)
    out += ["", "_n=25. On a task set this easy, quality saturates near 0.99 across "
            "the board, so treat sub-percent quality deltas as noise — the cost "
            "deltas are the real signal. A harder task set is needed to bend the "
            "frontier._", ""]
    return "\n".join(out)


def main() -> None:
    rows = []
    for model, effort in GRID:
        row = run_cell(model, effort)
        rows.append(row)
        flag = "  [SIMULATED]" if row["simulated"] else ""
        print(f"  {model:18} effort={row['effort']:5}  ${row['cost']:.5f}  "
              f"q={row['quality']:.3f}{flag}")

    out_path = Path(__file__).parent / "data" / (
        "effort_grid.md" if not any(r["simulated"] for r in rows)
        else "effort_grid_simulated.md"
    )
    out_path.write_text(format_markdown(rows))
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
