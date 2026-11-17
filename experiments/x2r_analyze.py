"""X2R analysis: the pre-registered replication of X2 (docs/X2R_REPLICATION.md).

Usage: python3 experiments/x2r_analyze.py [--db data/x2r_runs.db] [--allow-simulated]

Reuses ground_truth, outcome, jev_tier, evaluate, metrics, bootstrap and paired_save_diff UNCHANGED from
experiments/x2_jev_tier_picker.py. Only the data sources differ: the table comes from the x2r run log, the Haiku
classifier arm from experiments/x2r_classifier_cache.json, the Jev arm from experiments/x2r_jev_cache.json.
A simulated (mock) database is refused unless --allow-simulated is given; then every table is labelled SIMULATED.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))
from x2_jev_tier_picker import (  # noqa: E402
    ROSTER, bootstrap, evaluate, ground_truth, jev_tier, metrics, paired_save_diff, tier_of_route,
)
from router.classifier import heuristic_classify  # noqa: E402
from router.config import MODEL_TIER  # noqa: E402

STRATEGY = "x2r_tier"
TIERS = ["budget", "mid", "frontier"]
DEFAULT_TASKS = ROOT / "data" / "tasks_x2r.json"
DEFAULT_DB = ROOT / "data" / "x2r_runs.db"
DEFAULT_SIM_DB = ROOT / "data" / "x2r_runs_simulated.db"
CACHE_DIR = ROOT / "experiments"
PRIMARY = "g jev+safety t=0.5"
PRIMARY_REF = "d haiku classifier"
SECONDARY = "f jev t=0.9"


def verdict(diff: float, lo: float, hi: float, under_g: int, under_d: int) -> str:
    """The decision rule fixed in advance (docs/X2R_REPLICATION.md)."""
    if diff > 0 and lo > 0 and under_g <= under_d:
        return "REPLICATES"
    if diff > 0 and lo <= 0:
        return "DIRECTION HOLDS, NOT CONFIRMED"
    return "DOES NOT REPLICATE"


def load_x2r_table(db: str, task_ids: set) -> tuple[dict, bool, float]:
    """(table[task][tier] = (mean quality, mean cost, n), any simulated row seen, summed logged answer cost)."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    acc, sim, spend = defaultdict(lambda: defaultdict(list)), False, 0.0
    for r in c.execute("SELECT * FROM runs WHERE roster=? AND strategy=? AND COALESCE(quality_source,'judge')!='pending'",
                       (ROSTER, STRATEGY)):
        if r["task_id"] not in task_ids:
            continue
        sim = sim or bool(r["simulated"])
        t = MODEL_TIER.get(r["model"])
        if t:
            acc[r["task_id"]][t].append((r["quality_score"], r["cost_usd"]))
        spend += r["cost_usd"]
    c.close()
    table = {k: {t: (sum(q for q, _ in v) / len(v), sum(x for _, x in v) / len(v), len(v)) for t, v in d.items()}
             for k, d in acc.items()}
    return table, sim, spend


def analyze(db: str, tasks_path: str | Path = DEFAULT_TASKS, cache_dir: str | Path = CACHE_DIR,
            allow_simulated: bool = False, out=print) -> dict:
    cache_dir = Path(cache_dir)
    tasks = {t["id"]: t for t in json.loads(Path(tasks_path).read_text())["tasks"]}
    table, saw_sim, answer_spend = load_x2r_table(db, set(tasks))
    simulated = saw_sim or "simulated" in Path(db).name
    if simulated and not allow_simulated:
        raise SystemExit("refusing: this database holds SIMULATED (mock) data, not real results. "
                         "Re-run with --allow-simulated only to test the pipeline.")
    tag = "SIMULATED " if simulated else ""
    sfx = "_simulated" if simulated else ""
    cls = json.loads((cache_dir / f"x2r_classifier_cache{sfx}.json").read_text()) if (cache_dir / f"x2r_classifier_cache{sfx}.json").exists() else {}
    jev = json.loads((cache_dir / f"x2r_jev_cache{sfx}.json").read_text()) if (cache_dir / f"x2r_jev_cache{sfx}.json").exists() else {}

    if simulated:
        out("*" * 78 + "\n*** SIMULATED DATA (mock mode): pipeline test only, these numbers mean NOTHING ***\n" + "*" * 78)
    rows = {i: table[i] for i in sorted(tasks) if i in table and all(t in table[i] for t in TIERS)}
    excluded = [i for i in sorted(tasks) if i not in rows]
    gts = {i: ground_truth(r) for i, r in rows.items()}
    ids = sorted(rows)
    out(f"{tag}x2r tasks in file: {len(tasks)}; analysed (all three tiers answered): {len(ids)}")
    if excluded:
        out(f"{tag}EXCLUDED (no real answer for every tier after one retry): " + ", ".join(
            f"{i} (missing {[t for t in TIERS if t not in table.get(i, {})]})" for i in excluded))
    gt_counts = {k: sum(1 for g in gts.values() if g == v) for k, v in
                 {"budget": {"budget"}, "mid": {"mid"}, "frontier": {"frontier"}}.items()}
    out(f"{tag}ground-truth distribution: {gt_counts}")

    pol, over = {}, defaultdict(dict)
    pol["a frontier-only"] = {i: "frontier" for i in ids}
    hand = {i: tier_of_route(tasks[i]) for i in ids}
    pol["b hand labels"] = hand
    pol["c heuristic (free)"] = {i: tier_of_route(tasks[i], task_type=(c := heuristic_classify(tasks[i]["prompt"])).task_type,
                                                    difficulty=c.difficulty) for i in ids}
    pol[PRIMARY_REF] = {i: cls[i]["tier"] for i in ids if i in cls}
    over[PRIMARY_REF] = {i: cls[i]["cost"] for i in ids if i in cls}
    jid = [i for i in ids if i in jev]
    jov = {i: jev[i]["cost"] for i in jid}
    for t in (0.5, 0.7, 0.9):
        pol[f"f jev t={t}"] = {i: jev_tier(jev[i], t) for i in jid}
        over[f"f jev t={t}"] = jov
    for t in (0.0, 0.5, 0.7):
        pol[f"g jev+safety t={t}"] = {i: jev_tier(jev[i], t, hand[i]) for i in jid}
        over[f"g jev+safety t={t}"] = jov
    res = {n: evaluate(n, c, over.get(n, {}), rows, gts, ids) for n, c in pol.items()}

    out(f"\n== {tag}policies over the analysed tasks (95% CI = percentile bootstrap over tasks, 2000 resamples)")
    out(f"{'policy':24}{'n':>4}{'acc':>7}{'qret':>7}{'save%':>8}{'save CI':>17}{'under':>6}{'under CI':>11}")
    table_out = {}
    for n, r in res.items():
        if not r["per"]:
            continue
        m = metrics(r["per"])
        lo, hi = bootstrap(r["per"], "save")
        ulo, uhi = bootstrap(r["per"], "under")
        table_out[n] = dict(m, lo=lo, hi=hi)
        mark = "  <== PRIMARY (pre-registered)" if n == PRIMARY else ("  <== reference for primary" if n == PRIMARY_REF else (
            "  <== SECONDARY" if n == SECONDARY else ""))
        out(f"{n:24}{m['n']:>4}{m['acc']:>7.2f}{m['qret']:>7.3f}{100*m['save']:>8.1f}  [{100*lo:5.1f},{100*hi:5.1f}]{m['under']:>6}  [{ulo:.0f},{uhi:.0f}]{mark}")

    out(f"\n== {tag}paired net-saving difference (percentage points, 95% CI, paired bootstrap over shared tasks)")
    diffs = {}
    for j in [n for n in res if n[0] in "fg"]:
        for ref in ("c heuristic (free)", PRIMARY_REF):
            if not res[j]["per"] or not res[ref]["per"]:
                continue
            d, lo, hi = paired_save_diff(res[j]["per"], res[ref]["per"])
            shared = len({p["id"] for p in res[j]["per"]} & {p["id"] for p in res[ref]["per"]})
            diffs[(j, ref)] = (d, lo, hi, shared)
            mark = "  <== PRIMARY COMPARISON" if (j, ref) == (PRIMARY, PRIMARY_REF) else (
                "  <== SECONDARY COMPARISON" if (j, ref) == (SECONDARY, PRIMARY_REF) else "")
            out(f"{j:24} - {ref:20} {100*d:+6.1f} [{100*lo:+.1f}, {100*hi:+.1f}]  n={shared}{mark}")

    result = dict(simulated=simulated, n=len(ids), excluded=excluded, gt=gt_counts, metrics=table_out, diffs=diffs, verdict=None)
    out(f"\n== {tag}PRE-REGISTERED PRIMARY: {PRIMARY} vs {PRIMARY_REF}, net saving, paired bootstrap")
    if (PRIMARY, PRIMARY_REF) in diffs:
        d, lo, hi, shared = diffs[(PRIMARY, PRIMARY_REF)]
        ug, ud = table_out[PRIMARY]["under"], table_out[PRIMARY_REF]["under"]
        v = verdict(d, lo, hi, ug, ud)
        result["verdict"] = v
        out(f"difference {100*d:+.1f} pp, 95% CI [{100*lo:+.1f}, {100*hi:+.1f}], n={shared}; under-routed: g={ug}, d={ud}")
        out(f"{tag}VERDICT (rule fixed in advance): {v}")
    else:
        out("not computable: the Jev arm or the classifier arm is missing")
    if (SECONDARY, PRIMARY_REF) in diffs:
        d, lo, hi, shared = diffs[(SECONDARY, PRIMARY_REF)]
        out(f"{tag}secondary ({SECONDARY} vs {PRIMARY_REF}): {100*d:+.1f} pp, 95% CI [{100*lo:+.1f}, {100*hi:+.1f}], n={shared}")

    cls_cost, jev_cost = sum(v["cost"] for v in cls.values()), sum(v["cost"] for v in jev.values())
    out(f"\n{tag}spend: answers logged ${answer_spend:.6f} + classifier ${cls_cost:.6f} + Jev ${jev_cost:.6f} = "
        f"${answer_spend + cls_cost + jev_cost:.6f} (rubric-judge calls are not logged per run, so not included)")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="default data/x2r_runs.db (data/x2r_runs_simulated.db with --allow-simulated)")
    ap.add_argument("--tasks", default=str(DEFAULT_TASKS))
    ap.add_argument("--cache-dir", default=str(CACHE_DIR), help=argparse.SUPPRESS)
    ap.add_argument("--allow-simulated", action="store_true", help="analyse mock data (pipeline test only)")
    a = ap.parse_args(argv)
    dbp = a.db or str(DEFAULT_SIM_DB if a.allow_simulated else DEFAULT_DB)
    if not Path(dbp).exists():
        raise SystemExit(f"database not found: {dbp}")
    analyze(dbp, a.tasks, a.cache_dir, a.allow_simulated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
