#!/usr/bin/env python3
"""Third round, reasoning tasks only, where Sonnet's long answers are most of what the rule costs.
  referee: on a disagreement Sonnet picks between the two answers instead of solving from scratch
  third vote: GPT-4o mini breaks the tie and Sonnet is only used if it sides with neither
  draft: Chain of Draft, every model writes at most five words per step

    python3 experiments/cheap_tier_round3.py --max-spend 0.13
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "experiments")]

from analyze import bootstrap, net_reduction, retention  # noqa: E402
from cheap_tier_confirm import GROUPS, OUT as CONFIRM  # noqa: E402
from cheap_tier_new import paid_call, same  # noqa: E402
from cheap_tier_test import (BENCH, CLASSIFIER_USD, DEEPSEEK, HAIKU, MINI, OPUS, OUT as FIRST, SETS,  # noqa: E402
                             SONNET)
from router.adapters import SpendMeter  # noqa: E402
from router.scoring import committed_answer  # noqa: E402

OUT = ROOT / "data" / "cheap_tier_round3.json"
DRAFT = ("\nThink step by step, but keep only a minimum draft of each step, five words at most per step.")


def load():
    files = {f for f in SETS.values()} | {g[0] for g in GROUPS.values()}
    by_id = {t["id"]: t for f in files for t in json.loads((BENCH / f).read_text())["tasks"]}
    rows = []
    for r in json.loads(FIRST.read_text()):
        if r["set"] == "new tasks" and r["type"] == "reasoning":
            a = r["answers"]
            rows.append({"set": "picking", "id": r["id"], "difficulty": by_id[r["id"]]["difficulty"],
                         "haiku": a[HAIKU], "deepseek": a[DEEPSEEK], "sonnet": a[SONNET],
                         "agreed": same(a[HAIKU], a[DEEPSEEK]), "opus_usd": a[OPUS]["usd"], "opus_score": a[OPUS]["score"]})
    for r in json.loads(CONFIRM.read_text()):
        if r["type"] == "reasoning":
            rows.append({**r, "set": "hard set" if r["set"] == "hard set" else "unseen"})
    return rows, by_id


def referee_prompt(task, x, y):
    return (f"{task['prompt']}\n\nTwo solvers gave different answers.\nCandidate 1: {committed_answer(x['text'])}\n"
            f"Candidate 2: {committed_answer(y['text'])}\nCheck them and reply with only the final line in the form "
            "above. If both are wrong, give the right answer in that final line.")


def collect(cap):
    rows, by_id = load()
    earlier = {r["id"]: r for r in json.loads(OUT.read_text())} if OUT.exists() else {}
    with ThreadPoolExecutor(2) as pool:
        for r in rows:
            t = by_id[r["id"]]
            if not r["agreed"]:
                r["mini"] = paid_call(MINI, t["prompt"], t["reference"])
                if "referee" in earlier.get(r["id"], {}):  # dropped: Sonnet wrote full answers anyway
                    r["referee"] = earlier[r["id"]]["referee"]
        for r in sorted(rows, key=lambda x: x["set"] != "picking"):
            if r["set"] != "picking" or SpendMeter.usd >= cap:
                continue
            t = by_id[r["id"]]
            p = t["prompt"] + DRAFT
            h, d = pool.map(lambda m: paid_call(m, p, t["reference"]), (HAIKU, DEEPSEEK))
            r["draft"] = {"haiku": h, "deepseek": d, "agreed": same(h, d)}
            if not r["draft"]["agreed"]:
                r["draft"]["sonnet"] = paid_call(SONNET, p, t["reference"])
    OUT.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    print(f"Real spend ${SpendMeter.usd:.4f}")
    return rows


def cost(r):
    return r["haiku"]["usd"] + r["deepseek"]["usd"] + CLASSIFIER_USD


def base(r):
    return (cost(r), r["deepseek"]["score"]) if r["agreed"] else (cost(r) + r["sonnet"]["usd"], r["sonnet"]["score"])


def referee(r):
    return base(r) if r["agreed"] or "referee" not in r else (cost(r) + r["referee"]["usd"], r["referee"]["score"])


def third_vote(r):
    if r["agreed"] or "mini" not in r:
        return base(r)
    m = r["mini"]
    if same(m, r["haiku"]) or same(m, r["deepseek"]):
        return cost(r) + m["usd"], m["score"]
    return cost(r) + m["usd"] + r["sonnet"]["usd"], r["sonnet"]["score"]


def draft(r):
    d = r["draft"]; c = d["haiku"]["usd"] + d["deepseek"]["usd"] + CLASSIFIER_USD
    return (c, d["deepseek"]["score"]) if d["agreed"] else (c + d["sonnet"]["usd"], d["sonnet"]["score"])


def report(rows):
    groups = {"picking tasks": [r for r in rows if r["set"] == "picking"], "unseen": [r for r in rows if r["set"] == "unseen"],
              "unseen hard": [r for r in rows if r["set"] == "unseen" and r["difficulty"] == "hard"],
              "hard set": [r for r in rows if r["set"] == "hard set"]}
    print("| Idea | Group | n | Saved % 99% range | Quality % 99% range |\n|---|---|---|---|---|")
    for name, arm in (("agree else Sonnet", base), ("Sonnet referee", referee), ("mini third vote", third_vote), ("Chain of Draft", draft)):
        for g, part in groups.items():
            part = [r for r in part if arm is not draft or "draft" in r]
            if part:
                p = [(r["opus_usd"], *arm(r)[:1], r["opus_score"], arm(r)[1]) for r in part]
                c, q = bootstrap(p, net_reduction, alpha=.01), bootstrap(p, retention, alpha=.01)
                print(f"| {name} | {g} | {len(p)} | {c[0]:.1f} [{c[1]:.1f}, {c[2]:.1f}] | {q[0]:.1f} [{q[1]:.1f}, {q[2]:.1f}] |")
    dr = [r for r in rows if "draft" in r]
    if dr:
        print("mean output, normal vs draft: Haiku", round(sum(r["haiku"]["out"] for r in dr) / len(dr)), round(sum(r["draft"]["haiku"]["out"] for r in dr) / len(dr)),
              "DeepSeek", round(sum(r["deepseek"]["out"] for r in dr) / len(dr)), round(sum(r["draft"]["deepseek"]["out"] for r in dr) / len(dr)), "agreed", sum(r["draft"]["agreed"] for r in dr), "of", len(dr), "normal", sum(r["agreed"] for r in dr))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-spend", type=float)
    a = ap.parse_args()
    report(collect(a.max_spend) if a.max_spend else json.loads(OUT.read_text()))
