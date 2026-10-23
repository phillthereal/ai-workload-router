"""Is TypeSafe Jev's confidence calibrated on real email classification? (Step 0 go/no-go)

Usage:  python3 jev_calibration.py run    <real-traffic-dir> [--cap 0.10]   # live Jev calls (needs TYPESAFE_API_KEY)
        python3 jev_calibration.py report <real-traffic-dir>                # aggregates only, no calls

Reads the private calls.jsonl via shadow_test.load() (same indices as the shadow test). Raw responses go to
<dir>/jev/jev.jsonl; stdout carries aggregates only. Does NOT use the repo adapters or .cache/.
Haiku is the REFERENCE label, not ground truth.

Prompt split (identical for every email): `criteria` = the 7 category lines of the production prompt,
`state` = the text between "Email to classify:" and "Respond with:", `instructions` = one fixed line.
"""
from __future__ import annotations

import json, os, re, statistics, sys, time, urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "src"))
from shadow_test import CATS, HAIKU, PROTECTED, bootstrap, cost, load, rows_for  # noqa: E402

URL = "https://api.typesafe.ai/v1/systemone"
JEV_PER_TOK = 0.042 / 1e6
HAIKU_1K = 1.68  # shadow-test basis, USD per 1k emails
INSTRUCTIONS = "Classify the email into exactly one category."
BINS = [0, .5, .7, .8, .9, .95, 1.0001]
COSTLY = ("IGNORE", "FYI")


def split_prompt(p):
    """-> (criteria dict, state text). Raises if the production prompt shape changed."""
    crit = dict(re.findall(r"^- ([A-Z_]+): (.+)$", p.split("Email to classify:")[0], re.M))
    assert list(crit) == CATS and "Respond with:" in p, "unexpected prompt shape"
    return crit, p.split("Email to classify:\n", 1)[1].split("\n\nRespond with:")[0]


def key():
    for l in open(Path("/Users/caio/Ai Workload/.env")):
        if l.startswith("TYPESAFE_API_KEY="):
            return l.split("=", 1)[1].strip().strip("'\"")
    sys.exit("no TYPESAFE_API_KEY")


def call(k, crit, state):
    body = {"state": state, "model": "jev-latest", "questions": {"category": {
        "type": "choice", "instructions": INSTRUCTIONS, "criteria": crit}}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Authorization": f"Bearer {k}", "Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    out["latency"] = time.time() - t
    return out


def run(d, cap):
    calls, k, spent = load(d), key(), 0.0
    os.makedirs(Path(d) / "jev", exist_ok=True)
    p = Path(d) / "jev" / "jev.jsonl"
    done = {json.loads(l)["i"] for l in open(p)} if p.exists() else set()
    with open(p, "a") as f:
        for i, x in enumerate(calls):
            if i in done:
                continue
            crit, state = split_prompt(x["prompt"])
            try:
                o = call(k, crit, state)
                spent += o["usage"]["input_tokens"] * JEV_PER_TOK
                rec = dict(i=i, ok=True, resp=o)
            except Exception as e:  # never echo the body: may contain email text
                rec = dict(i=i, ok=False, err=type(e).__name__)
            f.write(json.dumps(rec) + "\n"); f.flush()
            if spent > cap * 0.9:
                sys.exit(f"stopping near cap: ${spent:.5f}")
    print(f"spent ${spent:.5f}")


def jev_rows(d, calls):
    ans = {}
    for l in open(Path(d) / "jev" / "jev.jsonl"):
        a = json.loads(l); ans[a["i"]] = a
    rows = []
    for i, x in enumerate(calls):
        a = ans.get(i)
        if not a or not a["ok"]:
            continue
        r = a["resp"]; q = r["answers"]["category"]; pr = q["probabilities"]
        rows.append(dict(ref=x["cat"], cat=q["choice"], p=max(pr.values()), conf=q["confidence"],
                         tok=r["usage"]["input_tokens"], lat=r["latency"],
                         hcost=cost(HAIKU, x["pt"], x["ct"]), model=r.get("model")))
    return rows


def calibration(pairs):
    """pairs: [(predicted prob, correct 0/1)] -> (reliability table, ECE)."""
    n, table, ece = len(pairs), [], 0.0
    for lo, hi in zip(BINS, BINS[1:]):
        b = [(p, c) for p, c in pairs if lo <= p < hi]
        if not b:
            continue
        mp, acc = sum(p for p, _ in b) / len(b), sum(c for _, c in b) / len(b)
        ece += len(b) / n * abs(mp - acc)
        table.append((lo, min(hi, 1.0), len(b), mp, acc))
    return table, ece


def auroc(pairs):
    """P(a right answer has higher confidence than a wrong one); 0.5 = confidence carries no signal."""
    pos = [p for p, c in pairs if c]; neg = [p for p, c in pairs if not c]
    return sum((a > b) + .5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))


def cascade(rows, T):
    jev = [r["p"] >= T and r["cat"] not in PROTECTED for r in rows]
    final = [r["cat"] if j else r["ref"] for r, j in zip(rows, jev)]
    cost_ = sum(r["tok"] * JEV_PER_TOK + (0 if j else r["hcost"]) for r, j in zip(rows, jev))
    n = len(rows)
    return dict(T=T, handled=sum(jev) / n, agree=sum(f == r["ref"] for f, r in zip(final, rows)) / n,
                miss=sum(r["ref"] == "ACTION_NEEDED" and f in COSTLY for f, r in zip(final, rows)),
                per1k=cost_ / n * 1000)


def show_cal(name, pairs, costly=None):
    t, e = calibration(pairs)
    print(f"\n{name}: ECE {e:.3f}, AUROC {auroc(pairs):.3f}")
    for lo, hi, n, mp, acc in t:
        print(f"  [{lo:.2f},{hi:.2f}) n={n:3d} predicted {mp:.3f} actual {acc:.3f}")


def report(d):
    calls = load(d); rows = jev_rows(d, calls); n = len(rows)
    pc = lambda t: f"{t[0]*100:.1f}% [{t[1]*100:.1f}, {t[2]*100:.1f}]"
    print(f"n={n}/{len(calls)} ok; model {Counter(r['model'] for r in rows)}; Haiku labels {dict(Counter(r['ref'] for r in rows))}")
    print(f"agreement with Haiku {pc(bootstrap([int(r['cat']==r['ref']) for r in rows]))}")
    for c in CATS:
        s = [r for r in rows if r["ref"] == c]
        rec = sum(r["cat"] == c for r in s) / len(s) * 100 if s else float("nan")
        print(f"  recall {c}: {rec:.1f}% (n={len(s)})")
    cm = [r for r in rows if r["ref"] == "ACTION_NEEDED" and r["cat"] in COSTLY]
    print(f"costly misses (Haiku ACTION_NEEDED -> Jev IGNORE/FYI): {len(cm)}")
    print("confusion:", {f"{a}->{b}": v for (a, b), v in sorted(Counter((r['ref'], r['cat']) for r in rows).items())})
    show_cal("Jev top-choice probability", [(r["p"], int(r["cat"] == r["ref"])) for r in rows])
    show_cal("Jev `confidence` field", [(r["conf"], int(r["cat"] == r["ref"])) for r in rows])
    for T in (.8, .9, .95):
        hi = [r for r in rows if r["p"] >= T]
        print(f"Jev >= {T}: n={len(hi)} ({len(hi)/n*100:.0f}% coverage) agree {sum(r['cat']==r['ref'] for r in hi)}/{len(hi)}, "
              f"mean p {sum(r['p'] for r in hi)/max(len(hi),1):.3f}, costly misses {sum(r['ref']=='ACTION_NEEDED' and r['cat'] in COSTLY for r in hi)}")
    mini = [r for r in rows_for(d, "gpt-4o-mini", calls) if r["cat"] is not None]
    show_cal(f"gpt-4o-mini self-reported confidence (n={len(mini)})", [(r["conf"] / 100, int(r["cat"] == r["ref"])) for r in mini])
    for T in (.8, .9, .95):
        hi = [r for r in mini if r["conf"] / 100 >= T]
        print(f"mini >= {T}: n={len(hi)} agree {sum(r['cat']==r['ref'] for r in hi)}/{len(hi)}, "
              f"costly misses {sum(r['ref']=='ACTION_NEEDED' and r['cat'] in COSTLY for r in hi)}")
    print(f"\nCascade (Jev if p>=T and not protected, else Haiku); Haiku-only ${HAIKU_1K}/1k; Jev tokens mean {statistics.mean(r['tok'] for r in rows):.0f}")
    for T in (.8, .9, .95):
        c = cascade(rows, T)
        print(f"  T={T}: Jev-handled {c['handled']*100:.0f}%, agreement {c['agree']*100:.1f}%, costly misses {c['miss']}, "
              f"${c['per1k']:.3f}/1k, savings {(1-c['per1k']/HAIKU_1K)*100:.0f}%")
    lat = sorted(r["lat"] for r in rows)
    print(f"latency median {statistics.median(lat):.2f}s p90 {lat[int(.9*n)-1]:.2f}s; total Jev spend ${sum(r['tok'] for r in rows)*JEV_PER_TOK:.5f}")


if __name__ == "__main__":
    a = sys.argv
    if len(a) < 3 or a[1] not in ("run", "report"):
        sys.exit(__doc__)
    run(a[2], float(a[a.index("--cap") + 1]) if "--cap" in a else 0.10) if a[1] == "run" else report(a[2])
