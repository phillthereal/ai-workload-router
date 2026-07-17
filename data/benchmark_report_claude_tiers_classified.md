# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20260717T164758-d2d01eaa`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.065656 | 0.995 | 25 |
| frontier_only (baseline) | $0.125980 | 0.992 | 25 |

## Headline

- **Cost reduction vs baseline:** 42.5% (net of routing overhead)
- **Quality retention:** 100.3% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): PASSED**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.065656 |
| Routing (classifier) spend | $0.006834 |
| **Router total, net** | **$0.072490** |
| Cost reduction, gross | 47.9% |
| **Cost reduction, net** | **42.5%** |
| **Routing overhead as % of savings** | **11.3%** |
| Classifier agreement with hand labels | 60.0% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 2709.6 | 1704.0 | 25 |
| frontier_only (baseline) | 4155.2 | 3662.6 | 25 |

- **Router is 34.8% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.002900, frontier_only (baseline) $0.005039. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $503.92 | $289.96 | $213.96 |
| 1,000,000 | $5,039.20 | $2,899.60 | $2,139.60 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.049595 | 0.994 | 8 |
| classification | router | $0.019293 | 0.996 | 8 |
| extraction | frontier_only | $0.014525 | 1.000 | 7 |
| extraction | router | $0.001615 | 1.000 | 7 |
| reasoning | frontier_only | $0.041715 | 1.000 | 4 |
| reasoning | router | $0.041715 | 1.000 | 4 |
| short_generation | frontier_only | $0.020145 | 0.975 | 6 |
| short_generation | router | $0.003033 | 0.983 | 6 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | frontier_only | $0.046580 | 0.988 | 13 |
| easy | router | $0.014118 | 1.000 | 13 |
| hard | frontier_only | $0.026475 | 1.000 | 4 |
| hard | router | $0.017806 | 1.000 | 4 |
| medium | frontier_only | $0.052925 | 0.994 | 8 |
| medium | router | $0.033732 | 0.984 | 8 |
