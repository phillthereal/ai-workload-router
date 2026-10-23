import unittest

from jev_calibration import auroc, calibration, split_prompt


class CalibrationMath(unittest.TestCase):
    def test_perfectly_calibrated_has_zero_ece(self):
        # 10 items at p=0.8, 8 right -> calibrated
        pairs = [(0.8, 1)] * 8 + [(0.8, 0)] * 2
        t, ece = calibration(pairs)
        self.assertAlmostEqual(ece, 0.0)
        self.assertEqual(t[0][2], 10)

    def test_overconfident_ece(self):
        # always claims 0.9, right half the time -> ECE 0.4
        _, ece = calibration([(0.9, 1), (0.9, 0)] * 5)
        self.assertAlmostEqual(ece, 0.4)

    def test_weighted_over_bins(self):
        # 2 items bin [.5,.7) pred .6 acc 1 -> gap .4; 2 items bin [.95,1] pred .99 acc 1 -> gap .01; ECE=.205
        _, ece = calibration([(0.6, 1)] * 2 + [(0.99, 1)] * 2)
        self.assertAlmostEqual(ece, 0.205)

    def test_auroc(self):
        self.assertEqual(auroc([(0.9, 1), (0.2, 0)]), 1.0)
        self.assertEqual(auroc([(0.5, 1), (0.5, 0)]), 0.5)

    def test_split_prompt(self):
        cats = ["ACTION_NEEDED", "JOB_CONFIRMATION", "JOB_REJECTION", "RECRUITER_REPLY", "JOB_ALERT", "FYI", "IGNORE"]
        p = "Intro\n" + "".join(f"- {c}: desc {c}\n" for c in cats) + "\nEmail to classify:\nBODY\n\nRespond with:\n- x"
        crit, state = split_prompt(p)
        self.assertEqual(list(crit), cats)
        self.assertEqual(state, "BODY")


if __name__ == "__main__":
    unittest.main()
