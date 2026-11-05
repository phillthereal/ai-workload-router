"""The consensus rule (two cheap answers, third vote, Sonnet) and the free lookup."""

import json
import unittest
from unittest import mock

from router import api
from router.adapters.base import Adapter, Response
from router.api import ANSWER_ONLY, CONSENSUS_FIRST, CONSENSUS_LAST, RulesPolicy, Router
from router.config import EFFORT_OFF, get_roster
from router.lookup import BENCHMARK_DIR, LABELLED_FILES, Lookup, public_lookup

RO = get_roster("cross_vendor")
PROMPT = "Which is right?\nEnd your reply with one final line in exactly this form:\nAnswer: <your answer>"


def task(task_type):
    return {"id": "t", "prompt": PROMPT, "task_type": task_type, "difficulty": "medium"}


def run(task_type, replies):
    """replies: model -> text. Returns the Result and the (model, prompt, effort) calls made."""
    calls = []

    class Scripted(Adapter):
        def __init__(self, model):
            self.model = model

        def complete(self, prompt, effort=None, max_tokens=None):
            calls.append((self.model, prompt, effort))
            return Response(text=replies[self.model], input_tokens=1000, output_tokens=100,
                            latency_ms=1.0, model=self.model, simulated=False, success=True)

    policy = RulesPolicy(True, consensus_on=("reasoning", "extraction"))
    with mock.patch.object(api, "get_adapter", lambda m, allow_mock_fallback=True: Scripted(m)):
        return Router(policy, "cross_vendor").complete(PROMPT, task=task(task_type)), calls


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

    def test_answer_only_is_not_added_to_a_prompt_with_no_final_line(self):
        c = api.Classification("extraction", "easy", 0.0, None, False)
        seen = []
        ctx = mock.Mock(roster=RO, cost=lambda r: 0.0)
        ctx.call.side_effect = lambda m, p, e=None: (seen.append(p), Response(
            text="same", input_tokens=1, output_tokens=1, latency_ms=1.0, model=m, simulated=False, success=True))[1]
        RulesPolicy()._consensus("Find the date.", c, ctx)
        self.assertEqual(set(seen), {"Find the date."})

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
        prompts = [t["prompt"] for t in json.loads((BENCHMARK_DIR / LABELLED_FILES[0]).read_text())["tasks"]]
        self.assertEqual(len(public_lookup(prompts).labels), 900)


if __name__ == "__main__":
    unittest.main()
