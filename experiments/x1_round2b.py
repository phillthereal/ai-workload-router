"""X1 round 2b: 4-seed robustness of round-1 passes (idea 2 kNN pre-stage, combo 1+2). OFFLINE, $0.
Same protocol as x1_round2.py (seed 77 original; 11/22/33 re-partition the 1,400 memoised items)."""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import x1_thresholds_knn as X
import x1_round2 as R


def run(rows_of, text_of, learn, val, test):
    Lr, Vr, Er = (rows_of(x) for x in (learn, val, test))
    Lt, Vt, Et = (text_of(x) for x in (learn, val, test))
    nul = lambda n: [("", 0, 0, [])] * n
    bd, bl, _ = X.decide(Vr, nul(len(Vr))); target = X._prec(Vr, bd, bl)
    iv = X.infos_for(X.Knn(Lt, [r["ref"] for r in Lr]), Vt)
    it = X.infos_for(X.Knn(Lt + Vt, [r["ref"] for r in Lr + Vr]), Et)
    cls, kp = X.select_class_thresholds(Vr, target), X.select_knn(Vr, iv, target)
    b = X.decide(Er, nul(len(Er))); s0 = X.summarize(Er, *b)
    out = {}
    for nm, kw in (("2 kNN", dict(knn=kp)), ("1+2", dict(cls=cls, knn=kp))):
        d, l, j = X.decide(Er, it, **kw); s = X.summarize(Er, d, l, j)
        ci = X.bootstrap_diff(Er, b[:2], (d, l))
        out[nm] = (kp, s0, s, ci, X.verdict(ci["cov"], ci["acc"], s["wrong"], s0["wrong"]))
    return out


def main():
    import jev_loop_public as P
    from router.rulebook import Rulebook
    h = Rulebook.load(P.BASE).content_hash()
    memo = {tuple(json.loads(k)): v for k, v in json.loads((P.LOOP / "jev_memo.json").read_text()).items()}
    items, learn, val, test = P.splits(P.load_csv("train.csv"), P.load_csv("test.csv"))
    ro, to = (lambda ids: [memo[(h, i)] for i in ids]), (lambda ids: [items[i][0] for i in ids])
    runs = {77: (learn, val, test)} | {s: R.partition(learn + val + test, s) for s in R.SEEDS}
    res = {s: run(ro, to, *p) for s, p in runs.items()}
    for nm in ("2 kNN", "1+2"):
        print(f"\n### {nm}\n| seed | (s,m) | TEST cov base->new | dCov [CI] | dAcc [CI] | wrong | skip-Jev | verdict |\n|---|---|---|---|---|---|---|---|")
        for s, r in res.items():
            kp, s0, t, ci, v = r[nm]
            print(f"| {s} | {kp} | {s0['cov']:.3f}->{t['cov']:.3f} | {(t['cov']-s0['cov'])*100:+.1f}pp [{ci['cov'][0]*100:+.1f},{ci['cov'][1]*100:+.1f}] | "
                  f"{(t['acc']-s0['acc'])*100:+.1f}pp [{ci['acc'][0]*100:+.1f},{ci['acc'][1]*100:+.1f}] | {s0['wrong']}->{t['wrong']} | {t['skip_jev']*100:.0f}% | {v} |")
        ps = [s for s, r in res.items() if r[nm][4] == "PASS"]
        print(f"PASS seeds {ps}; OVERALL {'PASS' if 77 in ps and len(ps) >= 3 else 'FAIL'}")


if __name__ == "__main__":
    main()
