# Cheapest-first + self-improving loop on a public dataset (Banking77)

Why: the email results (JEV_CALIBRATION.md, JEV_LOOP.md) used Haiku's label as reference, not truth, on private data.
Here the labels are **true ground truth** and the data is public, so the learned rulebook can be committed.

Data: Banking77 (PolyAI, CC-BY-4.0), official split, committed whole in `data/public/banking77/` (1.1 MB). 77 intents.
Base rulebook: `data/rulebooks/banking77_base.json`, 77 intent names with short generic descriptions.
Reproduce: `python3 jev_loop_public.py calibrate | mini | run --teacher manual | report` (live; needs TYPESAFE_API_KEY, and OPENAI_API_KEY for `mini`).

## Splits (seed 77, fixed)
- LEARN 300 + VAL 300 from train: the teacher sees only LEARN mistakes; every edit is **gated on VAL**; the final version is the **best VAL score** (the overfitting fix from JEV_LOOP.md).
- TEST 800 from test: all 800 for calibration; the first 300 are scored every round and never influence any decision.

## Step 0: calibration vs TRUE labels (base rulebook, jev-latest, n=800 TEST)
| | Jev (n=800) | Jev (same 300) | gpt-4o-mini self-reported (300) |
|---|---|---|---|
| accuracy | 84.1% | 84.0% | 76.0% |
| ECE | 0.058 | 0.060 | 0.143 |
| AUROC | 0.838 | 0.825 | 0.704 |
| accuracy at conf >= 0.90 | 94.2% | 93.4% | 83.7% |
| coverage at conf >= 0.90 | 71.5% (572) | 71.0% | 75.7% |

Jev reliability (top-choice probability): <0.5 n=26 acc 0.27 | 0.5-0.7 n=87 acc 0.60 | 0.7-0.8 n=50 acc 0.56 | 0.8-0.9 n=65 acc 0.72 | 0.9-0.95 n=49 acc 0.80 | >=0.95 n=523 acc 0.96.
Reading: confidence ranks right above wrong, and the >=0.95 bucket is near-honest (0.99 stated, 0.96 actual), but 0.7-0.95 is over-confident by 10-20 points; so with 77 near-neighbour intents a 0.90 threshold is not a 0.90 guarantee (94% here). Jev beats mini on accuracy, ECE and AUROC.

## Learning loop
Gate (reused from `jev_loop.gate`, on VAL): accept only if accuracy among p>=0.9 decisions does not drop AND (handled share or overall accuracy rises). Selection score = correct-and-sure answers / n on VAL. Stops: 8 rounds, 3 consecutive rejections, or spend (hard cap $0.50, loop stops at 80%).
Teacher: `--teacher manual` file handshake (`loop/teacher_prompt_N.txt` -> `loop/teacher_reply_N.txt`); an API teacher flag exists but is unused.

### Round 0 (baseline), 300-query slices
| slice | handled (p>=0.9) | accuracy at p>=0.9 | overall accuracy |
|---|---|---|---|
| LEARN | 72% | 92% | 80% |
| VAL | 72% | 87% | 78% |
| TEST300 | 71% | 93% | 84% |

### Rounds
| round | edit | gate (VAL) | VAL handled / acc@0.9 / acc | TEST300 handled / acc@0.9 / acc |
|---|---|---|---|---|
| 0 | base rulebook | baseline | 72% / 87% / 78% | 71% / 93% / 84% |
| 1 | note disambiguating transfer intents | rejected: accuracy among sure decisions dropped | — | — |
| 2 | broaden a top-up-fee description | rejected: no gain | — | — |
| 3 | broaden a card-top-up-fee description | rejected: no gain | — | — |

Stopped after 3 consecutive rejections; **selected version = round 0 (the base rulebook)**. TEST unchanged at
71% / 93% / 84%. Teacher: a Claude Sonnet agent via the file handshake. Total spend $0.29.

### Verdict: an honest null result
- **The calibration finding replicates on real ground truth.** On 800 public queries with true labels, Jev's
  confident answers are 94.2% right at 71.5% coverage, against 83.7% for GPT-4o mini, whose confidence separates
  right from wrong far worse (AUROC 0.70 vs 0.84; ECE 0.143 vs 0.058). The email result was not a quirk of one inbox.
- **The self-improving loop did not help here, and the gate correctly refused all three edits.** Two reasons are
  likely: the base rulebook already starts at 84% accuracy with clearly named intents, and Banking77's remaining
  errors sit between near-duplicate intents, where a one-line wording change trades one confusion for another.
  On the email task the starting descriptions were vague, which is where a teacher's clarifications paid off
  (holdout handled share 36% -> 60%).
- **The safety gate is doing its job in both directions:** it accepted useful edits on email, and here it kept
  three plausible-sounding edits out of production because they did not measurably help.
- Next lever if wanted: allow multi-edit proposals per round, or a larger LEARN slice, rather than loosening the gate.

## Limits
- Single run, one seed; 300-query slices move about 0.3 points per query. VAL is used for both gate and selection, so only TEST is unbiased.
- Jev decisions are not perfectly repeatable between runs; within a run they are memoised (`loop/jev_memo.json`).
- Total spend $0.29 of the $0.50 cap (calibration: Jev $0.130, mini $0.078; loop rounds $0.084).
