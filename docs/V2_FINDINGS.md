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
| claude-tiers | cascade, **Haiku verifier** | easy | **85.0%** | 99.6% | 6.2% | [.…verifier-haiku](../data/benchmark_report_claude_tiers_cascade_verifier-claude-haiku-4-5.md) |
| claude-tiers | cascade, **Haiku verifier** | **hard** | **73.8%** | **70.0%** | 14.7% | [.…verifier-haiku_hard](../data/benchmark_report_claude_tiers_cascade_verifier-claude-haiku-4-5_hard.md) |

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
   *This holds for the cascade's default (mid/Sonnet) verifier specifically —
   see "Verifier economics" below for what happens when the verifier itself
   is cheapened: on hard tasks, it does start trading quality away.*

## Verifier economics: does a cheaper verifier still gate correctly?

The cascade's verifier is configurable (`run_cascade(verifier_model=...)`, or
`run_benchmark.py --verifier`) and **defaults to the roster's MID tier** —
the independent grader. The cheap (BUDGET) verifier is a deliberate opt-in,
and the live runs below are why: it is a clean win on the easy set but a
quality failure on the hard one, and a default must uphold the system's
never-trade-quality guarantee on workloads it hasn't seen. Both verifiers
were run live against the same two task sets:

| Task set | Overhead, Sonnet verifier | Overhead, Haiku verifier | Quality, Sonnet | Quality, Haiku | Escalated, Sonnet | Escalated, Haiku |
|---|---|---|---|---|---|---|
| easy (n=25) | 31.1% of savings | **6.2%** | 0.988 (99.6% retention) | 0.988 (99.6% retention, unchanged) | 2/25 | 0/25 |
| hard (n=10) | 163.7% of savings | **14.7%** | 1.000 (100% retention) | **0.700 (70% retention)** | 5/10 | 1/10 |

**Result: mixed, and the hard-set half is a real finding, not noise.** On the
easy set the cheaper verifier is a clean win — overhead collapses (31.1% →
6.2%) and quality doesn't move at all, because the budget model's answers
were already good enough that the verifier's leniency never gets tested. On
the hard set the same swap is a **false economy**: overhead falls even
further in relative terms (163.7% → 14.7%), but mean quality drops from
1.000 to 0.700, well under the 95% retention bar the benchmark's hypothesis
requires (hypothesis: NOT MET, cost target passed, quality target failed).

**Root cause: self-verification circularity, exactly as the docstring warns.**
Three of the ten hard tasks (`cls-h02`, `rsn-h01`, `rsn-h04`) got a Haiku
answer the independent Opus judge scored **0.0** — completely wrong — yet the
Haiku verifier rated its own answer 0.95–1.0 (comfortably above the 0.7
accept threshold) and did not escalate. The one hard task Haiku *did*
escalate (`ext-h01`) is the one where its self-score happened to be 0.0 too —
the gate only worked when the model's own uncertainty happened to coincide
with actually being wrong, which is not something to rely on. The
independent Sonnet verifier, grading a different model's answer, caught these
same failures and escalated 5/10 hard tasks, holding 100% quality at far
higher (163.7%) overhead.

**The buyer's decision rule, extended:** the same "measure your easy-task
fraction" rule from the through-line below applies to the verifier choice
itself, not just to whether to route at all. A budget (self-verifying)
verifier is safe and cheap when the traffic is known to be within the budget
model's competence. The moment traffic can contain genuinely hard tasks, a
budget verifier cannot be trusted to catch its own model's failures — use an
independent (mid-tier or higher) verifier there, and pay the overhead that
independence costs.

*Live spend for this confirmation: ~$0.01 (35 new Haiku verifier calls,
~8.2K input / ~245 output tokens total; every answer call and Opus judge call
was already cached from the two runs above, so this experiment reused rather
than re-paid for them).*

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
python3 run_benchmark.py --roster claude_tiers  --strategy cascade --verifier claude-haiku-4-5
python3 run_benchmark.py --roster claude_tiers  --strategy cascade --verifier claude-haiku-4-5 --tasks data/tasks_hard.json
python3 run_effort_grid.py
```

First run makes live calls (for whichever provider keys are in `.env`) and
caches them; every run after is free and identical.
