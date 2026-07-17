"""Tests for router.report: latency stats + at-scale cost projection math
(added alongside the multi-vendor roster — see router.config)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Force the offline mock path before importing router modules — see the
# identical comment in test_end_to_end.py.
os.environ.setdefault("AWR_FORCE_MOCK", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from router import db  # noqa: E402
from router.report import PROJECTION_VOLUMES, build_report  # noqa: E402


class TestReportLatencyAndProjection(unittest.TestCase):
    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.db_path = Path(fd.name)
        db.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _log(self, **overrides):
        base = dict(
            run_group="g1", strategy="router", task_id="t1", task_type="classification",
            difficulty="easy", model="gpt-4o-mini", input_tokens=10, output_tokens=5,
            cost_usd=0.001, latency_ms=100.0, quality_score=0.9, success=True,
            db_path=self.db_path,
        )
        base.update(overrides)
        return db.log_run(**base)

    def test_latency_mean_median_and_delta(self):
        # router: known latencies -> mean 200, median 200
        self._log(task_id="t1", strategy="router", latency_ms=100.0)
        self._log(task_id="t2", strategy="router", latency_ms=200.0)
        self._log(task_id="t3", strategy="router", latency_ms=300.0)
        # frontier_only: known latencies -> mean 500, median 500
        self._log(task_id="t1", strategy="frontier_only", latency_ms=400.0, model="claude-opus-4-8")
        self._log(task_id="t2", strategy="frontier_only", latency_ms=600.0, model="claude-opus-4-8")

        report = build_report("g1", db_path=self.db_path)

        router_stats = report.latency_by_strategy["router"]
        self.assertAlmostEqual(router_stats["mean_ms"], 200.0)
        self.assertAlmostEqual(router_stats["median_ms"], 200.0)
        self.assertEqual(router_stats["n"], 3)

        frontier_stats = report.latency_by_strategy["frontier_only"]
        self.assertAlmostEqual(frontier_stats["mean_ms"], 500.0)
        self.assertAlmostEqual(frontier_stats["median_ms"], 500.0)
        self.assertEqual(frontier_stats["n"], 2)

        # router mean 200 vs frontier mean 500 -> router is 60% faster.
        self.assertAlmostEqual(report.latency_delta_pct, 60.0)

    def test_latency_delta_negative_when_router_slower(self):
        self._log(task_id="t1", strategy="router", latency_ms=800.0)
        self._log(task_id="t1", strategy="frontier_only", latency_ms=400.0, model="claude-opus-4-8")

        report = build_report("g1", db_path=self.db_path)

        # router (800) is slower than frontier (400) -> negative delta, -100%.
        self.assertAlmostEqual(report.latency_delta_pct, -100.0)

    def test_latency_stats_default_to_zero_when_strategy_missing(self):
        self._log(task_id="t1", strategy="router", latency_ms=100.0)
        # No frontier_only rows logged at all.

        report = build_report("g1", db_path=self.db_path)

        self.assertEqual(report.latency_by_strategy.get("frontier_only", {}), {})
        self.assertAlmostEqual(report.latency_delta_pct, 0.0)

    def test_cost_per_task_and_projection_at_volume(self):
        # router: 2 tasks, total cost 0.02 -> cost/task = 0.01
        self._log(task_id="t1", strategy="router", cost_usd=0.005)
        self._log(task_id="t2", strategy="router", cost_usd=0.015)
        # frontier_only: 2 tasks, total cost 0.20 -> cost/task = 0.10
        self._log(task_id="t1", strategy="frontier_only", cost_usd=0.08, model="claude-opus-4-8")
        self._log(task_id="t2", strategy="frontier_only", cost_usd=0.12, model="claude-opus-4-8")

        report = build_report("g1", db_path=self.db_path)

        self.assertAlmostEqual(report.cost_per_task_router, 0.01)
        self.assertAlmostEqual(report.cost_per_task_frontier, 0.10)

        self.assertEqual([p["volume"] for p in report.projections], list(PROJECTION_VOLUMES))

        proj_100k = report.projections[0]
        self.assertEqual(proj_100k["volume"], 100_000)
        self.assertAlmostEqual(proj_100k["baseline_monthly"], 10_000.0)
        self.assertAlmostEqual(proj_100k["router_monthly"], 1_000.0)
        self.assertAlmostEqual(proj_100k["savings_monthly"], 9_000.0)

        proj_1m = report.projections[1]
        self.assertEqual(proj_1m["volume"], 1_000_000)
        self.assertAlmostEqual(proj_1m["baseline_monthly"], 100_000.0)
        self.assertAlmostEqual(proj_1m["router_monthly"], 10_000.0)
        self.assertAlmostEqual(proj_1m["savings_monthly"], 90_000.0)

    def test_projection_zero_when_no_frontier_runs(self):
        self._log(task_id="t1", strategy="router", cost_usd=0.01)

        report = build_report("g1", db_path=self.db_path)

        self.assertAlmostEqual(report.cost_per_task_frontier, 0.0)
        for p in report.projections:
            self.assertAlmostEqual(p["baseline_monthly"], 0.0)


if __name__ == "__main__":
    unittest.main()
