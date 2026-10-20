"""Projection math with synthetic numbers (no real data)."""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWR_FORCE_MOCK", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import assess_real_traffic as a  # noqa: E402


class TestProjection(unittest.TestCase):
    def test_cost_haiku(self):  # 1M in at $1 plus 1M out at $5
        self.assertAlmostEqual(a.cost(a.BASE, 1_000_000, 1_000_000), 6.0)

    def test_total_and_cascade(self):
        calls = [dict(pt=1_000_000, ct=0, cat="IGNORE"), dict(pt=1_000_000, ct=0, cat="ACTION_NEEDED")]
        self.assertAlmostEqual(a.total(calls, lambda c: a.BASE), 2.0)
        self.assertAlmostEqual(a.total(calls, lambda c: a.MINI), 0.30)
        # mini for both + Haiku re-run of the protected one only
        self.assertAlmostEqual(a.cascade_total(calls), 0.30 + 1.0)

    def test_roster_frontier_is_production_model(self):
        self.assertEqual(a.ROSTERS["assess"].frontier, a.BASE)


if __name__ == "__main__":
    unittest.main()
