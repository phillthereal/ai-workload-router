import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import prices  # noqa: E402
from router.config import MODELS, Roster  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "openrouter_models_sample.json"


def feed_doc(*entries):
    return json.dumps({"data": [{"id": i, "pricing": {"prompt": p, "completion": c}} for i, p, c in entries]})


class TestParse(unittest.TestCase):
    def test_fixture_parses_per_million(self):
        ok, bad = prices.parse_feed(FIXTURE.read_bytes())
        self.assertEqual(bad, {})
        self.assertAlmostEqual(ok["anthropic/claude-opus-4.8"][1], 25.0)

    def test_malformed_documents_raise(self):
        for raw in (b"{nope", b"[]", b'{"data": 3}'):
            with self.assertRaises(prices.FeedError):
                prices.parse_feed(raw)

    def test_bad_prices_rejected_not_accepted(self):
        raw = feed_doc(("a", "-1", "1"), ("b", "nan", "1"), ("c", "x", "1"), ("d", "1e-6", "2e-6"))
        ok, bad = prices.parse_feed(raw)
        self.assertEqual(set(bad), {"a", "b", "c"})
        self.assertEqual(set(ok), {"d"})

    def test_size_cap(self):
        with self.assertRaises(prices.FeedError):
            prices.parse_feed(b" " * (prices.MAX_BYTES + 1))


class TestDrift(unittest.TestCase):
    def rows(self, feed_entries, threshold=10.0):
        feed, bad = prices.parse_feed(feed_doc(*feed_entries))
        return {r["model"]: r for r in prices.drift_rows(feed, bad, threshold)}

    def test_statuses(self):
        r = self.rows([
            ("openai/gpt-4o-mini", "1.5e-7", "6e-7"),
            ("deepseek/deepseek-chat", "2.5e-7", "1.1e-6"),    # -7.4% -> OK
            ("anthropic/claude-sonnet-5", "2e-6", "1e-5"),     # -33% -> DRIFT
            ("anthropic/claude-opus-4.8", "5e-3", "2.5e-2"),   # > $1000/1M -> SUSPECT
            ("anthropic/claude-haiku-4.5", "1e-7", "5e-7"),    # 10x down -> SUSPECT
        ])
        self.assertEqual(r["gpt-4o-mini"]["status"], "OK")
        self.assertEqual(r["deepseek-chat"]["status"], "OK")
        self.assertEqual(r["claude-sonnet-5"]["status"], "DRIFT")
        self.assertEqual(r["claude-opus-4-8"]["status"], "SUSPECT")
        self.assertEqual(r["claude-haiku-4-5"]["status"], "SUSPECT")
        self.assertEqual(r["gemini-2.0-flash"]["status"], "NOT_FOUND")

    def test_threshold_is_configurable(self):
        e = [("deepseek/deepseek-chat", "2.5e-7", "1.1e-6")]
        self.assertEqual(self.rows(e, threshold=5)["deepseek-chat"]["status"], "DRIFT")

    def test_unmapped_and_rejected(self):
        feed, bad = prices.parse_feed(feed_doc(("x/y", "-1", "1")))
        rows = {r["model"]: r["status"]
                for r in prices.drift_rows(feed, bad, ids={"gpt-4o-mini": "x/y"})}
        self.assertEqual(rows["gpt-4o-mini"], "REJECTED")
        self.assertEqual(rows["deepseek-chat"], "UNMAPPED")

    def test_suspect_not_used_for_recost(self):
        r = self.rows([("anthropic/claude-opus-4.8", "5e-3", "2.5e-2")])
        self.assertNotIn("claude-opus-4-8", prices.feed_prices(list(r.values())))


class TestRecost(unittest.TestCase):
    def test_recost_math(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.db")
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE runs (run_group, strategy, model, input_tokens, output_tokens, "
                      "routing_cost_usd, simulated)")
            c.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", [
                ("g1-x", "frontier_only", "claude-opus-4-8", 1_000_000, 1_000_000, 0, 0),  # 30
                ("g1-x", "router", "gpt-4o-mini", 1_000_000, 1_000_000, 0, 0),             # .75
                ("g1-x", "router", "claude-opus-4-8", 1_000_000, 0, 0, 0),                 # 5
                ("g1-x", "router", "gpt-4o-mini", 9_000_000, 9_000_000, 0, 1),             # simulated
            ])
            c.commit()
            c.close()
            res = prices.recost(p, "g1", {"claude-opus-4-8": (10.0, 50.0)})
            self.assertEqual(res["n"], 3)
            self.assertAlmostEqual(res["config"]["router"], 5.75)
            self.assertAlmostEqual(res["config"]["savings_pct"], (30 - 5.75) / 30 * 100)
            self.assertAlmostEqual(res["feed"]["frontier"], 60.0)
            self.assertAlmostEqual(res["feed"]["router"], 10.75)
            self.assertEqual(res["fallback"], ["gpt-4o-mini"])


class TestLadder(unittest.TestCase):
    def test_order_change_detected(self):
        r = {"t": Roster("t", budget="gpt-4o-mini", mid="deepseek-chat", frontier="claude-opus-4-8")}
        self.assertFalse(prices.ladder_changes({}, r)[0]["changed"])
        res = prices.ladder_changes({"deepseek-chat": (9.0, 99.0)}, r)[0]
        self.assertTrue(res["changed"])
        self.assertEqual(res["feed"], ["budget", "frontier", "mid"])


if __name__ == "__main__":
    unittest.main()
