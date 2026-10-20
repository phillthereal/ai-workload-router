"""Tests for router.governors + the latency fix (offline: scripted adapters)."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("AWR_FORCE_MOCK", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from router import api, db  # noqa: E402
from router.adapters.base import Adapter, Response  # noqa: E402
from router.api import BudgetPolicy, FrontierOnlyPolicy, JuryPolicy, Router, ShadowPolicy  # noqa: E402
from router.config import get_roster  # noqa: E402
from router.governors import RegretWindow  # noqa: E402
from router.report import build_report  # noqa: E402

R = get_roster("claude_tiers")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def factory(judge="1"):
    calls = []

    class A(Adapter):
        def __init__(self, m):
            self.m = m

        def complete(self, prompt, effort=None, max_tokens=None):
            calls.append(self.m)
            text = judge if prompt.startswith("Two answers were given") else "ans-" + self.m
            return Response(text=text, input_tokens=100, output_tokens=10,
                            latency_ms=5.0, model=self.m, simulated=False)

    def f(model, allow_mock_fallback=True):
        return A(model)
    f.calls = calls
    return f


class FixedTier:
    """Inner policy: always answers on `model`."""
    name = "fixed"

    def __init__(self, model):
        self.model = model

    def report_tags(self):
        return []

    def before_run_group(self, g):
        pass

    def run(self, prompt, task, ctx):
        return api._answer(prompt, self.model, None, ctx, trace=["inner"])


def go(policy, task_type="classification", f=None):
    f = f or factory()
    with mock.patch.object(api, "get_adapter", f):
        r = Router(policy, "claude_tiers", allow_mock_fallback=True)
        return r.complete("q?", task={"task_type": task_type}), f


def budget(spent, day=0, **kw):
    return BudgetPolicy(FixedTier(R.frontier), monthly_budget_usd=30.0, period_start=T0,
                        spent_usd=spent, now=lambda: T0 + timedelta(days=day), **kw)


class TestLatencyFix(unittest.TestCase):
    def test_jury_latency_is_sum_of_calls(self):
        res, _ = go(JuryPolicy(), "x")
        self.assertEqual(res.latency_ms, 20.0)  # 3 jurors disagree + frontier, 5ms each

    def test_report_latency_row_follows_experimental_strategy(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False); fd.close()
        p = Path(fd.name)
        try:
            db.init_db(p)
            for strat in ("jury", "frontier_only"):
                db.log_run(run_group="g", strategy=strat, task_id="t", task_type="c",
                           difficulty="easy", model="claude-opus-4-8", input_tokens=1,
                           output_tokens=1, cost_usd=.1, latency_ms=42.0, quality_score=1,
                           success=True, db_path=p)
            rep = build_report("g", experimental_strategy="jury", db_path=p)
            self.assertEqual(rep.latency_by_strategy["jury"]["mean_ms"], 42.0)
        finally:
            p.unlink(missing_ok=True)


class TestBudget(unittest.TestCase):
    def test_under_pace_passthrough(self):
        res, _ = go(budget(spent=0.5))  # day-0 pace = 1.0
        self.assertEqual(res.model, R.frontier)
        self.assertTrue(any("under pace" in t for t in res.trace))

    def test_over_pace_one_tier_cheaper(self):
        res, _ = go(budget(spent=2.0))
        self.assertEqual(res.model, R.mid)
        self.assertTrue(any("over pace" in t and "one tier cheaper" in t for t in res.trace))

    def test_protected_never_downgraded_even_over_cap(self):
        for spent in (2.0, 99.0):
            res, _ = go(budget(spent=spent), task_type="reasoning")
            self.assertEqual(res.model, R.frontier)
        self.assertTrue(any("OVER CAP" in t for t in res.trace))

    def test_unlabelled_is_protected(self):
        res, _ = go(budget(spent=99.0), task_type=None)
        self.assertEqual(res.model, R.frontier)

    def test_cap_goes_cheapest(self):
        res, _ = go(budget(spent=30.0))
        self.assertEqual(res.model, R.budget)
        self.assertTrue(any("OVER CAP" in t for t in res.trace))

    def test_spend_accumulates_and_counts(self):
        pol = budget(spent=2.0)
        go(pol)
        self.assertGreater(pol.spent, 2.0)
        self.assertEqual(pol.pressured["pace"], 1)

    def test_pace_grows_with_days(self):
        res, _ = go(budget(spent=2.0, day=5))  # pace = 6.0 -> pass-through
        self.assertEqual(res.model, R.frontier)

    def test_seed_from_log(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False); fd.close()
        p = Path(fd.name)
        try:
            db.init_db(p)
            for strat in ("live:x", "other"):
                db.log_run(run_group="g", strategy=strat, task_id="t", task_type="c",
                           difficulty="easy", model="m", input_tokens=1, output_tokens=1,
                           cost_usd=.25, latency_ms=1, quality_score=1, success=True,
                           db_path=p)
            pol = BudgetPolicy(FixedTier(R.frontier), 30.0, T0)
            self.assertAlmostEqual(pol.seed_from_log(p), 0.25)
        finally:
            p.unlink(missing_ok=True)


class TestShadow(unittest.TestCase):
    def test_p0_never_p1_always(self):
        for p, want in ((0.0, 0), (1.0, 5)):
            pol = ShadowPolicy(FixedTier(R.budget), p=p, seed=1)
            for _ in range(5):
                go(pol)
            self.assertEqual(pol.shadows, want)

    def test_answer_unchanged_cost_added_latency_excludes_shadow(self):
        plain, _ = go(FixedTier(R.budget))
        res, f = go(ShadowPolicy(FixedTier(R.budget), p=1.0))
        self.assertEqual(res.text, plain.text)
        self.assertEqual(res.model, R.budget)
        self.assertGreater(res.routing_cost_usd, 0)
        self.assertEqual(res.latency_ms, plain.latency_ms)
        self.assertIn(R.frontier, f.calls)
        self.assertTrue(any(t.startswith("shadow #1") for t in res.trace))

    def test_frontier_routed_is_not_shadowed(self):
        pol = ShadowPolicy(FixedTier(R.frontier), p=1.0)
        go(pol)
        self.assertEqual(pol.shadows, 0)

    def test_alarm_on_regret_not_on_clean(self):
        clean = ShadowPolicy(FixedTier(R.budget), p=1.0, min_samples=5)
        bad = ShadowPolicy(FixedTier(R.budget), p=1.0, min_samples=5)
        for _ in range(6):
            rc, _ = go(clean, f=factory("1"))
            rb, _ = go(bad, f=factory("0"))
        self.assertFalse(any("SHADOW ALARM" in t for t in rc.trace))
        self.assertTrue(any("SHADOW ALARM" in t for t in rb.trace))

    def test_regret_window_rolls(self):
        w = RegretWindow(window=4, threshold=0.4, min_samples=4)
        for r in (1, 1, 1, 1):
            w.record(r)
        self.assertTrue(w.alarm)
        for r in (0, 0, 0):
            w.record(r)
        self.assertFalse(w.alarm)


if __name__ == "__main__":
    unittest.main()
