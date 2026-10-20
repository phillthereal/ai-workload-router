# v5 — Jury of cheap cross-vendor models as the escalation signal

**Hypothesis.** v2 showed a cheap verifier grading its *own* model's answer is
unreliable (Haiku self-verifier: scored its own wrong hard-set answers 0.95-1.0,
quality 1.00 -> 0.70), while the independent Sonnet verifier held quality at
163.7% overhead-of-savings. Idea: skip the verifier. Ask three cheap models from
three vendors (gpt-4o-mini, deepseek-chat, claude-haiku-4-5); if they agree,
return the answer; if they disagree, escalate to the roster frontier (Opus).

**Verdict: negative on hard tasks, positive on easy ones.** Disagreement is a
useful but *leaky* escalation signal: independent cheap models make the *same*
mistakes on exactly the trap questions that matter, so agreement is not
correctness. It missed quality on the hard set that Sonnet-verification did not.

## Design (`src/router/jury.py`, `--strategy jury --jury-mode unanimous|majority`)

- Jurors answer the raw prompt (same cache keys as the other arms). Unanimous =
  juror 0 must agree with all others (agreement assumed transitive); majority =
  some answer is agreed by more than half the jurors (2-of-3).
- **Short answers** (both <= 40 characters after `scoring._normalize`, the same
  normalization `exact_match` uses): compared deterministically with
  `exact_match` containment in either direction ("Paris." == "paris"). Free.
- **Longer answers**: a pairwise "do these agree in substance? 1 or 0" call
  (max 8 tokens) on a juror from a *third* vendor. Anything other than a bare
  `0`/`1` counts as DISAGREE (fail toward quality).
- Accounting as in the cascade: answer_cost = returned answer's call;
  routing_cost = all other juror calls + agreement checks; on escalation all
  juror calls + checks are overhead and Opus is the answer.
- Tests: `tests/test_jury.py` (226 total green).

## Results (live, claude_tiers roster, Opus judge)

| Arm | Set | Quality | Net savings vs frontier | Overhead % of savings | Escalated |
|---|---|---|---|---|---|
| cascade, Sonnet verifier | hard (n=10) | 1.000 | -19.9% | 163.7% | 5/10 |
| cascade, Haiku self-verifier | hard | 0.700 | 73.8% | 14.7% | 1/10 |
| **jury, unanimous** | hard | **0.860** | 22.4% | 52.6% | 3/10 |
| **jury, majority** | hard | **0.760** | 25.1% | 50.2% | 2/10 |
| cascade, Sonnet verifier | easy (n=25) | 0.988 | 53.1% | 31.1% | 2/25 |
| cascade, Haiku self-verifier | easy | 0.988 | 85.0% | 6.2% | 0/25 |
| **jury, unanimous** | easy | **0.990** | 66.1% | 21.3% | 1/25 |

Reports: `data/benchmark_report_claude_tiers_jury_{unanimous,majority}_hard.md`,
`..._jury_unanimous.md`.

## What happened on the hard set

- Unanimous escalated rsn-h01, rsn-h04, ext-h01 (all correct after escalation).
  It failed on **cls-h02**: all three vendors confidently agreed on "possible"
  (reference: "impossible") — a correlated error no disagreement signal can see.
  It also passed **gen-h01** (quality 0.6) because the substance check judged two
  loosely-similar rewrites as agreeing (agreement != meeting the rubric).
- Majority additionally accepted a 2-of-3 wrong answer on **ext-h01** (0.0), so
  it traded another 0.10 of quality for ~2.7 points of savings: not worth it.
- Versus the self-verifier, the jury is better (0.86 vs 0.70) and still cheap
  (overhead 52.6% vs 14.7%, ~3x less than Sonnet's 163.7%). Versus the Sonnet
  verifier it is cheaper but does **not** hold quality.

## Easy set

Unanimous held quality (0.990, 99.8% retention) with 66.1% net savings, better
than the Sonnet-verified cascade (53.1%) but below the Haiku self-verifier
(85.0%), because three juror calls per task cost more than one answer + one
tiny verification.

## Caveats

n=10 hard / 25 easy; one run each, directional only (3 of 10 hard tasks decide
the quality number). Juror errors are correlated on trap questions, which is the
core finding rather than noise, but a different juror set (more diverse, or 5
jurors) might do better and was not tested. The jury has no escalation threshold
to tune. The report's latency table shows 0.0 for the jury row (the latency
section keys on the `router` strategy); this is a reporting quirk, not a result.
Opus-judge scores are generous on the easy set (see V2_FINDINGS).

## Spend

Total live spend for this experiment: about **$0.04** (all new cache entries
token-priced from config.py), under the $0.50 cap.

## Reproduce

```bash
python3 run_benchmark.py --roster claude_tiers --strategy jury --jury-mode unanimous --tasks data/tasks_hard.json
python3 run_benchmark.py --roster claude_tiers --strategy jury --jury-mode majority  --tasks data/tasks_hard.json
python3 run_benchmark.py --roster claude_tiers --strategy jury --jury-mode unanimous
```
