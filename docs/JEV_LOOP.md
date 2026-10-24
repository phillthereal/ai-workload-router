# Jev learning loop (Step 2): code done, learning rounds NOT yet run

**Status: blocked.** The loop needs a teacher LLM to propose rulebook edits. The specified teacher (claude-haiku-4-5)
returned a billing error on the Anthropic account (no credit), and using another vendor for the private emails was not
authorised, so **zero proposals were generated**. Only the round-0 baseline below is measured. Everything else is
implemented and unit-tested; to finish, top up the Anthropic account and run
`python3 jev_loop.py run <private-dir>` then `python3 jev_loop.py report <private-dir>` and fill in the table.

## Method
```
Jev (current rulebook) -> sure? (p >= 0.9 and not protected)
        yes -> handled
        no  -> teacher LLM sees disagreements with the reference -> proposes ONE generic edit
               -> re-run Jev on TRAIN -> gate -> keep or discard
```
- Data: the 150 unique production classifier prompts (same indices as `shadow_test.load()`), split **chronologically**
  by `started`: oldest 100 = TRAIN (learn + gate), newest 50 = HOLDOUT (scored every round for the chart, never used to accept or reject).
- Reference label = Haiku's recorded production label (free). It is a reference, not truth.
- Teacher: Haiku called through `AnthropicAdapter` directly (no record/replay `.cache/`, which stays at 878 files).
  It sees up to 6 TRAIN emails (costly misses first, then disagreements, then right-but-unsure) and the rulebook, and
  must return one edit: a reworded category description or one short note, generic, no names/addresses/quotes.
- Leak guard: a proposal containing an address, link, long hex id, or any 6-word run copied from an email is rejected (logged as a leak-guard rejection).
- Gate (on TRAIN), accept only if all hold: (a) agreement among p >= 0.9 decisions does not drop; (b) handled share
  rises OR overall agreement rises; (c) no NEW costly miss (Haiku ACTION_NEEDED -> Jev IGNORE/FYI at p >= 0.9).
- Stops: 8 rounds, 3 consecutive rejections, or spend near the cap ($1.00 hard cap; a TRAIN re-run costs about $0.003).
- Privacy: learned rulebook versions and the proposal log are written only to `<dir>/loop/`; nothing private is committed.
  The committed base rulebook (`data/rulebooks/email_base.json`) is generic wording written for this repo.

## Round 0 (generic base rulebook, T = 0.9; live, spend $0.0046)
| set | n | handled share | agreement (all) | agreement at p>=0.9 (n) | costly misses | cascade final agreement | Jev->Haiku cost/1k |
|---|---|---|---|---|---|---|---|
| TRAIN | 100 | 52% | 82% | 100% (54) | 0 | 100% | $0.869 |
| HOLDOUT | 50 | 38% | 76% | 100% (21) | 0 | 100% | $1.054 |

Haiku-only is $1.68/1k. Note the generic base rulebook handles 38-52% against 54% in Step 0 (which used the production
category wording): neutral wording costs a few points of coverage before any learning.

## Rounds
| round | accepted | TRAIN handled/agree/miss | HOLDOUT handled/agree/miss |
|---|---|---|---|
| 0 | baseline | 52% / 82% / 0 | 38% / 76% / 0 |
| 1-8 | not run (teacher unavailable) | | |

```
HOLDOUT handled share by round (each # = 2%)
 r0 ###################  38%
```

## Verdict
None yet: no edit has been proposed or tested, so there is no evidence either way that learned clarifications help.
Do not read the round-0 table as a learning result.

## Limits (apply once the loop runs)
- n=150 over about 3 days; HOLDOUT is only 50 emails, so one email moves handled share by 2 points.
- Haiku is the reference, not truth; the teacher and the reference are the same model family.
- Protected categories (ACTION_NEEDED, JOB_REJECTION, RECRUITER_REPLY) are never Jev-handled and are largely untested (n=13 ACTION_NEEDED, 0 others).
- Overfitting risk: edits are gated on TRAIN only. If TRAIN improves and HOLDOUT does not, that is a valid finding and must be reported as such.
- Single run, no variance estimate.
