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
