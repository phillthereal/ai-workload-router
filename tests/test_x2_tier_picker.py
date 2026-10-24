import importlib.util
import unittest
from pathlib import Path

_p = Path(__file__).resolve().parents[1] / "experiments" / "x2_jev_tier_picker.py"
_s = importlib.util.spec_from_file_location("x2", _p)
x2 = importlib.util.module_from_spec(_s)
_s.loader.exec_module(x2)


class X2Test(unittest.TestCase):
    def test_synthetic_demo(self):
        x2.demo()

    def test_bootstrap_degenerate(self):
        per = [dict(id="a", tier="budget", gt={"budget"}, q=1, cost=.001, fq=1, fc=.01, acc=True, under=False)] * 5
        lo, hi = x2.bootstrap(per, "save", B=50)
        self.assertAlmostEqual(lo, 0.9)
        self.assertAlmostEqual(hi, 0.9)


if __name__ == "__main__":
    unittest.main()
