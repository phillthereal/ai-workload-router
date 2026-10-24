import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
import x1_thresholds_knn as X  # noqa: E402


def rows(spec):
    """spec: list of (choice, p, correct)."""
    return [dict(i=str(k), ref=c if ok else "other", choice=c, p=p, tok=100) for k, (c, p, ok) in enumerate(spec)]


class Thresholds(unittest.TestCase):
    def test_lowers_when_precise_and_shrinks(self):
        # class A: 20 rows, perfectly right down to p=0.5 -> raw 0.30; w=20/30 -> 0.9-0.6*(0.6)=0.54
        a = rows([("A", 0.5 + 0.02 * k, True) for k in range(20)])
        t = X.select_class_thresholds(a, target=0.9)
        self.assertAlmostEqual(t["A"], 0.9 + 20 / 30 * (0.30 - 0.9), places=6)

    def test_small_class_keeps_default(self):
        self.assertEqual(X.select_class_thresholds(rows([("B", 0.5, True)] * 9), 0.9), {})

    def test_no_threshold_qualifies_keeps_default(self):
        bad = rows([("C", 0.95, False)] * 12)
        self.assertAlmostEqual(X.select_class_thresholds(bad, 0.9)["C"], 0.9)

    def test_decide_uses_class_threshold(self):
        r = rows([("A", 0.6, True), ("B", 0.6, True)])
        dec, _, _ = X.decide(r, [("", 0, 0, [])] * 2, cls={"A": 0.5})
        self.assertEqual(dec, [True, False])


class Knn(unittest.TestCase):
    def setUp(self):
        self.k = X.Knn(["card not arrived yet", "my card has not arrived", "card still not here arrived",
                        "lost my card arrived never", "card arrival late", "transfer failed to send money",
                        "money transfer did not go through"],
                       ["arr"] * 5 + ["xfer"] * 2)

    def test_nearest_label(self):
        lab, votes, sim = X.knn_info(self.k.neighbours("when will my card arrive, it has not arrived"))
        self.assertEqual(lab, "arr"); self.assertGreaterEqual(votes, 4); self.assertGreater(sim, 0.2)

    def test_identical_text_sim_one(self):
        self.assertAlmostEqual(self.k.neighbours("card arrival late")[0][0], 1.0, places=6)

    def test_agreement_stage_skips_jev(self):
        r = rows([("xfer", 0.4, True)])
        info = [("arr", 5, 0.9, [(0.9, "arr")] * 5)]
        dec, lab, jev = X.decide(r, info, knn=(0.5, 4))
        self.assertEqual((dec, lab, jev), ([True], ["arr"], [False]))
        # agreement path: Jev label p>=t1 and >=2 neighbours back it
        info = [("x", 2, 0.1, [(0.3, "xfer"), (0.2, "xfer"), (0.1, "a"), (0.1, "b"), (0.1, "c")])]
        self.assertEqual(X.decide(r, info, agree=(0.35, 2))[0], [True])
        self.assertEqual(X.decide(r, info, agree=(0.5, 2))[0], [False])

    def test_bootstrap_identical_is_zero(self):
        r = rows([("A", 0.95, True)] * 30)
        d = ([True] * 30, ["A"] * 30)
        ci = X.bootstrap_diff(r, d, d, B=50)
        self.assertEqual(ci["cov"], (0.0, 0.0)); self.assertEqual(ci["acc"], (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
