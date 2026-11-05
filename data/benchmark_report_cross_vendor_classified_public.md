# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20261105T092337-0eb276cf`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.271901 | 0.790 | 300 |
| frontier_only (baseline) | $1.829370 | 0.861 | 300 |

## Headline

- **Cost reduction vs baseline:** 84.1% (net of routing overhead)
- **Quality retention:** 91.8% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): NOT MET**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.271901 |
| Routing (classifier) spend | $0.018504 |
| **Router total, net** | **$0.290404** |
| Cost reduction, gross | 85.1% |
| **Cost reduction, net** | **84.1%** |
| **Routing overhead as % of savings** | **1.2%** |
| Classifier agreement with hand labels | 54.0% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 1810.4 | 935.4 | 300 |
| frontier_only (baseline) | 3530.3 | 1537.5 | 300 |

- **Router is 48.7% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.000968, frontier_only (baseline) $0.006098. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $609.79 | $96.80 | $512.99 |
| 1,000,000 | $6,097.90 | $968.01 | $5,129.89 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.494230 | 0.792 | 96 |
| classification | router | $0.007903 | 0.635 | 96 |
| extraction | frontier_only | $0.181135 | 0.869 | 84 |
| extraction | router | $0.003860 | 0.798 | 84 |
| reasoning | frontier_only | $0.227640 | 0.938 | 48 |
| reasoning | router | $0.157339 | 0.917 | 48 |
| short_generation | frontier_only | $0.926365 | 0.892 | 72 |
| short_generation | router | $0.102799 | 0.901 | 72 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | frontier_only | $0.181135 | 0.869 | 84 |
| easy | router | $0.003860 | 0.798 | 84 |
| hard | frontier_only | $0.146955 | 0.917 | 24 |
| hard | router | $0.076654 | 0.875 | 24 |
| medium | frontier_only | $1.501280 | 0.850 | 192 |
| medium | router | $0.191387 | 0.776 | 192 |
