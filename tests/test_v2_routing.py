"""
Tests for v2 routing: roster selection, the effort dial, and gate escalation.

The most important test in this file is TestV1Reproducibility — every other
feature here is worthless if adding it silently changed the published result.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.benchmark.tasks import load_tasks  # noqa: E402
from router.config import (  # noqa: E402
    BUDGET_MODEL,
    FRONTIER_MODEL,
    MID_MODEL,
    ROSTERS,
    get_roster,
)
from router.router import route_task  # noqa: E402


class TestV1Reproducibility(unittest.TestCase):
    """The published 53.6% run must route identically after every v2 change."""

    V1_MAP = {
        "classification": {"easy": BUDGET_MODEL, "medium": MID_MODEL, "hard": FRONTIER_MODEL},
        "extraction": {"easy": BUDGET_MODEL, "medium": MID_MODEL, "hard": FRONTIER_MODEL},
        "short_generation": {"easy": BUDGET_MODEL, "medium": MID_MODEL, "hard": FRONTIER_MODEL},
        "reasoning": {"easy": FRONTIER_MODEL, "medium": FRONTIER_MODEL, "hard": FRONTIER_MODEL},
    }

    def test_default_call_reproduces_v1_routing_on_every_task(self):
        for task in load_tasks():
            decision = route_task(task)
            self.assertEqual(
                decision.chosen_model,
                self.V1_MAP[task["task_type"]][task["difficulty"]],
                msg=f"v1 routing drift on {task['id']}",
            )

    def test_default_call_sends_no_effort_config(self):
        """None (not 'off') is the v1 shape: omit the thinking param entirely."""
        for task in load_tasks():
            self.assertIsNone(route_task(task).effort, msg=task["id"])

    def test_default_roster_is_cross_vendor(self):
        self.assertEqual(route_task(load_tasks()[0]).roster, "cross_vendor")


class TestRosterSelection(unittest.TestCase):
    def test_claude_roster_routes_to_claude_models(self):
        task = {"id": "t", "task_type": "classification", "difficulty": "easy", "prompt": "hi"}
        decision = route_task(task, roster_name="claude_tiers")
        self.assertEqual(decision.chosen_model, "claude-haiku-4-5")
        self.assertEqual(decision.roster, "claude_tiers")

    def test_same_policy_different_roster_yields_different_model(self):
        task = {"id": "t", "task_type": "extraction", "difficulty": "medium", "prompt": "hi"}
        self.assertEqual(route_task(task, roster_name="cross_vendor").chosen_model, "deepseek-chat")
        self.assertEqual(route_task(task, roster_name="claude_tiers").chosen_model, "claude-sonnet-5")

    def test_rosters_share_a_frontier(self):
        """Both ladders top out at Opus, which is why MODEL_TIER can stay flat."""
        self.assertEqual(ROSTERS["cross_vendor"].frontier, ROSTERS["claude_tiers"].frontier)

    def test_claude_roster_price_range_is_far_narrower(self):
        """The core v2 prediction, asserted as arithmetic: a single-vendor
        ladder has ~5x of headroom where three vendors had ~41x. The savings
        ceiling follows from this, so it should fail loudly if pricing drifts."""
        cross = get_roster("cross_vendor").price_range()
        claude = get_roster("claude_tiers").price_range()
        self.assertAlmostEqual(claude, 5.0, places=2)
        self.assertAlmostEqual(cross, 25.0 / 0.60, places=2)
        self.assertGreater(cross, claude * 8)

    def test_unknown_roster_raises(self):
        task = {"id": "t", "task_type": "classification", "difficulty": "easy", "prompt": "hi"}
        with self.assertRaises(KeyError):
            route_task(task, roster_name="nope")


class TestEffortPolicy(unittest.TestCase):
    def test_effort_applied_for_chosen_tier(self):
        task = {"id": "t", "task_type": "reasoning", "difficulty": "hard", "prompt": "hi"}
        decision = route_task(
            task, roster_name="claude_tiers", effort_policy={"frontier": "high"}
        )
        self.assertEqual(decision.chosen_model, "claude-opus-4-8")
        self.assertEqual(decision.effort, "high")

    def test_effort_for_other_tiers_does_not_leak(self):
        """A budget-tier task must not pick up the frontier tier's effort."""
        task = {"id": "t", "task_type": "classification", "difficulty": "easy", "prompt": "hi"}
        decision = route_task(
            task, roster_name="claude_tiers", effort_policy={"frontier": "max"}
        )
        self.assertEqual(decision.chosen_model, "claude-haiku-4-5")
        self.assertIsNone(decision.effort)

    def test_effort_on_haiku_is_dropped_not_escalated(self):
        """Haiku can't take effort. The right response is to drop the dial, not
        to escalate to a pricier model — effort is a preference, and a Haiku
        answer without thinking is cheaper than a Sonnet answer with it."""
        task = {"id": "t", "task_type": "classification", "difficulty": "easy", "prompt": "hi"}
        decision = route_task(
            task, roster_name="claude_tiers", effort_policy={"budget": "high"}
        )
        self.assertEqual(decision.chosen_model, "claude-haiku-4-5")
        self.assertIsNone(decision.effort)
        self.assertIn("does not support effort", decision.reason)

    def test_invalid_effort_raises(self):
        task = {"id": "t", "task_type": "classification", "difficulty": "easy", "prompt": "hi"}
        with self.assertRaises(ValueError):
            route_task(task, roster_name="claude_tiers", effort_policy={"budget": "turbo"})

    def test_off_is_a_valid_distinct_state(self):
        """'off' means explicitly disable thinking; None means send nothing.
        They are different requests and must both be accepted."""
        task = {"id": "t", "task_type": "reasoning", "difficulty": "easy", "prompt": "hi"}
        decision = route_task(
            task, roster_name="claude_tiers", effort_policy={"frontier": "off"}
        )
        self.assertEqual(decision.effort, "off")


class TestGateEscalation(unittest.TestCase):
    def test_oversized_prompt_escalates_past_haiku(self):
        """An easy task would normally route to budget. If the prompt can't fit
        there, the router must escalate rather than emit an impossible route."""
        task = {
            "id": "t",
            "task_type": "classification",
            "difficulty": "easy",
            "prompt": "word " * 200_000,
        }
        decision = route_task(task, roster_name="claude_tiers")
        self.assertNotEqual(decision.chosen_model, "claude-haiku-4-5")
        self.assertIn("claude-haiku-4-5", decision.gated_models)
        self.assertIn("exceeds", decision.reason)

    def test_no_gates_fire_on_normal_tasks(self):
        """Gates must be invisible until they matter — a benchmark of short
        prompts should record zero gate hits."""
        for task in load_tasks():
            self.assertEqual(route_task(task, roster_name="claude_tiers").gated_models, [])


class TestLabelOverrides(unittest.TestCase):
    """The seam the classifier plugs into: a predicted label must route through
    exactly the same policy as a hand-authored one."""

    def test_overrides_take_precedence_over_task_fields(self):
        task = {"id": "t", "task_type": "classification", "difficulty": "easy", "prompt": "hi"}
        decision = route_task(task, task_type="reasoning", difficulty="hard")
        self.assertEqual(decision.chosen_model, FRONTIER_MODEL)

    def test_override_matching_label_is_identical_to_no_override(self):
        task = {"id": "t", "task_type": "extraction", "difficulty": "medium", "prompt": "hi"}
        self.assertEqual(
            route_task(task).chosen_model,
            route_task(task, task_type="extraction", difficulty="medium").chosen_model,
        )


if __name__ == "__main__":
    unittest.main()
