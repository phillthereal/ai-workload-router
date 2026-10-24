# X2 - Can Jev pick the model tier cheaply and better?

Offline replay of logged real runs plus one live Jev call per task. Reproduce:
`python3 experiments/x2_jev_tier_picker.py --db <copy of main data/runs.db>` (Jev decisions are cached in
`experiments/x2_jev_cache.json`; `--live` only calls Jev for uncached tasks). The worktree's own `data/runs.db`
is empty (gitignored), so the main repo's db was copied read-only for analysis.

## Method
- **Roster**: `claude_tiers` (haiku = budget, sonnet = mid, opus = frontier). `cross_vendor` skipped: only 25 tasks and different tiers.
- **Table**: per task and tier, mean quality and mean cost over all non-simulated, non-pending rows (strategies frontier_only, router, cascade; jury excluded).
- **Ground truth (GT)**: cheapest tier with quality >= 0.95 x the task's logged opus quality.
- **Missing tiers**: tasks without a haiku or opus run are excluded (6 tasks: cls-v304, gen-v301, rsn-v303..305, ext-h01), leaving **n = 43**.
  Sonnet exists for only 9 tasks. If haiku fails and sonnet is unlogged, GT = {mid or frontier} (a choice of either is correct;
  choosing budget is under-routing). If a policy picks mid on a task with no sonnet run, the answer is imputed as the opus answer
  (pessimistic for savings; 1 case).
- **GT distribution**: 38 budget, 1 mid, 0 frontier-only, 4 mid-or-frontier. The benchmark is easy for haiku, so the achievable saving is large and tier accuracy is mostly "budget or not".
- **Cost** = logged cost of the chosen tier's answer + decision overhead. Net savings = 1 - policy cost / frontier-only cost.
- **Policies**: (a) frontier-only; (b) hand labels via `route_task` default rules; (c) free `heuristic_classify`; (d) Haiku classifier = logged
  `router` rows with routing_cost > 0 (its tier = model it routed to; overhead from the log; 34 of 43 tasks covered, none from tasks_v3);
  (e) v3 learned router = logged rows with `learned_evidence` (9 tasks only); (f) Jev picker; (g) Jev + safety asymmetry.
- **Jev picker**: one `choice` call per task, state = task prompt, options/criteria = the repo's generic `TIER_RULEBOOK`
  (budget / mid / frontier), written before this experiment and not tuned. Escalate to frontier if top prob < t. Overhead = Jev token cost (about $0.00002/task).
  (g): same, but when top prob < 0.9 never go cheaper than the hand label.
- **CIs**: percentile bootstrap over tasks (2000 resamples); paired bootstrap for differences.

## Results (n = 43 for policies covering all tasks; common subset n = 34 for head-to-head with the Haiku classifier)

| Policy (common n=34) | Tier acc | Quality retention | Net saving | 95% CI | Under-routed |
|---|---|---|---|---|---|
| a frontier-only | 0.12 | 1.000 | 0.0% | - | 0 |
| b hand labels | 0.44 | 1.005 | 25.9% | 12-44 | 0 |
| c heuristic (free) | 0.26 | 1.001 | 13.1% | 4-26 | 0 |
| d Haiku classifier | 0.62 | 1.002 | 36.2% | 21-56 | 1 (CI 0-3) |
| f Jev t=0.5 | 0.85 | 0.973 | 61.0% | 40-83 | 1 |
| f Jev t=0.7 | 0.74 | 0.973 | 53.9% | 33-77 | 1 |
| f Jev t=0.9 | 0.62 | 1.000 | 38.8% | 19-62 | 0 |
| g Jev+safety t=0.0/0.5 | 0.71 | 1.005 | 50.4% | 32-72 | 0 |
| g Jev+safety t=0.7 | 0.71 | 1.004 | 49.0% | 30-71 | 0 |

On all 43: heuristic 12.9% saving; Jev t=0.9 38.8%; Jev+safety 50.1% (0 under-routed); hand labels 25.5%. v3 learned router: 9 tasks only
(acc 1.00, 85.8% saving, 0 under) - not comparable, since it exists only for the v3 shapes and uses history.

Paired net-saving difference on the common 34 tasks (percentage points, 95% CI):

| Jev variant | vs heuristic | vs Haiku classifier |
|---|---|---|
| f t=0.5 | +47.9 [+28, +68] | +24.7 [+9, +42] |
| f t=0.9 | +25.7 [+2, +51] | +2.6 [-17, +24] |
| g t=0.0 | +37.3 [+18, +59] | +14.2 [+0.8, +31.8] |
| g t=0.7 | +35.9 [+16, +59] | +12.8 [-1.1, +31.1] |

## Verdict
**PASS, narrowly, for Jev + safety asymmetry (g, t<=0.5)**: net saving 50.4% vs 36.2% (Haiku classifier) and 13.1% (heuristic), quality retention 1.005,
0 under-routed (Haiku classifier: 1). The saving advantage over the Haiku classifier has a CI of [+0.8, +31.8] pp, i.e. barely excludes zero.
The plain picker at t=0.9 is **inconclusive** against the Haiku classifier (+2.6 [-17, +24]); at t=0.5/0.7 it saves more but under-routes
one task (cls-h02, p=0.89) and retention dips to 0.973, which fails the "under-routing <= theirs" bar only in the sense of tying the Haiku classifier at 1.
Versus the free heuristic Jev wins clearly in every variant.

## Limits
- n = 43 (34 common), one roster, 38 of 43 tasks are budget-solvable, so a "send everything to haiku" policy would look good here too; the test mostly measures whether a policy avoids needlessly sending easy tasks to opus. Frontier-needed tasks are only 5.
- Only 5 tasks are non-budget; under-routing differences of 0 vs 1 are noise. No sonnet data for most tasks, so mid accuracy is barely measurable.
- Jev never chose frontier outright on these prompts (33 budget, 10 mid); all frontier routing came from the confidence threshold or the hand-label floor. That is where the gain and the risk both come from.
- The variants (t, with/without safety) were all inspected; picking the best of 7 flatters the winner. The fixed, pre-registered config would be g with t=0.5 or f at t=0.9.
- Heuristic and hand-label tiers use `route_task` (gates applied); Haiku classifier tier is read from the model actually logged. Quality is the logged judge score (no re-scoring).
- Logged answers are means over repeated runs of the same task (and some from different strategies), so per-task quality is smoothed. Real-traffic prompts (long, email-like) differ from these short benchmark prompts; the v1 heuristic failure on real traffic is not tested here.
- Spend: Jev about $0.00077 total (43 calls), no other model calls.
