#!/usr/bin/env python3
"""Summarise shadow-mode rows: frontier spend vs what the router would have spent.

Usage: python scripts/shadow_report.py <db> [--include-simulated]

Stdlib only, opens the db read-only. The router figure is an estimate (list
prices on the frontier answer's token counts, cheap models assumed to agree);
see docs/SHADOW_MODE.md.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from router import db  # noqa: E402

CAVEAT = ("Estimate only: list prices on the frontier answer's token counts, cheap models assumed "
          "to agree. The router's answers were never produced, so their quality is unknown.")


def _pct(frontier: float, estimate: float) -> str:
    """Net change against the frontier: 'X% cheaper', or 'X% dearer' when the estimate is higher."""
    if not frontier:
        return "n/a"
    pct = 100 * (frontier - estimate) / frontier
    return f"{pct:.1f}% cheaper" if pct >= 0 else f"{-pct:.1f}% dearer"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Shadow-mode cost summary from a --log-db file")
    p.add_argument("db", help="SQLite run log written by the server's --log-db")
    p.add_argument("--include-simulated", action="store_true",
                   help="also count mock (simulated) rows; their numbers are not results")
    a = p.parse_args(argv)
    rows, skipped = db.shadow_rows_counted(a.db, include_simulated=a.include_simulated)
    if not rows:
        print(f"no shadow rows in {a.db}", file=sys.stderr)
        if skipped:
            print(f"{skipped} rows skipped: unreadable estimate", file=sys.stderr)
        return 1
    pre = "SIMULATED " if a.include_simulated else ""
    out: list[str] = []
    if a.include_simulated:
        out.append("WARNING: --include-simulated counts mock answers; these numbers are not results.")
    by_type: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    for r in rows:
        s = r["shadow"]
        b = by_type[s.get("task_type") or r["task_type"]]
        b[0] += 1
        b[1] += s["frontier_cost_usd"]
        b[2] += s["estimated_cost_usd"]
    front = sum(b[1] for b in by_type.values())
    est = sum(b[2] for b in by_type.values())
    out.append(f"{len(rows)} requests, frontier spend ${front:.4f}, router would have spent "
               f"${est:.4f} (estimate), {_pct(front, est)} (net)")
    out.append(f"{'task type':<18} {'requests':>8} {'frontier $':>12} {'est. router $':>14} {'% cheaper (net)':>16}")
    for t in sorted(by_type):
        n, f, e = by_type[t]
        out.append(f"{t:<18} {int(n):>8} {f:>12.4f} {e:>14.4f} {_pct(f, e):>16}")
    if skipped:
        out.append(f"{skipped} rows skipped: unreadable estimate")
    out.append(CAVEAT)
    for line in out:
        print(pre + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
