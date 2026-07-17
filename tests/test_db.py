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


if __name__ == "__main__":
    unittest.main()
