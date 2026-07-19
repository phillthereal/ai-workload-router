"""Tests for router.db: SQLite performance log (P0-5)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import db  # noqa: E402


class TestDb(unittest.TestCase):
    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.db_path = Path(fd.name)
        db.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _log(self, **overrides):
        base = dict(
            run_group="g1", strategy="router", task_id="t1", task_type="classification",
            difficulty="easy", model="deepseek-chat", input_tokens=10, output_tokens=5,
            cost_usd=0.001, latency_ms=100.0, quality_score=0.9, success=True,
            db_path=self.db_path,
        )
        base.update(overrides)
        return db.log_run(**base)

    def test_log_run_returns_id(self):
        row_id = self._log()
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)

    def test_summary_by_strategy_aggregates_correctly(self):
        self._log(task_id="t1", cost_usd=0.001, quality_score=0.9, strategy="router")
        self._log(task_id="t2", cost_usd=0.002, quality_score=0.8, strategy="router")
        self._log(task_id="t1", cost_usd=0.01, quality_score=0.95, strategy="frontier_only",
                   model="claude-opus-4-8")

        summary = db.summary_by_strategy("g1", db_path=self.db_path)

        self.assertEqual(summary["router"]["n"], 2)
        self.assertAlmostEqual(summary["router"]["total_cost"], 0.003)
        self.assertAlmostEqual(summary["router"]["mean_quality"], 0.85)

        self.assertEqual(summary["frontier_only"]["n"], 1)
        self.assertAlmostEqual(summary["frontier_only"]["total_cost"], 0.01)

    def test_summary_scoped_to_run_group(self):
        self._log(run_group="g1")
        self._log(run_group="g2")
        summary = db.summary_by_strategy("g1", db_path=self.db_path)
        self.assertEqual(summary["router"]["n"], 1)

    def test_fetch_runs_returns_rows(self):
        self._log(task_id="t1")
        rows = db.fetch_runs("g1", db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "t1")

    def test_verifier_scores_persisted_as_json(self):
        self._log(task_id="casc", strategy="cascade", verifier_scores=[0.85, 0.2])
        rows = db.fetch_runs("g1", db_path=self.db_path)
        self.assertEqual(json.loads(rows[0]["verifier_scores"]), [0.85, 0.2])

    def test_verifier_scores_default_null(self):
        # Non-cascade rows (and pre-existing callers) leave it NULL, not "".
        self._log(task_id="r")
        rows = db.fetch_runs("g1", db_path=self.db_path)
        self.assertIsNone(rows[0]["verifier_scores"])

    def test_created_at_defaults_to_now_when_not_given(self):
        """v3: every row logged from here forward gets a real timestamp
        even if the caller doesn't pass one — only genuinely pre-migration
        rows are NULL."""
        self._log(task_id="r")
        rows = db.fetch_runs("g1", db_path=self.db_path)
        self.assertIsNotNone(rows[0]["created_at"])

    def test_created_at_accepts_an_explicit_override(self):
        self._log(task_id="r", created_at="2020-01-01T00:00:00+00:00")
        rows = db.fetch_runs("g1", db_path=self.db_path)
        self.assertEqual(rows[0]["created_at"], "2020-01-01T00:00:00+00:00")

    def test_learned_evidence_persisted_as_json(self):
        self._log(task_id="l", learned_evidence={"chosen_tier": "budget", "direction": "downgraded"})
        rows = db.fetch_runs("g1", db_path=self.db_path)
        self.assertEqual(json.loads(rows[0]["learned_evidence"]), {"chosen_tier": "budget", "direction": "downgraded"})

    def test_learned_evidence_default_null(self):
        self._log(task_id="l")
        rows = db.fetch_runs("g1", db_path=self.db_path)
        self.assertIsNone(rows[0]["learned_evidence"])


class TestOutcomesForBucket(unittest.TestCase):
    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.db_path = Path(fd.name)
        db.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _log(self, **overrides):
        base = dict(
            run_group="g0", strategy="router", task_id="t1", task_type="extraction",
            difficulty="hard", model="claude-haiku-4-5", input_tokens=10, output_tokens=5,
            cost_usd=0.001, latency_ms=100.0, quality_score=0.9, success=True,
            db_path=self.db_path,
        )
        base.update(overrides)
        return db.log_run(**base)

    def test_filters_by_task_type_and_difficulty(self):
        self._log(task_type="extraction", difficulty="hard")
        self._log(task_type="extraction", difficulty="easy")
        self._log(task_type="reasoning", difficulty="hard")
        rows = db.outcomes_for_bucket("extraction", "hard", db_path=self.db_path)
        self.assertEqual(len(rows), 1)

    def test_excludes_simulated_rows(self):
        self._log(simulated=True)
        rows = db.outcomes_for_bucket("extraction", "hard", db_path=self.db_path)
        self.assertEqual(rows, [])

    def test_before_run_group_excludes_equal_and_later(self):
        self._log(run_group="20260101T000000-aaa")
        self._log(run_group="20260201T000000-bbb")
        rows = db.outcomes_for_bucket(
            "extraction", "hard", before_run_group="20260201T000000-bbb", db_path=self.db_path
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_group"], "20260101T000000-aaa")

    def test_no_cutoff_returns_everything(self):
        self._log(run_group="20260101T000000-aaa")
        self._log(run_group="20260201T000000-bbb")
        rows = db.outcomes_for_bucket("extraction", "hard", db_path=self.db_path)
        self.assertEqual(len(rows), 2)


class TestFrontierReferenceQuality(unittest.TestCase):
    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.db_path = Path(fd.name)
        db.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _log(self, **overrides):
        base = dict(
            run_group="g0", strategy="frontier_only", task_id="t1", task_type="extraction",
            difficulty="hard", model="claude-opus-4-8", input_tokens=10, output_tokens=5,
            cost_usd=0.01, latency_ms=100.0, quality_score=0.95, success=True,
            db_path=self.db_path,
        )
        base.update(overrides)
        return db.log_run(**base)

    def test_averages_matching_frontier_rows(self):
        self._log(quality_score=0.9)
        self._log(quality_score=1.0)
        result = db.frontier_reference_quality("extraction", "hard", "claude-opus-4-8", db_path=self.db_path)
        self.assertAlmostEqual(result, 0.95)

    def test_none_when_no_matching_evidence(self):
        result = db.frontier_reference_quality("reasoning", "easy", "claude-opus-4-8", db_path=self.db_path)
        self.assertIsNone(result)

    def test_ignores_non_frontier_models(self):
        self._log(model="claude-haiku-4-5", quality_score=0.5)
        result = db.frontier_reference_quality("extraction", "hard", "claude-opus-4-8", db_path=self.db_path)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
