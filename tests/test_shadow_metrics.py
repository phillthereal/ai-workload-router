import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shadow_test as s


def row(ref, cat, conf=95, c=1.0, h=10.0):
    return dict(ref=ref, cat=cat, conf=conf, ccost=c, hcost=h)


class T(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(s.parse_answer('x {"output":{"category":"FYI","confidence":"90"}}'), ("FYI", 90))
        self.assertEqual(s.parse_answer('{"category":"FYI","confidence":7}'), ("FYI", 7))
        for bad in ("", "no json", '{"category":"NOPE","confidence":1}', '{"category":"FYI"}'):
            self.assertIsNone(s.parse_answer(bad))

    def test_cascade(self):
        rows = [row("IGNORE", "IGNORE"),                 # kept cheap, agrees
                row("FYI", "IGNORE"),                    # kept cheap, wrong
                row("ACTION_NEEDED", "IGNORE"),          # kept cheap, costly miss
                row("ACTION_NEEDED", "ACTION_NEEDED"),   # protected -> escalate
                row("IGNORE", "IGNORE", conf=50),        # low conf -> escalate
                row("FYI", None, conf=None)]             # parse fail -> escalate
        m = s.metrics(rows)
        self.assertEqual((m["parse_fail"], m["costly_miss"], m["casc_costly_miss"], m["casc_escalated"]), (1, 1, 1, 3))
        self.assertAlmostEqual(m["agree"][0], 3 / 6)
        self.assertAlmostEqual(m["casc_agree"][0], 4 / 6)
        self.assertEqual(m["recall"]["JOB_REJECTION"], (None, 0))
        self.assertEqual(m["casc_prot_recall"], (0.5, 2))
        self.assertAlmostEqual(m["casc_per_1k"], (6 + 30) / 6 * 1000)
        self.assertAlmostEqual(m["casc_savings"], 1 - 36 / 60)


if __name__ == "__main__":
    unittest.main()
