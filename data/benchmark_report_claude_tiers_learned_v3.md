# AI Workload Router — Benchmark Report

**LIVE RESULTS** — every model in this run made real provider API calls (Anthropic/OpenAI/DeepSeek), cached to disk under `.cache/` for free, reproducible re-runs. The `rubric_judge` scores came from the real claude-opus-4-8 judge.

Run group: `20260719T113315-b7ff3a62`

## Strategy comparison

| Strategy | Total cost (USD) | Mean quality | N |
|---|---|---|---|
| router | $0.014319 | 1.000 | 14 |
| frontier_only (baseline) | $0.019370 | 1.000 | 14 |

## Headline

- **Cost reduction vs baseline:** 26.1% (net of routing overhead)
- **Quality retention:** 100.0% of baseline
- **Hypothesis (>= 40% cost reduction, >= 95% quality retention): NOT MET**

## Routing overhead

This run routed on the task set's hand-authored `(task_type, difficulty)` labels, so routing cost nothing and the net and gross savings figures are identical. Note that a real deployment does not have those labels — it has a prompt. Re-run with `--classify` to pay for predicting them and see the net figure move.

## Latency

Wall-clock latency per run — real on live calls, the stored value on cache replays (see router.adapters.cache).

| Strategy | Mean latency (ms) | Median latency (ms) | N |
|---|---|---|---|
| router | 1862.2 | 1047.7 | 14 |
| frontier_only (baseline) | 1949.2 | 1730.7 | 14 |

- **Router is 4.5% faster** than the frontier_only baseline on average (mean latency).

## At-scale cost projection

Cost per task in this run: router $0.001023, frontier_only (baseline) $0.001384. Projected monthly cost at scale, extrapolating linearly from that per-task rate:

| Volume (requests/month) | Baseline monthly $ | Router monthly $ | Savings/month $ |
|---|---|---|---|
| 100,000 | $138.36 | $102.28 | $36.08 |
| 1,000,000 | $1,383.57 | $1,022.79 | $360.79 |

*Caveat: this projection assumes production traffic resembles this benchmark's task-type/difficulty mix — real traffic will differ, so treat it as directional, not a committed forecast.*

## By task type

| task_type | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| classification | frontier_only | $0.002210 | 1.000 | 4 |
| classification | router | $0.000830 | 1.000 | 4 |
| extraction | frontier_only | $0.003340 | 1.000 | 4 |
| extraction | router | $0.000431 | 1.000 | 4 |
| reasoning | frontier_only | $0.012745 | 1.000 | 5 |
| reasoning | router | $0.011983 | 1.000 | 5 |
| short_generation | frontier_only | $0.001075 | 1.000 | 1 |
| short_generation | router | $0.001075 | 1.000 | 1 |

## By difficulty

| difficulty | strategy | cost | mean_quality | n |
|---|---|---|---|---|
| hard | frontier_only | $0.014730 | 1.000 | 9 |
| hard | router | $0.011484 | 1.000 | 9 |
| medium | frontier_only | $0.004640 | 1.000 | 5 |
| medium | router | $0.002835 | 1.000 | 5 |
