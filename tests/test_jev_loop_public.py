import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import jev_loop_public as P  # noqa: E402
from router.rulebook import Rulebook  # noqa: E402


class Splits(unittest.TestCase):
    def test_deterministic_disjoint_sizes(self):
        tr = [(f"t{i}", "a") for i in range(2000)]; te = [(f"e{i}", "b") for i in range(1500)]
        a, b = P.splits(tr, te), P.splits(tr, te)
        self.assertEqual(a[1:], b[1:])
        _, learn, val, test = a
        self.assertEqual((len(learn), len(val), len(test)), (P.N_LEARN, P.N_VAL, P.N_TEST))
        self.assertFalse(set(learn) & set(val))
        self.assertTrue(all(i.startswith("test:") for i in test))

    def test_real_data_present(self):
        tr, te = P.load_csv("train.csv"), P.load_csv("test.csv")
        self.assertEqual((len(tr), len(te)), (10003, 3080))
        self.assertEqual(len({c for _, c in tr}), 77)

    def test_base_rulebook_covers_all_77_intents(self):
        self.assertEqual(set(Rulebook.load(P.BASE).criteria), {c for _, c in P.load_csv("train.csv")})


def row(i, ref, choice, p):
    return dict(i=i, ref=ref, choice=choice, p=p, tok=1000)


class Loop(unittest.TestCase):
    def setUp(self):
        self.rb = Rulebook(1, "x", {"A": "a", "B": "b"})
        self.items = {f"s{i}": (f"please help me with my card number {i}", "A") for i in range(6)}
        self.ids = list(self.items)

    def evaluate(self, quality):
        """quality: {note-count: set of ids answered right (sure)}; wrong ones are wrong at p=.5"""
        def ev(rb, ids):
            ok = quality[len(rb.notes)]
            return [row(i, "A", "A", .95) if i in ok else row(i, "A", "B", .5) for i in ids]
        return ev

    def test_gate_uses_val_selection_picks_best_val_test_never_gates(self):
        learn, val, test = self.ids[:2], self.ids[2:4], self.ids[4:]
        q = {0: {"s0"}, 1: {"s0", "s2"}, 2: {"s0", "s2", "s3", "s4", "s5"}}  # round 2 is great on val, test irrelevant
        notes = iter(['{"edit":"note","text":"prefer intent A for first-type cases"}',
                      '{"edit":"note","text":"second generic rule about cases"}'])
        out = P.run_loop(self.items, learn, val, test, self.rb, self.evaluate(q), lambda p, r: next(notes), rounds=2)
        self.assertEqual([x["accepted"] for x in out], [None, True, True])
        self.assertEqual(P.select_final(out)["round"], 2)

    def test_rejects_and_stops_on_patience(self):
        q = {0: {"s0", "s2"}, 1: {"s0"}, 2: {"s0"}, 3: {"s0"}}  # every note makes val worse
        n = iter(f'{{"edit":"note","text":"rule {k} about things"}}' for k in range(9))
        out = P.run_loop(self.items, self.ids[:2], self.ids[2:4], self.ids[4:], self.rb, self.evaluate(q),
                         lambda p, r: next(n), rounds=8, patience=3)
        self.assertEqual(len(out), 4)  # baseline + 3 rejections
        self.assertTrue(all(x["accepted"] is False for x in out[1:]))
        self.assertEqual(P.select_final(out)["round"], 0)

    def test_teacher_cannot_copy_queries(self):
        out = P.run_loop(self.items, self.ids[:2], self.ids[2:4], self.ids[4:], self.rb,
                         self.evaluate({0: {"s0"}, 1: {"s0", "s2"}}),
                         lambda p, r: '{"edit":"note","text":"please help me with my card number 1"}', rounds=1)
        self.assertEqual(out[1]["reason"], "copies 6+ words from an email")

    def test_metrics_and_prompt(self):
        m = P.metrics([row("a", "A", "A", .95), row("b", "A", "B", .95), row("c", "A", "A", .6)])
        self.assertAlmostEqual(m["handled"], 2 / 3); self.assertAlmostEqual(m["agree_hi"], .5)
        self.assertAlmostEqual(m["score"], 1 / 3)
        tp = P.teacher_prompt(self.rb, [row("s1", "A", "B", .5)], self.items, [])
        self.assertIn("true=A classifier=B", tp)


class Misc(unittest.TestCase):
    def test_parse_mini(self):
        c = {"A": "a"}
        self.assertEqual(P.parse_mini('```{"category":"A","confidence":85}```', c), ("A", 85.0))
        self.assertEqual(P.parse_mini('{"category":"Z","confidence":85}', c), (None, None))
        self.assertEqual(P.parse_mini("nope", c), (None, None))

    def test_calib_summary(self):
        s = P.calib_summary([(.95, 1)] * 8 + [(.6, 0)] * 2, 10)
        self.assertAlmostEqual(s["coverage_090"], .8); self.assertEqual(s["acc_at_090"], 1.0)
        self.assertAlmostEqual(s["accuracy"], .8)


if __name__ == "__main__":
    unittest.main()
