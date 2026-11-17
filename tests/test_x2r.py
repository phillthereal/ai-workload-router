"""Offline tests for the X2 replication kit: task file, collector, analysis (mock mode, temp files, no network)."""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect = _load("x2r_collect", "experiments/x2r_collect.py")
analyze = _load("x2r_analyze", "experiments/x2r_analyze.py")
MOCK = {"AWR_FORCE_MOCK": "1"}


def _tasks(path):
    return json.loads(Path(path).read_text())["tasks"]


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class TaskFileTest(unittest.TestCase):
    def setUp(self):
        self.new = _tasks(ROOT / "data" / "tasks_x2r.json")
        self.old = [t for f in ("tasks", "tasks_hard", "tasks_v3") for t in _tasks(ROOT / "data" / f"{f}.json")]

    def test_count_and_unique_ids(self):
        self.assertEqual(len(self.new), 49)
        ids = [t["id"] for t in self.new]
        self.assertEqual(len(set(ids)), 49)
        self.assertTrue(all(i.startswith("x2r-") for i in ids))
        self.assertFalse(set(ids) & {t["id"] for t in self.old})

    def test_mix_equals_the_originals(self):
        key = lambda t: (t["task_type"], t["difficulty"], t["scoring"])  # noqa: E731
        self.assertEqual(Counter(map(key, self.new)), Counter(map(key, self.old)))

    def test_no_prompt_identical_to_an_original(self):
        old = {t["prompt"].strip().lower() for t in self.old}
        self.assertFalse([t["id"] for t in self.new if t["prompt"].strip().lower() in old])

    def test_schema_loads(self):
        from router.benchmark.tasks import load_tasks
        self.assertEqual(len(load_tasks(ROOT / "data" / "tasks_x2r.json")), 49)

    def test_reference_checker_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mod = _load("x2r_verify_refs", "experiments/x2r_verify_refs.py")
        self.assertGreaterEqual(len(mod.CHECKED), 20)


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        allt = _tasks(ROOT / "data" / "tasks_x2r.json")
        pick = [next(t for t in allt if t["id"] == i) for i in ("x2r-cls-01", "x2r-rsn-01", "x2r-gen-01")]
        self.tasks = self.tmp / "tasks3.json"
        self.tasks.write_text(json.dumps({"version": 2, "tasks": pick}))
        self.db = self.tmp / "run_simulated.db"
        self.argv = ["--tasks", str(self.tasks), "--db", str(self.db), "--cache-dir", str(self.tmp)]

    def rows(self):
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT task_id, model, simulated, roster, strategy FROM runs").fetchall()
        finally:
            c.close()

    def test_end_to_end_and_resume(self):
        with mock.patch.dict(os.environ, MOCK):
            self.assertEqual(_quiet(collect.main, self.argv), 0)
            rows = self.rows()
            self.assertEqual(len(rows), 9)  # 3 tasks x 3 tiers
            self.assertEqual({r[1] for r in rows}, {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"})
            self.assertTrue(all(r[2] == 1 and r[3] == "claude_tiers" and r[4] == "x2r_tier" for r in rows))
            cls = json.loads((self.tmp / "x2r_classifier_cache_simulated.json").read_text())
            jev = json.loads((self.tmp / "x2r_jev_cache_simulated.json").read_text())
            self.assertEqual(len(cls), 3)
            self.assertEqual(len(jev), 3)
            self.assertIn(next(iter(cls.values()))["tier"], ("budget", "mid", "frontier"))
            self.assertAlmostEqual(sum(jev[next(iter(jev))]["probs"].values()), 1.0)
            self.assertFalse((self.tmp / "x2r_jev_cache.json").exists())  # no real-named files from a mock run
            # resume: a second run must do no work
            with mock.patch.object(collect, "answer", side_effect=AssertionError("must not call")), \
                    mock.patch.object(collect, "JevClient", side_effect=AssertionError("must not call")):
                self.assertEqual(_quiet(collect.main, self.argv), 0)
            self.assertEqual(len(self.rows()), 9)

    def test_resume_after_partial_run(self):
        with mock.patch.dict(os.environ, MOCK):
            _quiet(collect.main, self.argv)
            c = sqlite3.connect(self.db)
            c.execute("DELETE FROM runs WHERE model='claude-sonnet-5' AND task_id='x2r-rsn-01'")
            c.commit()
            c.close()
            _quiet(collect.main, self.argv)
            self.assertEqual(len(self.rows()), 9)

    def test_default_names_are_simulated_without_live(self):
        p = collect.paths(False, None, self.tmp)
        self.assertEqual(p["db"].name, "x2r_runs_simulated.db")
        self.assertIn("simulated", p["jev"].name)
        self.assertIn("simulated", p["classifier"].name)
        self.assertEqual(collect.paths(True, None, self.tmp)["db"].name, "x2r_runs.db")

    def test_refusals(self):
        with mock.patch.dict(os.environ, MOCK):
            with self.assertRaises(SystemExit):  # --live while AWR_FORCE_MOCK is set
                _quiet(collect.main, self.argv[:2] + ["--live", "--db", str(self.tmp / "real.db")])
            with self.assertRaises(SystemExit):  # mock mode may not write to a non-simulated db name
                _quiet(collect.main, ["--tasks", str(self.tasks), "--db", str(self.tmp / "real.db")])
            with self.assertRaises(SystemExit):  # live may not write to a simulated db name
                collect.paths(True, str(self.tmp / "x_simulated.db"), self.tmp)
        env = {k: v for k, v in os.environ.items() if k != "AWR_FORCE_MOCK"}
        with mock.patch.dict(os.environ, env, clear=True):  # estimate over the cap: refuses before any call
            with mock.patch.object(collect, "answer", side_effect=AssertionError("no call allowed")):
                with self.assertRaises(SystemExit) as cm:
                    _quiet(collect.main, ["--tasks", str(self.tasks), "--db", str(self.tmp / "real.db"),
                                          "--cache-dir", str(self.tmp), "--live", "--cap", "0.0001"])
                self.assertIn("exceeds the cap", str(cm.exception))

    def test_probe_aborts_on_simulated_response(self):
        with mock.patch.dict(os.environ, MOCK):
            with self.assertRaises(collect.Abort):
                collect.probe(collect.Spend(1.0))

    def test_cap_stops_collection(self):
        with mock.patch.dict(os.environ, MOCK):
            self.assertEqual(_quiet(collect.main, self.argv + ["--cap", "0.0000001"]), 2)
        self.assertEqual(len(self.rows()), 0)

    def test_spend_cap_and_estimate(self):
        s = collect.Spend(0.01)
        s.add(0.005)
        with self.assertRaises(collect.CapHit):
            s.add(0.006)
        tasks = _tasks(ROOT / "data" / "tasks_x2r.json")
        est = collect.estimate(tasks, set(), {}, {})
        self.assertAlmostEqual(est["answers_budget"], 49 * 0.00037)
        self.assertLess(est["total"], 2.0)
        self.assertEqual(collect.estimate(tasks, {(t["id"], k) for t in tasks for k in collect.TIERS},
                                          {t["id"]: {} for t in tasks}, {t["id"]: {} for t in tasks})["total"], 0.0)


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        allt = _tasks(ROOT / "data" / "tasks_x2r.json")
        self.ids = ["x2r-cls-01", "x2r-rsn-01", "x2r-ext-01"]
        pick = [next(t for t in allt if t["id"] == i) for i in self.ids]
        self.tasks = self.tmp / "tasks3.json"
        self.tasks.write_text(json.dumps({"version": 2, "tasks": pick}))

    def _log(self, dbp, tid, tier, q, cost, simulated=False):
        from router import db
        model = {"budget": "claude-haiku-4-5", "mid": "claude-sonnet-5", "frontier": "claude-opus-4-8"}[tier]
        db.log_run(run_group="g", strategy="x2r_tier", task_id=tid, task_type="classification", difficulty="easy",
                   model=model, input_tokens=1, output_tokens=1, cost_usd=cost, latency_ms=1.0, quality_score=q,
                   success=True, simulated=simulated, roster="claude_tiers", db_path=dbp)

    def _synthetic(self):
        from router import db
        dbp = self.tmp / "synthetic.db"
        db.init_db(dbp)
        cost = {"budget": 0.001, "mid": 0.003, "frontier": 0.01}
        # A: budget fine -> GT budget; B: only mid fine -> GT mid; C: only frontier fine -> GT frontier
        quality = {self.ids[0]: (1, 1, 1), self.ids[1]: (0, 1, 1), self.ids[2]: (0, 0, 1)}
        for tid, qs in quality.items():
            for tier, q in zip(("budget", "mid", "frontier"), qs):
                self._log(dbp, tid, tier, q, cost[tier])
        (self.tmp / "x2r_classifier_cache.json").write_text(json.dumps({
            self.ids[0]: dict(tier="budget", cost=0.0005), self.ids[1]: dict(tier="budget", cost=0.0005),
            self.ids[2]: dict(tier="frontier", cost=0.0005)}))
        (self.tmp / "x2r_jev_cache.json").write_text(json.dumps({i: dict(
            choice="budget", probs={"budget": 0.95, "mid": 0.03, "frontier": 0.02}, cost=0.00002) for i in self.ids}))
        return dbp

    def test_known_answers(self):
        dbp = self._synthetic()
        lines = []
        r = analyze.analyze(str(dbp), self.tasks, self.tmp, out=lines.append)
        self.assertFalse(r["simulated"])
        self.assertEqual(r["n"], 3)
        self.assertEqual(r["gt"], {"budget": 1, "mid": 1, "frontier": 1})
        m = r["metrics"]
        self.assertAlmostEqual(m["a frontier-only"]["save"], 0.0)
        # d: budget, budget (under-routed), frontier -> cost 0.0015+0.0015+0.0105 over 0.03
        self.assertAlmostEqual(m["d haiku classifier"]["save"], 1 - 0.0135 / 0.03)
        self.assertEqual(m["d haiku classifier"]["under"], 1)
        self.assertAlmostEqual(m["d haiku classifier"]["acc"], 2 / 3)
        # f t=0.9: Jev is sure (p=0.95) and says budget everywhere -> 2 under-routed (tasks B and C)
        self.assertEqual(m["f jev t=0.9"]["under"], 2)
        self.assertAlmostEqual(m["f jev t=0.9"]["save"], 1 - 3 * (0.001 + 0.00002) / 0.03)
        text = "\n".join(lines)
        self.assertIn("PRIMARY", text)
        self.assertIn("VERDICT", text)
        self.assertNotIn("SIMULATED", text)
        self.assertEqual(len(r["excluded"]), 0)

    def test_missing_tier_task_is_excluded_and_listed(self):
        dbp = self._synthetic()
        c = sqlite3.connect(dbp)
        c.execute("DELETE FROM runs WHERE task_id=? AND model='claude-opus-4-8'", (self.ids[2],))
        c.commit()
        c.close()
        lines = []
        r = analyze.analyze(str(dbp), self.tasks, self.tmp, out=lines.append)
        self.assertEqual(r["n"], 2)
        self.assertEqual(r["excluded"], [self.ids[2]])
        self.assertIn("EXCLUDED", "\n".join(lines))

    def test_verdict_rule(self):
        v = analyze.verdict
        self.assertEqual(v(0.1, 0.02, 0.2, 0, 1), "REPLICATES")
        self.assertEqual(v(0.1, 0.02, 0.2, 1, 1), "REPLICATES")
        self.assertEqual(v(0.1, -0.01, 0.2, 0, 1), "DIRECTION HOLDS, NOT CONFIRMED")
        self.assertEqual(v(0.1, 0.02, 0.2, 2, 1), "DOES NOT REPLICATE")  # more under-routing
        self.assertEqual(v(-0.05, -0.1, 0.0, 0, 1), "DOES NOT REPLICATE")
        self.assertEqual(v(0.0, -0.1, 0.1, 0, 0), "DOES NOT REPLICATE")

    def test_refuses_simulated_database_without_flag(self):
        sim = self.tmp / "s_simulated.db"
        with mock.patch.dict(os.environ, MOCK):
            _quiet(collect.main, ["--tasks", str(self.tasks), "--db", str(sim), "--cache-dir", str(self.tmp)])
        with self.assertRaises(SystemExit) as cm:
            analyze.analyze(str(sim), self.tasks, self.tmp, out=lambda *_: None)
        self.assertIn("SIMULATED", str(cm.exception))
        with self.assertRaises(SystemExit):
            analyze.main(["--db", str(sim), "--tasks", str(self.tasks), "--cache-dir", str(self.tmp)])
        lines = []
        r = analyze.analyze(str(sim), self.tasks, self.tmp, allow_simulated=True, out=lines.append)
        self.assertTrue(r["simulated"])
        self.assertEqual(r["n"], 3)
        text = "\n".join(lines)
        self.assertIn("SIMULATED PRE-REGISTERED PRIMARY", text)
        self.assertGreaterEqual(text.count("SIMULATED"), 5)

    def test_simulated_rows_in_a_real_named_db_are_also_refused(self):
        dbp = self.tmp / "named_real.db"
        from router import db
        db.init_db(dbp)
        for tier in ("budget", "mid", "frontier"):
            self._log(dbp, self.ids[0], tier, 1, 0.001, simulated=True)
        with self.assertRaises(SystemExit):
            analyze.analyze(str(dbp), self.tasks, self.tmp, out=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
