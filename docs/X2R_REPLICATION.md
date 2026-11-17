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

## Results
Pending.
