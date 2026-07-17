# v2 Findings — the full strategy matrix

One page tying together every v2 experiment. All numbers are **live** (real API
calls, real Claude Opus 4.8 judge), n=25 for the published set and n=10 for the
hard set. Each row links to the committed report that produced it.

> These are directional, not statistically certified (small n; the Opus judge
> scores generously on the easy set). The value here is the *shape* of the
> results across conditions, not any single decimal.

## The matrix

| Roster | Strategy | Task set | Net cost ↓ | Quality | Overhead (% of savings) | Report |
|---|---|---|---|---|---|---|
| cross-vendor | labels (v1) | easy | **53.6%** | 100% | 0% (free labels) | [benchmark_report.md](../data/benchmark_report.md) |
| cross-vendor | **classifier** | easy | **65.6%** | 99.8% | 1.0% | […cross_vendor_classified](../data/benchmark_report_cross_vendor_classified.md) |
| claude-tiers | classifier | easy | **42.5%** | 100.3% | 11.3% | [.…claude_tiers_classified](../data/benchmark_report_claude_tiers_classified.md) |
| claude-tiers | **cascade** | easy | **53.1%** | 99.6% | 31.1% | [.…claude_tiers_cascade](../data/benchmark_report_claude_tiers_cascade.md) |
| claude-tiers | classifier | **hard** | **−6.3%** | 100% | (savings ≤ 0) | [.…classified_hard](../data/benchmark_report_claude_tiers_classified_hard.md) |
| claude-tiers | cascade | **hard** | **−19.9%** | 100% | 163.7% | [.…cascade_hard](../data/benchmark_report_claude_tiers_cascade_hard.md) |

## The (model × effort) frontier

Full table in [effort_grid.md](../data/effort_grid.md). The two lines that matter:

- **Sonnet 5 @ low effort dominates Opus 4.8 @ no-thinking** — 39% cheaper *and*
  fractionally higher quality. Dropping a tier and adding a little thinking beats
  staying on the frontier.
- **Opus effort is wasted here** — high effort adds ~20% cost for ~0 quality.
  The expensive end of the effort curve is flat on this workload.

## What each experiment answered

1. **Classifier vs hand labels (65.6% vs 53.6%).** Replacing hand-labeled
   difficulty with a cheap prompt classifier — what a real deployment must do —
   *raised* savings. On 7/25 tasks it routed cheaper than the labels; the judge
   scored them all 0.85–1.0. The labels were conservative; the classifier found
   the slack. Overhead was ~1% of savings.

2. **Within-vendor ceiling (42.5%).** A single-vendor ladder has ~5× price range
   vs ~41× cross-vendor, so its ceiling is lower — exactly as the price
   arithmetic predicts. Overhead is worse too (11%), because Haiku (the cheapest
   available classifier) isn't as cheap as GPT-4o mini.

3. **Cascade beats classifier within-vendor (53.1% vs 42.5%).** React-to-failure
   (try cheap, verify, escalate) out-saves predict-then-route by being less
   conservative — it tries Haiku on everything instead of pre-routing reasoning
   to Opus. It pays for it: 31% overhead and higher latency.

4. **Hard tasks flip the sign.** On a task set that actually separates the tiers
   (Haiku 0.70 / Sonnet 0.90 / Opus 1.00), both strategies go **cost-negative**
   while holding 100% quality. Every hard task needs the frontier, so routing
   only adds overhead — but it never trades quality away to discover that.

## The through-line

**Routing's savings are a direct function of how much genuinely-easy work a
workload contains.** They peak on easy, mixed traffic; they collapse to zero
(or below) on all-hard traffic. This is the demonstrated form of v1's insight:
*ceiling = workload composition × price range.* Quality, across every condition
and both strategies, was never the variable that gave — the failure mode of
over-aggressive routing here is wasted money, not degraded output.

**The buyer's decision rule that falls out of it:** measure your easy-task
fraction before adopting a router. If most of your traffic genuinely needs a
frontier model, a router will cost you money, not save it.

## Reproduce

```bash
python3 run_benchmark.py --roster cross_vendor --classify
python3 run_benchmark.py --roster claude_tiers  --classify
python3 run_benchmark.py --roster claude_tiers  --strategy cascade
python3 run_benchmark.py --roster claude_tiers  --strategy cascade --tasks data/tasks_hard.json
python3 run_effort_grid.py
```

First run makes live calls (for whichever provider keys are in `.env`) and
caches them; every run after is free and identical.
