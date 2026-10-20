# V5: budget governor + shadow sampling (offline simulation)

Code: `src/router/governors.py` (`BudgetPolicy`, `ShadowPolicy`), simulation: `simulate_v5.py`
(reproduce with `python3 simulate_v5.py`). **Zero API spend**: everything below resamples
logged outcomes from `data/runs.db` (opened read-only).

## What was built
- **BudgetPolicy(inner, monthly_budget_usd, period_start, protected_task_types=("reasoning",), hard_cap_usd=None)**.
  Spend-to-date is an in-memory accumulator (optionally `seed_from_log()` from live rows). Pace is linear in
  whole days (day 0 already has 1/30 of the budget). Over pace: unprotected task types go one tier cheaper.
  Spend at or over the hard cap (default = the budget): unprotected go to the cheapest tier; protected types
  keep the inner decision (frontier allowed), so they can push spend past the cap; every request's trace says
  so. Unlabelled requests count as protected. **This trades quality for cost by operator opt-in**, unlike the
  router default of never trading quality.
- **ShadowPolicy(inner, p, seed, window=50, threshold=0.2)**: with probability p also calls the roster
  frontier and asks a cheap model (roster budget tier) "is A at least as good as B? 1/0" using the v3
  fenced-untrusted style. Rolling regret over the last N shadows; `SHADOW ALARM` is appended to `Result.trace`
  above the threshold. Shadow + judge cost goes to `routing_cost_usd`; the returned answer and its latency are
  unchanged. Unparseable judge output counts as regret. Requests already routed to the frontier are not shadowed.

## Latency bug (jury 0.0)
Two causes. (1) `report.py`'s latency table looped over the hardcoded `router` strategy, so any other
experimental arm (jury, cascade) printed 0.0 / N=0. (2) The benchmark logged only the final response's latency,
ignoring the other calls a policy made (jurors, verifier, escalation). Fix: `Ctx.call` sums every successful
call's latency into `Result.latency_ms` (set in `Router.complete`, used by the benchmark and live logging), and the report
table follows `experimental_strategy`.

## Data table for the simulations
Source: non-simulated `claude_tiers` rows, mean (cost, quality) per task per tier. Tasks lacking budget or
frontier data, or any logged `router` row, are **excluded** (6 of 49 -> 43 tasks: 13 classification, 13
extraction, 7 short_generation, 10 reasoning). The unconstrained router's tier per task = the most common tier in its
logged `router` rows. **Mid tier was logged for only 9 tasks**; for the other 34 it is imputed: cost = frontier
cost x 0.650 (sonnet/opus ratio on the 9 shared tasks), quality = frontier quality minus the observed gap
(which is 0.0000 on those 9 tasks, so imputed mid is as good as frontier; optimistic). Traffic: 200 requests/day x
30 days, tasks sampled uniformly (seeded).

## A. Budget (20 seeded months; budget = 60% of unconstrained spend)

| Scenario | Final spend / budget | Mean quality (governed vs unconstrained 0.997) | Pressured decisions (of 6000, pace / cap) | Protected downgraded |
|---|---|---|---|---|
| protect reasoning (default) | **127%** (125-131%) | 0.988 | 3424 / 1037 | **0** |
| protect nothing | **101%** (100.6-101.2%) | 0.991 | 4848 / 174 | n/a |
| protect reasoning, budget 80% | 103% (101-106%) | 0.993 | 3312 / 138 | **0** |

Spend curve (seed 0, protect reasoning, budget $7.46 vs unconstrained $12.44):

| Day | 1 | 5 | 10 | 15 | 20 | 25 | 30 |
|---|---|---|---|---|---|---|---|
| Spend $ | 0.390 | 1.615 | 3.181 | 4.678 | 6.150 | 7.768 | 9.433 |
| Linear pace $ | 0.249 | 1.244 | 2.488 | 3.732 | 4.976 | 6.221 | 7.465 |

Findings:
- Protected types were never downgraded (0 across all runs, as required).
- **A 60% budget is infeasible while reasoning is protected**: reasoning is ~64% of unconstrained cost, so
  protected traffic alone exceeds the budget; the governor then spends 127% of it, loudly (trace says OVER CAP).
  That is the designed behavior, not a bug: the operator must pick budget or protection.
- With nothing protected, spend lands at about 101% of budget: the governor tracks pace but slightly
  overshoots (steps are discrete, and it only reacts after spend is already above pace). Treat the hard cap as
  approximate to ~1-3% unless the cap is set a margin below the budget.
- The quality cost is small on this data (0.997 -> 0.988-0.991), because logged haiku quality is high on these
  tasks. This is a property of this 43-task set; harder workloads will lose more.

## B. Shadow (300 seeded months per p; degradation at day 15)

Setup: budget-tier quality drops by 0.3 for half the task types (chosen per seed) from day 15; window 40
shadows, alarm needs >= 20 shadows and regret rate > 0.25; "routed at least as good" = routed quality >= frontier
quality - 0.05. Clean baseline regret on this data is 3.7%. **Simplification: the logged quality score is the
pairwise oracle**; a real cheap-model judge is noisier (and was not run, to keep spend at zero), so real false-alarm
rates and delays will be worse than shown. Shadow cost = frontier call + judge, with the judge approximated as 2x a
budget-tier call.

| p | False-alarm runs (before day 15) | Detected within 15 days | Median delay (requests after day 15, detected runs) | p90 delay | Shadow cost overhead |
|---|---|---|---|---|---|
| 1% | 0.0% | 28.7% | 1970 | 2839 | 1.2% |
| 2% | 0.0% | 63.7% | 1892 | 2793 | 2.4% |
| 5% | 0.0% | 83.3% | 869 | 1701 | 6.1% |
| 10% | 0.0% | 92.7% | 462 | 1743 | 12.0% |
| 20% | 0.0% | 98.3% | 246 | 958 | 24.0% |

Headline: detection time falls roughly as 1/p while overhead rises linearly with p. At 200 requests/day,
1% sampling mostly fails to catch a 15-day-old regression (it gets ~30 shadows, fewer than a window), 5% catches
a typical one in about 4 days of traffic (869 requests) for ~6% extra spend, and 10% in about 2 days for ~12%.
The delays are in requests, so at lower volume the same p detects proportionally later. Detection also depends on
the share of traffic the regression touches (here roughly a quarter of requests), and the delay figures exclude
the undetected runs, which flatters the low-p rows.

## Other simplifications
- Budget decisions in the simulation reuse `BudgetPolicy.decide` and `cheaper_tier`; the `RegretWindow` class is also the
  shipped one. Unit tests (`tests/test_governors.py`) cover the policies end to end with scripted adapters.
- Pace pressure remaps every provider call of the inner policy one tier down (cascade verifier included).
- Mid-tier imputation above; the 6 excluded tasks; traffic is stationary and uniform over tasks.
