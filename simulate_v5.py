"""Offline v5 simulations (budget governor + shadow sampling). ZERO API spend:
resamples logged non-simulated claude_tiers outcomes from data/runs.db (read-only).
Usage: python3 simulate_v5.py   (prints markdown tables)"""

import random
import sqlite3
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from router.config import get_roster, TIER_ORDER  # noqa: E402
from router.governors import BudgetPolicy, RegretWindow, cheaper_tier  # noqa: E402

R = get_roster("claude_tiers")
TIER_OF = {R.budget: "budget", R.mid: "mid", R.frontier: "frontier"}
PER_DAY, DAYS = 200, 30
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def load_table():
    """task_id -> {type, router_tier, budget:(c,q), mid:(c,q,imputed), frontier:(c,q)}."""
    c = sqlite3.connect(f"file:{ROOT/'data'/'runs.db'}?mode=ro", uri=True)
    rows = c.execute("SELECT task_id,task_type,strategy,model,cost_usd,quality_score FROM runs "
                     "WHERE roster='claude_tiers' AND simulated=0 AND strategy!='jury' "
                     "AND task_id NOT LIKE 'adhoc%'").fetchall()
    obs = defaultdict(lambda: defaultdict(list))
    typ, routed = {}, defaultdict(list)
    for tid, tt, strat, model, cost, q in rows:
        if model not in TIER_OF:
            continue
        typ[tid] = tt
        obs[tid][TIER_OF[model]].append((cost, q))
        if strat == "router":
            routed[tid].append(TIER_OF[model])
    mean = lambda xs, i: st.mean(x[i] for x in xs)  # noqa: E731
    both = [t for t in obs if "mid" in obs[t] and "frontier" in obs[t]]
    ratio = st.mean(mean(obs[t]["mid"], 0) / mean(obs[t]["frontier"], 0) for t in both)
    gap = max(0.0, st.mean(mean(obs[t]["frontier"], 1) - mean(obs[t]["mid"], 1) for t in both))
    table, dropped = {}, []
    for t, o in obs.items():
        if "budget" not in o or "frontier" not in o or t not in routed:
            dropped.append(t)
            continue
        f = (mean(o["frontier"], 0), mean(o["frontier"], 1))
        m = ((mean(o["mid"], 0), mean(o["mid"], 1), False) if "mid" in o
             else (f[0] * ratio, max(0.0, f[1] - gap), True))
        table[t] = {"type": typ[t], "router_tier": Counter(routed[t]).most_common(1)[0][0],
                    "budget": (mean(o["budget"], 0), mean(o["budget"], 1)),
                    "mid": m, "frontier": f}
    return table, dropped, len(both), ratio, gap


def traffic(table, seed):
    rng = random.Random(seed)
    ids = sorted(table)
    return [[rng.choice(ids) for _ in range(PER_DAY)] for _ in range(DAYS)]


class _Inner:
    name = "sim"
    def report_tags(self): return []  # noqa: E704


def sim_budget(table, seed, frac=0.6, protected=("reasoning",)):
    days = traffic(table, seed)
    unc = sum(table[t][table[t]["router_tier"]][0] for d in days for t in d)
    uq = st.mean(table[t][table[t]["router_tier"]][1] for d in days for t in d)
    pol = BudgetPolicy(_Inner(), unc * frac, T0, protected_task_types=protected)
    curve, q, prot_down, n = [], [], 0, 0
    for di, d in enumerate(days):
        for t in d:
            e = table[t]
            lvl, _ = pol.decide(e["type"], di)
            tier = e["router_tier"]
            new = tier if lvl == "ok" else ("budget" if lvl == "cap" else cheaper_tier(tier))
            if e["type"] in pol.protected and new != tier:
                prot_down += 1
            pol.spent += e[new][0]
            q.append(e[new][1])
            n += 1
        curve.append(pol.spent)
    return dict(unc=unc, budget=pol.budget, final=pol.spent, curve=curve, q=st.mean(q), uq=uq,
                pace=dict(pol.pressured), n=n, prot_down=prot_down)


# ---- shadow
TOL = 0.05          # routed "at least as good" if q_routed >= q_frontier - TOL
WINDOW, MIN_S, THRESH = 40, 20, 0.25


def sim_shadow(table, p, seed, drop=0.3):
    rng = random.Random(seed)
    types = sorted({e["type"] for e in table.values()})
    hit = set(rng.sample(types, len(types) // 2))
    days = traffic(table, seed)
    w = RegretWindow(WINDOW, THRESH, MIN_S)
    reqs = false_alarm = detect = None
    answers_cost = shadow_cost = 0.0
    n = 0
    for di, d in enumerate(days):
        for t in d:
            n += 1
            e = table[t]
            tier = e["router_tier"]
            c, q = e[tier][0], e[tier][1]
            if di >= 15 and tier == "budget" and e["type"] in hit:
                q = max(0.0, q - drop)
            answers_cost += c
            if rng.random() < p and tier != "frontier":
                shadow_cost += e["frontier"][0] + 2 * e["budget"][0]  # + judge ~ 2x a budget call
                if w.record(not (q >= e["frontier"][1] - TOL)) and detect is None:
                    if di < 15:
                        false_alarm = True
                    else:
                        detect = n - 15 * PER_DAY
    return dict(fa=bool(false_alarm), detect=detect, overhead=shadow_cost / answers_cost * 100)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main():
    table, dropped, nboth, ratio, gap = load_table()
    print(f"tasks usable: {len(table)} (dropped {len(dropped)} lacking budget/frontier/router rows); "
          f"mid observed for {sum(1 for e in table.values() if not e['mid'][2])}, imputed for "
          f"{sum(1 for e in table.values() if e['mid'][2])} (sonnet/opus cost ratio {ratio:.3f}, "
          f"quality gap {gap:.4f}, from {nboth} shared tasks)")
    tc = Counter(e["type"] for e in table.values()); print("types:", dict(tc))
    base = [e for e in table.values() if e["router_tier"] != "frontier"]
    reg = st.mean(0 if e[e["router_tier"]][1] >= e["frontier"][1] - TOL else 1 for e in base)
    print(f"clean routed-vs-frontier regret rate (non-frontier-routed tasks): {reg:.3f}")
    print("\n## A. Budget")
    for frac, prot in ((0.6, ("reasoning",)), (0.6, ()), (0.8, ("reasoning",))):
      print(f"\n### budget={frac:.0%} of unconstrained, protected={prot}")
      runs = [sim_budget(table, s, frac, prot) for s in range(20)]
      r0 = runs[0]
      print(f"seed0: unconstrained ${r0['unc']:.4f}, budget ${r0['budget']:.4f}, final ${r0['final']:.4f}")
      print("day | spend | budget-linear-pace")
      for d in (0, 4, 9, 14, 19, 24, 29):
          print(f"{d+1:>3} | {r0['curve'][d]:.4f} | {r0['budget']*(d+1)/DAYS:.4f}")
      for k in ("final", "q", "uq"):
          print(k, f"mean {st.mean(r[k] for r in runs):.4f}")
      print("final/budget:", f"{st.mean(r['final']/r['budget'] for r in runs):.3f}",
            "range", f"{min(r['final']/r['budget'] for r in runs):.3f}-{max(r['final']/r['budget'] for r in runs):.3f}")
      print("pressured pace/cap per run:", st.mean(r['pace']['pace'] for r in runs), st.mean(r['pace']['cap'] for r in runs),
            "of", r0['n'], "| protected downgraded:", sum(r['prot_down'] for r in runs))
      prot_share = sum(table[t][table[t]['router_tier']][0] for t in table if table[t]['type'] == 'reasoning') / sum(
          table[t][table[t]['router_tier']][0] for t in table)
      print(f"reasoning share of unconstrained cost (uniform tasks): {prot_share:.3f}")
    print("\n## B. Shadow")
    for p in (0.01, 0.02, 0.05, 0.10, 0.20):
        rs = [sim_shadow(table, p, s) for s in range(300)]
        det = [r["detect"] for r in rs if r["detect"] is not None]
        print(f"p={p:.2f} FA={st.mean(r['fa'] for r in rs):.3f} detected={len(det)/len(rs):.3f} "
              f"median={pct(det,.5) if det else None} p90={pct(det,.9) if det else None} "
              f"overhead={st.mean(r['overhead'] for r in rs):.1f}%")


if __name__ == "__main__":
    main()
