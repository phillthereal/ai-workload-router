"""Shadow mode: RulesPolicy.dry_run, ShadowComparePolicy, the shadow column and the report script."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("AWR_FORCE_MOCK", "1")

from router import db, gates  # noqa: E402
from router.adapters.base import Response  # noqa: E402
from router.api import Ctx, Router, RulesPolicy, default_policy  # noqa: E402
from router.lookup import LABELLED_TASKS, Lookup  # noqa: E402
from router.router import route_task  # noqa: E402
from router.shadow import ShadowComparePolicy  # noqa: E402

TYPES = ("classification", "extraction", "short_generation", "reasoning")
SHADOW_KEYS = {"task_type", "difficulty", "would_call", "would_keep", "estimated_cost_usd",
               "frontier_cost_usd", "saving_usd", "basis"}

# The runs table exactly as it was before the shadow column existed.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_group TEXT NOT NULL,
    strategy TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    latency_ms REAL NOT NULL,
    quality_score REAL NOT NULL,
    success INTEGER NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0,
    response_text TEXT NOT NULL DEFAULT '',
    effort TEXT,
    roster TEXT NOT NULL DEFAULT 'cross_vendor',
    routing_cost_usd REAL NOT NULL DEFAULT 0.0,
    classifier_agreed INTEGER,
    verifier_scores TEXT,
    created_at TEXT,
    learned_evidence TEXT,
    quality_source TEXT,
    prompt_text TEXT,
    jev_confidence REAL,
    jev_escalated INTEGER
)
"""


def one_task_per_type() -> list[dict]:
    tasks = json.loads(LABELLED_TASKS.read_text())
    return [next(t for t in tasks if t["task_type"] == typ) for typ in TYPES]


class DryRunMirrorsRunTests(unittest.TestCase):
    def setUp(self):
        self.tasks = one_task_per_type()
        # k=1 over these tasks only: a task's own prompt is its nearest neighbour, so the label is known.
        self.policy = default_policy(lookup=Lookup(self.tasks, k=1))

    def assert_mirrors(self, policy, prompt):
        """dry_run's would_call must be the first models run actually asks Ctx.call for."""
        real_call = Ctx.call
        called: list[str] = []

        def spy(ctx_self, model, p, *a, **kw):
            called.append(model)
            return real_call(ctx_self, model, p, *a, **kw)

        router = Router(policy, allow_mock_fallback=True, use_cache=False)
        plan = policy.dry_run(prompt, {"id": "adhoc", "prompt": prompt}, Ctx(router))
        with mock.patch.object(Ctx, "call", spy):
            router.complete(prompt)
        self.assertTrue(plan.would_call)
        self.assertEqual(called[:len(plan.would_call)], plan.would_call)
        self.assertIn(plan.would_keep, plan.would_call)
        return plan

    def test_would_call_is_the_prefix_run_actually_called(self):
        for task in self.tasks:
            with self.subTest(task_type=task["task_type"]):
                plan = self.assert_mirrors(self.policy, task["prompt"])
                self.assertEqual(plan.task_type, task["task_type"])

    def test_context_gated_prompt(self):
        # Grow a prompt until the budget model's context gate refuses it (limits from gates/config).
        task = next(t for t in self.tasks if t["task_type"] == "extraction")
        budget = Router(self.policy).roster.budget
        prompt = task["prompt"]
        while gates.fits_context(budget, prompt):
            prompt += "\n" + task["prompt"]
        plan = self.assert_mirrors(self.policy, prompt)
        d = route_task({"id": "adhoc", "prompt": prompt}, task_type=plan.task_type,
                       difficulty=plan.difficulty)
        self.assertIn(budget, d.gated_models)
        self.assertFalse(plan.consensus)
        self.assertEqual(plan.would_call, [d.chosen_model])

    def test_type_in_frontier_on_and_consensus_on(self):
        task = next(t for t in self.tasks if t["task_type"] == "reasoning")
        policy = RulesPolicy(use_labels=False, frontier_on=("reasoning",), consensus_on=("reasoning",),
                             lookup=Lookup(self.tasks, k=1))
        plan = self.assert_mirrors(policy, task["prompt"])
        self.assertEqual(plan.task_type, "reasoning")
        self.assertTrue(plan.consensus)

    def test_dry_run_makes_no_model_call(self):
        router = Router(self.policy, allow_mock_fallback=True)
        with mock.patch.object(Ctx, "call", side_effect=AssertionError("dry_run called a model")):
            for task in self.tasks:
                self.policy.dry_run(task["prompt"], {"id": "t", "prompt": task["prompt"]}, Ctx(router))

    def test_dry_run_without_lookup_raises(self):
        policy = RulesPolicy(use_labels=False)
        with self.assertRaises(ValueError):
            policy.dry_run("hi", {"id": "t", "prompt": "hi"}, Ctx(Router(policy, allow_mock_fallback=True)))


class ShadowComparePolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dbp = Path(self._tmp.name) / "shadow.db"

    def test_answers_on_frontier_and_logs_the_estimate(self):
        router = Router(ShadowComparePolicy(default_policy()), allow_mock_fallback=True, log_to=self.dbp)
        r = router.complete("Classify the sentiment of: I love this. Labels: positive, negative.")
        self.assertEqual(r.model, router.roster.frontier)
        self.assertIsNotNone(r.shadow)
        self.assertEqual(set(r.shadow), SHADOW_KEYS)
        self.assertTrue(r.shadow["would_call"])
        self.assertAlmostEqual(r.shadow["saving_usd"],
                               r.shadow["frontier_cost_usd"] - r.shadow["estimated_cost_usd"])
        self.assertAlmostEqual(r.shadow["frontier_cost_usd"], r.answer_cost_usd)
        self.assertTrue(any(t.startswith("shadow: router would call") for t in r.trace))
        conn = sqlite3.connect(self.dbp)
        strategy, shadow = conn.execute("SELECT strategy, shadow FROM runs").fetchone()
        conn.close()
        self.assertEqual(strategy, "live:shadow")
        self.assertEqual(json.loads(shadow), r.shadow)
        self.assertEqual(db.shadow_rows(self.dbp), [])  # mock rows are simulated
        rows = db.shadow_rows(self.dbp, include_simulated=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shadow"], r.shadow)
        self.assertTrue(rows[0]["simulated"])

    def test_estimate_counts_the_routers_suffix_tokens(self):
        tasks = one_task_per_type()
        policy = ShadowComparePolicy(default_policy(lookup=Lookup(tasks, k=1)))
        base = next(t for t in tasks if t["task_type"] == "extraction")["prompt"].replace("Answer:", "")
        fixed = Response("x", 400, 60, 1.0, "claude-opus-4-8", True)  # same frontier token counts
        shadows = {}
        with mock.patch.object(Ctx, "call", return_value=fixed):
            for name, prompt in (("bare", base), ("with_answer", base + "\nAnswer:")):
                shadows[name] = Router(policy, allow_mock_fallback=True).complete(prompt).shadow
        for sh in shadows.values():
            self.assertEqual(sh["task_type"], "extraction")
            self.assertEqual(sh["frontier_cost_usd"], shadows["bare"]["frontier_cost_usd"])
        self.assertGreater(shadows["bare"]["estimated_cost_usd"], shadows["with_answer"]["estimated_cost_usd"])

    def test_estimate_failure_still_returns_the_frontier_answer(self):
        inner = default_policy()
        with mock.patch.object(inner, "dry_run", side_effect=KeyError("boom")):
            r = Router(ShadowComparePolicy(inner), allow_mock_fallback=True).complete("hello")
        self.assertEqual(r.model, "claude-opus-4-8")
        self.assertTrue(r.text)
        self.assertIsNone(r.shadow)
        self.assertIn("shadow: no estimate (KeyError)", r.trace)

    def test_other_policies_leave_shadow_empty(self):
        router = Router(default_policy(), allow_mock_fallback=True, log_to=self.dbp)
        self.assertIsNone(router.complete("hello").shadow)
        conn = sqlite3.connect(self.dbp)
        self.assertEqual(conn.execute("SELECT shadow FROM runs").fetchone(), (None,))
        conn.close()
        self.assertEqual(db.shadow_rows(self.dbp, include_simulated=True), [])


class ShadowReportScriptTests(unittest.TestCase):
    SCRIPT = ROOT / "scripts" / "shadow_report.py"

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, timeout=60)

    def test_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "shadow.db"
            router = Router(ShadowComparePolicy(), allow_mock_fallback=True, log_to=dbp)
            for p in ("What is 17 * 23?", "Write a haiku about rain."):
                router.complete(p)
            res = self.run_script(str(dbp), "--include-simulated")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("requests", res.stdout)
            self.assertIn("SIMULATED", res.stdout)
            self.assertTrue(all(line.startswith("SIMULATED ") for line in res.stdout.splitlines()))
            self.assertIn("quality is unknown", res.stdout)
            res = self.run_script(str(dbp))
            self.assertEqual(res.returncode, 1)
            self.assertIn("no shadow rows", res.stdout + res.stderr)


class ShadowMigrationTests(unittest.TestCase):
    def test_old_db_gains_the_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "old.db"
            conn = sqlite3.connect(dbp)
            conn.execute(OLD_SCHEMA)
            conn.commit()
            conn.close()
            self.assertEqual(db.shadow_rows(dbp, include_simulated=True), [])  # no column yet: []
            db.init_db(dbp)
            conn = sqlite3.connect(dbp)
            cols = [row[1] for row in conn.execute("PRAGMA table_info(runs)")]
            conn.close()
            self.assertIn("shadow", cols)
            self.assertEqual(db.shadow_rows(dbp, include_simulated=True), [])
            r = Router(ShadowComparePolicy(), allow_mock_fallback=True, log_to=dbp).complete("hello")
            rows = db.shadow_rows(dbp, include_simulated=True)
            self.assertEqual([row["shadow"] for row in rows], [r.shadow])

    def test_missing_db_gives_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(db.shadow_rows(Path(tmp) / "nope.db"), [])

    def test_directory_or_non_sqlite_path_gives_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(db.shadow_rows(tmp, include_simulated=True), [])
            junk = Path(tmp) / "junk.db"
            junk.write_text("not a database\n" * 100)
            self.assertEqual(db.shadow_rows(junk, include_simulated=True), [])


class UnreadableEstimateTests(unittest.TestCase):
    def test_malformed_shadow_values_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "shadow.db"
            r = Router(ShadowComparePolicy(), allow_mock_fallback=True, log_to=dbp).complete("hello")
            base = dict(run_group="live", strategy="live:shadow", task_type="x", difficulty="y",
                        model="m", input_tokens=1, output_tokens=1, cost_usd=0.1, latency_ms=1.0,
                        quality_score=0.0, success=True, simulated=True, db_path=dbp)
            db.log_run(task_id="no-keys", shadow={"would_call": ["m"]}, **base)
            db.log_run(task_id="bad-json", shadow={}, **base)
            conn = sqlite3.connect(dbp)
            conn.execute("UPDATE runs SET shadow = '{not json' WHERE task_id = 'bad-json'")
            conn.commit()
            conn.close()
            rows, skipped = db.shadow_rows_counted(dbp, include_simulated=True)
            self.assertEqual([row["shadow"] for row in rows], [r.shadow])
            self.assertEqual(skipped, 2)
            res = subprocess.run([sys.executable, str(ShadowReportScriptTests.SCRIPT), str(dbp),
                                  "--include-simulated"], capture_output=True, text=True, timeout=60)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("2 rows skipped: unreadable estimate", res.stdout)


if __name__ == "__main__":
    unittest.main()
