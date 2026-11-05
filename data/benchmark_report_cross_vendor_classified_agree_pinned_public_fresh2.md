# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20261105T175936-c27f3a54`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.581798 | 0.877 | 300 |
| frontier_only (baseline) | $1.786560 | 0.882 | 300 |

## Headline

- **Cost reduction vs baseline:** 65.7% (net of routing overhead)
- **Quality retention:** 99.5% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): PASSED**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.581798 |
| Routing (classifier) spend | $0.031455 |
| **Router total, net** | **$0.613253** |
| Cost reduction, gross | 67.4% |
| **Cost reduction, net** | **65.7%** |
| **Routing overhead as % of savings** | **2.6%** |
| Classifier agreement with hand labels | 54.7% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 2624.6 | 1964.6 | 300 |
| frontier_only (baseline) | 3577.5 | 1573.0 | 300 |

- **Router is 26.6% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.002044, frontier_only (baseline) $0.005955. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $595.52 | $204.42 | $391.10 |
| 1,000,000 | $5,955.20 | $2,044.18 | $3,911.02 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.494765 | 0.802 | 96 |
| classification | router | $0.130387 | 0.792 | 96 |
| extraction | frontier_only | $0.173690 | 0.905 | 84 |
| extraction | router | $0.173690 | 0.905 | 84 |
| reasoning | frontier_only | $0.190515 | 0.938 | 48 |
| reasoning | router | $0.164309 | 0.938 | 48 |
| short_generation | frontier_only | $0.927590 | 0.924 | 72 |
| short_generation | router | $0.113411 | 0.920 | 72 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | frontier_only | $0.173690 | 0.905 | 84 |
| easy | router | $0.173690 | 0.905 | 84 |
| hard | frontier_only | $0.119880 | 0.875 | 24 |
| hard | router | $0.093674 | 0.875 | 24 |
| medium | frontier_only | $1.492990 | 0.873 | 192 |
| medium | router | $0.314433 | 0.866 | 192 |
