#!/usr/bin/env python3
"""Same rule, with and without Chain of Draft, on reasoning tasks not used for picking. Saves to
data/cheap_tier_draft.json. Without --max-spend it only reports, for free.

    python3 experiments/cheap_tier_draft.py --max-spend 0.033
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "experiments")]

from analyze import bootstrap, net_reduction, retention  # noqa: E402
from cheap_tier_new import paid_call, same  # noqa: E402
from cheap_tier_round3 import DRAFT, OUT as R3  # noqa: E402
from cheap_tier_test import BENCH, DEEPSEEK, HAIKU, SONNET  # noqa: E402
from router.adapters import SpendMeter  # noqa: E402

OUT = ROOT / "data" / "cheap_tier_draft.json"
MINI = "gpt-4o-mini"
ORDER = (("fresh 2", "tasks_public_fresh2.json"), ("fresh 1", "tasks_public_fresh.json"), ("hard set", "tasks_public_hard.json"))
MAX_CALL_USD = 0.0105  # one Sonnet reply at the 1,024-token limit


def load():
    rows = {r["id"]: r for r in json.loads(R3.read_text()) if r["set"] != "picking"}
    return [(group, t, rows[t["id"]]) for group, f in ORDER for t in json.loads((BENCH / f).read_text())["tasks"]
            if t["id"] in rows]


def vote(a, b, mini, sonnet):
    """The rule: (usd, score) from a pair of answers plus the fallback answers it needed."""
    usd = a["usd"] + b["usd"]
    if same(a, b):
        return usd, b["score"]
    usd += mini["usd"]
    if same(mini, a) or same(mini, b):
        return usd, mini["score"]
    return usd + sonnet["usd"], sonnet["score"]


def collect(cap):
    done = {r["id"]: r for r in json.loads(OUT.read_text())} if OUT.exists() else {}
    with ThreadPoolExecutor(2) as pool:
        for group, t, _ in load():
            if t["id"] in done:
                continue
            if SpendMeter.usd + MAX_CALL_USD >= cap:
                break
            p = t["prompt"] + DRAFT
            h, d = pool.map(lambda m: paid_call(m, p, t["reference"]), (HAIKU, DEEPSEEK))
            row = {"id": t["id"], "group": group, "haiku": h, "deepseek": d}
            if not same(h, d):
                row["mini"] = paid_call(MINI, p, t["reference"])
                if not (same(row["mini"], h) or same(row["mini"], d)):
                    row["sonnet"] = paid_call(SONNET, p, t["reference"])
            done[t["id"]] = row
            OUT.write_text(json.dumps(list(done.values()), indent=1, ensure_ascii=False) + "\n")
    print(f"Real spend ${SpendMeter.usd:.4f}, {len(done)} tasks done")


def plain(r):
    if r["agreed"]:
        return r["haiku"]["usd"] + r["deepseek"]["usd"], r["deepseek"]["score"]
    return vote(r["haiku"], r["deepseek"], r["mini"], r["sonnet"])


def draft(r):
    return vote(r["haiku"], r["deepseek"], r.get("mini"), r.get("sonnet"))


def line(label, p):
    c, q = bootstrap(p, net_reduction, alpha=.01), bootstrap(p, retention, alpha=.01)
    print(f"{label:34} n={len(p):3} saved {c[0]:5.1f} [{c[1]:5.1f},{c[2]:5.1f}] quality {q[0]:5.1f} [{q[1]:5.1f},{q[2]:5.1f}]")


def report():
    mine = {r["id"]: r for r in json.loads(OUT.read_text())}
    rows = [(g, t, r, mine[t["id"]]) for g, t, r in load() if t["id"] in mine]
    parts = {"all done": rows, "unseen": [x for x in rows if x[0] != "hard set"],
             "unseen hard": [x for x in rows if x[0] != "hard set" and x[1]["difficulty"] == "hard"],
             "hard set": [x for x in rows if x[0] == "hard set"],
             "all hard": [x for x in rows if x[1]["difficulty"] == "hard"]}
    for name, part in parts.items():
        if part:
            line(f"{name}: rule", [(r["opus_usd"], plain(r)[0], r["opus_score"], plain(r)[1]) for _, _, r, _ in part])
            line(f"{name}: rule + draft", [(r["opus_usd"], draft(d)[0], r["opus_score"], draft(d)[1]) for _, _, r, d in part])
            line(f"{name}: draft against rule", [(*[plain(r)[0], draft(d)[0], plain(r)[1], draft(d)[1]],) for _, _, r, d in part])
    n = len(rows)
    print("mean reply tokens plain/draft: Haiku", sum(r["haiku"]["out"] for _, _, r, _ in rows) // n, sum(d["haiku"]["out"] for *_, d in rows) // n,
          "DeepSeek", sum(r["deepseek"]["out"] for _, _, r, _ in rows) // n, sum(d["deepseek"]["out"] for *_, d in rows) // n)
    print("agreed plain/draft:", sum(r["agreed"] for _, _, r, _ in rows), sum(same(d["haiku"], d["deepseek"]) for *_, d in rows), "of", n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-spend", type=float)
    a = ap.parse_args()
    if a.max_spend:
        collect(a.max_spend)
    report()
