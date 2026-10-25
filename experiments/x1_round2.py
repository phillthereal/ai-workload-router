"""X1 round 2: Jev+kNN agreement with parameters chosen UNDER an error budget (VAL wrong-decided <= budget). OFFLINE, $0.

Usage: python3 experiments/x1_round2.py
Seed 77 = original split. Seeds 11/22/33 = the 1,400 memoised (LEARN+VAL+TEST) items re-partitioned 300/300/800
(Jev decisions exist only for those items). Params per seed chosen on that seed's VAL (kNN pool = LEARN),
TEST scored once (pool = LEARN+VAL).
"""
from __future__ import annotations

import json, random, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import x1_thresholds_knn as X

T1_GRID = [round(0.40 + 0.05 * i, 2) for i in range(10)]    # 0.40 .. 0.85 (0.90 = baseline itself)
M_GRID = (1, 2, 3, 4, 5)
S_GRID = [round(0.10 + 0.05 * i, 2) for i in range(13)]     # 0.10 .. 0.70
SEEDS = (11, 22, 33)
TB_GRID = (0.90, 0.92, 0.94, 0.96, 0.98, 0.99)   # tight variant only: base-threshold may rise


def decide_agree(rows, infos, g=None):
    """Baseline 0.90 rule; if g=(t1, m, s[, tb]): base threshold tb (default 0.90), and also decide when Jev p>=t1 and
    >= m neighbours with sim>=s voted for its label. -> (decided flags, labels)."""
    dec, lab = [], []
    for r, (_, _, _, nb) in zip(rows, infos):
        ok = bool(r["choice"]) and r["p"] >= (g[3] if g and len(g) > 3 else X.T0)
        if not ok and g and r["choice"] and r["p"] >= g[0]:
            ok = sum(l == r["choice"] and s >= g[2] for s, l in nb) >= g[1]
        dec.append(ok); lab.append(r["choice"])
    return dec, lab


def wrong(rows, dec, lab):
    return sum(x and r["ref"] != l for r, x, l in zip(rows, dec, lab))


def select_constrained(rows, infos, budget, tb_grid=(X.T0,)):
    """Grid (t1, m, s, tb): max coverage subject to wrong-decided <= budget (ties: fewer wrong, then stricter params).
    Agreement only ADDS decisions, so a budget below baseline wrong needs tb_grid > 0.90 (raised base threshold).
    If nothing is feasible -> the min-wrong setting (feasible=False). -> (params, coverage, wrong, feasible)."""
    cand = []
    for tb in tb_grid:
        cand.append(None if tb == X.T0 else (1.0, 1, 1.0, tb))
        cand += [(t, m, s, tb) for t in T1_GRID for m in M_GRID for s in S_GRID]
    best = None
    for g in cand:
        d, l = decide_agree(rows, infos, g)
        w = wrong(rows, d, l)
        key = (w <= budget, sum(d) if w <= budget else -w, -w, g or (0, 0, 0, 0))
        if best is None or key > best[0]:
            best = (key, g, sum(d), w)
    return best[1], best[2] / len(rows), best[3], best[0][0]


def partition(ids, seed):
    ids = list(ids); random.Random(seed).shuffle(ids)
    return ids[:300], ids[300:600], ids[600:]


def evaluate(rows_of, text_of, learn, val, test):
    Lr, Vr, Er = (rows_of(x) for x in (learn, val, test))
    Lt, Vt, Et = (text_of(x) for x in (learn, val, test))
    iv = X.infos_for(X.Knn(Lt, [r["ref"] for r in Lr]), Vt)
    it = X.infos_for(X.Knn(Lt + Vt, [r["ref"] for r in Lr + Vr]), Et)
    bv = decide_agree(Vr, iv); w0v = wrong(Vr, *bv)
    base = decide_agree(Er, it); out = {}
    for name, budget, tbg in (("equal", w0v, (X.T0,)), ("tight90", int(0.9 * w0v), TB_GRID)):
        g, cov_v, w_v, feas = select_constrained(Vr, iv, budget, tbg)
        d, l = decide_agree(Er, it, g)
        ci = X.bootstrap_diff(Er, base, (d, l))
        s0, s = X.summarize(Er, base[0], base[1], [True] * len(Er)), X.summarize(Er, d, l, [True] * len(Er))
        out[name] = dict(params=g, val_wrong0=w0v, val_budget=budget, val_cov=cov_v, val_wrong=w_v, feasible=feas, base=s0, test=s, ci=ci,
                         verdict=X.verdict(ci["cov"], ci["acc"], s["wrong"], s0["wrong"]))
    return out


def main():
    import jev_loop_public as P
    from router.rulebook import Rulebook
    h = Rulebook.load(P.BASE).content_hash()
    memo = {tuple(json.loads(k)): v for k, v in json.loads((P.LOOP / "jev_memo.json").read_text()).items()}
    items, learn, val, test = P.splits(P.load_csv("train.csv"), P.load_csv("test.csv"))
    rows_of = lambda ids: [memo[(h, i)] for i in ids]
    text_of = lambda ids: [items[i][0] for i in ids]
    runs = {77: (learn, val, test)} | {s: partition(learn + val + test, s) for s in SEEDS}
    res = {s: evaluate(rows_of, text_of, *p) for s, p in runs.items()}
    for name in ("equal", "tight90"):
        print(f"\n### budget: {name}")
        print("| seed | params (t1,m,s) | VAL wrong base/budget/chosen | VAL cov | TEST cov base->new | dCov [CI] | dAcc [CI] | wrong base->new | verdict |")
        print("|---|---|---|---|---|---|---|---|---|")
        ds = []
        for s, r in res.items():
            x = r[name]; b, t, ci = x["base"], x["test"], x["ci"]
            ds.append(((t["cov"] - b["cov"]) * 100, (t["acc"] - b["acc"]) * 100, t["wrong"] - b["wrong"]))
            print(f"| {s} | {x['params']} | {x['val_wrong0']}/{x['val_budget']}/{x['val_wrong']}{'' if x['feasible'] else ' (INFEASIBLE)'} | {x['val_cov']:.3f} | "
                  f"{b['cov']:.3f}->{t['cov']:.3f} | {ds[-1][0]:+.1f}pp [{ci['cov'][0]*100:+.1f},{ci['cov'][1]*100:+.1f}] | "
                  f"{ds[-1][1]:+.1f}pp [{ci['acc'][0]*100:+.1f},{ci['acc'][1]*100:+.1f}] | {b['wrong']}->{t['wrong']} | {x['verdict']} |")
        n = len(ds)
        print(f"mean dCov {sum(d[0] for d in ds)/n:+.2f}pp, dAcc {sum(d[1] for d in ds)/n:+.2f}pp, dWrong {sum(d[2] for d in ds)/n:+.2f}")
        passes = [s for s, r in res.items() if r[name]["verdict"] == "PASS"]
        print(f"PASS seeds: {passes}; OVERALL: {'PASS' if 77 in passes and len(passes) >= 3 else 'FAIL'}")


if __name__ == "__main__":
    main()
