"""Live shadow test: cheap models vs Haiku production labels on real email classifications.

Usage:  python3 shadow_test.py run    <real-traffic-dir> [--cap 0.25]   # live calls (needs keys)
        python3 shadow_test.py report <real-traffic-dir>                # aggregates only, no calls

<real-traffic-dir> holds the private calls.jsonl (fields: kind, prompt, pt, ct, cat, conf).
Raw answers go to <dir>/shadow/<model>.jsonl and disagreements to <dir>/shadow/protected_disagreements.txt;
stdout carries aggregates only. Adapters are built directly (NOT via get_adapter) so the
record/replay .cache/ never sees email text. Haiku is the REFERENCE, not ground truth.
"""
from __future__ import annotations

import json, os, random, re, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

CATS = ["ACTION_NEEDED", "JOB_CONFIRMATION", "JOB_REJECTION", "RECRUITER_REPLY", "JOB_ALERT", "FYI", "IGNORE"]
PROTECTED = ("ACTION_NEEDED", "JOB_REJECTION", "RECRUITER_REPLY")
MODELS = ("gpt-4o-mini", "deepseek-chat")
HAIKU = "claude-haiku-4-5"
CONF_FLOOR = 80


def parse_answer(text):
    """-> (category, confidence) or None. Accepts {"output":{...}} or a flat object, optionally fenced."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        d = d.get("output", d)
        cat, conf = d["category"], int(d["confidence"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return (cat, conf) if cat in CATS else None


def cost(model, pt, ct):
    from router.config import get_model
    return get_model(model).cost_for_tokens(pt, ct)


def bootstrap(vals, n=2000, seed=0):
    rng = random.Random(seed)
    k = len(vals)
    s = sorted(sum(vals[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return sum(vals) / k, s[int(n * .025)], s[int(n * .975) - 1]


def metrics(rows):
    """rows: dicts {ref, cat, conf, ccost, hcost} (cat/conf None = parse failure; costs in USD).
    Returns aggregate dict incl. cascade simulation (escalate -> Haiku's recorded answer)."""
    n = len(rows)
    agree = [1 if r["cat"] == r["ref"] else 0 for r in rows]
    recall = {}
    for c in CATS:
        sub = [r for r in rows if r["ref"] == c]
        recall[c] = (sum(r["cat"] == c for r in sub) / len(sub) if sub else None, len(sub))
    esc = [r["cat"] is None or r["cat"] in PROTECTED or r["conf"] < CONF_FLOOR for r in rows]
    final = [r["ref"] if e else r["cat"] for r, e in zip(rows, esc)]
    casc_agree = [1 if f == r["ref"] else 0 for f, r in zip(final, rows)]
    prot = [(f, r) for f, r in zip(final, rows) if r["ref"] in PROTECTED]
    cheap = sum(r["ccost"] for r in rows)
    hk = sum(r["hcost"] for r in rows)
    casc = cheap + sum(r["hcost"] for r, e in zip(rows, esc) if e)
    return dict(
        n=n, agree=bootstrap(agree), recall=recall,
        confusion=Counter((r["ref"], r["cat"] or "PARSE_FAIL") for r in rows),
        parse_fail=sum(r["cat"] is None for r in rows),
        costly_miss=sum(r["ref"] == "ACTION_NEEDED" and r["cat"] in ("IGNORE", "FYI") for r in rows),
        cost_per_1k=cheap / n * 1000, haiku_per_1k=hk / n * 1000,
        casc_agree=bootstrap(casc_agree), casc_escalated=sum(esc),
        casc_prot_recall=(sum(f == r["ref"] for f, r in prot) / len(prot) if prot else None, len(prot)),
        casc_costly_miss=sum(r["ref"] == "ACTION_NEEDED" and f in ("IGNORE", "FYI") for f, r in zip(final, rows)),
        casc_per_1k=casc / n * 1000, casc_savings=1 - casc / hk,
    )


def load(d):
    seen, out = set(), []
    for l in open(Path(d) / "calls.jsonl"):
        x = json.loads(l)
        if x["kind"] == "classify" and x["prompt"] not in seen and x["cat"]:
            seen.add(x["prompt"]); out.append(x)
    return out


def subject(prompt):
    m = re.search(r"^Subject: (.*)$", prompt, re.M)
    return m.group(1) if m else "?"


def run(d, cap):
    from router.adapters import _PROVIDER_BASE_URL
    from router.adapters.openai_compatible import OpenAICompatibleAdapter
    from router.config import get_model
    from router.secrets import get_api_key
    calls, spent = load(d), 0.0
    os.makedirs(Path(d) / "shadow", exist_ok=True)
    for m in MODELS:
        prov = get_model(m).provider
        ad = OpenAICompatibleAdapter(m, get_api_key(prov.upper() + "_API_KEY"), _PROVIDER_BASE_URL[prov], provider=prov)
        p = Path(d) / "shadow" / f"{m}.jsonl"
        done = {json.loads(l)["i"] for l in open(p)} if p.exists() else set()
        with open(p, "a") as f:
            for i, x in enumerate(calls):
                if i in done:
                    continue
                if spent > cap:
                    sys.exit(f"CAP HIT at ${spent:.4f}")
                r = ad.complete(x["prompt"])
                spent += cost(m, r.input_tokens, r.output_tokens)
                f.write(json.dumps(dict(i=i, text=r.text, pt=r.input_tokens, ct=r.output_tokens, ok=r.success)) + "\n")
                f.flush()
    print(f"spent ${spent:.4f}")


def rows_for(d, m, calls):
    ans = {}
    for l in open(Path(d) / "shadow" / f"{m}.jsonl"):
        a = json.loads(l); ans[a["i"]] = a
    rows = []
    for i, x in enumerate(calls):
        a = ans[i]; pr = parse_answer(a["text"]) if a["ok"] else None
        rows.append(dict(ref=x["cat"], cat=pr and pr[0], conf=pr and pr[1], ccost=cost(m, a["pt"], a["ct"]),
                         hcost=cost(HAIKU, x["pt"], x["ct"]), subj=subject(x["prompt"]), href=x["conf"]))
    return rows


def report(d):
    calls = load(d)
    print(f"unique classifier prompts: {len(calls)}; Haiku labels: {dict(Counter(x['cat'] for x in calls))}")
    lines = []
    for m in MODELS:
        rows = rows_for(d, m, calls); r = metrics(rows)
        pc = lambda t: f"{t[0]*100:.1f}% [{t[1]*100:.1f}, {t[2]*100:.1f}]"
        print(f"\n== {m} (n={r['n']})\nagreement {pc(r['agree'])}; parse failures {r['parse_fail']}; costly misses {r['costly_miss']}")
        for c, (v, k) in r["recall"].items():
            print(f"  recall {c}: {'UNTESTED' if v is None else f'{v*100:.1f}%'} (n={k})")
        print("  confusion (haiku -> cheap):", {f"{a}->{b}": v for (a, b), v in sorted(r["confusion"].items())})
        agr = [x for x in rows if x["cat"] == x["ref"] and x["conf"] is not None]
        dis = [x for x in rows if x["cat"] not in (None, x["ref"])]
        mean = lambda xs: f"{sum(x['conf'] for x in xs)/len(xs):.1f}" if xs else "n/a"
        print(f"  mean cheap confidence: agree {mean(agr)} (n={len(agr)}), disagree {mean(dis)} (n={len(dis)})")
        hi = [x for x in rows if x["conf"] is not None and x["conf"] >= CONF_FLOOR]
        print(f"  agreement when conf>={CONF_FLOOR}: {sum(x['cat']==x['ref'] for x in hi)}/{len(hi)}")
        print(f"  cost/1k ${r['cost_per_1k']:.3f} vs Haiku ${r['haiku_per_1k']:.3f}")
        print(f"  CASCADE: agreement {pc(r['casc_agree'])}, escalated {r['casc_escalated']}/{r['n']}, protected recall "
              f"{r['casc_prot_recall'][0]*100:.1f}% (n={r['casc_prot_recall'][1]}), costly misses {r['casc_costly_miss']}, "
              f"cost/1k ${r['casc_per_1k']:.3f}, savings {r['casc_savings']*100:.1f}%")
        lines += [f"[{m}] {x['subj']} | haiku={x['ref']} cheap={x['cat']}" for x in rows
                  if x["cat"] != x["ref"] and (x["ref"] in PROTECTED or x["cat"] in PROTECTED)]
    (Path(d) / "shadow" / "protected_disagreements.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    a = sys.argv
    if len(a) < 3 or a[1] not in ("run", "report"):
        sys.exit(__doc__)
    run(a[2], float(a[a.index("--cap") + 1]) if "--cap" in a else 0.25) if a[1] == "run" else report(a[2])
