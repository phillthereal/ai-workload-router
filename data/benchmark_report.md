# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20260717T082024-098a7982`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.058506 | 0.996 | 25 |
| frontier_only (baseline) | $0.125980 | 0.992 | 25 |

## Headline

- **Cost reduction vs baseline:** 53.6%
- **Quality retention:** 100.4% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): PASSED**

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 2237.1 | 1207.9 | 25 |
| frontier_only (baseline) | 4155.2 | 3662.6 | 25 |

- **Router is 46.2% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.002340, frontier_only (baseline) $0.005039. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $503.92 | $234.03 | $269.89 |
| 1,000,000 | $5,039.20 | $2,340.26 | $2,698.94 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.049595 | 0.994 | 8 |
| classification | router | $0.006790 | 1.000 | 8 |
| extraction | frontier_only | $0.014525 | 1.000 | 7 |
| extraction | router | $0.007073 | 1.000 | 7 |
| reasoning | frontier_only | $0.041715 | 1.000 | 4 |
| reasoning | router | $0.041715 | 1.000 | 4 |
| short_generation | frontier_only | $0.020145 | 0.975 | 6 |
| short_generation | router | $0.002928 | 0.983 | 6 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | frontier_only | $0.046580 | 0.988 | 13 |
| easy | router | $0.012170 | 1.000 | 13 |
| hard | frontier_only | $0.026475 | 1.000 | 4 |
| hard | router | $0.026475 | 1.000 | 4 |
| medium | frontier_only | $0.052925 | 0.994 | 8 |
| medium | router | $0.019862 | 0.988 | 8 |
