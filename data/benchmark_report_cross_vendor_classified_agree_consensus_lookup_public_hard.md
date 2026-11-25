# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20261124T181625-fda70f0c`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.029660 | 0.863 | 40 |
| frontier_only (baseline) | $0.242940 | 0.950 | 40 |

## Headline

- **Cost reduction vs baseline:** 83.5% (net of routing overhead)
- **Quality retention:** 90.8% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): NOT MET**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.029660 |
| Routing spend (classifier and discarded answers) | $0.010530 |
| **Router total, net** | **$0.040190** |
| Cost reduction, gross | 87.8% |
| **Cost reduction, net** | **83.5%** |
| **Routing overhead as % of savings** | **4.9%** |
| Classifier agreement with hand labels | 75.0% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 4594.6 | 2335.2 | 40 |
| frontier_only (baseline) | 3125.4 | 2091.0 | 40 |

- **Router is 47.0% slower** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.001005, frontier_only (baseline) $0.006073. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $607.35 | $100.47 | $506.88 |
| 1,000,000 | $6,073.50 | $1,004.75 | $5,068.75 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.002855 | 1.000 | 2 |
| classification | router | $0.000039 | 0.500 | 2 |
| extraction | frontier_only | $0.006700 | 1.000 | 3 |
| extraction | router | $0.000172 | 1.000 | 3 |
| reasoning | frontier_only | $0.232065 | 0.941 | 34 |
| reasoning | router | $0.029403 | 0.882 | 34 |
| short_generation | frontier_only | $0.001320 | 1.000 | 1 |
| short_generation | router | $0.000046 | 0.500 | 1 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| hard | frontier_only | $0.242940 | 0.950 | 40 |
| hard | router | $0.029660 | 0.863 | 40 |
