"""Tests for router.router: rules-based routing (P0-3)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.config import BUDGET_MODEL, FRONTIER_MODEL, MID_MODEL  # noqa: E402
from router.router import route_task  # noqa: E402


class TestRouter(unittest.TestCase):
    def test_easy_classification_routes_to_budget(self):
        task = {"id": "t1", "task_type": "classification", "difficulty": "easy"}
        decision = route_task(task)
        self.assertEqual(decision.chosen_model, BUDGET_MODEL)
        self.assertFalse(decision.quality_floor_applied)

    def test_medium_extraction_routes_to_mid(self):
        task = {"id": "t2", "task_type": "extraction", "difficulty": "medium"}
        decision = route_task(task)
        self.assertEqual(decision.chosen_model, MID_MODEL)

    def test_hard_short_generation_routes_to_frontier(self):
        task = {"id": "t3", "task_type": "short_generation", "difficulty": "hard"}
        decision = route_task(task)
        self.assertEqual(decision.chosen_model, FRONTIER_MODEL)

    def test_any_reasoning_routes_to_frontier_regardless_of_difficulty(self):
        for difficulty in ("easy", "medium", "hard"):
            task = {"id": f"rsn-{difficulty}", "task_type": "reasoning", "difficulty": difficulty}
            decision = route_task(task)
            self.assertEqual(decision.chosen_model, FRONTIER_MODEL)

    def test_quality_floor_escalates_budget_choice_to_frontier(self):
        task = {"id": "t5", "task_type": "classification", "difficulty": "easy"}
        decision = route_task(task, quality_floor=0.95)
        self.assertEqual(decision.chosen_model, FRONTIER_MODEL)
        self.assertTrue(decision.quality_floor_applied)

    def test_quality_floor_escalates_budget_choice_to_mid(self):
        task = {"id": "t6", "task_type": "classification", "difficulty": "easy"}
        decision = route_task(task, quality_floor=0.85)
        self.assertEqual(decision.chosen_model, MID_MODEL)
        self.assertTrue(decision.quality_floor_applied)

    def test_quality_floor_no_op_when_already_satisfied(self):
        task = {"id": "t7", "task_type": "reasoning", "difficulty": "hard"}
        decision = route_task(task, quality_floor=0.95)
        self.assertEqual(decision.chosen_model, FRONTIER_MODEL)
        self.assertFalse(decision.quality_floor_applied)

    def test_unknown_task_type_raises(self):
        task = {"id": "t8", "task_type": "translation", "difficulty": "easy"}
        with self.assertRaises(KeyError):
            route_task(task)


if __name__ == "__main__":
    unittest.main()
