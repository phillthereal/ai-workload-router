# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20260717T172816-428aceeb`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.018082 | 1.000 | 10 |
| frontier_only (baseline) | $0.019715 | 1.000 | 10 |

## Headline

- **Cost reduction vs baseline:** -6.3% (net of routing overhead)
- **Quality retention:** 100.0% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): NOT MET**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.018082 |
| Routing (classifier) spend | $0.002876 |
| **Router total, net** | **$0.020958** |
| Cost reduction, gross | 8.3% |
| **Cost reduction, net** | **-6.3%** |
| **Routing overhead as % of savings** | **176.1%** |
| Classifier agreement with hand labels | 20.0% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 2125.3 | 1695.5 | 10 |
| frontier_only (baseline) | 2398.0 | 1695.5 | 10 |

- **Router is 11.4% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.002096, frontier_only (baseline) $0.001972. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $197.15 | $209.58 | $-12.43 |
| 1,000,000 | $1,971.50 | $2,095.80 | $-124.30 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.001045 | 1.000 | 2 |
| classification | router | $0.001045 | 1.000 | 2 |
| extraction | frontier_only | $0.003915 | 1.000 | 3 |
| extraction | router | $0.002282 | 1.000 | 3 |
| reasoning | frontier_only | $0.013460 | 1.000 | 4 |
| reasoning | router | $0.013460 | 1.000 | 4 |
| short_generation | frontier_only | $0.001295 | 1.000 | 1 |
| short_generation | router | $0.001295 | 1.000 | 1 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| hard | frontier_only | $0.019715 | 1.000 | 10 |
| hard | router | $0.018082 | 1.000 | 10 |
