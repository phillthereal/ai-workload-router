"""
Tests for run_benchmark._report_path — the guard that keeps any run from
overwriting the published data/benchmark_report.md.

This guard has caught three distinct clobbers during development (a simulated
run, a --classify run on the default roster, and a hard-task run reusing a
classified filename), so it is worth pinning: exactly one run shape may write
the canonical file, and everything else gets a descriptive, non-colliding name.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_benchmark as rb  # noqa: E402


class TestPublishedShapeGuard(unittest.TestCase):
    def test_only_the_exact_published_shape_claims_the_canonical_file(self):
        path = rb._report_path("cross_vendor", all_real=True, classify=False,
                               effort_policy=None, strategy="router", tasks=None)
        self.assertEqual(path.name, "benchmark_report.md")

    def test_simulated_never_claims_canonical(self):
        path = rb._report_path("cross_vendor", all_real=False, classify=False,
                               effort_policy=None, strategy="router", tasks=None)
        self.assertIn("simulated", path.name)
        self.assertNotEqual(path.name, "benchmark_report.md")

    def test_classify_on_default_roster_does_not_claim_canonical(self):
        """cross_vendor --classify is the default roster and live, but a
        DIFFERENT experiment — it must not overwrite the published labels run."""
        path = rb._report_path("cross_vendor", all_real=True, classify=True,
                               effort_policy=None, strategy="router", tasks=None)
        self.assertNotEqual(path.name, "benchmark_report.md")
        self.assertIn("classified", path.name)

    def test_cascade_gets_its_own_name(self):
        path = rb._report_path("claude_tiers", all_real=True, classify=False,
                               effort_policy=None, strategy="cascade", tasks=None)
        self.assertIn("cascade", path.name)

    def test_hard_task_run_does_not_collide_with_easy(self):
        easy = rb._report_path("claude_tiers", all_real=True, classify=True,
                               effort_policy=None, strategy="router", tasks=None)
        hard = rb._report_path("claude_tiers", all_real=True, classify=True,
                               effort_policy=None, strategy="router",
                               tasks="data/tasks_hard.json")
        self.assertNotEqual(easy.name, hard.name)
        self.assertIn("hard", hard.name)

    def test_effort_policy_run_does_not_claim_canonical(self):
        path = rb._report_path("cross_vendor", all_real=True, classify=False,
                               effort_policy={"frontier": "low"}, strategy="router", tasks=None)
        self.assertNotEqual(path.name, "benchmark_report.md")
        self.assertIn("effort", path.name)


class TestTasksTag(unittest.TestCase):
    def test_default_tasks_has_no_tag(self):
        self.assertIsNone(rb._tasks_tag(None))
        self.assertIsNone(rb._tasks_tag("data/tasks.json"))

    def test_hard_tasks_tag(self):
        self.assertEqual(rb._tasks_tag("data/tasks_hard.json"), "hard")


class TestHardTaskSet(unittest.TestCase):
    """The hard probe set must stay loadable and schema-valid."""

    def test_hard_task_set_loads_and_validates(self):
        from router.benchmark.tasks import load_tasks
        tasks = load_tasks(Path(__file__).resolve().parents[1] / "data" / "tasks_hard.json")
        self.assertGreaterEqual(len(tasks), 10)
        for task in tasks:
            self.assertEqual(task["difficulty"], "hard")


class TestVerifierFlag(unittest.TestCase):
    """--verifier plumbing: the CLI flag that picks the cascade's quality-gate
    model (see router.cascade.run_cascade's cost-vs-independence trade-off)."""

    def test_verifier_defaults_to_none(self):
        """Omitting --verifier must fall through to run_cascade's own default
        (the roster's budget tier) rather than the CLI silently picking one."""
        args = rb._build_parser().parse_args(["--strategy", "cascade"])
        self.assertIsNone(args.verifier)

    def test_verifier_flag_sets_model_name(self):
        args = rb._build_parser().parse_args(
            ["--strategy", "cascade", "--verifier", "claude-haiku-4-5"]
        )
        self.assertEqual(args.verifier, "claude-haiku-4-5")

    def test_explicit_verifier_does_not_collide_with_default_verifier_report(self):
        """A cascade run with an explicit --verifier is a different experiment
        from the same roster/strategy/tasks with run_cascade's own default —
        it must get its own filename rather than silently overwriting the
        default-verifier report (this collision actually happened once; see
        docs/V2_FINDINGS.md's verifier-economics note)."""
        default_verifier = rb._report_path(
            "claude_tiers", all_real=True, classify=False, effort_policy=None,
            strategy="cascade", tasks=None, verifier=None,
        )
        haiku_verifier = rb._report_path(
            "claude_tiers", all_real=True, classify=False, effort_policy=None,
            strategy="cascade", tasks=None, verifier="claude-haiku-4-5",
        )
        self.assertNotEqual(default_verifier.name, haiku_verifier.name)
        self.assertIn("verifier-claude-haiku-4-5", haiku_verifier.name)


if __name__ == "__main__":
    unittest.main()
