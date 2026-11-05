#!/usr/bin/env python3
"""Free: score the combined rule on unseen tasks, with and without the paid classifier."""
import json, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "experiments")]
from analyze import bootstrap, net_reduction, retention
from cheap_tier_confirm import GROUPS, OUT as CONFIRM
from cheap_tier_new import same
from cheap_tier_round3 import OUT as R3
from cheap_tier_test import CLASSIFIER_USD
conf = json.loads(CONFIRM.read_text())
mini = {r["id"]: r["mini"] for r in json.loads(R3.read_text()) if "mini" in r}
def rule(r, cls):
    c = r["haiku"]["usd"] + r["deepseek"]["usd"] + cls
    if r["agreed"]:
        return c, r["deepseek"]["score"]
    if r["type"] == "reasoning":
        m = mini[r["id"]]; c += m["usd"]
        if same(m, r["haiku"]) or same(m, r["deepseek"]):
            return c, m["score"]
    return c + r["sonnet"]["usd"], r["sonnet"]["score"]
def line(label, p):
    c, q = bootstrap(p, net_reduction, alpha=.01), bootstrap(p, retention, alpha=.01)
    print(f"{label:42} n={len(p):3} saved {c[0]:5.1f} [{c[1]:5.1f},{c[2]:5.1f}] quality {q[0]:5.1f} [{q[1]:5.1f},{q[2]:5.1f}]")
def pair(r, cls):
    c, s = rule(r, cls); return (r["opus_usd"], c, r["opus_score"], s)
un = [r for r in conf if r["set"] != "hard set"]
parts = {"reasoning": [r for r in un if r["type"] == "reasoning"], "extraction": [r for r in un if r["type"] == "extraction"],
         "hard tasks": [r for r in un if r["difficulty"] == "hard"], "all unseen": un,
         "hard pooled": [r for r in un if r["difficulty"] == "hard"] + [r for r in conf if r["set"] == "hard set"]}
for lab, cls in (("paid classifier", CLASSIFIER_USD), ("free lookup", 0.0)):
    for k, v in parts.items():
        line(f"{lab}: {k}", [pair(r, cls) for r in v])
db = sqlite3.connect(ROOT / "data/runs.db"); db.row_factory = sqlite3.Row
for name in ("fresh 1", "fresh 2"):
    g = GROUPS[name][1]
    b = {x["task_id"]: x for x in db.execute("select * from runs where run_group=? and strategy='frontier_only'", (g,))}
    t = {x["task_id"]: x for x in db.execute("select * from runs where run_group=? and strategy='router'", (g,))}
    mine = {r["id"]: r for r in conf if r["set"] == name}
    p = [pair(mine[i], 0.0) if i in mine else (x["cost_usd"], t[i]["cost_usd"] + max(t[i]["routing_cost_usd"] - CLASSIFIER_USD, 0), x["quality_score"], t[i]["quality_score"]) for i, x in b.items()]
    line(f"whole 300 {name}, free lookup", p)
