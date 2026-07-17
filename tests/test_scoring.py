"""Tests for router.scoring: quality scoring harness (P0-4)."""

import os
import sys
import unittest
from pathlib import Path

# Force the offline mock judge path before importing router.scoring — see
# the identical comment in test_end_to_end.py for why this can't rely on
# tests/__init__.py under `python -m unittest discover -s tests`.
os.environ.setdefault("AWR_FORCE_MOCK", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.adapters.base import Response  # noqa: E402
from router.scoring import exact_match, rubric_judge, score  # noqa: E402


class TestExactMatch(unittest.TestCase):
    def test_identical_strings_match(self):
        self.assertEqual(exact_match("Apple Inc.", "Apple Inc."), 1.0)

    def test_case_and_punctuation_tolerant(self):
        self.assertEqual(exact_match("  apple inc!! ", "Apple Inc."), 1.0)

    def test_mismatch_scores_zero(self):
        self.assertEqual(exact_match("Microsoft", "Apple Inc."), 0.0)


class TestRubricJudge(unittest.TestCase):
    def test_score_in_bounds(self):
        task = {"id": "rsn-002", "task_type": "reasoning", "difficulty": "hard"}
        response = Response(
            text="...", input_tokens=10, output_tokens=10, latency_ms=100.0,
            model="claude-opus-4-8", simulated=True,
        )
        result = rubric_judge(task, response)
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 1.0)

    def test_frontier_scores_higher_than_budget_on_hard_reasoning(self):
        task = {"id": "rsn-002", "task_type": "reasoning", "difficulty": "hard"}
        frontier_resp = Response(
            text="...", input_tokens=10, output_tokens=10, latency_ms=100.0,
            model="claude-opus-4-8", simulated=True,
        )
        budget_resp = Response(
            text="...", input_tokens=10, output_tokens=10, latency_ms=100.0,
            model="deepseek-chat", simulated=True,
        )
        self.assertGreater(rubric_judge(task, frontier_resp), rubric_judge(task, budget_resp))

    def test_deterministic_not_random(self):
        task = {"id": "gen-001", "task_type": "short_generation", "difficulty": "easy"}
        response = Response(
            text="...", input_tokens=10, output_tokens=10, latency_ms=100.0,
            model="gpt-4o-mini", simulated=True,
        )
        self.assertEqual(rubric_judge(task, response), rubric_judge(task, response))


class TestScoreDispatch(unittest.TestCase):
    def test_dispatches_exact_match(self):
        task = {
            "id": "cls-001", "reference": "positive", "scoring": "exact_match",
            "task_type": "classification", "difficulty": "easy",
        }
        response = Response(
            text="positive", input_tokens=5, output_tokens=2, latency_ms=100.0,
            model="deepseek-chat", simulated=True,
        )
        self.assertEqual(score(task, response), 1.0)

    def test_unsupported_method_raises(self):
        task = {"id": "x", "scoring": "vibes"}
        response = Response(
            text="x", input_tokens=1, output_tokens=1, latency_ms=1.0,
            model="deepseek-chat", simulated=True,
        )
        with self.assertRaises(ValueError):
            score(task, response)


if __name__ == "__main__":
    unittest.main()
