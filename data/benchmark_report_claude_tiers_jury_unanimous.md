# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20261020T080621-38d36e09`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| jury | $0.020170 | 0.990 | 25 |
| frontier_only (baseline) | $0.125980 | 0.992 | 25 |

## Headline

- **Cost reduction vs baseline:** 66.1% (net of routing overhead)
- **Quality retention:** 99.8% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): PASSED**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.020170 |
| Routing (classifier) spend | $0.022574 |
| **Router total, net** | **$0.042744** |
| Cost reduction, gross | 84.0% |
| **Cost reduction, net** | **66.1%** |
| **Routing overhead as % of savings** | **21.3%** |


## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 0.0 | 0.0 | 0 |
| frontier_only (baseline) | 4155.2 | 3662.6 | 25 |

- **Router is 63.2% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: jury $0.001710, frontier_only (baseline) $0.005039. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $503.92 | $170.98 | $332.94 |
| 1,000,000 | $5,039.20 | $1,709.76 | $3,329.44 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.049595 | 0.994 | 8 |
| classification | jury | $0.000222 | 0.988 | 8 |
| extraction | frontier_only | $0.014525 | 1.000 | 7 |
| extraction | jury | $0.000173 | 1.000 | 7 |
| reasoning | frontier_only | $0.041715 | 1.000 | 4 |
| reasoning | jury | $0.019607 | 1.000 | 4 |
| short_generation | frontier_only | $0.020145 | 0.975 | 6 |
| short_generation | jury | $0.000168 | 0.975 | 6 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | frontier_only | $0.046580 | 0.988 | 13 |
| easy | jury | $0.000240 | 1.000 | 13 |
| hard | frontier_only | $0.026475 | 1.000 | 4 |
| hard | jury | $0.000259 | 0.988 | 4 |
| medium | frontier_only | $0.052925 | 0.994 | 8 |
| medium | jury | $0.019672 | 0.975 | 8 |
