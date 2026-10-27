import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.spend_guard import SpendGuard  # noqa: E402


class TestSpendGuard(unittest.TestCase):
    def test_budget_cap(self):
        g = SpendGuard(daily_budget_usd=0.10)
        self.assertFalse(g.over_budget())
        g.add_spend(0.06)
        self.assertFalse(g.over_budget())
        self.assertTrue(g.over_budget(extra_usd=0.04))  # worst case reaches the cap
        g.add_spend(0.05)
        self.assertTrue(g.over_budget())

    def test_rate_limit(self):
        g = SpendGuard(rate_limit_per_min=2)
        self.assertEqual([g.rate_ok() for _ in range(3)], [True, True, False])

    def test_frontier_off_by_default(self):
        self.assertFalse(SpendGuard().allow_frontier)


if __name__ == "__main__":
    unittest.main()
