# Live shadow test: cheap models vs. production Haiku on real email classification

Aggregates only. No email text, subjects, senders or ids are committed.

## Method
- Input: 150 unique classifier prompts from 3 days of real traffic in the owner's n8n email classifier (production model: Claude Haiku 4.5). Job-alert scoring calls excluded.
- Each prompt was sent byte-for-byte as production sent it to `gpt-4o-mini` and `deepseek-chat`, once each, sequentially, default parameters. Adapters were built directly (not via `get_adapter`), so the record/replay `.cache/` was untouched (878 files before and after).
- An answer counts only if it parses to JSON with a valid `category` and integer `confidence` (accepting a flat object or an `output` wrapper). Anything else is a parse failure.
- **Haiku's label is the reference, not ground truth.** "Agreement" means "same as Haiku"; where they differ Haiku may be the wrong one.
- Cascade (simulated from recorded answers, no extra calls): cheap model first; escalate to Haiku if the cheap model says a protected category (ACTION_NEEDED, JOB_REJECTION, RECRUITER_REPLY), reports confidence < 80, or fails to parse. Final label for escalated emails is Haiku's recorded answer; cost = cheap + Haiku.
- Cost uses real returned token counts x `src/router/config.py` prices. Haiku cost is recomputed from recorded tokens on the same 150 prompts ($1.68 per 1,000), lower than the $2.56 production figure, which likely includes other call volume.
- CIs: percentile bootstrap (2,000 resamples, seed 0, resampling emails).
- Actual spend: **$0.086** (cap $0.25).

Haiku reference label mix: IGNORE 79, FYI 35, JOB_ALERT 23, ACTION_NEEDED 13. **JOB_REJECTION, RECRUITER_REPLY (and JOB_CONFIRMATION) had n=0: UNTESTED.**

## Results

| | gpt-4o-mini | deepseek-chat |
|---|---|---|
| Agreement with Haiku | 90.0% [85.3, 94.7] | 91.3% [86.7, 95.3] |
| Parse failures | 0 / 150 | 0 / 150 |
| Recall ACTION_NEEDED | 92.3% (12/13) | 76.9% (10/13) |
| Recall JOB_REJECTION / RECRUITER_REPLY | UNTESTED (n=0) | UNTESTED (n=0) |
| Recall JOB_ALERT (n=23) | 100% | 100% |
| Recall FYI (n=35) | 82.9% | 85.7% |
| Recall IGNORE (n=79) | 89.9% | 93.7% |
| Costly misses (Haiku ACTION_NEEDED, cheap IGNORE/FYI) | 1 | 3 |
| Cost / 1,000 emails | $0.187 | $0.384 |
| Mean confidence, agree / disagree | 90.6 / 88.0 | 91.1 / 85.9 |

Calibration is weak: confidence barely separates agreements from disagreements, and most disagreements are reported at confidence >= 80 (15 of 15 for gpt-4o-mini, 10 of 13 for deepseek-chat).

Confusions (Haiku -> cheap, off-diagonal): gpt-4o-mini: FYI->ACTION_NEEDED 5, IGNORE->FYI 5, IGNORE->ACTION_NEEDED 3, ACTION_NEEDED->FYI 1, FYI->JOB_CONFIRMATION 1. deepseek-chat: IGNORE->FYI 5, FYI->IGNORE 3, ACTION_NEEDED->FYI 2, FYI->ACTION_NEEDED 2, ACTION_NEEDED->IGNORE 1.

### Cascade simulation (vs. Haiku-only $1.68 / 1k)

| | gpt-4o-mini -> Haiku | deepseek-chat -> Haiku |
|---|---|---|
| Escalated | 20 / 150 | 15 / 150 |
| Agreement | 95.3% [92.0, 98.7] | 94.0% [90.0, 97.3] |
| ACTION_NEEDED recall (n=13) | 92.3% | 84.6% |
| Costly misses remaining | 1 | 2 |
| Cost / 1,000 | $0.397 | $0.546 |
| Savings vs Haiku | 76.4% | 67.5% |

The cascade only escalates when the cheap model *claims* a protected category, so an ACTION_NEEDED email the cheap model calls FYI/IGNORE with high confidence is never rescued; that is the residual miss.

## Verdict
Both models are cheap and parse reliably, but neither is safe as a silent replacement: the gpt-4o-mini cascade is the better candidate (higher protected recall, 76% cheaper) yet still lost 1 of 13 action-needed emails relative to Haiku. Whether that miss is a real error or a Haiku error needs the owner's adjudication of the private disagreement list. Escalating every cheap FYI/IGNORE would remove the miss but would escalate most traffic and erase the savings.

## Limits
- n=150 from 3 days; the CIs are wide and one email moves agreement by ~0.7 pp. ACTION_NEEDED rests on n=13.
- Haiku is a reference, not truth; no human labels.
- JOB_REJECTION, RECRUITER_REPLY and JOB_CONFIRMATION are untested in this window. No claim about them can be made, and they are the protected classes that matter most.
- Single run, default temperature; model answers are not deterministic.

Reproduce: `python3 shadow_test.py run <dir>` then `python3 shadow_test.py report <dir>` (private input dir required). Math tests: `tests/test_shadow_metrics.py`.
