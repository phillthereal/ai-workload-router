# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20261105T095044-1b2f8b62`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.189740 | 0.912 | 40 |
| frontier_only (baseline) | $0.246615 | 0.950 | 40 |

## Headline

- **Cost reduction vs baseline:** 22.2% (net of routing overhead)
- **Quality retention:** 96.1% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): NOT MET**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.189740 |
| Routing (classifier) spend | $0.002050 |
| **Router total, net** | **$0.191789** |
| Cost reduction, gross | 23.1% |
| **Cost reduction, net** | **22.2%** |
| **Routing overhead as % of savings** | **3.6%** |
| Classifier agreement with hand labels | 12.5% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 2928.5 | 1892.7 | 40 |
| frontier_only (baseline) | 3390.9 | 2201.1 | 40 |

- **Router is 13.6% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.004795, frontier_only (baseline) $0.006165. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $616.54 | $479.47 | $137.06 |
| 1,000,000 | $6,165.38 | $4,794.73 | $1,370.64 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.003730 | 1.000 | 2 |
| classification | router | $0.003730 | 1.000 | 2 |
| extraction | frontier_only | $0.006050 | 1.000 | 3 |
| extraction | router | $0.002947 | 1.000 | 3 |
| reasoning | frontier_only | $0.235465 | 0.941 | 34 |
| reasoning | router | $0.183022 | 0.912 | 34 |
| short_generation | frontier_only | $0.001370 | 1.000 | 1 |
| short_generation | router | $0.000040 | 0.500 | 1 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| hard | frontier_only | $0.246615 | 0.950 | 40 |
| hard | router | $0.189740 | 0.912 | 40 |
