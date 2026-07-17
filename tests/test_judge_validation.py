"""Tests for router.judge_validation: inter-judge agreement math + the
offline validation harness (judge_validation.py's rescoring, human-label
sheet export, and graceful failure on runs with no stored response text)."""

import csv
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

# Force the offline mock path before importing router modules — see the
# identical comment in test_end_to_end.py. Under this flag, get_adapter()
# returns MockAdapter for every model, including the second judge
# (gpt-4o-mini), so this whole suite makes zero real network calls.
os.environ.setdefault("AWR_FORCE_MOCK", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from router import db  # noqa: E402
from router import judge_validation  # noqa: E402
from router.adapters import get_adapter  # noqa: E402
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.config import FRONTIER_MODEL  # noqa: E402
from router.scoring import score  # noqa: E402


class TestAgreementStats(unittest.TestCase):
    """Pure math: feed fabricated score pairs, assert mean-abs-diff and
    %-within-threshold by hand."""

    def test_mean_abs_diff_and_pct_within_threshold(self):
        pairs = [(0.90, 0.95), (0.50, 0.90), (0.20, 0.25), (0.80, 0.60)]
        # abs diffs: 0.05, 0.40, 0.05, 0.20
        result = judge_validation.agreement_stats(pairs, threshold=0.15)

        self.assertEqual(result["n"], 4)
        self.assertAlmostEqual(result["mean_abs_diff"], (0.05 + 0.40 + 0.05 + 0.20) / 4)
        # within 0.15: only the two 0.05 diffs -> 2/4 = 50%.
        self.assertAlmostEqual(result["pct_within_threshold"], 50.0)
        self.assertEqual(result["threshold"], 0.15)

    def test_perfect_agreement(self):
        pairs = [(0.7, 0.7), (0.3, 0.3), (1.0, 1.0)]
        result = judge_validation.agreement_stats(pairs)
        self.assertAlmostEqual(result["mean_abs_diff"], 0.0)
        self.assertAlmostEqual(result["pct_within_threshold"], 100.0)

    def test_empty_pairs_returns_none_stats(self):
        result = judge_validation.agreement_stats([])
        self.assertEqual(result["n"], 0)
        self.assertIsNone(result["mean_abs_diff"])
        self.assertIsNone(result["pct_within_threshold"])

    def test_custom_threshold_changes_pct(self):
        pairs = [(0.5, 0.6), (0.5, 0.7)]  # diffs: 0.1, 0.2
        tight = judge_validation.agreement_stats(pairs, threshold=0.05)
        loose = judge_validation.agreement_stats(pairs, threshold=0.25)
        self.assertAlmostEqual(tight["pct_within_threshold"], 0.0)
        self.assertAlmostEqual(loose["pct_within_threshold"], 100.0)


class TestJudgeValidationHarness(unittest.TestCase):
    """Offline smoke test of the full harness: populate a run_group with
    response_text (as run_benchmark.py now does), then re-score it with the
    second judge and round-trip the human label sheet."""

    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.db_path = Path(fd.name)
        db.init_db(self.db_path)
        fd2 = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        fd2.close()
        self.sheet_path = Path(fd2.name)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)
        self.sheet_path.unlink(missing_ok=True)

    def _populate_run_group(self, with_response_text: bool = True) -> str:
        tasks = load_tasks()
        run_group = f"test-{uuid.uuid4().hex[:8]}"
        for task in tasks:
            adapter = get_adapter(FRONTIER_MODEL)
            response = adapter.complete(task["prompt"])
            quality = score(task, response)
            db.log_run(
                run_group=run_group,
                strategy="frontier_only",
                task_id=task["id"],
                task_type=task["task_type"],
                difficulty=task["difficulty"],
                model=FRONTIER_MODEL,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=0.0,
                latency_ms=response.latency_ms,
                quality_score=quality,
                success=response.success,
                simulated=response.simulated,
                response_text=response.text if with_response_text else "",
                db_path=self.db_path,
            )
        return run_group

    def test_run_inter_judge_agreement_offline(self):
        run_group = self._populate_run_group()
        result = judge_validation.run_inter_judge_agreement(
            run_group=run_group, db_path=self.db_path
        )
        self.assertEqual(result["run_group"], run_group)
        self.assertEqual(result["primary_judge_model"], FRONTIER_MODEL)
        self.assertEqual(result["second_judge_model"], judge_validation.SECOND_JUDGE_MODEL)
        self.assertGreater(result["n"], 0)
        self.assertGreaterEqual(result["mean_abs_diff"], 0.0)
        self.assertEqual(len(result["details"]), result["n"])

    def test_defaults_to_latest_run_group(self):
        run_group = self._populate_run_group()
        result = judge_validation.run_inter_judge_agreement(db_path=self.db_path)
        self.assertEqual(result["run_group"], run_group)

    def test_no_runs_at_all_raises(self):
        with self.assertRaises(judge_validation.JudgeValidationError):
            judge_validation.run_inter_judge_agreement(db_path=self.db_path)

    def test_missing_response_text_fails_gracefully(self):
        run_group = self._populate_run_group(with_response_text=False)
        with self.assertRaises(judge_validation.JudgeValidationError):
            judge_validation.run_inter_judge_agreement(
                run_group=run_group, db_path=self.db_path
            )

    def test_export_human_label_sheet_writes_expected_columns(self):
        run_group = self._populate_run_group()
        out_path = judge_validation.export_human_label_sheet(
            run_group=run_group, path=self.sheet_path, db_path=self.db_path
        )
        self.assertTrue(out_path.exists())

        with out_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(
                reader.fieldnames,
                ["task_id", "task_type", "prompt", "model_answer", "rubric",
                 "judge_score", "human_score"],
            )
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertEqual(row["human_score"], "")
            self.assertLessEqual(len(row["prompt"]), 200)
            self.assertLessEqual(len(row["model_answer"]), 300)

    def test_score_human_agreement_round_trip(self):
        run_group = self._populate_run_group()
        out_path = judge_validation.export_human_label_sheet(
            run_group=run_group, path=self.sheet_path, db_path=self.db_path
        )

        with out_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            for i, row in enumerate(rows):
                # Fabricate human scores: perfect agreement on all but one.
                row["human_score"] = row["judge_score"] if i > 0 else "0.0"
                writer.writerow(row)

        result = judge_validation.score_human_agreement(out_path)
        self.assertEqual(result["n"], len(rows))
        self.assertEqual(result["skipped_blank_or_invalid"], 0)
        if len(rows) > 1:
            self.assertGreater(result["mean_abs_diff"], 0.0)

    def test_score_human_agreement_skips_blank_rows(self):
        run_group = self._populate_run_group()
        out_path = judge_validation.export_human_label_sheet(
            run_group=run_group, path=self.sheet_path, db_path=self.db_path
        )
        # Leave human_score blank for every row (the default export state).
        result = judge_validation.score_human_agreement(out_path)
        self.assertEqual(result["n"], 0)
        self.assertGreater(result["skipped_blank_or_invalid"], 0)

    def test_score_human_agreement_missing_file_raises(self):
        with self.assertRaises(judge_validation.JudgeValidationError):
            judge_validation.score_human_agreement(Path("/nonexistent/sheet.csv"))


if __name__ == "__main__":
    unittest.main()
