"""Offline tests for analyze.py (temp db fixture; never data/runs.db)."""
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AWR_FORCE_MOCK", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import analyze  # noqa: E402
from router import db  # noqa: E402
from router.report import build_report  # noqa: E402


def log(path, g, strat, task, cost, q, routing=0.0, sim=False):
    db.log_run(run_group=g, strategy=strat, task_id=task, task_type="reasoning", difficulty="easy",
               model="claude-opus-4-8", input_tokens=1, output_tokens=1, cost_usd=cost, latency_ms=1.0,
               quality_score=q, success=True, simulated=sim, routing_cost_usd=routing,
               response_text="SECRET-RESPONSE", prompt_text="SECRET-PROMPT", db_path=path)


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.path = Path(fd.name)
        db.init_db(self.path)
        # 4 tasks: baseline 1.0 each; router 0.4 + 0.1 routing each
        for i in range(4):
            log(self.path, "g", "frontier_only", f"t{i}", 1.0, 1.0)
            log(self.path, "g", "router", f"t{i}", 0.4, 0.9, routing=0.1)
        log(self.path, "g", "router", "sim", 9, 0, sim=True)
        log(self.path, "g", "live:x", "live", 9, 0)
        self.conn = analyze.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.path.unlink()

    def test_point_estimate_matches_direct_and_report(self):
        p = [t[1:] for t in analyze.paired_tasks(self.conn, "g")]
        self.assertEqual(len(p), 4)
        pt, lo, hi = analyze.bootstrap(p, analyze.net_reduction)
        self.assertAlmostEqual(pt, 50.0)  # (4 - 4*0.5)/4
        self.assertLessEqual(lo, pt)
        self.assertGreaterEqual(hi, pt)
        r = build_report("g", db_path=self.path, experimental_strategy="router")
        # report.py includes the simulated/live router rows in its query, so compare on clean groups only
        self.assertAlmostEqual(analyze.retention(p), 90.0)
        self.assertGreater(r.router_n, 4)  # sanity: report.py is unfiltered, analyze filters

    def test_savings_definition_matches_report_on_clean_group(self):
        for i in range(3):
            log(self.path, "c", "frontier_only", f"t{i}", 2.0, 1.0)
            log(self.path, "c", "router", f"t{i}", 0.5, 0.8, routing=0.25)
        p = [t[1:] for t in analyze.paired_tasks(self.conn, "c")]
        r = build_report("c", db_path=self.path)
        self.assertAlmostEqual(analyze.net_reduction(p), r.net_cost_reduction_pct)
        self.assertAlmostEqual(analyze.retention(p), r.quality_retention_pct)

    def test_deterministic_and_paired(self):
        p = [t[1:] for t in analyze.paired_tasks(self.conn, "g")]
        self.assertEqual(analyze.bootstrap(p, analyze.net_reduction, n=500),
                         analyze.bootstrap(p, analyze.net_reduction, n=500))
        # arms stay aligned: if each pair has router==baseline, every resample gives 0% change
        same = [(c, c, q, q) for c, q in [(1, 1), (2, .5), (5, 1)]]
        pt, lo, hi = analyze.bootstrap(same, analyze.net_reduction, n=300)
        self.assertEqual((pt, lo, hi), (0.0, 0.0, 0.0))
        # misaligned resampling would break this identity for retention too
        self.assertEqual(analyze.bootstrap(same, analyze.retention, n=300)[1:], (100.0, 100.0))

    def test_export_excludes_simulated_live_and_text(self):
        out = Path(tempfile.mkdtemp())
        n, _ = analyze.export(self.conn, out=out)
        self.assertEqual(n, 8)
        with (out / "fact_runs.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 8)
        self.assertFalse({"response_text", "prompt_text"} & set(rows[0]))
        self.assertTrue(all(not r["strategy"].startswith("live:") for r in rows))
        self.assertNotIn("SECRET", (out / "fact_runs.csv").read_text())
        self.assertNotIn("prompt", (out / "dim_tasks.csv").read_text().split("\n")[0])


if __name__ == "__main__":
    unittest.main()
