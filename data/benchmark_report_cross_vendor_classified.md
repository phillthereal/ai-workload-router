# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20260717T164636-ee1ddc99`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.042477 | 0.990 | 25 |
| frontier_only (baseline) | $0.125980 | 0.992 | 25 |

## Headline

- **Cost reduction vs baseline:** 65.6% (net of routing overhead)
- **Quality retention:** 99.8% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): PASSED**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.042477 |
| Routing (classifier) spend | $0.000822 |
| **Router total, net** | **$0.043299** |
| Cost reduction, gross | 66.3% |
| **Cost reduction, net** | **65.6%** |
| **Routing overhead as % of savings** | **1.0%** |
| Classifier agreement with hand labels | 68.0% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 1989.3 | 1201.2 | 25 |
| frontier_only (baseline) | 4155.2 | 3662.6 | 25 |

- **Router is 52.1% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.001732, frontier_only (baseline) $0.005039. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $503.92 | $173.20 | $330.72 |
| 1,000,000 | $5,039.20 | $1,731.97 | $3,307.23 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.049595 | 0.994 | 8 |
| classification | router | $0.000294 | 1.000 | 8 |
| extraction | frontier_only | $0.014525 | 1.000 | 7 |
| extraction | router | $0.000266 | 1.000 | 7 |
| reasoning | frontier_only | $0.041715 | 1.000 | 4 |
| reasoning | router | $0.041715 | 1.000 | 4 |
| short_generation | frontier_only | $0.020145 | 0.975 | 6 |
| short_generation | router | $0.000202 | 0.958 | 6 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | frontier_only | $0.046580 | 0.988 | 13 |
| easy | router | $0.012170 | 1.000 | 13 |
| hard | frontier_only | $0.026475 | 1.000 | 4 |
| hard | router | $0.010566 | 0.963 | 4 |
| medium | frontier_only | $0.052925 | 0.994 | 8 |
| medium | router | $0.019741 | 0.988 | 8 |
