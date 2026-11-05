# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20261105T142952-990bf63a`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.430709 | 0.820 | 300 |
| frontier_only (baseline) | $1.868720 | 0.887 | 300 |

## Headline

- **Cost reduction vs baseline:** 75.4% (net of routing overhead)
- **Quality retention:** 92.5% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): NOT MET**

## Routing overhead

This run predicted each task's `(task_type, difficulty)` from the prompt using the roster's budget model, rather than reading a hand-authored label. That is what a real deployment has — and it means the router costs money to run. That cost is charged against the savings below, not excluded from them.

| Metric | Value |
|---|---|
| Router model spend | $0.430709 |
| Routing (classifier) spend | $0.029603 |
| **Router total, net** | **$0.460312** |
| Cost reduction, gross | 77.0% |
| **Cost reduction, net** | **75.4%** |
| **Routing overhead as % of savings** | **2.1%** |
| Classifier agreement with hand labels | 55.7% |

*Agreement is measured against labels authored by one person for this task set. It is a sanity check, not an accuracy benchmark: a disagreement means the classifier and the author differ, not that the classifier is wrong.*

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 2537.1 | 1996.5 | 300 |
| frontier_only (baseline) | 3671.3 | 1586.4 | 300 |

- **Router is 30.9% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.001534, frontier_only (baseline) $0.006229. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $622.91 | $153.44 | $469.47 |
| 1,000,000 | $6,229.07 | $1,534.37 | $4,694.69 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.491140 | 0.812 | 96 |
| classification | router | $0.115146 | 0.792 | 96 |
| extraction | frontier_only | $0.177095 | 0.917 | 84 |
| extraction | router | $0.003700 | 0.738 | 84 |
| reasoning | frontier_only | $0.233690 | 0.938 | 48 |
| reasoning | router | $0.227651 | 0.938 | 48 |
| short_generation | frontier_only | $0.966795 | 0.917 | 72 |
| short_generation | router | $0.084211 | 0.876 | 72 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| easy | frontier_only | $0.177095 | 0.917 | 84 |
| easy | router | $0.003700 | 0.738 | 84 |
| hard | frontier_only | $0.157210 | 0.875 | 24 |
| hard | router | $0.151171 | 0.875 | 24 |
| medium | frontier_only | $1.534415 | 0.875 | 192 |
| medium | router | $0.275838 | 0.849 | 192 |
