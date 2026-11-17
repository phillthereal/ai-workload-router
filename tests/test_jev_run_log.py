"""Jev confidence + escalated flag in the run log (offline: scripted Jev, fake adapter)."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import db  # noqa: E402
from router.api import Router, RulesPolicy  # noqa: E402
from router.jev import JevClassifierPolicy  # noqa: E402
from router.rulebook import Rulebook  # noqa: E402
from tests.test_jev import BASE, Scripted, fallback_factory  # noqa: E402

OLD_SCHEMA = """CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_group TEXT NOT NULL, strategy TEXT NOT NULL,
    task_id TEXT NOT NULL, task_type TEXT NOT NULL, difficulty TEXT NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
    latency_ms REAL NOT NULL, quality_score REAL NOT NULL, success INTEGER NOT NULL)"""


def columns(path):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    finally:
        conn.close()


class JevRunLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "runs.db"
        self.rb = Rulebook.load(BASE)

    def tearDown(self):
        self.dir.cleanup()

    def rows(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("SELECT jev_confidence, jev_escalated FROM runs").fetchall()
        finally:
            conn.close()

    def test_fresh_db_has_columns(self):
        db.init_db(self.path)
        self.assertLessEqual({"jev_confidence", "jev_escalated"}, columns(self.path))

    def test_old_db_upgraded_in_place_rows_intact(self):
        conn = sqlite3.connect(self.path)
        conn.execute(OLD_SCHEMA)
        conn.execute("INSERT INTO runs (run_group, strategy, task_id, task_type, difficulty, model,"
                     " input_tokens, output_tokens, cost_usd, latency_ms, quality_score, success)"
                     " VALUES ('g','s','t1','qa','easy','m',10,5,0.01,100.0,0.9,1)")
        conn.commit()
        conn.close()
        self.assertNotIn("jev_confidence", columns(self.path))
        db.init_db(self.path)
        db.init_db(self.path)  # opening again is harmless
        self.assertLessEqual({"jev_confidence", "jev_escalated"}, columns(self.path))
        conn = sqlite3.connect(self.path)
        row = conn.execute("SELECT task_id, cost_usd, quality_score, jev_confidence, jev_escalated FROM runs").fetchall()
        conn.close()
        self.assertEqual(row, [("t1", 0.01, 0.9, None, None)])

    def test_jev_handled_run_stores_confidence_and_not_escalated(self):
        policy = JevClassifierPolicy(self.rb, 0.9, (), client=Scripted("FYI", 0.97))
        with mock.patch("router.api.get_adapter", fallback_factory()):
            Router(policy, log_to=self.path).complete("hello")
        self.assertEqual(self.rows(), [(0.97, 0)])

    def test_jev_escalated_run_stores_confidence_and_escalated(self):
        policy = JevClassifierPolicy(self.rb, 0.9, (), client=Scripted("FYI", 0.6))
        with mock.patch("router.api.get_adapter", fallback_factory()):
            Router(policy, log_to=self.path).complete("hello")
        self.assertEqual(self.rows(), [(0.6, 1)])

    def test_jev_failure_stores_null_confidence_but_escalated(self):
        policy = JevClassifierPolicy(self.rb, 0.9, (), client=Scripted("FYI", 0.99, ok=False))
        with mock.patch("router.api.get_adapter", fallback_factory()):
            Router(policy, log_to=self.path).complete("hello")
        self.assertEqual(self.rows(), [(None, 1)])

    def test_non_jev_run_stores_nulls(self):
        Router(RulesPolicy(use_labels=False), allow_mock_fallback=True, log_to=self.path).complete("hello")
        self.assertEqual(self.rows(), [(None, None)])


if __name__ == "__main__":
    unittest.main()
