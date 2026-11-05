"""Guard rails that keep a paid benchmark run honest. Offline: every provider is a stub."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import build_public_tasks  # noqa: E402
import run_benchmark  # noqa: E402
from router import adapters, classifier, scoring  # noqa: E402
from router.adapters.base import Adapter, Response  # noqa: E402
from router.adapters.cache import CachedAdapter  # noqa: E402
from router.api import Result, Router, RulesPolicy  # noqa: E402
from router.benchmark.tasks import validate_task  # noqa: E402
from router.scoring import JudgeError, final_answer, score  # noqa: E402


def response(text="0.9", simulated=False, success=True, tokens=1_000_000):
    return Response(text=text, input_tokens=tokens, output_tokens=0, latency_ms=1.0,
                    model="gpt-4o-mini", simulated=simulated, success=success)


class Stub(Adapter):
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, prompt, effort=None, max_tokens=None):
        self.calls += 1
        return self.reply


class FinalAnswer(unittest.TestCase):
    def test_a_wrong_conclusion_fails_even_if_working_mentions_the_right_answer(self):
        self.assertEqual(final_answer("First 18, doubled.\nAnswer: 36", "18"), 0.0)
        self.assertEqual(final_answer("B looks tempting.\nAnswer: C", "B"), 0.0)
        self.assertEqual(scoring.exact_match("B looks tempting.\nAnswer: C", "B"), 1.0)  # why it exists

    def test_format_alone_never_decides_the_score(self):
        for reply, reference in [("Answer: 18.00", "18"), ("answer: $1,800", "1800"),
                                 ("**Answer: B) Paris**", "B"), ("card arrival", "card_arrival"),
                                 ("Answer: the Tower", "Tower || spire"), ("Answer: spire.", "Tower || spire")]:
            self.assertEqual(final_answer(reply, reference), 1.0, reply)

    def test_the_last_answer_line_wins_and_no_answer_scores_zero(self):
        self.assertEqual(final_answer("Answer: 18\nActually:\nAnswer: 20", "18"), 0.0)
        self.assertEqual(final_answer("It might be 18", "18"), 0.0)
        self.assertEqual(final_answer("", "18"), 0.0)

    def test_score_dispatches_it(self):
        task = {"scoring": "final_answer", "reference": "18"}
        self.assertEqual(score(task, response("Answer: 18")), 1.0)


class TaskBuilder(unittest.TestCase):
    def test_every_converted_task_loads_and_a_right_answer_scores_one(self):
        rows = [
            build_public_tasks.banking("where is my card", "card_arrival", ["card_arrival", "x"], 1),
            build_public_tasks.squad({"answers": {"text": ["Paris", "Paris"]}, "context": "c", "question": "q"}, 2),
            build_public_tasks.gsm8k({"question": "q", "answer": "6 * 3 = 18\n#### 1,800"}, 3),
            build_public_tasks.mmlu_pro({"question": "q", "options": ["a", "b"], "answer": "B", "question_id": 4}, 4),
        ]
        for row, reply in zip(rows, ("Answer: card_arrival", "Answer: Paris", "Answer: 1800", "Answer: B")):
            self.assertTrue(validate_task(row))
            self.assertEqual(score(row, response(reply)), 1.0, row["id"])
        self.assertTrue(validate_task(build_public_tasks.ifeval({"prompt": "p"}, 5)))

    def test_unanswerable_squad_questions_are_skipped(self):
        self.assertIsNone(build_public_tasks.squad({"answers": {"text": []}, "context": "c", "question": "q"}, 1))


class ClassifierFallback(unittest.TestCase):
    def test_a_failed_classifier_call_is_flagged_all_the_way_to_the_result(self):
        with mock.patch.object(adapters, "get_adapter", return_value=Stub(response(success=False))):
            c = classifier.classify("Label this: great phone")
        self.assertTrue(c.fell_back)
        self.assertIsNone(c.model)

        fake = Stub(response("positive"))
        with mock.patch("router.api.classify_task", return_value=c), \
                mock.patch("router.api.get_adapter", return_value=fake):
            result = Router(RulesPolicy(use_labels=False), allow_mock_fallback=True).complete("x")
        self.assertTrue(result.classifier_fell_back)

    def test_a_working_classifier_is_not_flagged(self):
        reply = response("task_type=classification difficulty=easy", tokens=10)
        with mock.patch.object(adapters, "get_adapter", return_value=Stub(reply)):
            self.assertFalse(classifier.classify("Label this: great phone").fell_back)


class StrictJudge(unittest.TestCase):
    task = {"prompt": "p", "reference": "r", "scoring": "rubric_judge"}

    def judged(self, reply, strict):
        with mock.patch.object(adapters, "get_adapter", return_value=Stub(reply)), \
                mock.patch.object(scoring, "_real_judge_available", return_value=True):
            return score(self.task, response("answer"), strict=strict)

    def test_strict_refuses_mock_failed_and_unreadable_scores(self):
        for reply in (response("0.9", simulated=True), response("0.9", success=False), response("great")):
            with self.assertRaises(JudgeError):
                self.judged(reply, strict=True)

    def test_a_real_score_passes_and_the_default_path_is_unchanged(self):
        self.assertEqual(self.judged(response("0.9"), strict=True), 0.9)
        self.assertEqual(self.judged(response("great"), strict=False), 0.5)

    def test_strict_refuses_when_no_real_judge_is_available(self):
        with mock.patch.object(scoring, "_real_judge_available", return_value=False):
            with self.assertRaises(JudgeError):
                score(self.task, response("answer"), strict=True)


class SpendMeter(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(adapters.SpendMeter, "usd", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_calls_count_even_when_they_fail_and_replays_are_free(self):
        inner = Stub(response())  # 1M input tokens on gpt-4o-mini = $0.15
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("router.adapters.cache.CACHE_DIR", Path(tmp)):
            cached = CachedAdapter(adapters._MeteredAdapter(inner, "gpt-4o-mini"), "gpt-4o-mini")
            cached.complete("same prompt")
            cached.complete("same prompt")
        self.assertEqual(inner.calls, 1)
        self.assertAlmostEqual(adapters.SpendMeter.usd, 0.15)

        adapters._MeteredAdapter(Stub(response(success=False)), "gpt-4o-mini").complete("p")
        self.assertAlmostEqual(adapters.SpendMeter.usd, 0.30)


class MaxSpendRun(unittest.TestCase):
    def live(self):
        env = {k: v for k, v in os.environ.items() if k != "AWR_FORCE_MOCK"}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_refused_in_mock_mode(self):
        with mock.patch.dict(os.environ, {"AWR_FORCE_MOCK": "1"}), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                run_benchmark.main(["--max-spend", "1"])

    def test_stops_before_a_task_could_pass_the_cap_and_writes_no_report(self):
        with self.live(), mock.patch.object(run_benchmark, "_preflight"), \
                mock.patch.object(run_benchmark.db, "init_db"), \
                mock.patch.object(run_benchmark, "Router") as router, \
                mock.patch.object(run_benchmark, "build_report") as report:
            with self.assertRaises(SystemExit) as stopped:
                run_benchmark.main(["--max-spend", "0.05"])
        self.assertIn("STOPPED: spend cap", str(stopped.exception))
        router.return_value.complete.assert_not_called()
        report.assert_not_called()

    def test_preflight_failure_stops_before_any_task(self):
        with self.live(), mock.patch.object(run_benchmark, "get_adapter",
                                            return_value=Stub(response(success=False))), \
                mock.patch.object(run_benchmark.db, "init_db"), \
                mock.patch.object(run_benchmark, "Router") as router:
            with self.assertRaises(SystemExit) as stopped:
                run_benchmark.main(["--max-spend", "5"])
        self.assertIn("preflight failed", str(stopped.exception))
        router.return_value.complete.assert_not_called()

    def test_every_untrue_result_stops_the_run(self):
        good = dict(text="a", model="m", answer_cost_usd=0.0, routing_cost_usd=0.0, simulated=False)
        run_benchmark._check_real(Result(**good))
        for bad in ({"simulated": True}, {"classifier_fell_back": True},
                    {"trace": ["provider error on m (HTTP 429)"]}):
            with self.assertRaises(run_benchmark.RunStopped):
                run_benchmark._check_real(Result(**{**good, **bad}))


if __name__ == "__main__":
    unittest.main()
