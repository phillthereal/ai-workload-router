"""Tests for router.api: Router.complete per policy, failover, Result fields."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import api  # noqa: E402
from router.adapters.base import Adapter, Response  # noqa: E402
from router.api import (  # noqa: E402
    CascadePolicy, FrontierOnlyPolicy, LearnedPolicy, ProviderError, Result, Router,
    RulesPolicy,
)
from router.config import get_roster  # noqa: E402

TASK = {"id": "t1", "task_type": "classification", "difficulty": "easy",
        "prompt": "Classify the sentiment of: I love this."}


class FakeAdapter(Adapter):
    def __init__(self, model, fail):
        self.model, self.fail = model, fail

    def complete(self, prompt, effort=None, max_tokens=None):
        if self.fail == "raise":
            raise ConnectionError("boom")
        ok = self.fail != "unsuccessful"
        return Response(text="ok" if ok else "", input_tokens=10, output_tokens=5,
                        latency_ms=1.0, model=self.model, simulated=False, success=ok)


def fake_factory(failing, mode="raise"):
    calls = []

    def factory(model, allow_mock_fallback=True):
        calls.append((model, allow_mock_fallback))
        return FakeAdapter(model, mode if model in failing else None)
    factory.calls = calls
    return factory


class TestPolicies(unittest.TestCase):
    def check(self, policy, expect_model=None, **kw):
        r = Router(policy, "cross_vendor", allow_mock_fallback=True).complete(
            TASK["prompt"], task=TASK)
        self.assertIsInstance(r, Result)
        self.assertTrue(r.text and r.simulated and r.trace)
        self.assertGreater(r.answer_cost_usd, 0)
        self.assertEqual(r.response.model, r.model)
        if expect_model:
            self.assertEqual(r.model, expect_model)
        return r

    def test_frontier_only(self):
        r = self.check(FrontierOnlyPolicy(), get_roster().frontier)
        self.assertEqual(r.routing_cost_usd, 0)

    def test_rules_labels(self):
        r = self.check(RulesPolicy(use_labels=True), get_roster().budget)
        self.assertTrue(r.classifier_agreed)

    def test_rules_classifier(self):
        r = self.check(RulesPolicy(use_labels=False))
        self.assertIsNotNone(r.classifier_agreed)

    def test_learned(self):
        p = LearnedPolicy(use_labels=True)
        p.before_run_group("20000101T000000-x")
        r = self.check(p)
        self.assertIn("chosen_tier", r.learned_evidence)

    def test_cascade(self):
        r = self.check(CascadePolicy(escalate_threshold=0.7))
        self.assertIsInstance(r.verifier_scores, list)

    def test_plain_prompt_without_task(self):
        r = Router(RulesPolicy(use_labels=True)).complete("What is 2+2? Explain.")
        self.assertTrue(r.text)  # no labels -> falls back to classifier

    def test_report_tags(self):
        self.assertEqual(LearnedPolicy(use_labels=False).report_tags(), ["classified", "learned"])
        self.assertEqual(CascadePolicy("m").report_tags(), ["verifier-m"])
        self.assertEqual(RulesPolicy(True).report_tags(), [])


class TestFailover(unittest.TestCase):
    def run_router(self, failing, mode="raise", policy=None):
        f = fake_factory(failing, mode)
        with mock.patch.object(api, "get_adapter", f):
            r = Router(policy or FrontierOnlyPolicy(), "claude_tiers")
            return r, f, r.complete("hi", task=TASK)

    def test_budget_failure_climbs_to_mid(self):
        ro = get_roster("claude_tiers")
        _, f, res = self.run_router({ro.budget}, policy=RulesPolicy(True))
        self.assertEqual(res.model, ro.mid)
        self.assertTrue(any("provider error on " + ro.budget in t for t in res.trace))
        self.assertTrue(any("failover" in t for t in res.trace))
        self.assertTrue(all(flag is False for _, flag in f.calls))

    def test_unsuccessful_response_also_fails_over(self):
        ro = get_roster("claude_tiers")
        _, _, res = self.run_router({ro.budget}, "unsuccessful", RulesPolicy(True))
        self.assertEqual(res.model, ro.mid)

    def test_frontier_failure_raises(self):
        ro = get_roster("claude_tiers")
        with self.assertRaises(ProviderError):
            self.run_router({ro.frontier})

    def test_mock_fallback_mode_does_not_fail_over(self):
        f = fake_factory(set())
        with mock.patch.object(api, "get_adapter", f):
            Router(FrontierOnlyPolicy(), "claude_tiers", allow_mock_fallback=True).complete("hi")
        self.assertEqual([flag for _, flag in f.calls], [True])


if __name__ == "__main__":
    unittest.main()
