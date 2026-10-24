import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import jev_loop as L  # noqa: E402
from router.rulebook import Rulebook  # noqa: E402


def m(handled=.5, agree_hi=1.0, agree=.8, miss=()):
    return dict(handled=handled, agree_hi=agree_hi, agree=agree, miss=list(miss))


def row(i, ref, choice, p):
    return dict(i=i, ref=ref, choice=choice, p=p, tok=700, hcost=0.0005)


class Gate(unittest.TestCase):
    def test_accepts_handled_gain(self):
        self.assertTrue(L.gate(m(), m(handled=.6))[0])

    def test_accepts_agreement_gain_only(self):
        self.assertTrue(L.gate(m(), m(agree=.85))[0])

    def test_rejects_hi_agreement_drop(self):
        self.assertFalse(L.gate(m(), m(handled=.7, agree_hi=.95))[0])

    def test_rejects_new_costly_miss_but_allows_existing(self):
        self.assertFalse(L.gate(m(miss=[1]), m(handled=.7, miss=[1, 2]))[0])
        self.assertTrue(L.gate(m(miss=[1]), m(handled=.7, miss=[1]))[0])

    def test_rejects_no_gain(self):
        self.assertFalse(L.gate(m(), m())[0])


class Metrics(unittest.TestCase):
    def test_metrics(self):
        rows = [row(0, "IGNORE", "IGNORE", .95), row(1, "ACTION_NEEDED", "IGNORE", .92),
                row(2, "FYI", "IGNORE", .6), row(3, "ACTION_NEEDED", "ACTION_NEEDED", .99)]
        x = L.metrics(rows)
        self.assertEqual(x["handled"], .5)       # rows 0,1 (row 3 is protected)
        self.assertEqual(x["n_hi"], 3)
        self.assertAlmostEqual(x["agree_hi"], 2 / 3)
        self.assertEqual(x["miss"], [1])
        self.assertEqual(x["agree"], .5)


class Split(unittest.TestCase):
    def test_chronological_and_disjoint(self):
        calls = [dict(started=f"2026-08-0{d}") for d in (5, 1, 4, 2, 3, 6)]
        tr, ho = L.split_chrono(calls)
        self.assertEqual(len(tr), 4)
        self.assertFalse(set(tr) & set(ho))
        self.assertLess(max(calls[i]["started"] for i in tr), min(calls[i]["started"] for i in ho))


class Proposals(unittest.TestCase):
    rb = Rulebook.load(L.BASE)

    def test_parse_and_apply(self):
        p = L.parse_proposal('x {"edit":"note","text":"Shipping updates are FYI."} y', self.rb)
        self.assertEqual(L.apply_proposal(self.rb, p).notes, ["Shipping updates are FYI."])
        p = L.parse_proposal('{"edit":"criterion","option":"FYI","text":"new"}', self.rb)
        self.assertEqual(L.apply_proposal(self.rb, p).criteria["FYI"], "new")
        self.assertIsNone(L.parse_proposal('{"edit":"criterion","option":"NOPE","text":"t"}', self.rb))
        self.assertIsNone(L.parse_proposal("garbage", self.rb))

    def test_leak_guard(self):
        g = L.grams("please send the quarterly invoice to the finance team today")
        self.assertIsNotNone(L.leak("mail a@b.co", g))
        self.assertIsNotNone(L.leak("see http://x", g))
        self.assertIsNotNone(L.leak("send the quarterly invoice to the finance", g))
        self.assertIsNone(L.leak("Bills that need payment are action items.", g))


class Loop(unittest.TestCase):
    """Scripted Jev: an edit mentioning 'receipts' lifts item 2 to sure; 'bad' breaks sure item 0."""
    calls = [dict(started=f"2026-08-0{i+1}", cat="FYI" if i == 2 else "IGNORE",
                  prompt="- ACTION_NEEDED: a\n- JOB_CONFIRMATION: a\n- JOB_REJECTION: a\n- RECRUITER_REPLY: a\n"
                         f"- JOB_ALERT: a\n- FYI: a\n- IGNORE: a\n\nEmail to classify:\nbody {i}\n\nRespond with:\nx",
                  pt=1, ct=1) for i in range(6)]

    def evaluate(self, rb, idxs):
        txt = " ".join(rb.notes)
        out = []
        for i in idxs:
            ref = self.calls[i]["cat"]
            if "bad" in txt:
                choice, p = ("FYI", .95) if i == 0 else (ref, .95)
            elif i == 3:
                choice, p = ref, .6   # always unsure: keeps the loop supplied with examples
            elif i == 2:
                choice, p = (ref, .95) if "receipts" in txt else ("IGNORE", .6)
            else:
                choice, p = ref, .95
            out.append(dict(i=i, ref=ref, choice=choice, p=p, tok=700, hcost=.0005))
        return out

    def test_accepts_good_rejects_bad_and_stops(self):
        proposals = iter(['{"edit":"note","text":"Store receipts are FYI."}',
                          '{"edit":"note","text":"bad idea one"}',
                          '{"edit":"note","text":"bad idea two"}',
                          "garbage"])
        res = L.run_loop(self.calls, Rulebook.load(L.BASE), self.evaluate, lambda p: next(proposals), rounds=8)
        self.assertEqual([r["accepted"] for r in res], [None, True, False, False, False])
        self.assertEqual(res[-1]["version"], 2)          # only the accepted edit bumped the version
        self.assertGreater(res[1]["train"]["handled"], res[0]["train"]["handled"])


if __name__ == "__main__":
    unittest.main()
