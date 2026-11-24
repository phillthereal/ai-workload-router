"""The consensus rule (two cheap answers, third vote, Sonnet) and the free lookup."""

import json
import unittest
from unittest import mock

from router import api
from router.adapters.base import Adapter, Response
from router.api import (ANSWER_ONLY, CONSENSUS_FIRST, CONSENSUS_LAST, FINAL_LINE, RulesPolicy, Router,
                        default_policy)
from router.config import EFFORT_OFF, get_roster
from router.lookup import LABELLED_TASKS, Lookup, public_lookup

RO = get_roster("cross_vendor")
PROMPT = "Which is right?\nEnd your reply with one final line in exactly this form:\nAnswer: <your answer>"


def task(task_type):
    return {"id": "t", "prompt": PROMPT, "task_type": task_type, "difficulty": "medium"}


def run(task_type, replies, prompt=PROMPT, policy=None, down=()):
    """replies: model -> text. Models in `down` raise, so Ctx.call fails over.

    Returns the Result and the (model, prompt, effort) calls that were answered."""
    calls = []

    class Scripted(Adapter):
        def __init__(self, model):
            self.model = model

        def complete(self, prompt, effort=None, max_tokens=None):
            if self.model in down:
                raise ConnectionError("provider down")
            calls.append((self.model, prompt, effort))
            return Response(text=replies[self.model], input_tokens=1000, output_tokens=100,
                            latency_ms=1.0, model=self.model, simulated=False, success=True)

    policy = policy or RulesPolicy(True, consensus_on=("reasoning", "extraction"))
    labelled = {**task(task_type), "prompt": prompt} if task_type else None
    with mock.patch.object(api, "get_adapter", lambda m, allow_mock_fallback=True: Scripted(m)):
        return Router(policy, "cross_vendor").complete(prompt, task=labelled), calls


class TestConsensus(unittest.TestCase):
    def test_agreement_keeps_the_mid_answer_and_charges_the_other_call(self):
        r, calls = run("reasoning", {CONSENSUS_FIRST: "Answer: 4", RO.mid: "Working.\nAnswer: 4"})
        self.assertEqual(r.model, RO.mid)
        self.assertEqual([c[0] for c in calls], [CONSENSUS_FIRST, RO.mid])
        self.assertEqual(calls[0][2], EFFORT_OFF)
        self.assertGreater(r.routing_cost_usd, 0)

    def test_third_vote_keeps_its_own_answer_when_it_sides_with_either(self):
        r, calls = run("reasoning", {CONSENSUS_FIRST: "Answer: 4", RO.mid: "Answer: 5", RO.budget: "Answer: 4"})
        self.assertEqual(r.model, RO.budget)
        self.assertNotIn(CONSENSUS_LAST, [c[0] for c in calls])

    def test_no_side_goes_to_sonnet_and_every_earlier_call_is_charged(self):
        r, calls = run("reasoning", {CONSENSUS_FIRST: "Answer: 4", RO.mid: "Answer: 5",
                                     RO.budget: "Answer: 6", CONSENSUS_LAST: "Answer: 7"})
        self.assertEqual(r.model, CONSENSUS_LAST)
        self.assertEqual(calls[-1][2], EFFORT_OFF)
        self.assertNotIn(RO.frontier, [c[0] for c in calls])
        cheap = sum(api.get_model(m).cost_for_tokens(1000, 100) for m in (CONSENSUS_FIRST, RO.mid, RO.budget))
        self.assertAlmostEqual(r.routing_cost_usd, cheap)

    def test_extraction_skips_the_vote_and_asks_for_the_answer_only(self):
        r, calls = run("extraction", {CONSENSUS_FIRST: "Answer: 4", RO.mid: "Answer: 5", CONSENSUS_LAST: "Answer: 7"})
        self.assertEqual([c[0] for c in calls], [CONSENSUS_FIRST, RO.mid, CONSENSUS_LAST])
        self.assertTrue(all(c[1] == PROMPT + ANSWER_ONLY for c in calls))
        self.assertEqual(r.model, CONSENSUS_LAST)

    def test_a_prompt_with_no_final_line_is_asked_for_one_and_checked(self):
        r, calls = run("reasoning", {CONSENSUS_FIRST: "Because.\nAnswer: scattering",
                                     RO.mid: "Rayleigh.\nAnswer: scattering"}, prompt="Why is the sky blue?")
        self.assertEqual([c[0] for c in calls], [CONSENSUS_FIRST, RO.mid])
        self.assertTrue(all(c[1] == "Why is the sky blue?" + FINAL_LINE for c in calls))
        self.assertNotIn(RO.frontier, [c[0] for c in calls])
        self.assertEqual(r.text, "Rayleigh.\nAnswer: scattering")

    def test_extraction_with_no_final_line_gets_both_instructions(self):
        r, calls = run("extraction", {CONSENSUS_FIRST: "Answer: Paris", RO.mid: "Answer: Paris"},
                       prompt="What city is named in the passage?")
        self.assertTrue(all(c[1] == "What city is named in the passage?" + FINAL_LINE + ANSWER_ONLY
                            for c in calls))
        self.assertEqual(r.model, RO.mid)

    def test_the_default_runs_consensus_on_a_prompt_with_no_final_line(self):
        p = "A shop sells 3 boxes of 4 pens. How many pens?"
        ans = {CONSENSUS_FIRST: "Answer: 12", RO.mid: "3 x 4.\nAnswer: 12"}
        r, calls = run(None, ans, prompt=p, policy=default_policy())
        self.assertEqual((r.task_type, r.model), ("reasoning", RO.mid))
        self.assertEqual(r.trace[-1], f"{CONSENSUS_FIRST} and {RO.mid} agree")
        self.assertEqual([c[1] for c in calls], [p + FINAL_LINE] * 2)
        self.assertEqual(r.text, "3 x 4.\nAnswer: 12")

    def test_the_default_leaves_a_prompt_with_a_final_line_unchanged(self):
        p = "A shop sells 3 boxes of 4 pens. How many pens?\nEnd your reply with one final line:\nAnswer: <number>"
        r, calls = run(None, {CONSENSUS_FIRST: "Answer: 12", RO.mid: "Answer: 12"}, prompt=p,
                       policy=default_policy())
        self.assertEqual([(c[0], c[1]) for c in calls], [(CONSENSUS_FIRST, p), (RO.mid, p)])
        self.assertEqual((r.model, r.trace[-1]), (RO.mid, f"{CONSENSUS_FIRST} and {RO.mid} agree"))

    def test_the_default_uses_the_free_lookup_with_no_labels(self):
        ans = {CONSENSUS_FIRST: "Answer: 4", RO.mid: "Answer: 4"}
        p = "A shop sells 3 boxes of 4 pens. How many pens?\nEnd your reply with one final line:\nAnswer: <number>"
        r, calls = run(None, ans, prompt=p, policy=default_policy())
        self.assertEqual((r.task_type, r.model), ("reasoning", RO.mid))
        self.assertEqual(r.routing_cost_usd, api.get_model(CONSENSUS_FIRST).cost_for_tokens(1000, 100))

    def test_a_failover_onto_the_mid_model_is_not_an_agreement(self):
        # Haiku is down, so the first call fails over to the mid model, which then answers twice.
        r, calls = run("extraction", {RO.mid: "Answer: 4", CONSENSUS_LAST: "Answer: 4"}, down=(CONSENSUS_FIRST,))
        self.assertEqual([c[0] for c in calls], [RO.mid, RO.mid, CONSENSUS_LAST])
        self.assertEqual(r.model, CONSENSUS_LAST)
        self.assertIn(f"{RO.mid} answered twice after a failover: no cross-check", r.trace)
        self.assertNotIn(f"{RO.mid} and {RO.mid} agree", r.trace)
        self.assertIn(f"no agreement -> {CONSENSUS_LAST}", r.trace)

    def test_a_failover_onto_the_mid_model_still_gets_the_reasoning_vote(self):
        r, calls = run("reasoning", {RO.mid: "Answer: 4", RO.budget: "Answer: 5", CONSENSUS_LAST: "Answer: 6"},
                       down=(CONSENSUS_FIRST,))
        self.assertEqual([c[0] for c in calls], [RO.mid, RO.mid, RO.budget, CONSENSUS_LAST])
        self.assertIn(f"{RO.mid} answered twice after a failover: no cross-check", r.trace)
        self.assertEqual(r.model, CONSENSUS_LAST)

    def test_a_vote_from_a_model_that_already_answered_does_not_break_the_tie(self):
        # The vote model is down and fails over to the mid model, which already answered.
        r, calls = run("reasoning", {CONSENSUS_FIRST: "Answer: 4", RO.mid: "Answer: 5", CONSENSUS_LAST: "Answer: 6"},
                       down=(RO.budget,))
        self.assertEqual([c[0] for c in calls], [CONSENSUS_FIRST, RO.mid, RO.mid, CONSENSUS_LAST])
        self.assertEqual(r.model, CONSENSUS_LAST)
        self.assertIn(f"no agreement -> {CONSENSUS_LAST}", r.trace)
        self.assertNotIn(f"{RO.mid} broke the tie", r.trace)

    def test_agree_or_frontier_does_not_accept_one_model_agreeing_with_itself(self):
        r, calls = run("classification", {RO.mid: "Answer: x", RO.frontier: "Answer: x"}, down=(RO.budget,),
                       policy=RulesPolicy(True, agree_on=("classification",)))
        self.assertEqual([c[0] for c in calls], [RO.mid, RO.mid, RO.frontier])
        self.assertEqual(r.model, RO.frontier)
        self.assertIn(f"{RO.mid} answered twice after a failover: no cross-check", r.trace)
        self.assertIn(f"{RO.mid} and {RO.mid} differ -> frontier", r.trace)

    def test_agree_or_frontier_still_agrees_across_two_models(self):
        r, calls = run("classification", {RO.budget: "Answer: x", RO.mid: "Answer: x"},
                       policy=RulesPolicy(True, agree_on=("classification",)))
        self.assertEqual([c[0] for c in calls], [RO.budget, RO.mid])
        self.assertEqual(r.trace[-1], f"{RO.budget} and {RO.mid} agree")

    def test_other_types_stay_on_the_rules_table(self):
        r, calls = run("classification", {RO.mid: "Answer: x"})
        self.assertEqual([c[0] for c in calls], [RO.mid])


class TestLookup(unittest.TestCase):
    TASKS = [{"prompt": "add these numbers and find the total", "task_type": "reasoning", "difficulty": "hard"},
             {"prompt": "add the numbers for the total sum", "task_type": "reasoning", "difficulty": "hard"},
             {"prompt": "add numbers total please", "task_type": "reasoning", "difficulty": "medium"},
             {"prompt": "label this review as positive or negative", "task_type": "classification", "difficulty": "medium"}]

    def test_nearest_tasks_vote_on_type_and_difficulty_at_no_cost(self):
        c = Lookup(self.TASKS, k=3).classify({"prompt": "find the total of these numbers"})
        self.assertEqual((c.task_type, c.difficulty, c.cost_usd, c.agreed_with_label),
                         ("reasoning", "hard", 0.0, None))

    def test_compares_with_the_label_when_there_is_one(self):
        c = Lookup(self.TASKS, k=1).classify({"prompt": "label this review", "task_type": "reasoning",
                                              "difficulty": "hard"})
        self.assertIs(c.agreed_with_label, False)

    def test_a_prompt_sharing_no_words_uses_the_free_heuristic(self):
        c = Lookup(self.TASKS).classify({"prompt": "zzz"})
        self.assertEqual(c.cost_usd, 0.0)
        self.assertIsNone(c.model)

    def test_tested_prompts_never_reach_the_lookup(self):
        prompts = [t["prompt"] for t in json.loads(LABELLED_TASKS.read_text())[:300]]
        self.assertEqual(len(public_lookup(prompts).labels), 900)


if __name__ == "__main__":
    unittest.main()
