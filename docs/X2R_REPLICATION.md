# X2R - Replication of the X2 tier-picker result on fresh tasks (pre-registration)

Written BEFORE any real data exists. Nothing below may be changed after the first real call is made, except to fix a typo or to
append the results. The tasks, the pipeline and the decision rule are fixed here and in the code committed with this file.

## Why
[X2](X2_JEV_TIER_PICKER.md) found that Jev plus a safety rule saved 50.4% of cost against 36.2% for the Haiku classifier. That result is
weaker than it looks:
- only 43 tasks, mostly easy (38 of 43 solvable by the cheapest tier), and only 5 tasks where a cheaper tier was not enough;
- 7 policy variants were inspected after the fact, and the best one was reported, which flatters the winner;
- many tasks had no sonnet answer (or no haiku/opus answer) so the ground truth was partly guessed, and the Haiku classifier covered only 34 of the 43 tasks;
- answers were old, averaged logged runs from different strategies, not one clean run per tier.

This replication removes those problems: new tasks, one variant fixed in advance, and a real answer from every tier for every task.

## Design
- **Tasks**: `data/tasks_x2r.json`, 49 new tasks (ids `x2r-*`). They have the same count for every (task_type, difficulty, scoring)
  combination as the three original files combined (49 tasks). They were written before any model was run and without reading the Jev
  rulebook or code, and none reuses an original prompt or scenario. Difficulty labels are the author's judgement made before running anything.
  Computable exact-match references are re-checked by code: `python3 experiments/x2r_verify_refs.py`.
- **Roster**: `claude_tiers` (budget = claude-haiku-4-5, mid = claude-sonnet-5, frontier = claude-opus-4-8), same as X2.
- **Data**: every task gets one real answer from each of the three tiers, scored with the repo's own scorer (exact match, or the rubric judge
  `claude-opus-4-8`) and logged by `experiments/x2r_collect.py`. One run per task and tier. No averaging over repeats.
- **Ground truth (same rule as X2)**: the cheapest tier whose quality is >= 0.95 x the task's opus quality.
- **Arms** (all as in X2): a frontier-only; b hand labels; c free heuristic; d Haiku LLM classifier (one call per task, the tier is what
  `route_task` picks from its labels, the classifier cost counts as overhead); f Jev picker at t = 0.5 / 0.7 / 0.9; g Jev + safety rule at t = 0.0 / 0.5 / 0.7
  (pinned `jev-1.14.0`, the repo's `TIER_RULEBOOK`, state = the task prompt, one call per task). The analysis code is reused unchanged
  from `experiments/x2_jev_tier_picker.py`: `experiments/x2r_analyze.py`.
- **Metrics**: tier accuracy, quality retention, net saving (1 - policy cost / frontier-only cost, decision overhead included) with a 95% percentile
  bootstrap CI, number of under-routed tasks, paired bootstrap for differences (2000 resamples, fixed seeds).

## Primary comparison (fixed in advance)
**g, Jev + safety at t = 0.5, versus d, the Haiku classifier, on net saving, paired bootstrap over all 49 tasks.**

Secondary (reported, not decisive): f at t = 0.9 versus d. All other variants are exploratory and are printed only for completeness;
no verdict may be drawn from them.

## Decision rule (fixed in advance)
- **Replicates**: g's net saving is higher than d's, the 95% CI of the paired difference excludes zero, AND g under-routes no more tasks than d.
- **Direction holds, not confirmed**: the difference is positive but its 95% CI includes zero.
- **Does not replicate**: anything else (difference zero or negative, or a positive significant difference with more under-routing than d).

The script prints the verdict by exactly this rule (`verdict()` in `experiments/x2r_analyze.py`).

## Exclusion rule
A task is dropped only if a real answer could not be obtained for a tier after one retry. Every dropped task is listed by the collector
(`experiments/x2r_dropped.json`) and by the analysis output. A task is never dropped because of its result, its label or how a policy handles it.
If a classifier or Jev call fails twice, that task is left out of that arm only (and the paired comparison uses the tasks both arms cover); the output states n.

## Guard rails
- Spend cap: **USD 2.00** in total across answers, judge, classifier and Jev (`--cap`). The collector prints an estimate first and refuses to
  start if it exceeds the cap, makes one budget-model call and aborts if it comes back simulated, aborts on any later simulated response,
  prints the running total every 10 tasks and stops the moment the cap is passed. Re-running resumes without repeating paid work.
- Without `--live` everything is mock: data goes to `data/x2r_runs_simulated.db` and `*_simulated.json` caches, and the analysis refuses
  those files unless `--allow-simulated` is passed (then every table is labelled SIMULATED).
- Known limits accepted up front: one run per task and tier means some noise; the rubric judge is claude-opus-4-8, the same model as the
  frontier tier (it grades itself, as in X1/X2); 49 tasks give wide CIs, which is why the rule above includes a "not confirmed" outcome.

## Results (real run, 17 November 2026)

**Verdict by the rule fixed above: DIRECTION HOLDS, NOT CONFIRMED.**

All 49 tasks got a real answer from all three tiers; no task was dropped. Ground truth: 41 budget, 7 mid, 1 frontier.

| Policy (n = 49) | Tier acc | Quality retention | Net saving | 95% CI | Under-routed |
|---|---|---|---|---|---|
| a frontier-only | 0.02 | 1.000 | 0.0% | - | 0 |
| b hand labels | 0.29 | 1.000 | 26.0% | 13.4-41.9 | 0 |
| c heuristic (free) | 0.14 | 0.978 | 19.7% | 6.5-34.7 | 2 |
| **d Haiku classifier (reference)** | 0.47 | 0.996 | 37.9% | 21.0-56.9 | 3 |
| f Jev t=0.5 | 0.73 | 0.964 | 57.2% | 37.7-78.4 | 3 |
| f Jev t=0.7 | 0.61 | 0.964 | 50.7% | 33.2-71.0 | 2 |
| f Jev t=0.9 (secondary) | 0.49 | 1.000 | 33.5% | 17.8-52.8 | 0 |
| g Jev+safety t=0.0 | 0.57 | 1.000 | 43.3% | 25.9-63.0 | 0 |
| **g Jev+safety t=0.5 (primary)** | 0.57 | 1.000 | 43.3% | 25.9-63.0 | 0 |
| g Jev+safety t=0.7 | 0.55 | 1.000 | 42.9% | 25.6-62.7 | 0 |

- **Primary (g at t=0.5 vs d)**: +5.4 percentage points of net saving, 95% CI [-5.2, +15.4]. The difference is positive but the
  interval includes zero. Under-routed tasks: g 0, d 3.
- **Secondary (f at t=0.9 vs d)**: -4.4 points, 95% CI [-20.2, +10.2]. No difference shown.

### What this means for the X2 claim
- X2 reported 50.4% vs 36.2% (+14.2 points, CI +0.8 to +31.8). On fresh tasks the Haiku classifier's figure reproduced closely
  (36.2% then, 37.9% now), but the Jev + safety figure fell to 43.3% and the advantage shrank to +5.4 points with an interval that
  includes zero. **The saving advantage over the Haiku classifier is not confirmed.** Part of the original gap was most likely the
  best-of-7 selection that X2 itself flagged.
- What held in both experiments: Jev + safety routed **no task too cheaply** and kept quality retention at 1.000, while the Haiku
  classifier under-routed 1 task in X2 and 3 here (retention 0.996). With counts this small that is a consistent sign, not a proven difference.
- Both Jev + safety and the classifier clearly beat the free heuristic and hand labels on saving.

### Exploratory only (no verdict may be drawn)
- Plain Jev at t=0.5 and t=0.7 saved more than the classifier (+19.3 [+7.5, +34.0] and +12.8 [+4.7, +22.0] points) but routed 3 and 2
  tasks too cheaply, with retention 0.964. These variants were not pre-registered.
- Jev never chose the frontier tier outright (33 budget, 16 mid), as in X2; all its frontier routing came from the hand-label floor.
  The classifier sent 21 of 49 tasks to the frontier tier, which is where it gives up saving.

### Data notes
- The six answers scored 0 were genuinely wrong, not formatting misses: haiku on x2r-ext-05, ext-06, rsn-03, rsn-11, rsn-12 and sonnet on x2r-ext-07.
- One run per task and tier means noise: sonnet missed x2r-ext-07 that haiku got right, and on four rubric tasks opus scored at or below a cheaper tier.
- Spend: USD 0.44 in total by the collector's running count (answers 0.290, classifier 0.018, Jev 0.001, the rest rubric-judge calls and the probe), against the 2.00 cap.
- Reproduce the tables: `python3 experiments/x2r_analyze.py` with the raw run log `data/x2r_runs.db` (local, not in git). The Jev and
  classifier decisions are committed (`experiments/x2r_jev_cache.json`, `experiments/x2r_classifier_cache.json`), and the per-task quality
  and cost of every tier's answer is in `experiments/x2r_results.json`.
