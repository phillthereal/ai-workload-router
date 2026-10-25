# Improvement round, October 2026: what was tried, what survived

Six ideas tested against one bar, set before any results were seen: **better on held-out data, no more costly
errors, at equal or lower cost**. A result had to reproduce when re-run, and, where the data allowed, hold
across four different data splits rather than one. Detail is in each linked doc. Total live spend: about $0.04.

| Idea | Data | Result | Verdict |
|---|---|---|---|
| Per-category confidence thresholds | Banking77, true labels | Coverage −0.4 pts; only 3 of 77 classes had enough data to tune | ❌ Fail — [X1](X1_THRESHOLDS_KNN.md) |
| Reuse labels of very similar past items (kNN pre-stage) | Banking77 | +1.6 pts coverage, 11% fewer Jev calls on the original split — but added errors on 2 of 3 other splits | ❌ Fail on robustness — [X1](X1_THRESHOLDS_KNN.md) |
| Jev + kNN agreement, tuned for coverage | Banking77 | +10.3 pts coverage but 41 wrong vs 33 | ❌ Fail — [X1](X1_THRESHOLDS_KNN.md) |
| Same, tuned under a hard error budget | Banking77 | +4.4 pts, same errors on the original split — passed 1 of 4 splits | ❌ Fail on robustness (a fluke the multi-split check caught) — [X1](X1_THRESHOLDS_KNN.md) |
| Jev picks the router's model tier, with a never-cheaper-than-the-label safety rule | 43 benchmark tasks with logged real answers | 50.4% net saving vs 36.2% for the Haiku classifier (+14.2 pts, CI +0.8 to +31.8), 100% quality, 0 tasks routed too cheaply | ⚠️ Promising, unreplicated — [X2](X2_JEV_TIER_PICKER.md) |
| Security guards for the email chain | Synthetic attacks + 150 real emails (aggregates) | Spoofed-sender phishing: 6/6 buried by sender rules → 0/6 with a security-keyword veto, which cancels ~1% of real rule decisions. Fencing the email text: free (+3 handled of 159). Malformed-answer guard closes an API-drift gap. Injection attempts: 0/72 buried even before any fix | ✅ Pass — [X3](X3_SCENARIOS.md) |

## What this changes
1. **Ship the security guards** (veto, fence, malformed-answer guard) to the live email chain — the only
   robust win, and it closes a real hole: a phishing email spoofing a trusted promo sender was silently filed
   as "ignore". Staged and tested on a switched-off copy; live change awaits the owner's approval.
2. **Replicate the Jev tier picker before building on it.** Its sample is small and skewed (38 of 43 tasks
   only needed the cheap model) and the best of 7 variants was picked. The fix is a fresh, more varied task set
   with frontier answers — blocked for now because the Anthropic API account (Opus) has no credit.
3. **Stop tuning thresholds on Banking77.** Three different "cheaper decisions" ideas each looked good on one
   split and failed on others. The base Jev threshold of 0.90 is the robust choice on this data.

## Method lessons
- **One split lies.** Two of the round's "passes" vanished under a four-split re-test. Every future result in
  this repo should be checked across splits before it ships.
- **Pre-registered bars beat post-hoc stories.** Each verdict above follows the rule written before the run,
  including the ones that would have been nicer to call a win.
- Limits: the four Banking77 splits re-partition the same 1,400 items, so they are not independent; the email
  sample is 150 messages over three days; X2's n is 43.
