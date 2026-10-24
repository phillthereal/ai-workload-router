"""X1: per-class thresholds, TF-IDF kNN reuse, Jev+kNN agreement, vs the single 0.90 threshold. OFFLINE replay, $0.

Banking77 only (true labels; saved round-0 Jev decisions). Email is skipped: see docs/X1_THRESHOLDS_KNN.md.
Usage: python3 experiments/x1_thresholds_knn.py
"""
from __future__ import annotations

import itertools, json, math, random, re, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

T0 = 0.90
K = 5
JEV_PER_TOK = 0.042 / 1e6
FB_COST = (1200 * 0.15 + 30 * 0.60) / 1e6      # gpt-4o-mini fallback per undecided item (assumed 1.2k in / 30 out)
SHRINK_N = 10
P_GRID = [round(0.30 + 0.05 * i, 2) for i in range(15)] + [0.95, 0.99]   # 0.30 .. 0.99
S_GRID = [round(0.10 + 0.05 * i, 2) for i in range(17)]                   # 0.10 .. 0.90


# ---- TF-IDF kNN (stdlib) ----------------------------------------------------------------------------------

def _feats(text):
    t = text.lower()
    words = re.findall(r"\w+", t)
    p = f"  {' '.join(words)}  "
    return Counter(words), Counter(p[i:i + 3] for i in range(len(p) - 2))


def _norm(c, idf):
    v = {g: (1 + math.log(n)) * idf.get(g, 0.0) for g, n in c.items()}
    z = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {g: x / z for g, x in v.items()}


class Tfidf:
    """Word + char-3gram TF-IDF (sublinear tf, smooth idf, L2) fitted on `texts`; sim = mean of the two cosines."""

    def __init__(self, texts):
        fs = [_feats(t) for t in texts]
        n = len(fs)
        self.idf = []
        for k in (0, 1):
            df = Counter(g for f in fs for g in f[k])
            self.idf.append({g: math.log((1 + n) / (1 + d)) + 1 for g, d in df.items()})

    def vec(self, text):
        f = _feats(text)
        return (_norm(f[0], self.idf[0]), _norm(f[1], self.idf[1]))

    @staticmethod
    def sim(a, b):
        return 0.5 * sum(sum(x * b[k].get(g, 0.0) for g, x in a[k].items()) for k in (0, 1))


class Knn:
    def __init__(self, texts, labels):
        self.tf, self.labels = Tfidf(texts), list(labels)
        self.vecs = [self.tf.vec(t) for t in texts]

    def neighbours(self, text, k=K):
        q = self.tf.vec(text)
        s = sorted(((self.tf.sim(q, v), i) for i, v in enumerate(self.vecs)), key=lambda x: (-x[0], x[1]))[:k]
        return [(sim, self.labels[i]) for sim, i in s]


def knn_info(nb):
    """-> (majority label, votes for it, top-1 sim). Ties broken by higher summed similarity, then label."""
    c, w = Counter(l for _, l in nb), defaultdict(float)
    for s, l in nb:
        w[l] += s
    lab = max(c, key=lambda l: (c[l], w[l], l))
    return lab, c[lab], nb[0][0]


# ---- selection (all on the tuning split only) ---------------------------------------------------------------

def precision(rows, dec):
    d = [r for r, x in zip(rows, dec) if x]
    return (sum(r["choice"] == r["ref"] for r in d) / len(d)) if d else 1.0


def select_class_thresholds(rows, target, shrink_n=SHRINK_N, grid=P_GRID):
    """Per predicted class: lowest grid t with precision(p>=t) >= target (no t qualifies -> 0.90); classes with
    n < shrink_n tuning rows keep 0.90; others are shrunk t = 0.9 + n/(n+shrink_n) * (t_raw - 0.9)."""
    by = defaultdict(list)
    for r in rows:
        if r["choice"]:
            by[r["choice"]].append(r)
    out = {}
    for c, rs in by.items():
        n = len(rs)
        if n < shrink_n:
            continue
        raw = T0
        for t in grid:
            sel = [r for r in rs if r["p"] >= t]
            if sel and sum(r["choice"] == r["ref"] for r in sel) / len(sel) >= target:
                raw = t
                break
        out[c] = T0 + n / (n + shrink_n) * (raw - T0)
    return out


def decide(rows, infos, *, cls=None, knn=None, agree=None):
    """-> (decided flags, labels, jev_used flags). Pipeline order: kNN stage -> Jev with threshold (+agreement).
    knn=(s, m): kNN decides alone if votes>=m and top-1 sim>=s (Jev skipped). agree=(t1, m): Jev label with
    p>=t1 also decided if >= m of the 5 neighbours voted for it."""
    dec, lab, jev = [], [], []
    for r, (nl, nv, ns, nb) in zip(rows, infos):
        if knn and nv >= knn[1] and ns >= knn[0]:
            dec.append(True); lab.append(nl); jev.append(False); continue
        t = (cls or {}).get(r["choice"], T0)
        ok = bool(r["choice"]) and r["p"] >= t
        if not ok and agree and r["choice"]:
            ok = r["p"] >= agree[0] and sum(l == r["choice"] for _, l in nb) >= agree[1]
        dec.append(ok); lab.append(r["choice"]); jev.append(True)
    return dec, lab, jev


def _prec(rows, dec, lab):
    d = [(r, l) for r, x, l in zip(rows, dec, lab) if x]
    return (sum(r["ref"] == l for r, l in d) / len(d)) if d else 1.0


def best_grid(score_cov, grid):
    """grid of param tuples -> the one with max (coverage, precision) subject to precision >= target; None if none."""
    best = None
    for g in grid:
        cov, pr = score_cov(g)
        if pr is not None and (best is None or (cov, pr) > best[0]):
            best = ((cov, pr), g)
    return best[1] if best else None


def select_knn(rows, infos, target):
    def f(g):
        dec, lab, _ = decide(rows, infos, knn=g)
        pr = _prec(rows, dec, lab)
        return sum(dec) / len(rows), (pr if pr >= target else None)
    return best_grid(f, [(s, m) for s in S_GRID for m in (3, 4, 5)])


def select_agree(rows, infos, target, cls=None):
    def f(g):
        dec, lab, _ = decide(rows, infos, cls=cls, agree=g)
        pr = _prec(rows, dec, lab)
        return sum(dec) / len(rows), (pr if pr >= target else None)
    return best_grid(f, [(t, m) for t in P_GRID if t < T0 for m in (1, 2, 3, 4, 5)])


# ---- evaluation ---------------------------------------------------------------------------------------------

def summarize(rows, dec, lab, jev):
    n = len(rows)
    d = [r["ref"] == l for r, x, l in zip(rows, dec, lab) if x]
    cost = sum((r["tok"] * JEV_PER_TOK if j else 0) + (0 if x else FB_COST) for r, x, j in zip(rows, dec, jev))
    return dict(n=n, cov=len(d) / n, acc=(sum(d) / len(d)) if d else 1.0, wrong=len(d) - sum(d),
                skip_jev=1 - sum(jev) / n, per1k=cost / n * 1000)


def bootstrap_diff(rows, a, b, B=2000, seed=0):
    """Paired item bootstrap of (b - a) for coverage and accuracy-among-decided. a/b = (dec, lab) lists.
    -> dict cov=(lo,hi), acc=(lo,hi) in absolute fractions."""
    n = len(rows)
    ok = lambda p: [x and r["ref"] == l for r, x, l in zip(rows, *p)]
    da, ca, db, cb = a[0], ok(a), b[0], ok(b)
    rng, cd, ad = random.Random(seed), [], []
    for _ in range(B):
        ix = rng.choices(range(n), k=n)
        na, ka, nb_, kb = (sum(da[i] for i in ix), sum(ca[i] for i in ix),
                           sum(db[i] for i in ix), sum(cb[i] for i in ix))
        cd.append((nb_ - na) / n)
        ad.append((kb / nb_ if nb_ else 1.0) - (ka / na if na else 1.0))
    q = lambda v: (sorted(v)[int(0.025 * B)], sorted(v)[int(0.975 * B) - 1])
    return dict(cov=q(cd), acc=q(ad))


ACC_TOL = -0.01   # PASS needs the accuracy-difference CI lower bound >= -1.0 pp


def verdict(cov_ci, acc_ci, wrong, wrong0):
    if cov_ci[0] <= 0:
        return "FAIL (coverage gain not significant)"
    if acc_ci[0] < ACC_TOL:
        return "FAIL (accuracy CI < -1pp)"
    return "PASS" if wrong <= wrong0 else "PARTIAL (acc ok, but more wrong-decided items)"


# ---- data ---------------------------------------------------------------------------------------------------

def load():
    import jev_loop_public as P
    from router.rulebook import Rulebook
    h = Rulebook.load(P.BASE).content_hash()
    memo = {tuple(json.loads(k)): v for k, v in json.loads((P.LOOP / "jev_memo.json").read_text()).items()}
    items, learn, val, test = P.splits(P.load_csv("train.csv"), P.load_csv("test.csv"))
    rows = lambda ids: [memo[(h, i)] for i in ids]
    txt = lambda ids: [items[i][0] for i in ids]
    return dict(learn=(rows(learn), txt(learn)), val=(rows(val), txt(val)), test=(rows(test), txt(test)))


def infos_for(knn, texts):
    return [knn_info(nb) + (nb,) for nb in map(knn.neighbours, texts)]


def main():
    D = load()
    (Lr, Lt), (Vr, Vt), (Er, Et) = D["learn"], D["val"], D["test"]
    base_v = decide(Vr, [("", 0, 0, [])] * len(Vr))
    target = _prec(Vr, base_v[0], base_v[1])
    print(f"LEARN {len(Lr)} / VAL {len(Vr)} / TEST {len(Er)}; baseline VAL: cov {sum(base_v[0])/len(Vr):.3f} "
          f"precision(target) {target:.4f}")
    # tuning: kNN pool = LEARN, scored on VAL.  Final: pool = LEARN+VAL, scored on TEST.
    kv = Knn(Lt, [r["ref"] for r in Lr]); iv = infos_for(kv, Vt)
    cls = select_class_thresholds(Vr, target)
    kp = select_knn(Vr, iv, target)
    ag = select_agree(Vr, iv, target)
    print(f"idea1 class thresholds on {len(cls)} classes (n>={SHRINK_N}); mean {sum(cls.values())/max(len(cls),1):.3f}, "
          f"min {min(cls.values()):.3f}\nidea2 (s, min votes) = {kp}\nidea3 (t1, min votes) = {ag}")
    kt = Knn(Lt + Vt, [r["ref"] for r in Lr + Vr]); it = infos_for(kt, Et)
    nul = [("", 0, 0, [])] * len(Er)
    cfg = {"baseline 0.90": {}, "1 per-class thr": dict(cls=cls), "2 kNN alone-stage": dict(knn=kp),
           "3 Jev+kNN agree": dict(agree=ag)}
    # idea-2-alone as a pure classifier (no Jev), for reference
    pd = [nv >= kp[1] and ns >= kp[0] for _, nv, ns, _ in it]
    pl = [i[0] for i in it]
    print(f"\nkNN rule as the ONLY classifier on TEST (no Jev): coverage {sum(pd)/len(Er):.3f}, "
          f"accuracy {_prec(Er, pd, pl):.3f}")
    # combos, chosen on VAL (in-sample for tuned params; kNN pool=LEARN)
    names = ["1", "2", "3"]
    combos = {}
    for r in range(1, 4):
        for sub in itertools.combinations(names, r):
            kw = {}
            if "1" in sub: kw["cls"] = cls
            if "2" in sub: kw["knn"] = kp
            if "3" in sub: kw["agree"] = ag
            combos["+".join(sub)] = kw
    sel = []
    for nm, kw in combos.items():
        d, l, _ = decide(Vr, iv, **kw)
        if kp is None and "2" in nm or ag is None and "3" in nm: continue
        pr = _prec(Vr, d, l)
        if pr >= target: sel.append((sum(d) / len(Vr), pr, -len(nm), nm))
    best = max(sel)[3] if sel else None
    for nm, kw in combos.items():
        if (kp is None and "2" in nm) or (ag is None and "3" in nm): continue
        cfg[f"combo {nm}" + (" (VAL-best)" if nm == best else "")] = kw
    base = decide(Er, nul)
    res = {nm: decide(Er, it, **kw) for nm, kw in cfg.items()}
    s0 = summarize(Er, *base[:1], base[1], base[2])
    print("\n| pipeline | coverage | acc decided | wrong-decided | skip-Jev | $/1k | dCov [95% CI] | dAcc [95% CI] | verdict |")
    print("|---|---|---|---|---|---|---|---|---|")
    for nm, (d, l, j) in res.items():
        s = summarize(Er, d, l, j)
        if nm.startswith("baseline"):
            print(f"| {nm} | {s['cov']:.3f} | {s['acc']:.3f} | {s['wrong']} | {s['skip_jev']:.2f} | {s['per1k']:.4f} | - | - | - |")
            continue
        ci = bootstrap_diff(Er, (base[0], base[1]), (d, l))
        v = verdict(ci["cov"], ci["acc"], s["wrong"], s0["wrong"])
        print(f"| {nm} | {s['cov']:.3f} | {s['acc']:.3f} | {s['wrong']} | {s['skip_jev']:.2f} | {s['per1k']:.4f} | "
              f"{(s['cov']-s0['cov'])*100:+.1f}pp [{ci['cov'][0]*100:+.1f},{ci['cov'][1]*100:+.1f}] | "
              f"{(s['acc']-s0['acc'])*100:+.1f}pp [{ci['acc'][0]*100:+.1f},{ci['acc'][1]*100:+.1f}] | {v} |")
    print(f"\nbaseline wrong-decided {s0['wrong']}; VAL-best combo: {best}")


if __name__ == "__main__":
    main()
