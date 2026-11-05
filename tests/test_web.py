"""Tests for router.web. Offline: real-mode tests stub every adapter."""

import os
import sys
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

    def test_provider_failure_is_a_502(self):
        self.stub(lambda m, *a, **k: StubAdapter(m, success=False))
        c = TestClient(web.create_app(), base_url="http://localhost")
        self.assertEqual(c.post("/route", json={"prompt": "x"}).status_code, 502)

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


if __name__ == "__main__":
    unittest.main()
