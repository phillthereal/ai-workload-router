# v3 Findings — the learned router, evaluated held-out

One page on what happened when the [V3 design](V3_DESIGN.md) was built and run
against tasks it had never seen. All numbers are **live** (real API calls, real
Claude Opus 4.8 judge), n=14 for the held-out set. Every row links to the
committed report that produced it.

> Directional, not statistically certified — n=14, one roster, and the held-out
> set was deliberately authored to repeat existing task *shapes* (that is the
> learned router's operating condition, and the conditionality is the finding —
> see the caveats). The value is the shape of the result, not the decimals.

## The headline

On 14 held-out tasks (`data/tasks_v3.json`) the learned router — identical to
the static router except that it consults `data/runs.db` before trusting the
cold prediction — routed **22.7% cheaper than the static router at identical
quality (1.000 vs 1.000, every task correct under both arms)**:

| Arm | Total cost | Mean quality | Net saving vs frontier-only | Report |
|---|---|---|---|---|
| frontier-only baseline | $0.019370 | 1.000 | — | (baseline arm of both runs) |
| static router (labels) | $0.018516 | 1.000 | 4.4% | [.…claude_tiers_v3](../data/benchmark_report_claude_tiers_v3.md) |
| **learned router** | **$0.014319** | **1.000** | **26.1%** | [.…claude_tiers_learned_v3](../data/benchmark_report_claude_tiers_learned_v3.md) |

Both arms routed on hand labels (no classifier call) to isolate the one
variable under test: what history adds. The learned router's own lookup is
pure SQL + feature matching — **it makes no LLM calls, so its routing
overhead is $0.000000**, logged through the same `routing_cost_usd` column as
every other strategy's overhead rather than waved away.

Why the static router only saves 4.4% here: the held-out set is mostly hard
tasks, and on hard tasks the static policy sends nearly everything to the
frontier — the same cost-collapse v2 demonstrated on `tasks_hard.json`. That
is exactly the headroom the learned router exists to claw back: history knows
Haiku already handled these *shapes*, the cold policy cannot.

## What it did, task by task

9 of 14 tasks downgraded on evidence; all 9 scored 1.00 after the downgrade:

| Decision | Tasks | What happened |
|---|---|---|
| **Downgraded** frontier→budget | ext-v301..303, rsn-v301, rsn-v302 | Hard extraction/reasoning shapes with n≥5 logged Haiku successes at quality ≥ the 95%-of-frontier bar. Haiku answered all five correctly, including the per-unit-vs-total and coreference traps. |
| **Downgraded** mid→budget | cls-v301..303, ext-v304 | Medium classification/extraction shapes with strong budget-tier history. All correct. |
| **Refused to downgrade** | rsn-v303, rsn-v304, cls-v304 | Evidence exists but fails the bar — e.g. rsn-v303's bucket shows budget weighted quality 0.741 against a 0.950 bar, with a recent clear failure flagged (`concerning: true`). Stayed at the frontier; both scored 1.00 there. |
| **Cold start** | rsn-v305, gen-v301 | No/thin matching history. Degraded exactly to the classifier's tier, per the design's hard rule. |

The refusal row is the finding that matters most. rsn-v303 is the same
overflow-cap trap shape that Haiku historically flunked (logged mean 0.60 in
that bucket) — the evidence threshold declined the downgrade and the task got
its 1.00 at the frontier. A router that only had the 9 wins and not these 3
refusals would just be "route everything cheap" with extra steps.

Sample logged evidence (persisted per-row in the new `learned_evidence`
column, the same way `verifier_scores` is):

```json
{"tier": "budget", "n": 14, "effective_n": 7.35, "weighted_quality": 1.0,
 "quality_bar": 0.95, "meets_threshold": true,  "concerning": false}   // ext-v301 → downgrade
{"tier": "budget", "n": 9,  "effective_n": 4.25, "weighted_quality": 0.741,
 "quality_bar": 0.95, "meets_threshold": false, "concerning": true}    // rsn-v303 → refuse
```

## Feasibility: the eval was almost infeasible, and how that was resolved

The design doc's schema warning was correct and biting: `runs` had no
`created_at` column, so after adding it (nullable, via the `_MIGRATIONS`
pattern), **every one of the 495 pre-existing non-simulated rows is undated
(NULL) forever** — and the decay rule treats NULL as maximally stale. The raw
counts looked healthy (38 (feature-bucket × tier) combinations with n≥5
logged outcomes), but at minimum decay weight none of them could clear the
evidence threshold: a scan of all 35 existing tasks produced **zero
overrides** on undated history. Honest verdict at that point: infeasible as
logged.

The record/replay cache resolved it without spend: re-running the eight
historical live `claude_tiers` configurations (2 classified, 4 cascade with
the default verifier, 2 cascade with the Haiku verifier — the same
configurations at the same multiplicities as the pre-v3 history) replays
every answer, verifier, and judge call from `.cache/` at **$0.00, zero new
API calls**, while logging fresh rows that carry real timestamps. Training
history regenerated; the committed v2 report files touched by those replays
were restored untouched.

The chronological split then holds by construction: `run_group` strings sort
chronologically, and the learned router only queries evidence from run groups
strictly earlier than its own — a task can never see outcomes from its own
batch, its siblings, or the future.

## Caveats — read these before quoting 22.7%

- **The saving is conditional on shape repetition.** The held-out tasks are
  new prompts, but they were written to repeat feature shapes the log had
  already seen ≥5 times — because that is the only regime in which the
  learned router does anything at all. A workload with no repeated shapes
  gets cold start everywhere, i.e. exactly the static router's numbers at $0
  extra overhead. The honest general claim is "savings scale with your
  workload's shape-repetition rate", which is the v1 composition insight
  wearing a third hat.
- **Training history is benchmark reruns** — literally identical tasks
  recurring, the friendliest possible case for a bucket-based similarity
  function. Real traffic repeats shapes approximately, not exactly; the
  lightweight features would blur more there, in both directions.
- **No quality separation was observed** (every arm scored 1.000 on every
  task), so "at identical quality" means no regression was observed at n=14,
  not that one is impossible. The refusal mechanism is what carries the
  quality argument, and it fired 3 times exactly where the historical
  evidence said it should.
- The evidence pool is bounded by tasks whose prompts exist in
  `data/tasks*.json` (the `runs` table does not store prompts, so
  fine-grained features are recomputed from the task registry by `task_id`).
  A production deployment would log a prompt fingerprint per row instead.

## Live spend

$0.0231 total against a $3.00 budget: 28 new API calls (15 Opus — answers
plus one judge call, 4 Sonnet, 9 Haiku), token-priced from the cache entries
they wrote. The history-regeneration replays and both baseline arms cost
$0.00 (100% cache replay). Everything else in this eval — including the
second run's entire frontier baseline — re-used cached responses.

## Reproduce

```bash
# regenerate dated training history (all cache-replayed, ~$0)
python3 run_benchmark.py --roster claude_tiers --classify
python3 run_benchmark.py --roster claude_tiers --classify --tasks data/tasks_hard.json
python3 run_benchmark.py --roster claude_tiers --strategy cascade            # x2
python3 run_benchmark.py --roster claude_tiers --strategy cascade --tasks data/tasks_hard.json  # x2
python3 run_benchmark.py --roster claude_tiers --strategy cascade --verifier claude-haiku-4-5
python3 run_benchmark.py --roster claude_tiers --strategy cascade --verifier claude-haiku-4-5 --tasks data/tasks_hard.json

# the held-out eval: static vs learned on tasks the log has never seen
python3 run_benchmark.py --roster claude_tiers --tasks data/tasks_v3.json
python3 run_benchmark.py --roster claude_tiers --tasks data/tasks_v3.json --learned
```
