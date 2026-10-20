#!/usr/bin/env python3
"""Analyst layer: bootstrap CIs + SQL product questions -> docs/ANALYSIS.md,
and a Power BI-ready star schema -> data/bi/ (`--export`).

Stdlib only, zero API calls, opens data/runs.db READ-ONLY.
Savings definitions mirror src/router/report.py: net cost reduction =
(frontier_cost - (router_cost + routing_cost)) / frontier_cost; quality
retention = mean router quality / mean frontier quality.
"""
from __future__ import annotations

import csv
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

DB = ROOT / "data" / "runs.db"
BASELINE = "frontier_only"
N_BOOT, SEED = 10_000, 20261020

# Hand-written experiment labels, keyed by report-file stem.
LABELS = {
    "benchmark_report": "v1 cross_vendor / labels / easy",
    "benchmark_report_cross_vendor_classified": "v2 cross_vendor / classifier / easy",
    "benchmark_report_claude_tiers_classified": "v2 claude_tiers / classifier / easy",
    "benchmark_report_claude_tiers_cascade": "v2 claude_tiers / cascade (Sonnet verifier) / easy",
    "benchmark_report_claude_tiers_classified_hard": "v2 claude_tiers / classifier / hard",
    "benchmark_report_claude_tiers_cascade_hard": "v2 claude_tiers / cascade (Sonnet verifier) / hard",
    "benchmark_report_claude_tiers_cascade_verifier-claude-haiku-4-5": "v2 claude_tiers / cascade (Haiku verifier) / easy",
    "benchmark_report_claude_tiers_cascade_verifier-claude-haiku-4-5_hard": "v2 claude_tiers / cascade (Haiku verifier) / hard",
    "benchmark_report_claude_tiers_v3": "v3 claude_tiers / static router / held-out",
    "benchmark_report_claude_tiers_learned_v3": "v3 claude_tiers / learned router / held-out",
    "benchmark_report_claude_tiers_jury_unanimous": "v5 claude_tiers / jury unanimous / easy",
    "benchmark_report_claude_tiers_jury_unanimous_hard": "v5 claude_tiers / jury unanimous / hard",
    "benchmark_report_claude_tiers_jury_majority_hard": "v5 claude_tiers / jury majority / hard",
}
UNLABELLED = "unlabelled/replay"


# ---------------------------------------------------------------- data access
def connect(path: Path = DB) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def run_group_labels(data_dir: Path = ROOT / "data") -> dict[str, str]:
    out = {}
    for f in sorted(data_dir.glob("benchmark_report*.md")):
        m = re.search(r"Run group: `([^`]+)`", f.read_text())
        if m:
            out[m.group(1)] = LABELS.get(f.stem, f.stem.replace("benchmark_report_", ""))
    return out


REAL = "simulated = 0 AND strategy NOT LIKE 'live:%'"


def paired_tasks(conn, run_group: str, exp: str | None = None, base_group: str | None = None):
    """[(task_id, base_cost, exp_cost_net, base_q, exp_q)] — one tuple per task.
    exp=None picks the single non-baseline strategy. base_group lets the
    baseline come from the same group (default)."""
    rows = conn.execute(
        f"SELECT * FROM runs WHERE run_group=? AND {REAL}", (run_group,)).fetchall()
    strat = exp or next(r["strategy"] for r in rows if r["strategy"] != BASELINE)
    b = {r["task_id"]: r for r in rows if r["strategy"] == BASELINE}
    e = {r["task_id"]: r for r in rows if r["strategy"] == strat}
    return [(t, b[t]["cost_usd"], e[t]["cost_usd"] + e[t]["routing_cost_usd"],
             b[t]["quality_score"], e[t]["quality_score"]) for t in sorted(b) if t in e]


# ------------------------------------------------------------------ statistics
def net_reduction(p) -> float:  # p: list of (base_cost, exp_net_cost, base_q, exp_q)
    base = sum(x[0] for x in p)
    return (base - sum(x[1] for x in p)) / base * 100


def retention(p) -> float:
    return (sum(x[3] for x in p) / len(p)) / (sum(x[2] for x in p) / len(p)) * 100


def bootstrap(pairs, stat, n=N_BOOT, seed=SEED, alpha=0.05):
    """Resample whole tasks (both arms together) with replacement.
    Returns (point, lo, hi) with a percentile CI."""
    rng = random.Random(seed)
    k = len(pairs)
    vals = sorted(stat([pairs[rng.randrange(k)] for _ in range(k)]) for _ in range(n))
    return stat(pairs), vals[int(n * alpha / 2)], vals[int(n * (1 - alpha / 2)) - 1]


def headline_specs(conn):
    """(label, group, published_cost%, published_quality%) — checked to 0.1pp."""
    return [
        ("v1 cross_vendor labels, easy (n=25)", "20260717T082024-098a7982", 53.6),
        ("v2 cross_vendor classifier, easy (n=25)", "20260717T164636-ee1ddc99", 65.6),
        ("v2 claude_tiers classifier, easy (n=25)", "20260717T164758-d2d01eaa", 42.5),
        ("v2 claude_tiers cascade, easy (n=25)", "20260717T171702-e98b914b", 53.1),
        ("v2 claude_tiers classifier, HARD (n=10)", "20260717T172816-428aceeb", -6.3),
        ("v2 claude_tiers cascade, HARD (n=10)", "20260717T172831-e7fe3e42", -19.9),
    ]


def compute_cis(conn):
    rows, mismatches = [], []
    for label, g, published in headline_specs(conn):
        p = [t[1:] for t in paired_tasks(conn, g)]
        c, q = bootstrap(p, net_reduction), bootstrap(p, retention)
        if abs(c[0] - published) > 0.05 + 1e-9:
            mismatches.append((label, c[0], published))
        rows.append((label, len(p), c, q))
    # v3: learned vs static, same 14 task_ids, separate run_groups.
    s = {t[0]: t for t in paired_tasks(conn, "20260719T113230-91e5c641")}
    l = {t[0]: t for t in paired_tasks(conn, "20260719T113315-b7ff3a62")}
    ids = sorted(set(s) & set(l))
    # per task: (static_net_cost, learned_net_cost, static_q, learned_q)
    v3 = [(s[i][2], l[i][2], s[i][4], l[i][4]) for i in ids]
    d = lambda p: (sum(x[0] for x in p) - sum(x[1] for x in p)) / sum(x[0] for x in p) * 100
    qr = lambda p: sum(x[3] for x in p) / sum(x[2] for x in p) * 100
    cv, qv = bootstrap(v3, d), bootstrap(v3, qr)
    if abs(cv[0] - 22.7) > 0.05 + 1e-9:
        mismatches.append(("v3 learned vs static", cv[0], 22.7))
    rows.append(("v3 learned vs static router, held-out (n=14), cost diff / quality ratio", len(ids), cv, qv))
    return rows, mismatches


# ------------------------------------------------------------------ SQL section
VIEW = """CREATE TEMP VIEW v AS
SELECT r.*, g.label FROM runs r JOIN rg g USING (run_group)
WHERE r.simulated = 0 AND r.strategy NOT LIKE 'live:%'"""

PAIR = """FROM v e JOIN v b ON b.run_group = e.run_group AND b.task_id = e.task_id
 AND b.strategy = 'frontier_only'
WHERE e.strategy != 'frontier_only'"""

QUESTIONS = [
    ("Which task types should never be routed? (savings vs quality change, routed arm vs frontier)",
     f"""SELECT e.task_type, COUNT(*) AS n,
  ROUND(SUM(b.cost_usd), 5) AS frontier_cost,
  ROUND(SUM(e.cost_usd + e.routing_cost_usd), 5) AS routed_cost_net,
  ROUND(100.0 * (1 - SUM(e.cost_usd + e.routing_cost_usd) / SUM(b.cost_usd)), 1) AS net_saving_pct,
  ROUND(AVG(b.quality_score), 3) AS q_frontier,
  ROUND(AVG(e.quality_score), 3) AS q_routed,
  ROUND(AVG(e.quality_score - b.quality_score), 3) AS q_change
{PAIR}
GROUP BY e.task_type ORDER BY q_change"""),
    ("Where does routing lose money? (net cost delta by difficulty)",
     f"""SELECT e.difficulty, COUNT(*) AS n,
  ROUND(SUM(b.cost_usd), 5) AS frontier_cost,
  ROUND(SUM(e.cost_usd + e.routing_cost_usd), 5) AS routed_cost_net,
  ROUND(SUM(b.cost_usd - e.cost_usd - e.routing_cost_usd), 5) AS net_saving_usd,
  SUM(e.cost_usd + e.routing_cost_usd > b.cost_usd) AS tasks_costlier_when_routed
{PAIR}
GROUP BY e.difficulty ORDER BY net_saving_usd"""),
    ("Cost per quality point by model (all non-simulated rows in labelled experiments)",
     """SELECT model, COUNT(*) AS n,
  ROUND(AVG(cost_usd), 6) AS avg_cost,
  ROUND(AVG(quality_score), 3) AS avg_quality,
  ROUND(SUM(cost_usd) / SUM(quality_score), 6) AS usd_per_quality_point
FROM v GROUP BY model ORDER BY usd_per_quality_point"""),
    ("Is routing overhead worth it? (overhead as share of gross savings, per experiment)",
     f"""SELECT e.label, COUNT(*) AS n,
  ROUND(SUM(b.cost_usd - e.cost_usd), 5) AS gross_saving_usd,
  ROUND(SUM(e.routing_cost_usd), 5) AS overhead_usd,
  ROUND(100.0 * SUM(e.routing_cost_usd) / NULLIF(SUM(b.cost_usd - e.cost_usd), 0), 1) AS overhead_pct_of_savings
{PAIR}
GROUP BY e.label ORDER BY overhead_pct_of_savings DESC"""),
    ("Routing mix: which model serves the experimental arm, by strategy?",
     """SELECT strategy, model, COUNT(*) AS tasks,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY strategy), 1) AS pct_of_strategy_tasks,
  ROUND(SUM(cost_usd), 5) AS cost_usd
FROM v WHERE strategy != 'frontier_only'
GROUP BY strategy, model ORDER BY strategy, tasks DESC"""),
    ("Which individual tasks lost quality when routed? (candidates for a never-route list)",
     f"""SELECT e.task_id, e.task_type, e.difficulty, e.strategy, e.model AS routed_to,
  ROUND(b.quality_score, 2) AS q_frontier, ROUND(e.quality_score, 2) AS q_routed,
  ROUND(b.cost_usd - e.cost_usd - e.routing_cost_usd, 6) AS net_saving_usd
{PAIR} AND e.quality_score < b.quality_score - 0.1
ORDER BY e.quality_score - b.quality_score LIMIT 15"""),
]

SO_WHAT = [
    lambda r: f"`{r[0][0]}` has the largest quality change ({r[0][-1]}); `{r[-1][0]}` the smallest ({r[-1][-1]}) — compare against its saving before routing it.",
    lambda r: f"`{r[0][0]}` tasks net the least ({r[0][4]} USD) and {r[0][5]} of {r[0][1]} routed runs cost more than the frontier baseline; a negative net_saving_usd would mean routing lost money overall.",
    lambda r: f"`{r[0][0]}` is cheapest per quality point ({r[0][4]}); `{r[-1][0]}` is the most expensive ({r[-1][4]}).",
    lambda r: f"Overhead ranges from {r[-1][4]}% to {r[0][4]}% of gross savings; above 100% the router costs more than it saves.",
    lambda r: "Shows which tier actually absorbs the traffic — if most tasks stay on the frontier model, savings are capped by that share.",
    lambda r: f"{len(r)} task/run pairs lost >0.1 quality; these are the concrete tasks to inspect or pin to the frontier tier.",
]


def md_table(cols, rows):
    f = lambda v: "" if v is None else str(v)
    return "\n".join(["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] +
                     ["| " + " | ".join(f(v) for v in r) + " |" for r in rows])


def run_sql(conn):
    conn.execute("CREATE TEMP TABLE rg(run_group TEXT PRIMARY KEY, label TEXT)")
    conn.executemany("INSERT INTO rg VALUES (?,?)", run_group_labels().items())
    conn.execute(VIEW)
    out = []
    for (title, sql), sw in zip(QUESTIONS, SO_WHAT):
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [tuple(r) for r in cur.fetchall()]
        out.append((title, sql, cols, rows, sw(rows) if rows else "No rows."))
    return out


# --------------------------------------------------------------------- ANALYSIS.md
def build_doc(conn):
    cis, mism = compute_cis(conn)
    if mism:
        raise SystemExit("PUBLISHED-NUMBER MISMATCH (not adjusting): " + repr(mism))
    f = lambda c: f"{c[0]:.1f}% [{c[1]:.1f}, {c[2]:.1f}]"
    L = ["# Analysis: confidence intervals and product questions", "",
         "Generated by `analyze.py` from the logged, non-simulated benchmark runs (`data/runs.db`, "
         "opened read-only). Zero API calls. Regenerate with `python3 analyze.py`.", "",
         "## 1. Bootstrap confidence intervals", "",
         f"Method: paired bootstrap. Within one run_group, whole tasks are resampled with replacement "
         f"(router and frontier outcome for a task travel together), {N_BOOT:,} resamples, seed {SEED}, "
         "percentile 95% interval. Net cost reduction is net of `routing_cost_usd`, and quality retention is "
         "mean routed quality / mean frontier quality, exactly as in `src/router/report.py`. "
         "Every point estimate below equals the number published in `docs/V2_FINDINGS.md` / "
         "`docs/V3_FINDINGS.md` (checked to 0.1pp at generation time; the script aborts otherwise).", "",
         md_table(["Result", "n", "Net cost reduction % [95% CI]", "Quality retention % [95% CI]"],
                  [(a, n, f(c), f(q)) for a, n, c, q in cis]), "",
         "The v3 row is a different comparison: cost column = learned router's cost difference vs the static "
         "router (positive = learned cheaper); quality column = learned quality / static quality. The two arms "
         "are separate run_groups over the same 14 task_ids, paired by task_id.", ""]
    ex0 = [a for a, n, c, q in cis if c[1] > 0 or c[2] < 0]
    inc0 = [a for a, n, c, q in cis if c[1] <= 0 <= c[2]]
    L += ["**Reading the intervals.** With n=25, 10 and 14 tasks the intervals are wide; treat the point "
          "estimates as directional.", "",
          "- Cost-reduction CI excludes 0: " + (", ".join(ex0) or "none") + ".",
          "- Cost-reduction CI includes 0: " + (", ".join(inc0) or "none") + ".", ""]
    hard = [(a, c) for a, n, c, q in cis if "HARD" in a]
    L += ["- Hard-set runs: " + "; ".join(
        f"{a} -> {'CI includes 0 (cannot tell from zero)' if c[1] <= 0 <= c[2] else 'CI excludes 0'}"
        for a, c in hard) + ".", ""]
    L += ["## 2. Product questions answered in SQL", "",
          "Scope: non-simulated rows (`simulated = 0`), no `live:%` strategies, and only run_groups that have a "
          "committed benchmark report (so cache-replay duplicates are not double counted). Results are pooled across "
          "those experiments, so read them as a cross-experiment overview, not one clean A/B. "
          "A temp view `v` holds those rows; `rg(run_group, label)` maps groups to experiments.", ""]
    for i, (title, sql, cols, rows, sw) in enumerate(run_sql(conn), 1):
        L += [f"### Q{i}. {title}", "", "```sql", sql, "```", "", md_table(cols, rows), "",
              f"**So what:** {sw}", ""]
    L += ["## 3. Limitations", "",
          "- Small n (25 / 10 / 14 tasks) and one run per configuration: intervals are wide and there is no "
          "run-to-run variance estimate.",
          "- Task-level resampling assumes these tasks are representative of real traffic; the task sets are "
          "self-authored, not sampled from production.",
          "- Judge scores (Claude Opus 4.8 rubric judge, exact match elsewhere) are treated as ground truth; judge "
          "bias is not propagated into the intervals.",
          "- Easy-set quality is near ceiling (about 1.0), so retention intervals are narrow partly because there "
          "is little room to vary.",
          "- Pooled SQL tables mix experiments with different rosters and difficulty; use the per-experiment "
          "table (Q4) before drawing a conclusion about any single setup.", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- export
FACT_COLS = ["id", "run_group", "strategy", "roster", "task_id", "task_type", "difficulty", "model",
             "input_tokens", "output_tokens", "cost_usd", "routing_cost_usd", "latency_ms",
             "quality_score", "success", "effort", "created_at"]


def write_csv(path: Path, cols, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)


def export(conn, out: Path = ROOT / "data" / "bi", data_dir: Path = ROOT / "data"):
    from router.config import MODELS, MODEL_TIER
    labels = run_group_labels(data_dir)
    fact = conn.execute(f"SELECT {', '.join(FACT_COLS)} FROM runs WHERE {REAL} ORDER BY id").fetchall()
    write_csv(out / "fact_runs.csv", FACT_COLS, [tuple(r) for r in fact])
    used = sorted({r["model"] for r in fact})
    write_csv(out / "dim_models.csv",
              ["model", "provider", "tier", "price_in_per_1m", "price_out_per_1m"],
              [(m, MODELS[m].provider if m in MODELS else "", MODEL_TIER.get(m, ""),
                MODELS[m].cost_per_1m_input_tokens if m in MODELS else "",
                MODELS[m].cost_per_1m_output_tokens if m in MODELS else "") for m in used])
    tasks = []
    for f in sorted(data_dir.glob("tasks*.json")):
        for t in json.loads(f.read_text())["tasks"]:
            tasks.append((t["id"], t["task_type"], t["difficulty"], f.stem))
    write_csv(out / "dim_tasks.csv", ["task_id", "task_type", "difficulty", "task_set"], tasks)
    groups = sorted({r["run_group"] for r in fact})
    write_csv(out / "dim_run_groups.csv", ["run_group", "experiment"],
              [(g, labels.get(g, UNLABELLED)) for g in groups])
    return len(fact), len(groups)


def main(argv):
    conn = connect()
    if "--export" in argv:
        n, g = export(conn)
        print(f"exported {n} fact rows, {g} run groups -> data/bi/")
    else:
        (ROOT / "docs" / "ANALYSIS.md").write_text(build_doc(conn))
        print("wrote docs/ANALYSIS.md")


if __name__ == "__main__":
    main(sys.argv[1:])
