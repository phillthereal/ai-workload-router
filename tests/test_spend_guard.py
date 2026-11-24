import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import spend_guard  # noqa: E402
from router.spend_guard import DEFAULT_SPEND_STORE, SpendGuard, spend_store_from_env  # noqa: E402


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


class TestSpendStore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sub" / "spend.db"  # parent made on demand

    def test_total_survives_restart(self):
        a = SpendGuard(daily_budget_usd=0.50, store=self.path)
        a.add_spend(0.30)
        del a
        b = SpendGuard(daily_budget_usd=0.50, store=self.path)
        self.assertAlmostEqual(b.spent_today(), 0.30, delta=1e-9)
        self.assertFalse(b.over_budget())
        self.assertTrue(b.over_budget(extra_usd=0.20))
        self.assertTrue(SpendGuard(daily_budget_usd=0.30, store=self.path).over_budget())

    def test_two_guards_share_one_file(self):
        a = SpendGuard(daily_budget_usd=0.10, store=self.path)
        b = SpendGuard(daily_budget_usd=0.10, store=self.path)
        a.add_spend(0.04)
        self.assertAlmostEqual(b.spent_today(), 0.04, delta=1e-9)
        b.add_spend(0.06)
        self.assertAlmostEqual(a.spent_today(), 0.10, delta=1e-9)
        self.assertTrue(a.over_budget())

    def test_day_rollover_reads_zero(self):
        g = SpendGuard(store=self.path)
        g.add_spend(0.25)
        with mock.patch.object(SpendGuard, "_today", staticmethod(lambda: "2099-01-01")):
            self.assertEqual(g.spent_today(), 0.0)
            self.assertFalse(g.over_budget())
            g.add_spend(0.01)
            self.assertAlmostEqual(g.spent_today(), 0.01, delta=1e-9)
        self.assertAlmostEqual(g.spent_today(), 0.25, delta=1e-9)  # yesterday's row kept apart

    def test_concurrent_adds_sum_exactly(self):
        guards = [SpendGuard(store=self.path) for _ in range(4)]
        threads = [threading.Thread(target=guards[i % 4].add_spend, args=(0.01,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertAlmostEqual(SpendGuard(store=self.path).spent_today(), 0.20, delta=1e-9)

    def test_store_from_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWR_SPEND_STORE", None)
            self.assertEqual(spend_store_from_env(), DEFAULT_SPEND_STORE)
        with mock.patch.dict(os.environ, {"AWR_SPEND_STORE": ""}):
            self.assertIsNone(spend_store_from_env())
        with mock.patch.dict(os.environ, {"AWR_SPEND_STORE": str(self.path)}):
            self.assertEqual(spend_store_from_env(), self.path)

    def test_default_store_is_data_spend_db(self):
        self.assertEqual(DEFAULT_SPEND_STORE.name, "spend.db")
        self.assertEqual(DEFAULT_SPEND_STORE.parent.name, "data")

    def test_locked_store_keeps_spend_pending_and_flushes_later(self):
        g = SpendGuard(daily_budget_usd=0.05, store=self.path)
        g.add_spend(0.01)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        with mock.patch.object(spend_guard, "_TIMEOUT_S", 0.1), \
                self.assertLogs("router.spend_guard", "WARNING"):
            blocker.execute("BEGIN EXCLUSIVE")
            g.add_spend(0.02)  # must not raise
            g.add_spend(0.03)
            self.assertAlmostEqual(g.spent_today(), 0.05, delta=1e-9)  # pending only, file unreadable
            self.assertTrue(g.over_budget())  # pending >= budget, and fails closed anyway
            blocker.execute("ROLLBACK")
        g.add_spend(0.04)  # flushes 0.02 + 0.03 pending together with this amount
        self.assertAlmostEqual(SpendGuard(store=self.path).spent_today(), 0.10, delta=1e-9)
        self.assertAlmostEqual(g.spent_today(), 0.10, delta=1e-9)  # pending cleared, not double counted

    def test_unreadable_store_fails_closed(self):
        g = SpendGuard(daily_budget_usd=100.0, store=self.path)
        with mock.patch.object(SpendGuard, "_connect", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs("router.spend_guard", "WARNING"):
            self.assertTrue(g.over_budget())
            self.assertEqual(g.spent_today(), 0.0)
            g.add_spend(0.5)
            self.assertAlmostEqual(g.spent_today(), 0.5, delta=1e-9)

    def test_empty_store_is_in_memory(self):
        g = SpendGuard(store="")
        self.assertIsNone(g._store)
        g.add_spend(0.2)
        self.assertAlmostEqual(g.spent_today(), 0.2, delta=1e-9)

    def test_directory_store_is_refused(self):
        self.path.parent.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "AWR_SPEND_STORE"):
            SpendGuard(store=self.path.parent)


if __name__ == "__main__":
    unittest.main()
