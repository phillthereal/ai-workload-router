import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
import x1_round2 as R  # noqa: E402


def row(choice, p, ok):
    return dict(ref=choice if ok else "z", choice=choice, p=p, tok=1)


NB = lambda lab, n, s=0.5: [(s, lab)] * n + [(0.0, "q")] * (5 - n)


class Constrained(unittest.TestCase):
    def setUp(self):
        # r0 baseline-decided right; r1,r2 low-p right with 3 votes; r3 low-p wrong with 3 votes; r4 low-p wrong, 0 votes
        self.rows = [row("a", 0.95, True), row("a", 0.6, True), row("a", 0.6, True), row("a", 0.6, False), row("a", 0.6, False)]
        self.infos = [("", 0, 0, NB("a", 5))] + [("", 0, 0, NB("a", 3))] * 3 + [("", 0, 0, NB("a", 0))]

    def test_budget_respected_and_maximises(self):
        g, cov, w, ok = R.select_constrained(self.rows, self.infos, budget=0)
        self.assertEqual((cov, w, ok), (0.2, 0, True))  # r3 has the same votes as r1/r2: no error-free gain
        g, cov, w, ok = R.select_constrained(self.rows, self.infos, budget=1)
        self.assertEqual((cov, w, ok), (0.8, 1, True))

    def test_infeasible_unless_base_threshold_raised(self):
        rows = [row("a", 0.95, False), row("a", 0.95, True)]
        infos = [("", 0, 0, NB("a", 0))] * 2
        self.assertFalse(R.select_constrained(rows, infos, budget=0)[3])
        self.assertTrue(R.select_constrained(rows, infos, budget=0, tb_grid=(0.9, 0.99))[3])

    def test_sim_floor_filters_votes(self):
        d, _ = R.decide_agree(self.rows, self.infos, (0.4, 3, 0.6))
        self.assertEqual(d, [True, False, False, False, False])


if __name__ == "__main__":
    unittest.main()
