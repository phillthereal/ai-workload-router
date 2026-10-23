# Jev calibration, Step 0 go/no-go (aggregates only)

Question: is TypeSafe Jev's confidence honest on real email classification? Reproduce with
`python3 jev_calibration.py report <private-dir>` (the `run` subcommand makes the live calls).

## Method
- Data: the 150 unique production classifier prompts from the shadow test (same indices as `shadow_test.load()`), one Jev call each, single run, `jev-1.14.0`, via `POST /v1/systemone` directly (no repo adapters, no `.cache/`).
- Prompt split, identical for every email: `criteria` = the 7 category lines of the production prompt; `state` = text between "Email to classify:" and "Respond with:"; `instructions` = one fixed line.
- Reference = Haiku's production label. It is a reference, **not truth**: a "miss" can be Haiku's error.
- Spend: $0.0048 (114k input tokens at $0.042/1M), cap was $0.10. 0 failed calls.

## Results (n=150; Haiku labels: IGNORE 79, FYI 35, JOB_ALERT 23, ACTION_NEEDED 13)
- Agreement with Haiku: 80.0% [73.3, 86.0] (bootstrap 95% CI).
- Recall: IGNORE 96.2% (79), FYI 85.7% (35), ACTION_NEEDED 69.2% (13), JOB_ALERT 21.7% (23; 18 sent to IGNORE).
- Costly misses (Haiku ACTION_NEEDED -> Jev IGNORE/FYI): 4 of 13. JOB_CONFIRMATION, JOB_REJECTION, RECRUITER_REPLY: n=0, untested.

### Calibration (top-choice probability)
| Jev p | n | mean predicted | actual agreement |
|---|---|---|---|
| <0.5 | 6 | 0.48 | 0.33 |
| 0.5-0.7 | 32 | 0.60 | 0.38 |
| 0.7-0.8 | 12 | 0.74 | 0.67 |
| 0.8-0.9 | 14 | 0.85 | 0.86 |
| 0.9-0.95 | 22 | 0.93 | 1.00 |
| >=0.95 | 64 | 0.99 | 1.00 |

ECE 0.075 (the separate `confidence` field: ECE 0.060, same shape). Errors sit in the low-confidence bins (over-confident below 0.7, slightly under-confident above 0.9).
**Jev >= 0.90: 86 emails (57% coverage), 86/86 agree with Haiku, 0 costly misses.** With n=86 the true error rate could still be a few percent (rule of three: 95% upper bound about 3.5%).

### Contrast: gpt-4o-mini self-reported confidence (n=150)
ECE 0.029 looks better, but it is an artefact: every answer claims 80-100, so the bins are nearly one bucket. Discrimination (AUROC, chance of ranking a right answer above a wrong one) is 0.92 for Jev vs 0.68 for mini. At confidence >= 0.90 mini keeps 113 emails with 7 disagreements (1 costly miss); Jev's >= 0.90 bucket has none. Mini's "confidently wrong" is a lack of signal, which Jev's probabilities do not share here.

### Cascade (Jev first; escalate to Haiku if p < T or Jev picks ACTION_NEEDED / JOB_REJECTION / RECRUITER_REPLY)
| T | Jev-handled | final agreement | costly misses | cost per 1k | savings vs Haiku $1.68/1k |
|---|---|---|---|---|---|
| 0.80 | 61% | 99.3% | 0 | $0.703 | 58% |
| 0.90 | 54% | 100% | 0 | $0.827 | 51% |
| 0.95 | 41% | 100% | 0 | $1.040 | 38% |

Jev input averages 761 tokens/email (its criteria are resent each call). Savings come mostly from Haiku calls avoided; Jev cost is about $0.03/1k.

### Latency
Median 0.32 s, p90 0.50 s per call.

## Verdict: GO (provisional)
At T=0.90 the high-confidence bucket (86 emails, 57% coverage) matches its stated confidence (predicted 0.97, actual 1.00) with 0 costly misses; T=0.80 also leaves 0 misses. Jev's confidence is informative where mini's is not.

## Limits
- n=150 over about 3 days, one inbox, one run (no variance across reruns); ~86 emails in the key bucket.
- Haiku is the reference, not truth; agreement is not accuracy.
- Three protected categories (JOB_CONFIRMATION, JOB_REJECTION, RECRUITER_REPLY) have n=0 and ACTION_NEEDED n=13; the cascade's safety for them is untested, and 0 misses on 13 is a weak guarantee.
- JOB_ALERT recall is poor (21.7%), but those misses all fall below 0.90 so the cascade routes them to Haiku; Jev's prompt split may be suboptimal for it.
- Jev version `jev-1.14.0` (`jev-latest` may change under us).
