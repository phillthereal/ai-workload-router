"""
Tests for routing-overhead accounting in router.report.

The claim these defend: the router's savings figure is NET of what the router
itself costs to run. A cost-optimisation project that excludes the cost of its
own optimiser is measuring the wrong thing.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import db  # noqa: E402
from router.report import build_report, format_report_markdown  # noqa: E402


def _log(db_path, strategy, cost, routing=0.0, agreed=None, quality=0.95):
    db.log_run(
        run_group="g",
        strategy=strategy,
        task_id="t",
        task_type="classification",
        difficulty="easy",
        model="claude-haiku-4-5" if strategy == "router" else "claude-opus-4-8",
        input_tokens=100,
        output_tokens=50,
        cost_usd=cost,
        latency_ms=100.0,
        quality_score=quality,
        success=True,
        routing_cost_usd=routing,
        classifier_agreed=agreed,
        roster="claude_tiers",
        db_path=db_path,
    )


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "runs.db"
        db.init_db(self.db_path)


class TestNetVsGross(_DbCase):
    def test_routing_cost_reduces_the_headline(self):
        for _ in range(10):
            _log(self.db_path, "router", 0.04, routing=0.002)
            _log(self.db_path, "frontier_only", 0.10)
        report = build_report("g", db_path=self.db_path)
        self.assertAlmostEqual(report.cost_reduction_pct, 60.0)
        self.assertAlmostEqual(report.net_cost_reduction_pct, 58.0)
        self.assertLess(report.net_cost_reduction_pct, report.cost_reduction_pct)

    def test_net_equals_gross_when_labels_are_used(self):
        """The v1-compatibility guarantee: with no classifier there is no
        overhead, so adopting the net figure as the headline does not restate
        the published result."""
        for _ in range(10):
            _log(self.db_path, "router", 0.04, routing=0.0)
            _log(self.db_path, "frontier_only", 0.10)
        report = build_report("g", db_path=self.db_path)
        self.assertEqual(report.routing_cost, 0.0)
        self.assertAlmostEqual(report.net_cost_reduction_pct, report.cost_reduction_pct)

    def test_overhead_expressed_as_share_of_savings(self):
        for _ in range(10):
            _log(self.db_path, "router", 0.04, routing=0.002)
            _log(self.db_path, "frontier_only", 0.10)
        report = build_report("g", db_path=self.db_path)
        # $0.02 of routing against $0.60 of gross savings.
        self.assertAlmostEqual(report.routing_overhead_pct_of_savings, 0.02 / 0.60 * 100)

    def test_baseline_is_not_charged_routing_overhead(self):
        """frontier_only does not route, so it has no classifier call to pay
        for. Charging it one would understate the router's true overhead."""
        for _ in range(5):
            _log(self.db_path, "router", 0.04, routing=0.002)
            _log(self.db_path, "frontier_only", 0.10, routing=0.999)  # must be ignored
        report = build_report("g", db_path=self.db_path)
        self.assertAlmostEqual(report.routing_cost, 0.01)

    def test_hypothesis_judged_on_net_not_gross(self):
        """A router whose overhead eats its margin must FAIL, even though the
        gross figure clears the bar. This is the test that makes the honesty
        structural rather than cosmetic."""
        for _ in range(10):
            # Gross: 58% reduction (clears 40%). Routing overhead: $0.20,
            # dragging net down to 38% — below target.
            _log(self.db_path, "router", 0.042, routing=0.02)
            _log(self.db_path, "frontier_only", 0.10)
        report = build_report("g", db_path=self.db_path)
        self.assertGreater(report.cost_reduction_pct, 40.0)
        self.assertLess(report.net_cost_reduction_pct, 40.0)
        self.assertFalse(report.hypothesis_passed)

    def test_projection_uses_net_per_task_cost(self):
        """At 1M requests/month the overhead is 1M classifier calls. Excluding
        it overstates the saving by the term that scales fastest."""
        for _ in range(10):
            _log(self.db_path, "router", 0.04, routing=0.002)
            _log(self.db_path, "frontier_only", 0.10)
        report = build_report("g", db_path=self.db_path)
        self.assertAlmostEqual(report.cost_per_task_router, 0.042)


class TestClassifierAgreement(_DbCase):
    def test_agreement_is_percentage_of_matches(self):
        for i in range(10):
            _log(self.db_path, "router", 0.04, routing=0.002, agreed=(i < 7))
            _log(self.db_path, "frontier_only", 0.10)
        self.assertAlmostEqual(
            build_report("g", db_path=self.db_path).classifier_agreement_pct, 70.0
        )

    def test_agreement_is_none_when_nothing_was_predicted(self):
        for _ in range(5):
            _log(self.db_path, "router", 0.04, agreed=None)
            _log(self.db_path, "frontier_only", 0.10)
        self.assertIsNone(build_report("g", db_path=self.db_path).classifier_agreement_pct)


class TestReportRendering(_DbCase):
    def test_overhead_section_renders_when_routing_cost_exists(self):
        for _ in range(5):
            _log(self.db_path, "router", 0.04, routing=0.002, agreed=True)
            _log(self.db_path, "frontier_only", 0.10)
        md = format_report_markdown(build_report("g", db_path=self.db_path))
        self.assertIn("## Routing overhead", md)
        self.assertIn("Routing overhead as % of savings", md)
        self.assertIn("not an accuracy", md)  # the agreement caveat must ship with the number

    def test_label_run_says_routing_was_free_and_why_that_is_unrealistic(self):
        for _ in range(5):
            _log(self.db_path, "router", 0.04, routing=0.0)
            _log(self.db_path, "frontier_only", 0.10)
        md = format_report_markdown(build_report("g", db_path=self.db_path))
        self.assertIn("## Routing overhead", md)
        self.assertIn("--classify", md)

    def test_headline_is_labelled_net(self):
        for _ in range(5):
            _log(self.db_path, "router", 0.04, routing=0.002)
            _log(self.db_path, "frontier_only", 0.10)
        md = format_report_markdown(build_report("g", db_path=self.db_path))
        self.assertIn("net of routing overhead", md)


if __name__ == "__main__":
    unittest.main()
