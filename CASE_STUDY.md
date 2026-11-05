# AI Workload Router — Product Case Study

**Author:** Fil Ivanov · **Role:** Product (end-to-end: problem framing → spec → benchmark → result)

> A cost-optimizing routing layer for LLM applications. This case study shows how I took a sprawling idea, isolated the one assumption worth testing, cut scope to test it fast, and tested the result with live benchmarks, including one that missed its target. The code is the evidence; the product thinking is the exhibit.

---

## TL;DR

Teams building on LLMs overpay by sending every request to one premium model "to be safe." The **AI Workload Router** classifies each task and routes it to the cheapest model that can still do it well, logging cost and quality on every call so routing improves over time.

**On 300 public tasks, the router's default rule cut cost 86.4% and kept 98.7% of quality, so it met both targets.** With 99% certainty it saves at least 80.8% and keeps at least 95.1%. The first version missed the quality target on public tasks, at 84.1% cheaper and 91.8% of quality, and that miss drove the changes described below. An earlier 25-task benchmark that I wrote myself had looked good too, at 53.6% cheaper with my hand labels, but those tasks were too easy to show the problem. Quality on hard tasks alone is still not proven. Every number is from live API calls. A hand check and a code check only partly confirmed the judge on the public run, and the target is still missed when code replaces the judge wherever it can.

The most useful finding wasn't the headline number — it was *why* the savings stopped where they did. That's the part a good PM cares about, and it's below.

---

## The current result: both targets met

The first public test missed on quality, so I changed how the router decides and retested it on fresh public tasks. The targets stayed the same: at least 40% cheaper than sending everything to Opus, and at least 95% of its quality.

| Version | Tasks | Cheaper | Quality kept | Targets |
|---|---|---|---|---|
| First version | First 300 | 84.1% | 91.8% | Not met |
| Cheap models must agree on classification | New 300 | 75.4% | 92.5% | Not met |
| Extraction also sent to Opus | Another new 300 | 65.7% | 99.5% | Met |
| **Current default** | Same 300 as the row above | **86.4%** | **98.7%** | **Met** |

Only the last two rows use the same tasks, and I updated model prices before the last row, so read this as a trend, not an exact comparison.

| Current default | Tasks | Cheaper | 99% range | Quality kept | 99% range |
|---|---|---|---|---|---|
| All | 300 | **86.4%** | 80.8% to 90.7% | **98.7%** | 95.1% to 102.4% |
| Reasoning | 48 | 84.1% | 67.7% to 92.4% | 97.8% | 90.9% to 100.0% |
| Extraction | 84 | 87.5% | 82.8% to 91.4% | 98.7% | 89.9% to 108.3% |
| Classification | 96 | 71.0% | 59.2% to 81.9% | 98.7% | 90.9% to 106.8% |
| Short writing | 72 | 94.8% | 87.8% to 97.3% | 99.3% | 94.3% to 104.2% |
| Hard | 24 | 80.0% | 56.7% to 92.9% | 95.2% | 80.0% to 100.0% |

**How the default works.** A free lookup compares each prompt with similar labelled tasks to pick its type, so the paid classifier is gone. On reasoning and extraction, Haiku 5.5 and DeepSeek both answer, and the DeepSeek answer is kept if they agree. If they differ on reasoning, GPT-4o mini votes, and Sonnet 5.5 decides when it sides with neither. Extraction skips the vote. Classification goes to Opus when the two cheap models differ. The cost of every discarded answer is included.

**What moved.** Against the first version, classification quality rose from 80.3% to 98.7%, and extraction from 91.8% to 98.7%. Reasoning went mostly to Opus and saved only 29.9%. It now saves 84.1% at the same 97.8% quality. Classification and extraction gave up some saving to get there.

**An idea I left out.** Chain of Draft asks models to keep their working very short. On 96 reasoning tasks it saved more, 92.0% against 80.4%, but quality fell from 98.9% to 95.6%, and to 90.5% on 48 hard tasks.

**What is not proven.** Reasoning, extraction and hard tasks each fall below 95% at the worst end of their ranges, because the groups are small. Hard tasks are the real gap, at 80.0% on 24 tasks. These 300 tasks had already been used to confirm the rule, though not to pick it.

## The first public test: the quality target was missed

I reran the router on public tasks, with real API calls and the classifier choosing each route. The targets were unchanged: at least 40% cheaper than sending everything to Opus, and at least 95% of its quality. The runs cost $2.7228 in total, including one attempt that stopped early.

| Run | Cheaper than all-Opus | 95% range | Quality kept | 95% range | Targets |
|---|---|---|---|---|---|
| Mixed, 300 tasks | 84.1% | 79.0% to 88.7% | **91.8%** | 86.9% to 96.6% | **Not met** |
| Hard, 40 tasks | 22.2% | 9.0% to 40.3% | 96.1% | 89.5% to 100.0% | Apply to the mixed run only |

| Task type (mixed run) | Tasks | Cheaper | Quality kept |
|---|---|---|---|
| Classification | 96 | 96.6% | **80.3%** |
| Extraction | 84 | 95.1% | 91.8% |
| Reasoning | 48 | 29.9% | 97.8% |
| Short writing | 72 | 88.6% | 101.1% |

**The loss is concentrated.** Classification gave up the most quality, and reasoning saved the least. On the hard set the router sent 27 of 40 tasks to Opus, so it saved much less. My 25 tasks scored near 0.99 on every tier, so they could not have shown this.

**Checking the judge.** Short writing is scored by an LLM judge, so I checked it two ways. I scored 29 randomly picked answers by hand, without seeing the model or the judge's score. We agreed on pass or fail for 22 of the 29, but the correlation was only about 0.03, so the check did not confirm the judge. A code check then tested 95 of the 99 instruction rules on the 69 tasks it could cover. On those tasks the router kept 98.2% of Opus's quality (87.9% to 109.4%), not the judge's 101.1%. The judge gave 0.9 or more to 15 answers that broke a rule, and under 0.5 to 4 that passed. With code in place of the judge on those tasks, the mixed run would keep 90.8% (85.6% to 96.0%). The target is still missed, and the published scores stay as they are.

The rest of this case study is the earlier work, in the order it happened. Its numbers come from smaller or private tests.

---

## The problem

Teams shipping LLM features route all traffic to a single premium model because comparing models per-task is tedious and there's no cheap way to know which tasks a smaller model can handle. The result is an API bill that scales linearly with usage and no visibility into the price/quality tradeoff being made on every call.

The person who feels this: **the engineer or founder who owns the bill** and has only one lever — "use a cheaper model everywhere and hope quality holds."

## The one bet worth testing

The original concept (compiled by ChatGPT) was expansive: a multi-agent orchestrator decomposing tasks across four specialist models, adaptive routing, a full web app, three build phases. Most of that is machinery built *before* knowing whether the core idea pays off.

I cut it to a single testable hypothesis:

> **For a realistic task mix, routing to cheaper models where they suffice cuts cost meaningfully while holding quality within an acceptable band of the frontier-only baseline.**

If that's false, no orchestration UI matters. So v1 proves it on a benchmark before anything else gets built. Everything that didn't serve that proof — task decomposition, the multi-agent layer, the frontend, adaptive routing — was explicitly deferred, not deleted. (Full scope decisions in [`docs/PRD.md`](docs/PRD.md).)

## How I defined "done" before building

Two decisions that make the result trustworthy:

- **A named baseline.** "Reduce cost" is meaningless without a reference. The baseline is *every task sent to the frontier model* (Claude Opus 4.8). All deltas are measured against it.
- **A quality floor, not just a cost target.** Cost savings that tank quality are worthless, so success required holding ≥95% of baseline quality — measured, not assumed.

## What I built (the Prove-it MVP)

A backend benchmark harness, deliberately minimal:

| Piece | What it does |
|---|---|
| **Provider adapter layer** | One internal interface over multiple model APIs, so a model is swappable by config. |
| **Rules router** | Classifies each task and picks a tier: easy → budget model, medium → mid, hard/reasoning → frontier. |
| **Live LLM judge** | Grades open-ended answers 0–1 against a rubric, using a strong model as the judge. |
| **Record & replay cache** | Every real API response is cached, so the whole benchmark re-runs for free and is fully reproducible by anyone. |
| **Performance log** | Every run records task, model, tokens, cost, latency, and quality — the data layer that would drive adaptive routing later. |

**Models (this run):** the three tiers now span three real vendors — GPT-4o mini (OpenAI, $0.15 / $0.60 per 1M tokens, budget), DeepSeek Chat (DeepSeek, ~$0.27 / $1.10, mid), and Claude Opus 4.8 (Anthropic, $5 / $25, frontier). The judge is Claude Opus 4.8, cross-validated by a second, independent judge — see "Validating the judge" below. This is the second iteration of the benchmark: the first run kept all three tiers on Claude models (Opus / Sonnet / Haiku) to prove the method fast on a single working key, and landed 32.5% savings. This run swaps the budget and mid tiers onto OpenAI and DeepSeek, widening the price range the router has to work with — see "The insight that matters" for what that did to the ceiling.

**Benchmark:** 25 tasks weighted to resemble real production traffic — mostly classification and extraction, some short generation, a minority of hard reasoning (roughly 32% / 28% / 24% / 16%).

## The first result: 25 tasks I wrote

| Strategy | Total cost | Mean quality | Mean latency |
|---|---|---|---|
| Frontier-only (baseline) | $0.1260 | 0.992 | 4,155 ms |
| **Router** | **$0.0585** | **0.996** | **2,237 ms** |

- **Cost reduction: 53.6%**
- **Quality retention: 100.4%** of baseline (the router edged it, within noise)
- **Latency: 46.2% faster** (median 1,208 ms vs. 3,663 ms) — cheaper models also happen to respond faster, so this is a second win at zero extra cost, not a tradeoff.
- **Hypothesis (≥40% cost reduction, ≥95% quality retention): PASSED on these 25 tasks.** The 300-task public test did not pass.
- The router sent 11 tasks to the budget tier (GPT-4o mini), 7 to the mid tier (DeepSeek Chat), 7 to the frontier (Opus) — a sensible spread by difficulty, not a blunt "cheapest everywhere."

## What this looks like at scale

Extrapolating linearly from this run's per-task cost (router $0.00234 vs. baseline $0.00504):

| Volume | Baseline / month | Router / month | Saved / month |
|---|---|---|---|
| 100,000 requests | $503.92 | $234.03 | $269.89 |
| 1,000,000 requests | $5,039.20 | $2,340.26 | $2,698.94 |

This is directional, not a forecast — it assumes production traffic resembles this benchmark's task mix, and real traffic will differ. It's meant to make the shape of the savings tangible, not to be quoted as a committed number.

## Validating the judge

An LLM judge grading its own routing decisions is a circularity worth checking. A second, independent judge (GPT-4o mini — a different vendor from the primary judge, Claude Opus 4.8) re-scored all 30 rubric-judged answers from this run blind. Agreement: **mean absolute difference of 0.010, with 96.7% of scores within 0.15 of each other.** That's a meaningfully lower bar than ground truth, but it's evidence the primary judge isn't an outlier — two different judges, from two different vendors, converge on similar scores. That check covers this 25-task run only. On the public run, a hand check did not confirm the judge, and the second judge has not been run.

## The insight that matters

The hypothesis set a 40% cost target. The first (all-Anthropic) run landed at 32.5%; this cross-vendor run landed at 53.6% — and *why* the ceiling moved is the real product lesson.

**The savings ceiling is set by how much of your workload genuinely needs the frontier model, and by how wide a price range the router has to work with — not by how clever the router is.**

In this task mix, reasoning tasks are 16% of the tasks but ~33% of the baseline cost, and they correctly route to the frontier model under *both* strategies, in both runs — a cheaper model measurably underperforms on them. That third of the bill is "locked": no routing can reduce it without losing quality. What changed between the two runs is everything else: swapping the budget and mid tiers from Claude models onto OpenAI and DeepSeek widened the price range available to the router, and the savings on the unlocked two-thirds of the bill grew with it — 32.5% → 53.6%. That's not a fluke; it's the "cross-provider routing widens the ceiling" line that was future work in the first iteration, executed and confirmed here.

The actionable takeaway for anyone adopting this: **there are two levers, not one — your traffic composition (fixed, sets what's locked) and your vendor price range (a choice you control).** A product that is 80% classification will save far more than one that is 50% hard reasoning; and widening the vendor range you route across raises the ceiling further, exactly as this run demonstrates. The performance log makes the composition side of that visible, which is the point.

That's a more honest and more useful finding than a suspiciously round 40% would have been.

## v2: I built the next steps — and found where routing stops paying

The "what I'd do next" list below is no longer hypothetical. I replaced the hand-labeled difficulty with a real prompt classifier, built the confidence-based escalation (item 2), and ran four follow-up experiments live. Three findings changed how I think about the product.

**Predicting difficulty is cheaper than labeling it — and the labels were the conservative part.** A real deployment gets a prompt, not a difficulty label, so I replaced my hand labels with a cheap classifier that predicts difficulty from the prompt — and reports savings *net of that prediction's cost*. It *raised* cross-vendor savings from 53.6% to 65.6% at equal quality: on 7 of 25 tasks it routed to a cheaper model than my labels dictated, all judged 0.85–1.00. I had been over-labeling difficulty; the classifier found the slack, for a routing cost of ~1% of savings. The honest, automatable version was *better* than the hand-tuned one.

**React-to-failure beats predict-ahead within a vendor.** The cascade — try Haiku, let a cheap reference-free verifier check the answer, escalate to Opus only on a failed check — netted 53.1% within the Claude ladder, beating the predict-then-route classifier's 42.5%. It wins by being less conservative: it tries the cheapest model on everything and discovers what works, instead of pre-routing all reasoning to the frontier. The cost is honest — its overhead (verifier calls plus discarded cheap attempts) runs 31% of savings, and it adds latency.

**Routing stops paying on hard work — the most useful thing I learned.** The published 25-task set saturates quality near 0.99 on every tier, so it can't answer "does routing hold quality when tasks are hard?" I built a 10-task hard set (mostly objective exact-match, no judge leniency) that separates the tiers cleanly: Haiku 0.70, Sonnet 0.90, Opus 1.00. On it, both strategies held 100% quality but went cost-*negative* — every hard task needs the frontier, so routing only adds overhead. That is the sharpest form of the composition insight: **the router's value is a direct function of how much easy work exists, and on an all-hard workload it is worse than doing nothing — but it never trades away quality to find that out.** The buyer's decision rule falls out of this: measure your easy-task fraction before adopting a router. I turned that rule into an [interactive ROI calculator](https://phillthereal.github.io/ai-workload-router/calculator.html), calibrated on the live anchors above — it tells you your savings ceiling before you build anything, including when the honest answer is "don't."

Everything is additive and reproducible — the default configuration still reproduces the original 53.6% run exactly, and each experiment is a one-line command in the README.

## What I'd do next

1. **Adaptive routing** — feed the performance log back into the router so model choice is learned per task type, not hand-configured. (The log was designed for this from day one; the v2 classifier is a first step — prediction from the prompt rather than a static rule.)
2. **Confidence-based escalation** — ✅ *built in v2 (the cascade): try the cheap model first, a reference-free verifier checks the answer, escalate only on a failed check.* Next: tune the escalate threshold per task type from the performance log, rather than a single global value.
3. **More vendors** — cross-provider routing is now proven across three (Anthropic, OpenAI, DeepSeek); v5 added Kimi (Moonshot) as a fourth, offline-only so far; a Gemini adapter is the next candidate to test whether the ceiling keeps climbing or starts to plateau.
4. **Judge checks** — the second judge agreed on the 25-task run (96.7%), and the eval has grown to 300 public tasks. A hand check on the public run did not confirm the judge, so the next steps are running the second judge there and using code wherever a rule can be checked. The v2 hard set (objective exact-match) is a start at reducing judge dependence.

## v3: the router learns from its own log

Item 1 above — adaptive routing — is no longer a design, it's a result. Every
run this project has ever made is sitting in `data/runs.db`: task type,
model, cost, quality, success. The router used to *re-derive* what it already
paid to learn — the classifier predicted difficulty cold from the prompt,
every single time, with no memory of the last hundred times it saw a task
like this one. v3 is the fix, built and evaluated: consult that outcome
history before routing, and only let it push a task to a cheaper tier when
the evidence clears a quality bar strong enough to trust — the same
never-trade-quality guarantee that held through v1/v2, applied via a
decision rule modeled directly on the v2 verifier finding (cheap signals are
fine on easy tasks and a liability on hard ones).

Held out on 14 tasks the log had never seen (chronological split — no task
can see outcomes from its own batch or the future), the learned router beat
the static classifier by **22.7% at identical quality, 1.000 vs. 1.000**:
$0.014319 against $0.018516. Against the frontier-only baseline, that's
**26.1% net savings, versus just 4.4% for the static router on this same
task set** — the held-out mix is mostly hard tasks, exactly where a cold
policy collapses back to routing everything to the frontier; the learned
router's log already knew Haiku had handled these shapes. The lookup itself
is SQL over the run log, not an LLM call, so it costs **$0** to consult.

Task by task: 9 of 14 downgraded on strong evidence, and all 9 scored 1.00
after the downgrade. 3 more had evidence on file but **refused** to
downgrade — history showed the cheap tier failing that exact task shape, so
the task stayed at the frontier and scored 1.00 there. That refusal
mechanism is the finding that matters most: a router that only kept the 9
wins, with no refusals, would just be "route everything cheap" with extra
steps. The remaining 2 tasks hit thin or no history and fell back to the
static classifier's tier — the designed cold-start rule, not a bug.

**The caveat that has to travel with the 22.7%:** the saving is conditional
on the workload repeating task shapes the log has already seen. The held-out
tasks were deliberately authored to repeat shapes logged ≥5 times — the only
regime in which a learned router does anything at all. A workload with no
repeated shapes gets cold start everywhere, which is exactly the static
router's numbers, at the same $0 extra overhead. Full write-up, including the
feasibility fight over undated history and the per-task evidence log:
[`docs/V3_FINDINGS.md`](docs/V3_FINDINGS.md). Original design:
[`docs/V3_DESIGN.md`](docs/V3_DESIGN.md).

## v4: from benchmark to usable router

v1–v3 showed routing could work on my own tasks, but the router only existed inside a benchmark script — nothing an app could call. v4 has no new headline number; it is the product surface:

- **One API.** `Router(policy).complete(prompt)` returns the answer, the model, the cost and a "why" trace. Every strategy is an interchangeable policy behind it.
- **Real failover.** A provider error retries the next model up the ladder and is recorded in the trace, instead of a silent fake answer.
- **A live learning signal.** The v3 learned router needed an answer key that production never has. `feedback(request_id, score)` replaces it; self-verification evidence is down-weighted so it can never trigger a cheaper route alone.
- **Drop-in endpoint.** OpenAI-compatible — change one URL in an existing app. Localhost-only by default, optional auth, 1 MB request cap, logging opt-in because it stores prompts.

I also found and fixed a test bug that had silently switched later tests to live paid APIs (6 tiny calls, under $0.001), and added a guard so it can't recur.

## v5: stress-testing the router

Five experiments, each attacking an assumption, reported as measured — including what didn't work.

1. **Jury of cheap models (3 vendors, escalate only on disagreement).** Easy set: 66.1% net savings at 0.99 quality, between the independent verifier (53.1%) and the self-verifier (85.0%). Hard set: quality 0.86, against 1.00 for the independent verifier and 0.70 for self-verification. On one hard task all three vendors agreed on the same wrong answer. **Cheap models share blind spots — agreement isn't correctness.**
2. **Fool the router (red-team, defensive).** Cost-inflation prompts ("use your most capable model") moved routing 7/10 → 0/10 after hardening; verifier/jury injection is 0 after hardening. Making a hard task look easy still works 2/9 — a documented residual risk. Original prompts are pinned so published results still reproduce.
3. **Live price tracking** (free OpenRouter price list, treated as untrusted). v1's 53.6% holds at today's prices (53.58%). It caught Sonnet 5 at −33% vs list and a Kimi resale price ~12× below first-party; no roster's model order would change.
4. **Budget governor + shadow sampling (offline simulation).** Shadow-checking 5% of traffic detects an injected quality drop in 83% of simulated months (median ~870 requests) for ~6% overhead (1%: 29%; 10%: 93%). Protected task types were never downgraded. With reasoning protected, a 60% budget is infeasible (reasoning is ~64% of cost) and the governor flags that rather than hiding it.
5. **Kimi K3 (Moonshot).** Priced above Sonnet ($3/$15 against $2/$10) with mandatory billed reasoning on top, so on short mid-tier tasks it can only compete on quality, never price (offline projection from real token counts; live quality run pending). The router now defaults Kimi to low reasoning effort, because Moonshot's own default is its most expensive. A live outage confirmed failover to Opus.

## v6: a classifier that teaches itself

Test the one assumption first, then build, then measure honestly. v5's shadow test on 150 real emails showed a cheap model (GPT-4o mini) agreeing with the expensive one 90% of the time, but sometimes confidently wrong, so "escalate when unsure" couldn't be trusted. The assumption worth testing: does a model with *calibrated* confidence fix that?

**Step 0, under half a cent.** TypeSafe's Jev is a decision model that returns calibrated probabilities. At 90% confidence or higher, Jev agreed with Haiku on 86 of 86 emails (57% coverage) with 0 missed action items. GPT-4o mini at the same bar was wrong on 7 of 113, with 1 missed action item. Jev's confidence separates right from wrong 92% of the time against mini's 68%. Jev agrees with Haiku less overall (80% vs 90%); the honesty of its confidence is what matters.

**The loop.** Jev decides; if unsure, the expensive model does. A teacher proposes one generic rulebook edit per round, kept only if a re-test on training emails shows more right answers and no new missed action items. Data split by date: the 100 oldest emails to learn from, the 50 newest never seen.

**Result on the 50 unseen emails.** The share Jev handled alone rose from 36% to 60% by round 6 (36, 38, 44, 60, 60 at rounds 0, 1, 3, 4, 6) with 0 missed action items. Cascade cost fell from $1.09 to $0.68 per 1,000 emails against $1.68 for Haiku-only, a 59% saving. 6 edits were kept and 2 rejected by the gate. Total spend was about $0.06; the teacher ran on a Claude subscription, not paid API.

**Confidence you can trust is worth more than accuracy on average.**

**The overfitting lesson.** Rounds 7-8 reached 66% but caused 1 missed action item the training-only gate couldn't see, so round 6 is the keeper. The fix is to choose the version by a held-out score; in production, that's the owner's own corrections. Write-up: [`docs/JEV_LOOP.md`](docs/JEV_LOOP.md).

## v7: into production

**The cheapest model is no model.** The last step was deploying these ideas into the owner's live email automation. A reality check on 150 real emails from the live classifier: the router's free length-based difficulty heuristic sent 100% to the expensive model, so 0% saved. The benchmark shortcut didn't survive real data.

**What did survive.** Rules learned only from the older 100 emails, tested on the newest 50 unseen: exact-sender rules decided 28% (14/14 correct, 0 important emails misfiled); adding Jev on the rest brought it to 62% decided with no paid model, all correct in that sample. The owner's own hand-audited sender map (~560 senders from inbox history) covered 77% of emails; its Job Alerts folder agreed 21/21 and Promos 35/38. 29 recruiter-like senders filed under promos were excluded, so a rejection or interview invite can never be auto-ignored.

**The shipped chain, cheapest first.** Sender rules (278 free decisions) → Jev with the learned round-6 rulebook (decides only when at least 90% sure) → GPT-4o mini fallback. Important categories (action needed, rejections, recruiter replies, confirmations) always go to the fallback. Each decision records which stage made it. No Anthropic API dependency remains: the old Haiku classifier stopped when its API credit ran out, and the new chain doesn't need it.

| Stage | Pre-release replay, 23 emails |
|---|---|
| Sender rules vs old classifier | 5/5 agreed |
| Jev vs old classifier | 7/7 agreed |
| Fallback vs old classifier | 10/11 agreed |
| Important emails decided by a cheap stage | 0 |
| Errors | 0 |

**Most of the savings came from not calling a model at all — and the biggest quality win was a bug you only find by measuring.**

**Release discipline, and two bugs.** Built as a switched-off copy, replayed with all Notion writing disabled, then cut over with the old version kept for one-click rollback. Measuring found the first bug: the live classifier had never been shown who sent an email (a field-shape mismatch rendered an empty sender on every prompt since July). One-line fix. The first live job alert exposed the second, a duplicate-check bug dating from July, hidden because the automation had been off; found from the run log, fixed and verified offline the same day. First verified live run: 2 emails decided by Jev, no paid model, no errors.

## Honest limitations

- **The judge is only partly confirmed.** On the 25-task run a second judge from a different vendor agreed (96.7% within 0.15, 0.010 mean difference), but both are LLMs. On the public run my hand check agreed on pass or fail for 22 of 29 answers, with a correlation of only about 0.03, and a code check found 15 answers the judge scored too generously and 4 too harshly. The second judge has not been run there.
- **The default rule depends on a final `Answer:` line.** Agreement is checked on that line, and every benchmark prompt asks for one. A prompt without one skips the rule for reasoning and extraction and follows the rules table, so everyday prompts save less than the benchmark shows.
- **The lookup has only seen benchmark tasks.** It knows four task types and rates almost all writing as medium, so hard writing goes to DeepSeek rather than Opus. No real paid request has gone through the server or web app with the new rule.
- **The public test is still a fixed mix.** It is not a real workload, the mix drives the answer, and the models have probably seen public benchmarks in training. Answers are capped at 1,024 tokens, and the mixed run printed 11 cut-offs while the database shows 14 answers at the limit. That gap isn't explained.
- **25 tasks is a starting eval.** It was enough to demonstrate the method, not to certify a savings figure, and its quality result did not carry over to the public test.
- **v5 results are small-n and partly modelled.** The jury's errors are correlated (shared blind spots); the red-team runs use n of 5–10; the budget simulation imputes some mid-tier quality (optimistic); Kimi's quality is unmeasured — only its price is projected.
- **The Jev result is small-n.** 150 emails over 3 days, only 50 unseen; Haiku is the reference, not truth; rejection and recruiter-reply emails are untested.
- **The live chain numbers are early.** They come from a 23-email pre-release test and the first live runs; the fallback path and the fixed job-saving step had not yet run live at time of writing. The old classifier is the reference, not truth.
- **Single-turn tasks only.** Real workloads include multi-turn and tool-use flows this benchmark doesn't cover.
- **The at-scale projection is directional.** It's a linear extrapolation from this run's per-task cost and assumes production traffic resembles this benchmark's task mix — real traffic will differ, sometimes substantially.

---

*Reproduce it: it works with any subset of provider keys. Add whichever of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `DEEPSEEK_API_KEY` you have to `.env` and run `python run_benchmark.py`. Any model whose provider key is missing falls back to a labeled mock adapter, so the harness always runs end-to-end. First run makes live calls (for whichever keys are present) and caches them; every run after is free and identical.*
