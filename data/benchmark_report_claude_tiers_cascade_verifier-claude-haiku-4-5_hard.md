# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20260717T204417-7af1dd0c`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| cascade | $0.002649 | 0.700 | 10 |
| frontier_only (baseline) | $0.019715 | 1.000 | 10 |

## Headline

- **Cost reduction vs baseline:** 73.8% (net of routing overhead)
- **Quality retention:** 70.0% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): NOT MET**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.002649 |
| Routing (classifier) spend | $0.002510 |
| **Router total, net** | **$0.005159** |
| Cost reduction, gross | 86.6% |
| **Cost reduction, net** | **73.8%** |
| **Routing overhead as % of savings** | **14.7%** |


## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 0.0 | 0.0 | 0 |
| frontier_only (baseline) | 2398.0 | 1695.5 | 10 |

- **Router is 47.4% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: cascade $0.000516, frontier_only (baseline) $0.001972. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $197.15 | $51.59 | $145.56 |
| 1,000,000 | $1,971.50 | $515.90 | $1,455.60 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | cascade | $0.000152 | 0.500 | 2 |
| classification | frontier_only | $0.001045 | 1.000 | 2 |
| extraction | cascade | $0.001060 | 1.000 | 3 |
| extraction | frontier_only | $0.003915 | 1.000 | 3 |
| reasoning | cascade | $0.001190 | 0.500 | 4 |
| reasoning | frontier_only | $0.013460 | 1.000 | 4 |
| short_generation | cascade | $0.000247 | 1.000 | 1 |
| short_generation | frontier_only | $0.001295 | 1.000 | 1 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| hard | cascade | $0.002649 | 0.700 | 10 |
| hard | frontier_only | $0.019715 | 1.000 | 10 |
