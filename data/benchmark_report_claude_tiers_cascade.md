# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20260717T171702-e98b914b`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| cascade | $0.028948 | 0.988 | 25 |
| frontier_only (baseline) | $0.125980 | 0.992 | 25 |

## Headline

- **Cost reduction vs baseline:** 53.1% (net of routing overhead)
- **Quality retention:** 99.6% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): PASSED**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.028948 |
| Routing (classifier) spend | $0.030141 |
| **Router total, net** | **$0.059089** |
| Cost reduction, gross | 77.0% |
| **Cost reduction, net** | **53.1%** |
| **Routing overhead as % of savings** | **31.1%** |


## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 0.0 | 0.0 | 0 |
| frontier_only (baseline) | 4155.2 | 3662.6 | 25 |

- **Router is 45.6% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: cascade $0.002364, frontier_only (baseline) $0.005039. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $503.92 | $236.36 | $267.56 |
| 1,000,000 | $5,039.20 | $2,363.56 | $2,675.64 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | cascade | $0.003911 | 0.988 | 8 |
| classification | frontier_only | $0.049595 | 0.994 | 8 |
| extraction | cascade | $0.001615 | 1.000 | 7 |
| extraction | frontier_only | $0.014525 | 1.000 | 7 |
| reasoning | cascade | $0.021665 | 1.000 | 4 |
| reasoning | frontier_only | $0.041715 | 1.000 | 4 |
| short_generation | cascade | $0.001757 | 0.967 | 6 |
| short_generation | frontier_only | $0.020145 | 0.975 | 6 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | cascade | $0.003180 | 1.000 | 13 |
| easy | frontier_only | $0.046580 | 0.988 | 13 |
| hard | cascade | $0.002854 | 0.975 | 4 |
| hard | frontier_only | $0.026475 | 1.000 | 4 |
| medium | cascade | $0.022914 | 0.975 | 8 |
| medium | frontier_only | $0.052925 | 0.994 | 8 |
