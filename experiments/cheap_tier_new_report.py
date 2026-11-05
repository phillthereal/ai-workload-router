#!/usr/bin/env python3
"""Compare the new ideas with the plain agree-else-Sonnet rule on the same tasks."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "experiments")]
from analyze import bootstrap, net_reduction, retention
from cheap_tier_new import NEW, LOCAL, same
from cheap_tier_test import CLASSIFIER_USD, DEEPSEEK, HAIKU, OPUS, OUT, SONNET
rows = {r["id"]: r for r in json.loads(OUT.read_text())}
data = json.loads(NEW.read_text())
def show(label, items):
    if not items: return
    c, q = bootstrap(items, net_reduction, alpha=.01), bootstrap(items, retention, alpha=.01)
    print(f"{label:48} n={len(items):3} saved {c[0]:5.1f} [{c[1]:5.1f},{c[2]:5.1f}] quality {q[0]:5.1f} [{q[1]:5.1f},{q[2]:5.1f}]")
def agree_pairs(ids, get):
    p = []; kept = 0
    for i in ids:
        o = rows[i]["answers"][OPUS]; a = get(i)
        h, d = a[HAIKU], a[DEEPSEEK]; ok = same(h, d); kept += ok
        s = a.get(SONNET) or rows[i]["answers"][SONNET]
        p.append((o["usd"], h["usd"] + d["usd"] + (0 if ok else s["usd"]) + CLASSIFIER_USD, o["score"], (d if ok else s)["score"]))
    return p, kept
ext = [i for i, r in rows.items() if r["set"] == "new tasks" and r["type"] == "extraction"]
p, k = agree_pairs(ext, lambda i: rows[i]["answers"]); show(f"extraction plain agree (kept {k})", p)
for name in ("trimmed", "answer only", "trimmed answer only"):
    res = data.get(name, {}); ids = [i for i in ext if i in res]
    p, k = agree_pairs(ids, lambda i: res[i]); show(f"extraction {name} (kept {k})", p)
    print("   mean tokens out, haiku", round(sum(res[i][HAIKU]["out"] for i in ids) / max(1, len(ids))), "in", round(sum(res[i][HAIKU]["in"] for i in ids) / max(1, len(ids))))
ot = data.get("opus trimmed", {})
show("Opus on trimmed passage", [(rows[i]["answers"][OPUS]["usd"], a["usd"] + CLASSIFIER_USD, rows[i]["answers"][OPUS]["score"], a["score"]) for i, a in ot.items()])
ex = data.get("expression", {})
if ex:
    print("Haiku expression on maths:", sum(a["score"] for a in ex.values()), "of", len(ex), "| Haiku normal:", sum(rows[i]["answers"][HAIKU]["score"] for i in ex), "| Opus:", sum(rows[i]["answers"][OPUS]["score"] for i in ex), "| out tokens", round(sum(a["out"] for a in ex.values()) / len(ex)))
for m in LOCAL:
    res = data.get(m, {})
    if res:
        for kind in ("reasoning", "extraction"):
            ids = [i for i in res if rows[i]["type"] == kind]
            if ids: print(m, kind, "score", round(sum(res[i]["score"] for i in ids) / len(ids), 3), "opus", round(sum(rows[i]["answers"][OPUS]["score"] for i in ids) / len(ids), 3), "n", len(ids))
        for kind, sets in (("reasoning", ("new tasks",)), ("extraction", ("new tasks",)), ("reasoning", ("hard set",))):
            ids = [i for i in res if rows[i]["type"] == kind and rows[i]["set"] in sets]
            pr = []; kept = 0
            for i in ids:
                a = rows[i]["answers"]; h, l, s, o = a[HAIKU], res[i], a[SONNET], a[OPUS]; ok = same(h, l); kept += ok
                pr.append((o["usd"], h["usd"] + (0 if ok else s["usd"]) + CLASSIFIER_USD, o["score"], (h if ok else s)["score"]))
            show(f"{m}+Haiku agree, else Sonnet: {sets[0]} {kind} (kept {kept})", pr)
