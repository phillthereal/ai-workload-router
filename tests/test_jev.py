"""Jev router pieces, offline: no network (client is scripted or mocked)."""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.adapters import typesafe  # noqa: E402
from router.adapters.base import Adapter, Response  # noqa: E402
from router.adapters.typesafe import Decision, JevClient  # noqa: E402
from router.api import Router  # noqa: E402
from router.jev import JevClassifierPolicy, JevTierPolicy  # noqa: E402
from router.rulebook import Rulebook  # noqa: E402

BASE = Path(__file__).resolve().parents[1] / "data" / "rulebooks" / "email_base.json"
PROT = ("ACTION_NEEDED",)


class Scripted:
    def __init__(self, choice, p, ok=True):
        self.d = Decision(choice, p, {choice: p, "OTHER": 1 - p}, 700, 1.0, success=ok, error=None if ok else "HTTP 500")
        self.seen = []

    def decide(self, state, instructions, criteria):
        self.seen.append((state, instructions, criteria))
        return self.d


def fallback_factory(text="FYI"):
    calls = []

    class A(Adapter):
        def __init__(self, m):
            self.m = m

        def complete(self, prompt, effort=None, max_tokens=None):
            calls.append(prompt)
            return Response(text, 100, 5, 1.0, self.m, False)

    def f(model, allow_mock_fallback=True):
        return A(model)
    f.calls = calls
    return f


def run(policy, state="hello", text="FYI"):
    f = fallback_factory(text)
    with mock.patch("router.api.get_adapter", f):
        return Router(policy).complete(state), f


class RulebookTests(unittest.TestCase):
    def test_round_trip_and_hash(self):
        rb = Rulebook.load(BASE)
        with tempfile.TemporaryDirectory() as t:
            rb.save(Path(t) / "r.json")
            self.assertEqual(Rulebook.load(Path(t) / "r.json"), rb)
        self.assertEqual(len(rb.criteria), 7)
        e = rb.with_note("Order receipts are FYI.")
        self.assertNotEqual(e.content_hash(), rb.content_hash())
        self.assertEqual(e.version, rb.version + 1)
        self.assertIn("Order receipts", e.render_instructions())
        self.assertNotEqual(rb.with_criterion("FYI", "x").content_hash(), rb.content_hash())
        self.assertEqual(rb.content_hash(), Rulebook(99, rb.instructions, rb.criteria).content_hash())

    def test_parse_choice(self):
        rb = Rulebook.load(BASE)
        self.assertEqual(rb.parse_choice("It is JOB_ALERT."), "JOB_ALERT")
        self.assertIsNone(rb.parse_choice("no idea"))


class ClientTests(unittest.TestCase):
    def test_body_shape(self):
        b = typesafe.build_body("S", "I", {"A": "a", "B": "b"})
        self.assertEqual(b, {"state": "S", "model": "jev-latest", "questions": {"answer": {
            "type": "choice", "instructions": "I", "criteria": {"A": "a", "B": "b"}}}})

    def test_request_and_parse(self):
        out = {"answers": {"answer": {"choice": "A", "confidence": 90, "probabilities": {"A": .9, "B": .1}}},
               "usage": {"input_tokens": 1000}}
        seen = {}

        def fake(req, timeout):
            seen["auth"] = req.get_header("Authorization")
            seen["body"] = json.loads(req.data)
            return io.BytesIO(json.dumps(out).encode())
        with mock.patch.dict("os.environ", {"AWR_FORCE_MOCK": ""}), mock.patch("urllib.request.urlopen", fake):
            d = JevClient("k").decide("S", "I", {"A": "a", "B": "b"})
        self.assertEqual((d.choice, d.top_prob, d.input_tokens), ("A", .9, 1000))
        self.assertAlmostEqual(d.cost_usd, 0.000042)
        self.assertEqual(seen["auth"], "Bearer k")
        self.assertEqual(seen["body"]["questions"]["answer"]["criteria"], {"A": "a", "B": "b"})

    def test_safe_errors(self):
        body = io.BytesIO(b'{"error": {"type": "rate_limit", "message": "secret email text"}}')
        e = urllib.error.HTTPError("u", 429, "m", {}, body)
        with mock.patch.dict("os.environ", {"AWR_FORCE_MOCK": ""}), mock.patch("urllib.request.urlopen", side_effect=e):
            d = JevClient("k").decide("S", "I", {"A": "a"})
        self.assertFalse(d.success)
        self.assertEqual(d.error, "HTTP 429 rate_limit")
        self.assertNotIn("secret", repr(d))
        with mock.patch.dict("os.environ", {"AWR_FORCE_MOCK": ""}), mock.patch("urllib.request.urlopen", side_effect=OSError("secret")):
            self.assertEqual(JevClient("k").decide("S", "I", {"A": "a"}).error, "OSError")

    def test_mock_deterministic(self):
        c = {k: "d" for k in "ABCD"}
        a, b = typesafe.mock_decision("same", c), typesafe.mock_decision("same", c)
        self.assertEqual(a, b)
        self.assertTrue(a.simulated and 0.5 <= a.top_prob <= 0.99)
        self.assertAlmostEqual(sum(a.probabilities.values()), 1.0)
        with mock.patch.dict("os.environ", {"AWR_FORCE_MOCK": "1"}):
            self.assertEqual(JevClient().decide("same", "i", c), a)


class PolicyTests(unittest.TestCase):
    rb = Rulebook.load(BASE)

    def pol(self, choice, p, **kw):
        c = Scripted(choice, p, **kw)
        return JevClassifierPolicy(self.rb, 0.9, PROT, client=c), c

    def test_handled(self):
        pol, c = self.pol("IGNORE", 0.95)
        r, f = run(pol)
        self.assertEqual((r.choice, r.escalated, r.model, f.calls), ("IGNORE", False, "jev-latest", []))
        self.assertGreater(r.routing_cost_usd, 0)
        self.assertEqual(r.answer_cost_usd, 0.0)
        self.assertEqual(r.jev_probabilities["IGNORE"], 0.95)

    def test_threshold_escalates_and_uses_same_rulebook(self):
        pol, c = self.pol("IGNORE", 0.89)
        r, f = run(pol, "body", text="JOB_ALERT")
        self.assertTrue(r.escalated)
        self.assertEqual(r.choice, "JOB_ALERT")
        self.assertEqual(len(f.calls), 1)
        self.assertIn(self.rb.criteria["FYI"], f.calls[0])
        self.assertEqual(c.seen[0][2], self.rb.criteria)

    def test_protected_escalates_even_if_sure(self):
        pol, _ = self.pol("ACTION_NEEDED", 0.99)
        r, f = run(pol, text="FYI")
        self.assertTrue(r.escalated)
        self.assertEqual(r.choice, "FYI")

    def test_jev_failure_escalates(self):
        pol, _ = self.pol(None, 0.0, ok=False)
        r, f = run(pol, text="IGNORE")
        self.assertTrue(r.escalated)
        self.assertEqual(r.choice, "IGNORE")
        self.assertIn("HTTP 500", r.trace[0])

    def test_mock_mode_end_to_end(self):
        r = Router(JevClassifierPolicy(self.rb, 0.0, ())).complete("some email")
        self.assertTrue(r.simulated)
        self.assertIn(r.choice, self.rb.criteria)

    def test_tier_policy(self):
        r, f = run(JevTierPolicy(client=Scripted("budget", 0.95)))
        self.assertEqual((r.choice, r.escalated), ("budget", False))
        r, f = run(JevTierPolicy(client=Scripted("budget", 0.6)))
        self.assertEqual((r.choice, r.escalated), ("frontier", True))


if __name__ == "__main__":
    unittest.main()
