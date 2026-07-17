"""
Tests for router.cascade — escalate-on-failure routing.

The verifier's score is patched in the behavioural tests so accept-cheap and
escalate paths are both exercised deterministically. (Under forced mock the
verifier's fabricated prose carries no number, so it would always parse to 0.0
and always escalate — fine as a fail-safe, useless for testing the accept path.)
The parse logic and the fail-safe direction are tested directly.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import cascade  # noqa: E402
from router.cascade import CascadeResult, _parse_adequacy, run_cascade  # noqa: E402
from router.config import get_roster  # noqa: E402

TASK = {"id": "t", "task_type": "classification", "difficulty": "easy",
        "prompt": "Is this positive or negative? 'I love it.'"}


class TestParseAdequacy(unittest.TestCase):
    def test_parses_plain_scores(self):
        self.assertEqual(_parse_adequacy("0.9"), 0.9)
        self.assertEqual(_parse_adequacy("1.0"), 1.0)
        self.assertEqual(_parse_adequacy("0"), 0.0)

    def test_unparseable_escalates(self):
        """The fail-safe: an unreadable quality gate must fail toward quality
        (score 0.0 -> escalate), never toward cost."""
        self.assertEqual(_parse_adequacy("escalate please"), 0.0)
        self.assertEqual(_parse_adequacy(""), 0.0)
        self.assertEqual(_parse_adequacy(None), 0.0)

    def test_clamps_out_of_range(self):
        # The regex only admits 0-1 tokens, so a bare "5" finds no match and
        # fails safe rather than being read as adequate.
        self.assertEqual(_parse_adequacy("5"), 0.0)


class TestAcceptCheapAnswer(unittest.TestCase):
    @patch.object(cascade, "verify_adequacy", return_value=(0.9, 0.0001))
    def test_high_verifier_score_stops_at_budget(self, _mock):
        result = run_cascade(TASK, roster_name="claude_tiers", escalate_threshold=0.7)
        self.assertFalse(result.escalated)
        self.assertEqual(result.tiers_used, ["claude-haiku-4-5"])
        self.assertEqual(result.response.model, "claude-haiku-4-5")

    @patch.object(cascade, "verify_adequacy", return_value=(0.9, 0.0001))
    def test_overhead_is_only_the_verifier_when_not_escalated(self, _mock):
        result = run_cascade(TASK, roster_name="claude_tiers", escalate_threshold=0.7)
        # Overhead is the single verifier check; no discarded attempt.
        self.assertAlmostEqual(result.overhead_cost, 0.0001)
        self.assertGreater(result.answer_cost, 0.0)


class TestEscalateOnFailure(unittest.TestCase):
    @patch.object(cascade, "verify_adequacy", return_value=(0.3, 0.0001))
    def test_low_verifier_score_climbs_to_frontier(self, _mock):
        result = run_cascade(TASK, roster_name="claude_tiers", escalate_threshold=0.7)
        self.assertTrue(result.escalated)
        self.assertEqual(result.tiers_used, ["claude-haiku-4-5", "claude-opus-4-8"])
        self.assertEqual(result.response.model, "claude-opus-4-8")

    @patch.object(cascade, "verify_adequacy", return_value=(0.3, 0.0001))
    def test_discarded_attempt_becomes_overhead_not_answer(self, _mock):
        result = run_cascade(TASK, roster_name="claude_tiers", escalate_threshold=0.7)
        # answer_cost is the frontier answer; overhead carries the verifier
        # check AND the sunk cost of the discarded budget attempt.
        self.assertGreater(result.overhead_cost, 0.0001)
        self.assertEqual(result.response.model, "claude-opus-4-8")

    @patch.object(cascade, "verify_adequacy", return_value=(0.3, 0.0001))
    def test_frontier_answer_is_never_itself_verified(self, _mock):
        """The top of the ladder is the last resort — there is nothing to
        escalate to, so it is returned without a (wasted) verifier call."""
        result = run_cascade(TASK, roster_name="claude_tiers", escalate_threshold=0.7)
        self.assertEqual(len(result.verifier_scores), 1)  # only the budget attempt was checked


class TestThreshold(unittest.TestCase):
    @patch.object(cascade, "verify_adequacy", return_value=(0.7, 0.0001))
    def test_threshold_is_inclusive(self, _mock):
        """A score exactly at the threshold accepts (>=), not escalates."""
        result = run_cascade(TASK, roster_name="claude_tiers", escalate_threshold=0.7)
        self.assertFalse(result.escalated)


class TestResultProperties(unittest.TestCase):
    def test_total_cost_is_answer_plus_overhead(self):
        r = CascadeResult(response=None, answer_cost=0.05, overhead_cost=0.01)
        self.assertAlmostEqual(r.total_cost, 0.06)

    def test_escalated_reflects_ladder_depth(self):
        self.assertFalse(CascadeResult(None, 0.0, 0.0, tiers_used=["a"]).escalated)
        self.assertTrue(CascadeResult(None, 0.0, 0.0, tiers_used=["a", "b"]).escalated)


class TestDefaultLadder(unittest.TestCase):
    @patch.object(cascade, "verify_adequacy", return_value=(0.3, 0.0001))
    def test_default_ladder_is_budget_then_frontier(self, _mock):
        """Canonical two-tier: cheapest, then strongest — skipping mid so the
        mid verifier never vets its own answer."""
        roster = get_roster("claude_tiers")
        result = run_cascade(TASK, roster_name="claude_tiers")
        self.assertEqual(result.tiers_used, [roster.budget, roster.frontier])


if __name__ == "__main__":
    unittest.main()
