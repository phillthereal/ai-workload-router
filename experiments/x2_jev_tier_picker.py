"""X2: can Jev (TypeSafe decision model) pick the model tier cheaply and better? Offline replay of logged runs.

Usage: python3 experiments/x2_jev_tier_picker.py [--db PATH] [--live]   (--live: call Jev for uncached tasks, hard cap $0.10)
Ground truth, policies and metrics: docs/X2_JEV_TIER_PICKER.md. Jev decisions are cached in experiments/x2_jev_cache.json.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from router.adapters.typesafe import JevClient  # noqa: E402
from router.classifier import heuristic_classify  # noqa: E402
from router.config import MODEL_TIER, get_roster  # noqa: E402
from router.jev import TIER_RULEBOOK  # noqa: E402
from router.router import route_task  # noqa: E402

TIERS = ["budget", "mid", "frontier"]
RANK = {t: i for i, t in enumerate(TIERS)}
ROSTER = "claude_tiers"
BAR = 0.95
JEV_CAP_USD = 0.10
CACHE = ROOT / "experiments" / "x2_jev_cache.json"
TASK_FILES = ["tasks.json", "tasks_hard.json", "tasks_v3.json"]


def load_tasks() -> dict:
    out = {}
    for f in TASK_FILES:
        t = json.load(open(ROOT / "data" / f))
        out.update({x["id"]: x for x in (t if isinstance(t, list) else t["tasks"])})
    return out


def load_table(db: str) -> tuple[dict, dict]:
    """(table[task][tier] = (mean quality, mean cost, n) from non-simulated claude_tiers rows, router_rows)."""
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    acc = defaultdict(lambda: defaultdict(list))
    rr = defaultdict(list)
    for r in c.execute("SELECT * FROM runs WHERE simulated=0 AND roster=? AND strategy!='jury' "
                       "AND COALESCE(quality_source,'judge')!='pending'", (ROSTER,)):
        t = MODEL_TIER.get(r["model"])
        if t:
            acc[r["task_id"]][t].append((r["quality_score"], r["cost_usd"]))
        if r["strategy"] == "router":
            rr[r["task_id"]].append(dict(r))
    table = {k: {t: (sum(q for q, _ in v) / len(v), sum(x for _, x in v) / len(v), len(v)) for t, v in d.items()}
             for k, d in acc.items()}
    return table, rr


def ground_truth(row: dict):
    """Cheapest tier meeting BAR x frontier quality. Returns (allowed set of correct tiers, min rank) or None.
    Mid unknown and budget fails -> GT is {mid, frontier} (cannot tell which)."""
    bar = BAR * row["frontier"][0]
    ok = lambda t: t in row and row[t][0] >= bar  # noqa: E731
    if ok("budget"):
        return {"budget"}
    if "mid" in row:
        return {"mid"} if ok("mid") else {"frontier"}
    return {"mid", "frontier"}


def outcome(row: dict, tier: str):
    """(quality, cost, imputed) of the logged answer at `tier`; unknown mid is imputed as the frontier answer."""
    if tier in row:
        return row[tier][0], row[tier][1], False
    return row["frontier"][0], row["frontier"][1], True


def tier_of_route(task: dict, **kw) -> str:
    return MODEL_TIER[route_task(task, roster_name=ROSTER, **kw).chosen_model]


def jev_decisions(tasks: dict, ids: list, live: bool) -> tuple[dict, float]:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    client, spent = JevClient(), 0.0
    for i in ids:
        if i in cache:
            continue
        if not live:
            continue
        d = client.decide(tasks[i]["prompt"], TIER_RULEBOOK.render_instructions(), TIER_RULEBOOK.criteria)
        if not d.success:
            print("jev failed", i, d.error)
            continue
        cache[i] = dict(choice=d.choice, probs=d.probabilities, tokens=d.input_tokens, cost=d.cost_usd)
        spent += d.cost_usd
        CACHE.write_text(json.dumps(cache, indent=1))
        if spent > JEV_CAP_USD:
            raise SystemExit("jev cap hit")
    return cache, spent


def jev_tier(d: dict, t: float, hand: str | None = None, floor_p: float = 0.9) -> str:
    top = max(d["probs"].values())
    tier = d["choice"] if d["choice"] in RANK and top >= t else "frontier"
    if hand is not None and top < floor_p and RANK[tier] < RANK[hand]:
        tier = hand  # safety asymmetry: unsure -> never cheaper than the hand label
    return tier


def evaluate(name, choices: dict, overheads: dict, rows: dict, gts: dict, ids: list) -> dict:
    ids = [i for i in ids if i in choices]
    return dict(name=name, ids=ids, per=[dict(
        id=i, tier=choices[i], gt=gts[i], q=outcome(rows[i], choices[i])[0],
        cost=outcome(rows[i], choices[i])[1] + overheads.get(i, 0.0), fq=rows[i]["frontier"][0],
        fc=rows[i]["frontier"][1], acc=choices[i] in gts[i],
        under=RANK[choices[i]] < min(RANK[g] for g in gts[i])) for i in ids])


def metrics(per: list) -> dict:
    n = len(per)
    return dict(n=n, acc=sum(p["acc"] for p in per) / n, qret=sum(p["q"] for p in per) / max(sum(p["fq"] for p in per), 1e-9),
                save=1 - sum(p["cost"] for p in per) / sum(p["fc"] for p in per), under=sum(p["under"] for p in per))


def bootstrap(per: list, key: str, B=2000, seed=0) -> tuple[float, float]:
    rng, vals = random.Random(seed), []
    for _ in range(B):
        vals.append(metrics([rng.choice(per) for _ in per])[key])
    vals.sort()
    return vals[int(.025 * B)], vals[int(.975 * B)]


def paired_save_diff(a: list, b: list, B=2000, seed=1) -> tuple[float, float, float]:
    """Net savings(a) - net savings(b) on the shared tasks, bootstrap CI."""
    bm = {p["id"]: p for p in b}
    pairs = [(p, bm[p["id"]]) for p in a if p["id"] in bm]
    rng, vals = random.Random(seed), []
    for _ in range(B):
        s = [rng.choice(pairs) for _ in pairs]
        vals.append(metrics([x for x, _ in s])["save"] - metrics([y for _, y in s])["save"])
    vals.sort()
    return (metrics([x for x, _ in pairs])["save"] - metrics([y for _, y in pairs])["save"], vals[int(.025 * B)], vals[int(.975 * B)])


def build(db: str, live: bool):
    tasks, (table, rr) = load_tasks(), load_table(db)
    rows = {i: r for i, r in table.items() if i in tasks and "budget" in r and "frontier" in r}
    gts = {i: ground_truth(r) for i, r in rows.items()}
    ids = sorted(rows)
    jev, spent = jev_decisions(tasks, ids, live)
    pol, over = {}, defaultdict(dict)
    pol["a frontier-only"] = {i: "frontier" for i in ids}
    hand = {i: tier_of_route(tasks[i]) for i in ids}
    pol["b hand labels"] = hand
    pol["c heuristic (free)"] = {i: tier_of_route(tasks[i], task_type=(c := heuristic_classify(tasks[i]["prompt"])).task_type,
                                                    difficulty=c.difficulty) for i in ids}
    # d Haiku LLM classifier: logged router rows with routing overhead > 0 (the classified arm)
    d_ch, d_ov = {}, {}
    for i in ids:
        rs = [r for r in rr.get(i, []) if r["routing_cost_usd"] > 0]
        if rs:
            ts = [MODEL_TIER[r["model"]] for r in rs]
            d_ch[i] = max(set(ts), key=ts.count)
            d_ov[i] = sum(r["routing_cost_usd"] for r in rs) / len(rs)
    pol["d haiku classifier"], over["d haiku classifier"] = d_ch, d_ov
    # e v3 learned router: logged rows carrying learned_evidence (hand labels + history, $0 overhead)
    e_ch = {}
    for i in ids:
        rs = [r for r in rr.get(i, []) if r["learned_evidence"]]
        if rs:
            e_ch[i] = MODEL_TIER[rs[-1]["model"]]
    pol["e v3 learned"] = e_ch
    jid = [i for i in ids if i in jev]
    jov = {i: jev[i]["cost"] for i in jid}
    for t in (0.5, 0.7, 0.9):
        pol[f"f jev t={t}"] = {i: jev_tier(jev[i], t) for i in jid}
        over[f"f jev t={t}"] = jov
    for t in (0.0, 0.5, 0.7):
        pol[f"g jev+safety t={t}"] = {i: jev_tier(jev[i], t, hand[i]) for i in jid}
        over[f"g jev+safety t={t}"] = jov
    return tasks, rows, gts, ids, pol, over, jev, spent


def report(db: str, live: bool):
    tasks, rows, gts, ids, pol, over, jev, spent = build(db, live)
    res = {n: evaluate(n, c, over.get(n, {}), rows, gts, ids) for n, c in pol.items()}
    common = sorted(set(res["d haiku classifier"]["ids"]) & set(res["f jev t=0.9"]["ids"]) & set(ids)) if "f jev t=0.9" in res else []
    print(f"n with ground truth = {len(ids)} (budget+frontier logged); GT counts:",
          {k: sum(1 for g in gts.values() if g == v) for k, v in
           {"budget": {"budget"}, "mid": {"mid"}, "frontier": {"frontier"}, "mid|frontier": {"mid", "frontier"}}.items()})
    print(f"jev spend so far (cached decisions): ${sum(v['cost'] for v in jev.values()):.6f}; this run ${spent:.6f}")
    for label, sub in (("ALL tasks each policy covers", None), (f"COMMON subset n={len(common)}", set(common))):
        print(f"\n== {label}")
        print(f"{'policy':24}{'n':>4}{'acc':>7}{'qret':>7}{'save%':>8}{'save CI':>17}{'under':>6}{'under CI':>11}")
        for n, r in res.items():
            per = [p for p in r["per"] if sub is None or p["id"] in sub]
            if not per:
                continue
            m = metrics(per)
            lo, hi = bootstrap(per, "save")
            ulo, uhi = bootstrap(per, "under")
            print(f"{n:24}{m['n']:>4}{m['acc']:>7.2f}{m['qret']:>7.3f}{100*m['save']:>8.1f}  [{100*lo:5.1f},{100*hi:5.1f}]{m['under']:>6}  [{ulo:.0f},{uhi:.0f}]")
    if common:
        print("\n== paired net-saving difference on common tasks (percentage points, 95% CI)")
        for j in [n for n in res if n[0] in "fg"]:
            for ref in ("c heuristic (free)", "d haiku classifier"):
                d, lo, hi = paired_save_diff([p for p in res[j]["per"] if p["id"] in common],
                                             [p for p in res[ref]["per"] if p["id"] in common])
                print(f"{j:24} - {ref:20} {100*d:+6.1f} [{100*lo:+.1f}, {100*hi:+.1f}]")
    imp = sum(1 for p in res["f jev t=0.9"]["per"] if p["tier"] == "mid" and "mid" not in rows[p["id"]]) if "f jev t=0.9" in res else 0
    print("\nimputed-mid cases (jev t=0.9):", imp)
    return res


def demo():
    """Synthetic checks: ground truth, missing-tier handling, metrics, safety asymmetry."""
    row = dict(budget=(1.0, .001, 1), frontier=(1.0, .01, 1))
    assert ground_truth(row) == {"budget"}
    assert ground_truth(dict(budget=(0.5, .001, 1), frontier=(1.0, .01, 1))) == {"mid", "frontier"}
    assert ground_truth(dict(budget=(0.5, .001, 1), mid=(0.96, .004, 1), frontier=(1.0, .01, 1))) == {"mid"}
    assert ground_truth(dict(budget=(0.9, .001, 1), mid=(0.9, .004, 1), frontier=(1.0, .01, 1))) == {"frontier"}
    assert outcome(row, "mid") == (1.0, .01, True)
    d = dict(choice="budget", probs={"budget": .6, "mid": .3, "frontier": .1})
    assert jev_tier(d, 0.5) == "budget" and jev_tier(d, 0.7) == "frontier"
    assert jev_tier(d, 0.0, hand="mid") == "mid" and jev_tier(dict(d, probs={"budget": .95}), 0.0, hand="mid") == "budget"
    r = evaluate("x", {"t": "budget"}, {"t": 0.001}, {"t": row}, {"t": {"budget"}}, ["t"])
    m = metrics(r["per"])
    assert abs(m["save"] - 0.8) < 1e-9 and m["under"] == 0 and m["acc"] == 1
    r = evaluate("x", {"t": "budget"}, {}, {"t": dict(budget=(0.5, .001, 1), frontier=(1.0, .01, 1))}, {"t": {"mid", "frontier"}}, ["t"])
    assert metrics(r["per"])["under"] == 1
    print("demo ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "runs.db"))
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    demo()
    report(a.db, a.live)
