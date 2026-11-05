#!/usr/bin/env python3
"""Free final checks on the confirmation answers: the two-step fallback and the full 300-task picture."""
import json, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "experiments")]
from analyze import bootstrap, net_reduction, retention
from cheap_tier_confirm import GROUPS, OUT, pairs
from cheap_tier_new import same
from cheap_tier_test import CLASSIFIER_USD
rows = json.loads(OUT.read_text())
unseen = [r for r in rows if r["set"] != "hard set"]
def two_step(part):
    p = []
    for r in part:
        cheap = r["haiku"]["usd"] + r["deepseek"]["usd"] + CLASSIFIER_USD
        if r["agreed"]:
            p.append((r["opus_usd"], cheap, r["opus_score"], r["deepseek"]["score"])); continue
        s = r["sonnet"]; cheap += s["usd"]
        if same(s, r["haiku"]) or same(s, r["deepseek"]):
            p.append((r["opus_usd"], cheap, r["opus_score"], s["score"]))
        else:
            p.append((r["opus_usd"], cheap + r["opus_usd"], r["opus_score"], r["opus_score"]))
    return p
def line(label, p):
    c, q = bootstrap(p, net_reduction, alpha=.01), bootstrap(p, retention, alpha=.01)
    print(f"{label:44} n={len(p):3} saved {c[0]:5.1f} [{c[1]:5.1f},{c[2]:5.1f}] quality {q[0]:5.1f} [{q[1]:5.1f},{q[2]:5.1f}]")
parts = {"reasoning": [r for r in unseen if r["type"] == "reasoning"], "extraction": [r for r in unseen if r["type"] == "extraction"],
         "hard tasks": [r for r in unseen if r["difficulty"] == "hard"], "all unseen": unseen, "hard set": [r for r in rows if r["set"] == "hard set"]}
for k, v in parts.items():
    line("two-step " + k, two_step(v))
hard_all = parts["hard tasks"] + parts["hard set"]
for fb in ("sonnet", "opus"):
    line(f"hard pooled, else {fb}", pairs(hard_all, fb))
line("hard pooled, two-step", two_step(hard_all))
db = sqlite3.connect(ROOT / "data" / "runs.db"); db.row_factory = sqlite3.Row
for name in ("first set", "fresh 1", "fresh 2"):
    g = GROUPS[name][1]
    base = {x["task_id"]: x for x in db.execute("select * from runs where run_group=? and strategy='frontier_only'", (g,))}
    rt = {x["task_id"]: x for x in db.execute("select * from runs where run_group=? and strategy='router'", (g,))}
    new = {tuple(p[:1])[0]: p for p in []}
    mine = {r["id"]: r for r in rows if r["set"] == name}
    p = []
    for t, b in base.items():
        if t in mine:
            p.append(pairs([mine[t]], "sonnet")[0])
        else:
            p.append((b["cost_usd"], rt[t]["cost_usd"] + rt[t]["routing_cost_usd"], b["quality_score"], rt[t]["quality_score"]))
    line(f"whole 300, {name}, new rule on 2 types", p)
print("new-rule rows", len(rows), "all type mix check done")
