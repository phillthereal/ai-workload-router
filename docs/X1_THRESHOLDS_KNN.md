# X1: per-class thresholds, kNN reuse, Jev+kNN agreement (offline, $0)

Script: `experiments/x1_thresholds_knn.py` (deterministic, stdlib; replays saved Jev decisions, no API calls).
Tests: `tests/test_x1_thresholds_knn.py` (synthetic).

## Method
- Data: Banking77, true labels. Splits from `jev_loop_public.splits()` (seed 77): LEARN 300 / VAL 300 / TEST 800.
  Jev decisions: round-0 base-rulebook hash (`1954094fbc23`) from `loop/jev_memo.json`.
- Baseline: Jev top prob >= 0.90 is decided, else expensive fallback. VAL precision at 0.90 = 0.8704 = the target all ideas must meet on VAL.
- All parameters are chosen on VAL (kNN pool = LEARN while tuning). TEST is scored once, kNN pool = LEARN+VAL.
- Idea 1: per predicted class, lowest p-grid threshold (0.30..0.99) whose VAL precision >= target; none qualifies -> 0.90.
  Shrinkage: classes with < 10 VAL rows keep 0.90; others t = 0.9 + n/(n+10) * (t_raw - 0.9).
- Idea 2: TF-IDF (word + char-3gram, sublinear tf, smooth idf, L2; sim = mean of the two cosines), k=5.
  Rule: >= m of 5 neighbours agree AND top-1 sim >= s; (s, m) chosen on VAL (m in 3,4,5; strict 5/5 is nearly impossible with ~4-8 examples per class, so m was tuned). kNN stage runs BEFORE Jev; items it decides skip Jev.
- Idea 3: also decide when Jev label has p >= t1 (t1 < 0.90) and >= m of the 5 neighbours voted for that label; (t1, m) chosen on VAL.
- Cost per 1k: Jev $0.042/1M input tokens x recorded tokens (for items that reach Jev) + fallback gpt-4o-mini
  (ASSUMED 1.2k in / 30 out tokens at $0.15/$0.60 per 1M = $0.000198) for every undecided item. Fallback accuracy is not modelled.
- CIs: paired bootstrap over TEST items, 2000 resamples, seed 0, on coverage and accuracy-among-decided differences vs baseline.
- Verdict rule: PASS = coverage-difference CI lower bound > 0 AND accuracy-difference CI lower bound >= -1.0 pp AND wrong-decided count <= baseline.
  PARTIAL = first two hold but wrong-decided count rose. Anything else FAIL.
- Combined pipeline: all 7 subsets of ideas were built; the one with highest VAL coverage at VAL precision >= target is "VAL-best" (not chosen on TEST).

## Results (TEST, n=800)
Tuned: idea 1 shrunk thresholds exist for only 3 of 77 classes (the rest have < 10 VAL rows); idea 2 (s, m) = (0.55, 4); idea 3 (t1, m) = (0.50, 2).

| pipeline | coverage | acc decided | wrong-decided | skip-Jev | $/1k | dCov [95% CI] | dAcc [95% CI] | verdict |
|---|---|---|---|---|---|---|---|---|
| baseline 0.90 | 0.715 | 0.942 | 33 | 0.00 | 0.1488 | - | - | - |
| 1 per-class thr | 0.711 | 0.946 | 31 | 0.00 | 0.1495 | -0.4pp [-0.9,+0.0] | +0.3pp [-0.0,+0.8] | FAIL (coverage gain not significant) |
| 2 kNN alone-stage | 0.731 | 0.944 | 33 | 0.11 | 0.1350 | +1.6pp [+0.8,+2.6] | +0.1pp [-0.3,+0.6] | PASS |
| 3 Jev+kNN agree | 0.818 | 0.937 | 41 | 0.00 | 0.1285 | +10.3pp [+8.2,+12.4] | -0.5pp [-1.4,+0.3] | FAIL (accuracy CI < -1pp) |
| combo 1 | 0.711 | 0.946 | 31 | 0.00 | 0.1495 | -0.4pp [-0.9,+0.0] | +0.3pp [-0.0,+0.8] | FAIL (coverage gain not significant) |
| combo 2 | 0.731 | 0.944 | 33 | 0.11 | 0.1350 | +1.6pp [+0.8,+2.6] | +0.1pp [-0.3,+0.6] | PASS |
| combo 3 | 0.818 | 0.937 | 41 | 0.00 | 0.1285 | +10.3pp [+8.2,+12.4] | -0.5pp [-1.4,+0.3] | FAIL (accuracy CI < -1pp) |
| combo 1+2 | 0.728 | 0.947 | 31 | 0.11 | 0.1357 | +1.3pp [+0.2,+2.2] | +0.4pp [-0.2,+1.1] | PASS |
| combo 1+3 | 0.814 | 0.940 | 39 | 0.00 | 0.1292 | +9.9pp [+7.8,+12.0] | -0.2pp [-1.2,+0.7] | FAIL (accuracy CI < -1pp) |
| combo 2+3 (VAL-best) | 0.820 | 0.938 | 41 | 0.11 | 0.1174 | +10.5pp [+8.5,+12.6] | -0.5pp [-1.5,+0.4] | FAIL (accuracy CI < -1pp) |
| combo 1+2+3 | 0.816 | 0.940 | 39 | 0.11 | 0.1181 | +10.1pp [+8.0,+12.1] | -0.2pp [-1.3,+0.8] | FAIL (accuracy CI < -1pp) |

baseline wrong-decided 33; VAL-best combo: 2+3

kNN rule as the only classifier (no Jev): coverage 11.5%, accuracy 98.9% (high precision, tiny coverage).

## Verdicts
1. Per-class thresholds: FAIL. Only 3 classes have enough VAL data; coverage -0.4 pp [-0.9, 0.0]. 300 VAL rows over 77 classes cannot support per-class tuning.
2. kNN stage before Jev: PASS. Coverage +1.6 pp [+0.8, +2.6], accuracy +0.1 pp [-0.3, +0.6], wrong-decided 33 = 33, and 11% of items skip Jev (cost/1k 0.1488 -> 0.1350). Small gain, but the only clean pass.
3. Jev+kNN agreement: FAIL by the rule. Coverage +10.3 pp [+8.2, +12.4], accuracy -0.5 pp [-1.4, +0.3] (lower bound below -1 pp), wrong-decided 33 -> 41 (+8). Large coverage gain and the accuracy point loss is small, but the strict bar is not met; a bar tolerating the accuracy CI would call it PARTIAL.
- Best combined by VAL (2+3): coverage 82.0% vs 71.5%, accuracy 93.8% vs 94.2%, wrong-decided 41 vs 33, cost/1k $0.1174 vs $0.1488. Fails the strict bar for the same reason as idea 3. Best strictly-passing pipeline: 1+2 (+1.3 pp coverage, wrong 31) or idea 2 alone.

## Limits
- One TEST sample of 800; LEARN+VAL pool differs in size from the tuning pool (kNN similarities shift slightly upward).
- VAL used for several parameters (in-sample for the combos' VAL selection).
- Fallback accuracy/cost are assumptions; net accuracy of the full cascade is not measured. Extra decided items add wrong-decided errors unless the fallback would also have erred.
- Email data NOT evaluated: `calls.jsonl` (needed by `shadow_test.load()`, for text, reference labels and timestamps) is absent from the real-traffic folder (`raw/` is empty), and n is only 150 (chronological holdout 50), too small for any of the three ideas, and kNN needs email text. No email data is in this repo or doc.

## Round 2: agreement rule under an error budget (offline, $0)
Script `experiments/x1_round2.py`, tests `tests/test_x1_round2.py`. Hypothesis: round-1 agreement failed because its params maximised VAL coverage with no error cap.

Method. Rule: baseline (Jev p >= tb, default 0.90) OR (Jev p >= t1 AND >= m neighbours with sim >= s voted for Jev's label). Grid t1 0.40-0.85, m 1-5, s 0.10-0.70. On VAL (kNN pool = LEARN) pick max coverage subject to VAL wrong-decided <= budget. Budget "equal" = baseline VAL wrong; "tight90" = floor(0.9 x baseline). Agreement only adds decisions, so the tight budget is unreachable unless the base threshold rises: for tight90 only, tb is also searched (0.90-0.99). TEST scored once (pool = LEARN+VAL), same bar as round 1 (coverage CI > 0, accuracy-diff CI lower bound >= -1 pp, wrong <= baseline), bootstrap as before.
Seeds: 77 = original split. Jev decisions exist only for the 1,400 seed-77 items, so seeds 11/22/33 RE-PARTITION those same 1,400 items into 300/300/800 (not new data; the TEST sets overlap across seeds and with the seed-77 LEARN/VAL items, so seeds are not independent). Each seed has its own baseline.

### Equal error budget (VAL wrong <= baseline VAL wrong)
| seed | (t1,m,s) | VAL wrong base/budget/chosen | TEST cov base->new | dCov [95% CI] | dAcc [95% CI] | TEST wrong | verdict |
|---|---|---|---|---|---|---|---|
| 77 | (0.50,1,0.55) | 28/28/28 | 0.715->0.759 | +4.4pp [+3.0,+5.9] | +0.3pp [+0.2,+0.5] | 33->33 | PASS |
| 11 | (0.70,1,0.50) | 19/19/19 | 0.716->0.745 | +2.9pp [+1.8,+4.0] | -0.3pp [-1.0,+0.2] | 48->52 | PARTIAL |
| 22 | (0.75,1,0.35) | 19/19/19 | 0.719->0.790 | +7.1pp [+5.5,+9.0] | -0.9pp [-2.0,-0.1] | 43->53 | FAIL (acc CI < -1pp) |
| 33 | (0.50,2,0.55) | 15/15/15 | 0.713->0.735 | +2.2pp [+1.4,+3.4] | -0.1pp [-0.6,+0.3] | 51->53 | PARTIAL |

Mean over 4 seeds: dCov +4.16 pp, dAcc -0.25 pp, dWrong +4.0. PASS on 1 of 4 seeds -> **FAIL**.

### Tight budget (VAL wrong <= 90% of baseline; base threshold tb also tuned)
| seed | (t1,m,s,tb) | VAL wrong base/budget/chosen | TEST cov base->new | dCov [95% CI] | dAcc [95% CI] | TEST wrong | verdict |
|---|---|---|---|---|---|---|---|
| 77 | (0.45,1,0.15,0.99) | 28/25/25 | 0.715->0.863 | +14.8pp [+11.8,+17.8] | -2.3pp [-4.3,-0.4] | 33->56 | FAIL |
| 11 | (0.45,1,0.35,0.94) | 19/17/17 | 0.716->0.801 | +8.5pp [+6.1,+11.0] | -0.4pp [-1.8,+1.1] | 48->56 | FAIL |
| 22 | (0.50,1,0.35,0.94) | 19/17/17 | 0.719->0.833 | +11.4pp [+8.9,+13.9] | -2.1pp [-3.8,-0.4] | 43->64 | FAIL |
| 33 | (0.60,1,0.35,0.96) | 15/13/13 | 0.713->0.771 | +5.9pp [+3.5,+8.4] | +1.2pp [-0.5,+2.7] | 51->48 | PASS |

Mean: dCov +10.1 pp, dAcc -0.92 pp, dWrong +12.3. PASS on 1 of 4 -> **FAIL**.

### Verdict: round 2 FAIL (both budgets; needs original split AND >= 3 of 4 seeds, got 1 of 4)
- The hypothesis half-holds: with an equal VAL budget the original split now passes (+4.4 pp coverage with wrong 33 = 33, versus +10.3 pp / 41 wrong before), but the cap does not transfer. VAL error counts are 15-28, so "wrong <= baseline" on VAL is noisy and TEST wrong rises by 2-10 on three of four re-partitions.
- The tight budget picks aggressive settings (tb 0.94-0.99 plus t1 0.45-0.6, m=1) that fit VAL's few errors and overfit: TEST wrong rises to 56-64 on 3 seeds.
- Caveats: seed 77 passing partly reflects a split already seen in round 1; re-partitioned seeds share items, so passes would not be independent either; fallback accuracy is not modelled.
