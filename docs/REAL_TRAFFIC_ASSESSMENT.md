# Real-traffic assessment (cost projection only)

Aggregates from the retained n8n runs of the live email classifier (2026-07-31 to 2026-08-03, 3.2 days). Zero API spend: token counts come from n8n's recorded run data, and routing uses the free offline `heuristic_classify` then `route_task`. **Quality on cheaper models is unmeasured. This is a cost projection, not a quality result.**

## Observed traffic
- 102 executions retained; 154 successful classifier calls (150 unique prompts, so a few re-runs/tests are included) and 40 job-alert scoring calls. All Haiku 4.5.
- Tokens are **real** (provider-reported), not estimated.
- Categories (classifier calls): IGNORE 53%, FYI 23%, JOB_ALERT 16%, ACTION_NEEDED 8%. No JOB_REJECTION or RECRUITER_REPLY occurred in this window, so those protected categories are untested here.
- Rate: about 48 calls/day in the window (the owner's historical figure is about 300 emails/week, roughly 43/day, consistent).

## 1. Current cost (Haiku 4.5, $1 / $5 per 1M tokens)
| step | calls | avg tokens in/out | cost/call |
|---|---|---|---|
| classify | 154 | 1,263 / 84 | $0.00168 |
| job-alert scoring | 40 | 1,454 / 385 | $0.00338 |

Blended (alert spend attributed to classified emails): **$0.00256 per email = $2.56 per 1,000 emails.** Per month: $3.68 at the observed rate (1,439 calls), $3.33 at 300/week.

## 2. Routing mix (free path)
Roster defined in the script: budget gpt-4o-mini, mid deepseek-chat, frontier claude-haiku-4-5 (honest baseline). Result: **100% of calls route to Haiku.** Every prompt is at least 3,700 characters (instructions plus the email body), so the length-based difficulty proxy marks every one "hard", which maps to frontier. The free heuristic therefore yields **0% savings** on this traffic; it cannot distinguish easy from hard emails because difficulty is just prompt length.

## 3. Projected cost (blended per email, vs Haiku-for-everything)
| scenario | per 1k emails | savings | per month (observed / 300 per wk) |
|---|---|---|---|
| Routed as-is | $2.56 | 0.0% | $0 / $0 |
| Routed + protected categories on Haiku | $2.56 | 0.0% | $0 / $0 |
| All gpt-4o-mini (upper bound) | $0.36 | 86.1% | $3.17 / $2.86 |
| Cascade: mini first, protected categories re-run on Haiku | $0.49 | 80.8% | $2.97 / $2.69 |

The cascade row is the realistic one if a cheap model is ever used: it pays for mini on everything plus Haiku again on the 8.4% of calls Haiku labelled protected.

## 4. Risk and recommendation
Misfiling IGNORE, FYI or JOB_ALERT is cheap. Missing ACTION_NEEDED, JOB_REJECTION or RECRUITER_REPLY is expensive (they create tracker rows and tasks). **Keep these three on Haiku regardless.** Caveat: a protected email can only be identified after classification, so the safe design is the cascade (cheap first, escalate protected and low-confidence results), and the cheap model's recall on protected categories is exactly the unmeasured risk.

Absolute savings are small (about $3 per month). The next step, if worthwhile, is a live shadow test costing a few cents: run gpt-4o-mini on a sample and compare its categories (especially protected recall) with Haiku's.

## 5. Backlog estimate (2026-08-03 to 2026-10-20, 78 days)
**Estimate**, assuming Haiku and the blended $0.00256 per email: about $9.57 at the observed rate (about 3,740 emails) or $8.56 at 300/week (about 3,340 emails).

## Method and limits
- Source: recorded token usage of the Haiku sub-node runs in n8n; failed calls (connection errors) excluded and unbilled.
- Costs use prices in `src/router/config.py`; mini token counts assumed equal to Haiku's (tokenizers differ slightly).
- Window is 3.2 days and includes some test/re-run calls; rates extrapolate that.
- Alert-scoring calls were routed with the same heuristic, and the cascade protects only classifier categories.
- Reproduce: `python3 assess_real_traffic.py <calls.jsonl>` (private extraction, not in the repo).
