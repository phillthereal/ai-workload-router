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

from router import db  # noqa: E402
from router.api import Ctx, Router, RulesPolicy, default_policy  # noqa: E402
from router.lookup import LABELLED_TASKS, Lookup  # noqa: E402
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

    def test_would_call_is_the_prefix_run_actually_called(self):
        real_call = Ctx.call
        for task in self.tasks:
            called: list[str] = []

            def spy(ctx_self, model, prompt, *a, _called=called, **kw):
                _called.append(model)
                return real_call(ctx_self, model, prompt, *a, **kw)

            router = Router(self.policy, allow_mock_fallback=True)
            with self.subTest(task_type=task["task_type"]), mock.patch.object(Ctx, "call", spy):
                plan = self.policy.dry_run(task["prompt"], {"id": "t", "prompt": task["prompt"]},
                                           Ctx(router))
                self.assertEqual(plan.task_type, task["task_type"])
                router.complete(task["prompt"])
                self.assertTrue(plan.would_call)
                self.assertEqual(called[:len(plan.would_call)], plan.would_call)
                self.assertIn(plan.would_keep, plan.would_call)

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


if __name__ == "__main__":
    unittest.main()
