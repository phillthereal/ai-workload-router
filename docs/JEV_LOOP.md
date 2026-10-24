# Jev learning loop (Step 2): a classifier that improves its own rulebook

**Result:** over 8 rounds, Jev's share of emails handled on its own rose from **36% to 60%** on held-out emails it
never learned from, with **zero important emails missed**, cutting the Jev→Haiku cascade from $1.09 to $0.68 per
1,000 emails (Haiku-only: $1.68). Two later rounds pushed it to 66% but introduced one miss the training-only gate
could not see: **round 6 is the version to keep.** Total spend $0.054.

## Method
```
Jev (current rulebook) -> sure? (p >= 0.9 and not protected)
        yes -> handled
        no  -> teacher sees disagreements with the reference -> proposes ONE generic edit
               -> re-run Jev on TRAIN -> gate -> keep or discard
```
- Data: the 150 unique production classifier prompts (same indices as `shadow_test.load()`), split **chronologically**
  by `started`: oldest 100 = TRAIN (learn + gate), newest 50 = HOLDOUT (scored every round, never used to accept or reject).
- Reference label = Haiku's recorded production label (free). It is a reference, not truth.
- Teacher: a Claude Sonnet agent answering through a file handshake (`--teacher manual`), so the loop costs no
  Anthropic API credit. An API teacher (`--teacher claude-haiku-4-5`) is also supported. The teacher sees up to 6
  TRAIN emails (costly misses first, then disagreements, then right-but-unsure) and returns one edit: a reworded
  category description or one short note, generic, with no names, addresses or quotes.
- Leak guard: a proposal containing an address, link, long hex id, or any 6-word run copied from an email is rejected.
- Gate (on TRAIN), accept only if all hold: (a) agreement among p >= 0.9 decisions does not drop; (b) handled share
  rises OR overall agreement rises; (c) no NEW costly miss (Haiku ACTION_NEEDED -> Jev IGNORE/FYI at p >= 0.9).
- Stops: 8 rounds, 3 consecutive rejections, or spend near the cap ($1.00 hard cap).
- Privacy: learned rulebook versions and the proposal log stay in the private data directory; nothing private is
  committed. The committed base rulebook (`data/rulebooks/email_base.json`) is generic wording written for this repo.

## Rounds (live, `jev-1.14.0`, T = 0.9)
| round | edit (described generically) | gate | TRAIN handled / agree / misses | HOLDOUT handled / agree / misses | HOLDOUT cost/1k |
|---|---|---|---|---|---|
| 0 | generic base rulebook | baseline | 51% / 82% / 0 | 36% / 74% / 0 | $1.092 |
| 1 | broaden job-alert description to single listings and apply nudges | accepted | 55% / 88% / 0 | 38% / 84% / 0 | $1.056 |
| 2 | broaden "ignore" to several notification types | rejected: sure-agreement dropped | — | — | — |
| 3 | note: waitlist and notification confirmations are "ignore", not FYI | accepted | 60% / 94% / 0 | 44% / 82% / 0 | $0.972 |
| 4 | note: single job-posting emails are job alerts | accepted | 71% / 95% / 0 | 60% / 80% / 0 | $0.675 |
| 5 | note: brand marketing and marketplace notices are "ignore" | rejected: sure-agreement dropped | — | — | — |
| 6 | note: time-window delivery notices need action | accepted | 70% / 96% / 0 | **60% / 80% / 0** | **$0.681** |
| 7 | note: new-device sign-in alerts need action | accepted | 72% / 97% / 0 | 64% / 78% / **1** | $0.618 |
| 8 | note: marketplace offer notices are "ignore" | accepted | 76% / 98% / 0 | 66% / 82% / **1** | $0.574 |

```
HOLDOUT handled share by round (each # = 2%)
 r0 ##################             36%
 r1 ###################            38%
 r3 ######################         44%
 r4 ##############################  60%
 r6 ##############################  60%
 r7 ################################  64%  (1 costly miss)
 r8 #################################  66%  (1 costly miss)
```

## Verdict
- **The loop works on unseen data.** Holdout handled share rose from 36% to 60% with zero costly misses through
  round 6; the cascade's per-1,000 cost fell 38% versus round 0 and 59% versus Haiku-only.
- **The gate caught bad edits:** 2 of 8 proposals were rejected because they made Jev's confident answers less reliable.
- **It also overfits, and the training-only gate cannot see it.** Training agreement climbed to 98% while holdout
  agreement peaked at 84% after round 1, and rounds 7–8 introduced a costly miss on holdout that training never
  showed. Narrow, sender-specific notes generalise worse than reworded category descriptions.
- **Design fix this points to:** pick the version by a held-out validation score, and stop when it stops improving,
  instead of accepting every edit that passes on training. In production, the owner's corrections in the email log
  are the natural validation set.

## Limits
- n=150 over about 3 days; HOLDOUT is only 50 emails, so one email moves handled share by 2 points.
- Haiku is the reference, not truth; Jev can only learn to agree with it.
- Protected categories (ACTION_NEEDED, JOB_REJECTION, RECRUITER_REPLY) are never Jev-handled and are largely
  untested (n=13 ACTION_NEEDED in total, 0 of the others).
- Jev's decisions are not perfectly repeatable: re-running the base rulebook gave 51%/36% handled, against 52%/38%
  in an earlier run. Each run's numbers are internally consistent because decisions are cached within the run.
- Single run, one teacher. Comparing teachers (Sonnet vs Haiku) is the natural follow-up.
