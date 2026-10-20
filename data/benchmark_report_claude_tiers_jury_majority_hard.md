# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20261020T080613-682c20d2`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| jury | $0.009762 | 0.760 | 10 |
| frontier_only (baseline) | $0.019715 | 1.000 | 10 |

## Headline

- **Cost reduction vs baseline:** 25.1% (net of routing overhead)
- **Quality retention:** 76.0% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): NOT MET**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.009762 |
| Routing (classifier) spend | $0.004998 |
| **Router total, net** | **$0.014760** |
| Cost reduction, gross | 50.5% |
| **Cost reduction, net** | **25.1%** |
| **Routing overhead as % of savings** | **50.2%** |


## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 0.0 | 0.0 | 0 |
| frontier_only (baseline) | 2398.0 | 1695.5 | 10 |

- **Router is 48.1% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: jury $0.001476, frontier_only (baseline) $0.001972. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $197.15 | $147.60 | $49.55 |
| 1,000,000 | $1,971.50 | $1,476.02 | $495.48 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.001045 | 1.000 | 2 |
| classification | jury | $0.000017 | 0.500 | 2 |
| extraction | frontier_only | $0.003915 | 1.000 | 3 |
| extraction | jury | $0.000054 | 0.667 | 3 |
| reasoning | frontier_only | $0.013460 | 1.000 | 4 |
| reasoning | jury | $0.009668 | 1.000 | 4 |
| short_generation | frontier_only | $0.001295 | 1.000 | 1 |
| short_generation | jury | $0.000023 | 0.600 | 1 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| hard | frontier_only | $0.019715 | 1.000 | 10 |
| hard | jury | $0.009762 | 0.760 | 10 |
