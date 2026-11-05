# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20261105T192443-526ca924`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.217072 | 0.870 | 300 |
| frontier_only (baseline) | $1.786560 | 0.882 | 300 |

## Headline

- **Cost reduction vs baseline:** 86.4% (net of routing overhead)
- **Quality retention:** 98.7% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): PASSED**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.217072 |
| Routing spend (classifier and discarded answers) | $0.026682 |
| **Router total, net** | **$0.243754** |
| Cost reduction, gross | 87.8% |
| **Cost reduction, net** | **86.4%** |
| **Routing overhead as % of savings** | **1.7%** |
| Classifier agreement with hand labels | 98.3% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 2940.7 | 2238.2 | 300 |
| frontier_only (baseline) | 3577.5 | 1573.0 | 300 |

- **Router is 17.8% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.000813, frontier_only (baseline) $0.005955. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $595.52 | $81.25 | $514.27 |
| 1,000,000 | $5,955.20 | $812.51 | $5,142.69 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.494765 | 0.802 | 96 |
| classification | router | $0.131760 | 0.792 | 96 |
| extraction | frontier_only | $0.173690 | 0.905 | 84 |
| extraction | router | $0.017004 | 0.893 | 84 |
| reasoning | frontier_only | $0.190515 | 0.938 | 48 |
| reasoning | router | $0.021101 | 0.917 | 48 |
| short_generation | frontier_only | $0.927590 | 0.924 | 72 |
| short_generation | router | $0.047207 | 0.918 | 72 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | frontier_only | $0.173690 | 0.905 | 84 |
| easy | router | $0.017004 | 0.893 | 84 |
| hard | frontier_only | $0.119880 | 0.875 | 24 |
| hard | router | $0.016299 | 0.833 | 24 |
| medium | frontier_only | $1.492990 | 0.873 | 192 |
| medium | router | $0.183768 | 0.865 | 192 |
