"""Tests for router.web. Offline: real-mode tests stub every adapter."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from fastapi.testclient import TestClient

    from router import web
except ImportError:  # the CI test job runs with nothing installed
    web = None

from router.adapters.base import Adapter, Response  # noqa: E402
from router.spend_guard import SpendGuard  # noqa: E402


class StubAdapter(Adapter):
    """Answers everything with a fixed text, as a real (non-simulated) model."""

    def __init__(self, model, text="0.9", success=True):
        self.model, self.text, self.success = model, text, success

    def complete(self, prompt, effort=None, max_tokens=None):
        return Response(self.text, 1000, 1000, 1.0, self.model, False, success=self.success)



@unittest.skipIf(web is None, "fastapi not installed")
class DemoMode(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AWR_WEB_ALLOW_PAID", None)
        os.environ.pop("AWR_FORCE_MOCK", None)
        self.c = TestClient(web.create_app(), base_url="http://localhost")

    def test_route_is_simulated_and_labelled_by_default(self):
        r = self.c.post("/route", json={"prompt": "What is 2+2?"})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertTrue(b["simulated"])
        self.assertEqual(b["quality_source"], "simulated")
        self.assertTrue(0.0 < b["quality_score"] <= 1.0)
        self.assertEqual(b["roster"], "cross_vendor")
        self.assertIs(b["degraded"], False)
        self.assertGreater(b["cost_usd"], 0)
        self.assertEqual(os.environ["AWR_FORCE_MOCK"], "1")

    def test_ui_is_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Route task", r.text)

    def test_bad_input_is_refused(self):
        self.assertEqual(self.c.post("/route", json={"prompt": ""}).status_code, 422)
        self.assertEqual(self.c.post("/route", json={"prompt": "x" * 32_001}).status_code, 422)
        self.assertEqual(self.c.post("/route", json={"prompt": "x", "roster": "nope"}).status_code, 400)

    def test_browser_attacks_are_refused(self):
        # DNS rebinding: a foreign Host header.
        self.assertEqual(
            self.c.post("/route", json={"prompt": "x"}, headers={"Host": "evil.test"}).status_code, 400)
        # Cross-site form post: a non-JSON content type skips the CORS preflight.
        r = self.c.post("/route", content='{"prompt": "x"}', headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 422)


@unittest.skipIf(web is None, "fastapi not installed")
class PaidMode(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"AWR_WEB_ALLOW_PAID": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AWR_FORCE_MOCK", None)

    def stub(self, factory):
        for target in ("router.api.get_adapter", "router.adapters.get_adapter", "router.web.get_adapter"):
            p = mock.patch(target, factory)
            p.start()
            self.addCleanup(p.stop)

    def test_real_answer_gets_a_verifier_score_and_is_charged(self):
        self.stub(lambda m, *a, **k: StubAdapter(m))
        os.environ["AWR_WEB_DAILY_BUDGET_USD"] = "100"
        c = TestClient(web.create_app(), base_url="http://localhost")
        b = c.post("/route", json={"prompt": "What is 2+2?"}).json()
        self.assertFalse(b["simulated"])
        self.assertEqual(b["quality_source"], "verifier")
        self.assertEqual(b["quality_score"], 0.9)
        self.assertGreater(b["routing_cost_usd"], 0)  # classifier + verifier
        self.assertAlmostEqual(b["cost_usd"], b["answer_cost_usd"] + b["routing_cost_usd"])

    def test_spend_stops_at_the_daily_budget(self):
        self.stub(lambda m, *a, **k: StubAdapter(m))
        os.environ["AWR_WEB_DAILY_BUDGET_USD"] = "0.000001"
        c = TestClient(web.create_app(), base_url="http://localhost")
        self.assertEqual(c.post("/route", json={"prompt": "x"}).status_code, 200)
        self.assertEqual(c.post("/route", json={"prompt": "x"}).status_code, 429)

    def test_real_spend_lands_in_the_spend_store(self):
        self.stub(lambda m, *a, **k: StubAdapter(m))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "spend.db"
        os.environ["AWR_SPEND_STORE"] = str(store)  # restored by the patch.dict in setUp
        os.environ["AWR_WEB_DAILY_BUDGET_USD"] = "100"
        c = TestClient(web.create_app(), base_url="http://localhost")
        b = c.post("/route", json={"prompt": "What is 2+2?"}).json()
        self.assertFalse(b["simulated"])
        self.assertGreater(b["cost_usd"], 0)
        self.assertAlmostEqual(SpendGuard(store=store).spent_today(), b["cost_usd"], delta=1e-9)

    def test_provider_failure_is_a_502(self):
        self.stub(lambda m, *a, **k: StubAdapter(m, success=False))
        c = TestClient(web.create_app(), base_url="http://localhost")
        self.assertEqual(c.post("/route", json={"prompt": "x"}).status_code, 502)

    def test_degraded_result_is_answered_without_a_quality_score(self):
        self.stub(lambda m, *a, **k: StubAdapter(m))
        orig = web.Router.complete

        def degraded_complete(self, *a, **k):
            res = orig(self, *a, **k)
            res.degraded = True
            return res
        p = mock.patch.object(web.Router, "complete", degraded_complete)
        p.start()
        self.addCleanup(p.stop)
        c = TestClient(web.create_app(), base_url="http://localhost")
        b = c.post("/route", json={"prompt": "x"}).json()
        self.assertEqual(b["answer"], "0.9")
        self.assertTrue(b["degraded"])
        self.assertIsNone(b["quality_score"])
        self.assertEqual(b["quality_source"], "unavailable")

    def test_failed_verifier_keeps_the_answer_without_a_score(self):
        self.stub(lambda m, *a, **k: StubAdapter(m))
        p = mock.patch("router.web.get_adapter", lambda m, *a, **k: StubAdapter(m, success=False))
        p.start()
        self.addCleanup(p.stop)
        c = TestClient(web.create_app(), base_url="http://localhost")
        b = c.post("/route", json={"prompt": "x"}).json()
        self.assertEqual(b["answer"], "0.9")
        self.assertIsNone(b["quality_score"])
        self.assertEqual(b["quality_source"], "unavailable")


@unittest.skipIf(web is None, "fastapi not installed")
class VerifierChoice(unittest.TestCase):
    """_score must not let the answering model grade its own answer."""

    def run_score(self, answered_by, roster=None):
        from types import SimpleNamespace

        result = SimpleNamespace(response=Response("a", 1, 1, 1.0, answered_by, False),
                                 model=answered_by, simulated=False)
        seen = []

        def fake_verify(task, response, model, complete=None):
            seen.append(model)
            return 0.8, 0.001

        with mock.patch("router.web.verify_adequacy", fake_verify):
            if roster is None:
                out = web._score("p", result, "cross_vendor")
            else:
                with mock.patch("router.web.get_roster", lambda name=None: roster):
                    out = web._score("p", result, "cross_vendor")
        return seen[0], out

    def test_mid_answer_is_not_graded_by_the_mid_model(self):
        mid = web.get_roster("cross_vendor").mid
        verifier, (score, source, _) = self.run_score(mid)
        self.assertNotEqual(verifier, mid)
        self.assertEqual(source, "verifier")
        self.assertEqual(score, 0.8)

    def test_budget_answer_is_not_graded_by_the_budget_model(self):
        budget = web.get_roster("cross_vendor").budget
        verifier, (_, source, _) = self.run_score(budget)
        self.assertNotEqual(verifier, budget)
        self.assertEqual(source, "verifier")

    def test_self_check_when_every_candidate_wrote_the_answer(self):
        from types import SimpleNamespace

        from router.api import CONSENSUS_FIRST

        same = SimpleNamespace(budget=CONSENSUS_FIRST, mid=CONSENSUS_FIRST, frontier="f")
        verifier, (_, source, _) = self.run_score(CONSENSUS_FIRST, roster=same)
        self.assertEqual(verifier, CONSENSUS_FIRST)
        self.assertEqual(source, "self-check")

    def test_ui_has_the_self_check_label(self):
        text = web.UI_PATH.read_text(encoding="utf-8")
        self.assertIn("Self-check (the same model graded its own answer)", text)


if __name__ == "__main__":
    unittest.main()
