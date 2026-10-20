"""Tests for router.jury (offline: scripted adapters, no network)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import api  # noqa: E402
from router.adapters.base import Adapter, Response  # noqa: E402
from router.api import JuryPolicy, Router  # noqa: E402
from router.config import get_model  # noqa: E402
from router.jury import DEFAULT_JURORS as J  # noqa: E402

FRONT = "claude-opus-4-8"
LONG = "Because of the trap in the wording, the final value is " + "x" * 60


def factory_for(answers, check="1"):
    """answers: model -> text; a prompt starting 'Two answers' gets `check`."""
    calls = []

    class A(Adapter):
        def __init__(self, model):
            self.model = model

        def complete(self, prompt, effort=None, max_tokens=None):
            is_check = prompt.startswith("Two answers")
            calls.append((self.model, "check" if is_check else "answer"))
            text = check if is_check else answers.get(self.model, "front")
            return Response(text=text, input_tokens=100, output_tokens=10,
                            latency_ms=1.0, model=self.model, simulated=False)

    def f(model, allow_mock_fallback=True):
        return A(model)
    f.calls = calls
    return f


def run(answers, mode="unanimous", check="1"):
    f = factory_for(answers, check)
    with mock.patch.object(api, "get_adapter", f):
        r = Router(JuryPolicy(mode=mode), "claude_tiers", allow_mock_fallback=True)
        return r.complete("What is the capital of France?"), f


def cost(model):
    return get_model(model).cost_for_tokens(100, 10)


class TestJury(unittest.TestCase):
    def test_unanimous_agree_no_escalation_and_accounting(self):
        # "Paris." / "paris" / "PARIS" also covers scoring-normalization reuse.
        res, f = run({J[0]: "Paris.", J[1]: "paris", J[2]: "PARIS"})
        self.assertNotIn(FRONT, [m for m, _ in f.calls])
        self.assertEqual(res.model, J[0])
        self.assertAlmostEqual(res.answer_cost_usd, cost(J[0]))
        self.assertAlmostEqual(res.routing_cost_usd, cost(J[1]) + cost(J[2]))
        self.assertTrue(any("consensus" in t for t in res.trace))

    def test_one_dissent_escalates_unanimous(self):
        res, _ = run({J[0]: "Paris", J[1]: "Paris", J[2]: "Lyon"})
        self.assertEqual(res.model, FRONT)
        self.assertAlmostEqual(res.answer_cost_usd, cost(FRONT))
        self.assertAlmostEqual(res.routing_cost_usd, sum(cost(m) for m in J))

    def test_one_dissent_majority_returns_without_escalating(self):
        res, f = run({J[0]: "Paris", J[1]: "Lyon", J[2]: "Paris"}, mode="majority")
        self.assertEqual(res.model, J[0])
        self.assertNotIn(FRONT, [m for m, _ in f.calls])

    def test_majority_all_differ_escalates(self):
        res, _ = run({J[0]: "a", J[1]: "b", J[2]: "c"}, mode="majority")
        self.assertEqual(res.model, FRONT)

    def test_long_answers_use_checker_from_third_vendor(self):
        res, f = run({m: LONG for m in J}, check="1")
        checks = [m for m, kind in f.calls if kind == "check"]
        self.assertEqual(checks, [J[2], J[1]])  # pair (0,1)->J2, pair (0,2)->J1
        self.assertNotEqual(res.model, FRONT)
        self.assertGreater(res.routing_cost_usd, cost(J[1]) + cost(J[2]))

    def test_unparseable_check_is_disagree(self):
        res, _ = run({m: LONG for m in J}, check="Sure, probably yes!")
        self.assertEqual(res.model, FRONT)

    def test_check_zero_is_disagree(self):
        res, _ = run({m: LONG for m in J}, check="0")
        self.assertEqual(res.model, FRONT)

    def test_default_jurors_are_three_distinct_vendors(self):
        vendors = {get_model(m).provider for m in JuryPolicy().jurors}
        self.assertEqual(len(vendors), 3)

    def test_bad_mode_and_tags(self):
        with self.assertRaises(ValueError):
            JuryPolicy(mode="x")
        self.assertEqual(JuryPolicy(mode="majority").report_tags(), ["jury-majority"])


if __name__ == "__main__":
    unittest.main()
