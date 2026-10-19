"""Online feedback loop: live logging, Router.feedback, source-weighted evidence."""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import db, learned  # noqa: E402
from router.api import CascadePolicy, LearnedPolicy, Router, RulesPolicy  # noqa: E402

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
PROMPT = "Extract the company name from this sentence about a widget maker."


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.path = Path(fd.name)
        self.addCleanup(self.path.unlink, missing_ok=True)
        db.init_db(self.path)

    def rows(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute("SELECT * FROM runs")]
        finally:
            c.close()

    def live(self):
        r = Router(RulesPolicy(use_labels=False), allow_mock_fallback=True, log_to=self.path)
        return r, r.complete(PROMPT)

    def test_request_id_and_row(self):
        _, res = self.live()
        (row,) = self.rows()
        self.assertEqual(row["task_id"], res.request_id)
        self.assertEqual(row["strategy"], "live:router")
        self.assertEqual(row["quality_source"], "pending")

    def test_no_logging_by_default(self):
        res = Router(RulesPolicy(use_labels=False), allow_mock_fallback=True).complete(PROMPT)
        self.assertIsNone(res.request_id)

    def test_feedback_validation(self):
        r, res = self.live()
        with self.assertRaises(KeyError):
            r.feedback("nope", 0.5)
        for bad in (-0.1, 1.5, "x", True):
            with self.assertRaises(ValueError):
                r.feedback(res.request_id, bad)
        with self.assertRaises(ValueError):
            r.feedback(res.request_id, 0.5, source="bogus")
        r.feedback(res.request_id, 0.8)
        self.assertEqual(self.rows()[0]["quality_source"], "user")

    def test_null_source_is_judge_and_pending_excluded(self):
        db.init_db(self.path)
        base = dict(run_group="g0", strategy="router", task_type="extraction",
                    difficulty="hard", model="m", input_tokens=1, output_tokens=1,
                    cost_usd=0.0, latency_ms=1.0, success=True, db_path=self.path)
        db.log_run(task_id="a", quality_score=0.9, **base)
        db.log_run(task_id="b", quality_score=0.0, quality_source="pending", **base)
        rows = db.outcomes_for_bucket("extraction", "hard", db_path=self.path)
        self.assertEqual([r["task_id"] for r in rows], ["a"])
        self.assertEqual(rows[0]["quality_source"], "judge")
        self.assertEqual(db.frontier_reference_quality("extraction", "hard", "m",
                                                       db_path=self.path), 0.9)

    def _evidence(self, source, n):
        roster = learned.get_roster(None)
        model = roster.model_for_tier("budget")
        for i in range(n):
            db.log_run(run_group="live", strategy="live:x", task_id=f"r{i}",
                       task_type="extraction", difficulty="hard", model=model,
                       input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0,
                       quality_score=1.0, success=True, quality_source=source,
                       prompt_text=PROMPT, created_at=NOW.isoformat(), db_path=self.path)
        feats = learned.extract_features(PROMPT, "extraction", "hard")
        return learned._gather_evidence("budget", feats, roster, None, self.path, {}, NOW)

    def test_verifier_weighs_less_than_judge(self):
        n = learned.MIN_EVIDENCE_N
        self.assertTrue(self._evidence("judge", n).meets_threshold)
        self.path.unlink()
        db.init_db(self.path)
        ev = self._evidence("verifier", n)
        self.assertEqual(ev.n, n)
        self.assertFalse(ev.meets_threshold)

    def test_user_overrides_provisional_verifier(self):
        r = Router(CascadePolicy(), allow_mock_fallback=True, log_to=self.path)
        res = r.complete(PROMPT)
        row = self.rows()[0]
        if row["quality_source"] == "verifier":  # mock verifier accepted the budget answer
            self.assertEqual(row["quality_score"], res.verifier_scores[-1])
        r.feedback(res.request_id, 0.1)
        row = self.rows()[0]
        self.assertEqual((row["quality_source"], row["quality_score"]), ("user", 0.1))


if __name__ == "__main__":
    unittest.main()
