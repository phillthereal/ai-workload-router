#!/usr/bin/env python3
"""Confirm the picked rule on tasks it was not chosen on. Opus's scores and costs come from the
saved frontier_only rows, so Opus is never called: Haiku and DeepSeek answer, Sonnet only on a
disagreement. Extraction prompts ask for the answer line only.

    python3 experiments/cheap_tier_confirm.py --max-spend 0.30
"""
import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "experiments")]

from analyze import bootstrap, net_reduction, retention  # noqa: E402
from cheap_tier_new import ANSWER_ONLY, paid_call, same  # noqa: E402
from cheap_tier_test import BENCH, CLASSIFIER_USD, DEEPSEEK, HAIKU, SONNET, TYPES  # noqa: E402
from router.adapters import SpendMeter  # noqa: E402

OUT = ROOT / "data" / "cheap_tier_confirm.json"
GROUPS = {"first set": ("tasks_public.json", "20261105T092337-0eb276cf"),
          "fresh 1": ("tasks_public_fresh.json", "20261105T142952-990bf63a"),
          "fresh 2": ("tasks_public_fresh2.json", "20261105T175936-c27f3a54"),
          "hard set": ("tasks_public_hard.json", "20261105T095044-1b2f8b62")}


def collect(cap):
    db = sqlite3.connect(ROOT / "data" / "runs.db"); db.row_factory = sqlite3.Row
    rows = []
    with ThreadPoolExecutor(2) as pool:
        for name, (file, group) in GROUPS.items():
            base = {r["task_id"]: r for r in db.execute(
                "select * from runs where run_group=? and strategy='frontier_only' and simulated=0", (group,))}
            for t in json.loads((BENCH / file).read_text())["tasks"]:
                if t["task_type"] not in TYPES or t["scoring"] != "final_answer" or t["id"] not in base:
                    continue
                if SpendMeter.usd >= cap:
                    print("Stopped at the cap", file=sys.stderr)
                    return rows
                p = t["prompt"] + (ANSWER_ONLY if t["task_type"] == "extraction" else "")
                h, d = pool.map(lambda m: paid_call(m, p, t["reference"]), (HAIKU, DEEPSEEK))
                ok = h["answered"] and d["answered"] and same(h, d)
                s = None if ok else paid_call(SONNET, p, t["reference"])
                o = base[t["id"]]
                rows.append({"set": name, "id": t["id"], "type": t["task_type"], "difficulty": t["difficulty"],
                             "agreed": ok, "haiku": h, "deepseek": d, "sonnet": s,
                             "opus_usd": o["cost_usd"], "opus_score": o["quality_score"]})
    return rows


def pairs(rows, fallback):
    out = []
    for r in rows:
        cheap = r["haiku"]["usd"] + r["deepseek"]["usd"] + CLASSIFIER_USD
        if r["agreed"]:
            out.append((r["opus_usd"], cheap, r["opus_score"], r["deepseek"]["score"]))
        elif fallback == "sonnet":
            out.append((r["opus_usd"], cheap + r["sonnet"]["usd"], r["opus_score"], r["sonnet"]["score"]))
        else:
            out.append((r["opus_usd"], cheap + r["opus_usd"], r["opus_score"], r["opus_score"]))
    return out


def report(rows):
    print(f"Real spend ${SpendMeter.usd:.4f}, tasks {len(rows)}")
    unseen = [r for r in rows if r["set"] != "hard set"]
    parts = {"unseen reasoning": [r for r in unseen if r["type"] == "reasoning"],
             "unseen extraction": [r for r in unseen if r["type"] == "extraction"],
             "unseen hard tasks": [r for r in unseen if r["difficulty"] == "hard"],
             "unseen all": unseen, "hard set (used in picking)": [r for r in rows if r["set"] == "hard set"]}
    for fb in ("sonnet", "opus"):
        print(f"\nDisagreement goes to {fb}\n| Group | n | Kept | Saved % 99% range | Quality % 99% range |\n|---|---|---|---|---|")
        for name, part in parts.items():
            if part:
                p = pairs(part, fb)
                c, q = bootstrap(p, net_reduction, alpha=.01), bootstrap(p, retention, alpha=.01)
                print(f"| {name} | {len(p)} | {sum(r['agreed'] for r in part)} | {c[0]:.1f} [{c[1]:.1f}, {c[2]:.1f}] | {q[0]:.1f} [{q[1]:.1f}, {q[2]:.1f}] |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-spend", type=float, required=True)
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    if a.report_only:
        report(json.loads(OUT.read_text()))
    else:
        data = collect(a.max_spend)
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        report(data)
