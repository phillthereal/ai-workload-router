"""
Tests for router.classifier — predicting (task_type, difficulty) from a prompt.

These run under AWR_FORCE_MOCK, so `classify()` exercises the heuristic
fallback rather than a real model call. That is the correct thing to test
offline: the model path's accuracy is an empirical question a live run answers,
but the fallback logic, the cost accounting, and the fail-safe direction are all
testable for free and are where the design decisions live.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.adapters.base import Response  # noqa: E402
from router.classifier import Classification, classify, classify_task, heuristic_classify  # noqa: E402
from router.config import ROSTERS  # noqa: E402
from router.scoring import DIFFICULTIES, TASK_TYPES  # noqa: E402


class _FakeRealAdapter:
    """A stand-in for a real (non-mock) provider adapter: returns a fixed,
    non-simulated, successful Response so classify()'s parsing branch runs
    instead of falling back to heuristic_classify. Used to exercise the
    output-parsing fail-safes, which AWR_FORCE_MOCK's simulated path never
    reaches (see classify()'s `if response.simulated or not response.success`
    short-circuit)."""

    def __init__(self, text):
        self._text = text

    def complete(self, prompt, max_tokens=None):
        return Response(
            text=self._text, input_tokens=20, output_tokens=5, latency_ms=1.0,
            model="claude-haiku-4-5", simulated=False, success=True,
        )


class TestHeuristicClassify(unittest.TestCase):
    def test_returns_valid_values(self):
        result = heuristic_classify("Extract the company name from: 'Apple Inc. did a thing.'")
        self.assertIn(result.task_type, TASK_TYPES)
        self.assertIn(result.difficulty, DIFFICULTIES)

    def test_heuristic_is_free(self):
        """The free path must actually be free — this is what the model
        classifier's cost gets compared against."""
        self.assertEqual(heuristic_classify("anything at all").cost_usd, 0.0)
        self.assertIsNone(heuristic_classify("anything at all").model)

    def test_reasoning_wins_over_other_markers(self):
        """A reasoning prompt often also contains an extraction or
        classification verb. The reasoning demand is what dominates the routing
        decision, so it must be checked first."""
        result = heuristic_classify(
            "Classify each item, then explain your reasoning for the ordering."
        )
        self.assertEqual(result.task_type, "reasoning")

    def test_recognises_each_task_type(self):
        cases = {
            "Extract the email address from this text.": "extraction",
            "Classify this review as positive or negative.": "classification",
            "Summarize the following in one sentence.": "short_generation",
            "If all cats are animals, is Fluffy an animal? Explain your reasoning.": "reasoning",
        }
        for prompt, expected in cases.items():
            self.assertEqual(heuristic_classify(prompt).task_type, expected, msg=prompt)

    def test_is_deterministic(self):
        prompt = "Summarize the following passage in two sentences."
        self.assertEqual(heuristic_classify(prompt), heuristic_classify(prompt))


class TestClassifyTask(unittest.TestCase):
    TASK = {
        "id": "x-1",
        "task_type": "extraction",
        "difficulty": "easy",
        "prompt": "Extract the company name from: 'Apple Inc. announced a product.'",
    }

    def test_use_labels_reproduces_v1_exactly_and_for_free(self):
        """The control arm: labels straight through, zero routing overhead."""
        result = classify_task(self.TASK, use_labels=True)
        self.assertEqual(result.task_type, "extraction")
        self.assertEqual(result.difficulty, "easy")
        self.assertEqual(result.cost_usd, 0.0)
        self.assertTrue(result.agreed_with_label)

    def test_classified_arm_scores_agreement_against_the_label(self):
        result = classify_task(self.TASK, use_labels=False)
        self.assertIsNotNone(result.agreed_with_label)
        self.assertEqual(
            result.agreed_with_label,
            result.task_type == "extraction" and result.difficulty == "easy",
        )

    def test_agreement_is_none_without_a_label(self):
        unlabelled = {"id": "u-1", "prompt": "Do a thing."}
        self.assertIsNone(classify_task(unlabelled, use_labels=False).agreed_with_label)

    def test_falls_back_to_heuristic_under_forced_mock(self):
        """A mock adapter fabricates prose, not labels. Parsing it would inject
        noise into routing, so a simulated response must trigger the heuristic
        instead of being trusted."""
        result = classify_task(self.TASK, use_labels=False)
        self.assertTrue(result.simulated)
        self.assertEqual(result.cost_usd, 0.0)

    def test_classifier_uses_the_rosters_budget_model(self):
        """Routing overhead must scale with the CHEAPEST tier, not the frontier
        one — that is what keeps it a rounding error."""
        self.assertEqual(ROSTERS["claude_tiers"].budget, "claude-haiku-4-5")
        self.assertEqual(ROSTERS["cross_vendor"].budget, "gpt-4o-mini")


class TestMalformedRealPrediction(unittest.TestCase):
    """classify()'s output-parsing fail-safes, exercised on a real (non-mock)
    response so the heuristic short-circuit doesn't hide them. Every case
    must fail toward the SAFE assumption ("reasoning"/"hard", which escalates
    to the frontier tier) rather than a cheap default — an unparseable
    classifier answer is exactly the situation the design doc says should
    cost money, not quality."""

    def test_invalid_difficulty_token_falls_back_to_hard(self):
        with patch("router.adapters.get_adapter",
                   return_value=_FakeRealAdapter("task_type=extraction difficulty=impossible")):
            result = classify("some prompt", roster_name="claude_tiers")
        self.assertEqual(result.task_type, "extraction")  # valid token kept
        self.assertEqual(result.difficulty, "hard")  # invalid token -> safe default
        self.assertFalse(result.simulated)

    def test_invalid_task_type_token_falls_back_to_reasoning(self):
        with patch("router.adapters.get_adapter",
                   return_value=_FakeRealAdapter("task_type=bogus difficulty=easy")):
            result = classify("some prompt", roster_name="claude_tiers")
        self.assertEqual(result.task_type, "reasoning")  # invalid token -> safe default
        self.assertEqual(result.difficulty, "easy")  # valid token kept

    def test_completely_unparseable_response_defaults_to_reasoning_hard(self):
        with patch("router.adapters.get_adapter",
                   return_value=_FakeRealAdapter("I refuse to answer that question.")):
            result = classify("some prompt", roster_name="claude_tiers")
        self.assertEqual(result.task_type, "reasoning")
        self.assertEqual(result.difficulty, "hard")

    def test_malformed_response_still_carries_its_real_cost(self):
        """A parse failure is not the same thing as a simulated response — the
        classifier model was really called and really billed, so cost_usd
        must reflect that call even though its content was unusable."""
        with patch("router.adapters.get_adapter",
                   return_value=_FakeRealAdapter("garbage output")):
            result = classify("some prompt", roster_name="claude_tiers")
        self.assertFalse(result.simulated)
        self.assertGreater(result.cost_usd, 0.0)
        self.assertEqual(result.model, "claude-haiku-4-5")


class TestCostAccounting(unittest.TestCase):
    def test_classification_carries_its_own_cost(self):
        """The whole point of the dataclass: routing is not free, and the
        number that says so travels with the prediction."""
        self.assertIn("cost_usd", Classification.__dataclass_fields__)

    def test_cost_is_never_negative(self):
        self.assertGreaterEqual(heuristic_classify("hello").cost_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
