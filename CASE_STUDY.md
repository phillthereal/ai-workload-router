# AI Workload Router — Product Case Study

**Author:** Fil Ivanov · **Role:** Product (end-to-end: problem framing → spec → benchmark → result)

> A cost-optimizing routing layer for LLM applications. This case study shows how I took a sprawling idea, isolated the one assumption worth testing, cut scope to test it fast, and proved the result with a live benchmark. The code is the evidence; the product thinking is the exhibit.

---

## TL;DR

Teams building on LLMs overpay by sending every request to one premium model "to be safe." The **AI Workload Router** classifies each task and routes it to the cheapest model that can still do it well, logging cost and quality on every call so routing improves over time.

**On a 25-task benchmark, routing across a three-vendor roster cut cost 53.6% with no measurable quality loss (100.4% of the frontier-only baseline) and ran 46.2% faster.** The hypothesis (≥40% cost reduction, ≥95% quality retention) **passed.** Every number is from live API calls, graded by an LLM judge that's itself been cross-checked by a second, independent judge, and the whole run is reproducible from cache.

The most useful finding wasn't the headline number — it was *why* the savings stopped where they did. That's the part a good PM cares about, and it's below.

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

## The result

| Strategy | Total cost | Mean quality | Mean latency |
|---|---|---|---|
| Frontier-only (baseline) | $0.1260 | 0.992 | 4,155 ms |
| **Router** | **$0.0585** | **0.996** | **2,237 ms** |

- **Cost reduction: 53.6%**
- **Quality retention: 100.4%** of baseline (the router edged it, within noise)
- **Latency: 46.2% faster** (median 1,208 ms vs. 3,663 ms) — cheaper models also happen to respond faster, so this is a second win at zero extra cost, not a tradeoff.
- **Hypothesis (≥40% cost reduction, ≥95% quality retention): PASSED.**
- The router sent 11 tasks to the budget tier (GPT-4o mini), 7 to the mid tier (DeepSeek Chat), 7 to the frontier (Opus) — a sensible spread by difficulty, not a blunt "cheapest everywhere."

## What this looks like at scale

Extrapolating linearly from this run's per-task cost (router $0.00234 vs. baseline $0.00504):

| Volume | Baseline / month | Router / month | Saved / month |
|---|---|---|---|
| 100,000 requests | $503.92 | $234.03 | $269.89 |
| 1,000,000 requests | $5,039.20 | $2,340.26 | $2,698.94 |

This is directional, not a forecast — it assumes production traffic resembles this benchmark's task mix, and real traffic will differ. It's meant to make the shape of the savings tangible, not to be quoted as a committed number.

## Validating the judge

An LLM judge grading its own routing decisions is a circularity worth checking. A second, independent judge (GPT-4o mini — a different vendor from the primary judge, Claude Opus 4.8) re-scored all 30 rubric-judged answers from this run blind. Agreement: **mean absolute difference of 0.010, with 96.7% of scores within 0.15 of each other.** That's a meaningfully lower bar than ground truth, but it's evidence the primary judge isn't an outlier — two different judges, from two different vendors, converge on similar scores. The remaining step is a human-labeled subset (a label sheet is already exported for this run) to check both judges against an actual human, not just against each other.

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

**Routing stops paying on hard work — the most useful thing I learned.** The published 25-task set saturates quality near 0.99 on every tier, so it can't answer "does routing hold quality when tasks are hard?" I built a 10-task hard set (mostly objective exact-match, no judge leniency) that separates the tiers cleanly: Haiku 0.70, Sonnet 0.90, Opus 1.00. On it, both strategies held 100% quality but went cost-*negative* — every hard task needs the frontier, so routing only adds overhead. That is the sharpest form of the composition insight: **the router's value is a direct function of how much easy work exists, and on an all-hard workload it is worse than doing nothing — but it never trades away quality to find that out.** The buyer's decision rule falls out of this: measure your easy-task fraction before adopting a router.

Everything is additive and reproducible — the default configuration still reproduces the original 53.6% run exactly, and each experiment is a one-line command in the README.

## What I'd do next

1. **Adaptive routing** — feed the performance log back into the router so model choice is learned per task type, not hand-configured. (The log was designed for this from day one; the v2 classifier is a first step — prediction from the prompt rather than a static rule.)
2. **Confidence-based escalation** — ✅ *built in v2 (the cascade): try the cheap model first, a reference-free verifier checks the answer, escalate only on a failed check.* Next: tune the escalate threshold per task type from the performance log, rather than a single global value.
3. **More vendors** — cross-provider routing is now proven across three (Anthropic, OpenAI, DeepSeek); a fourth, cheaper or faster provider (e.g., a Gemini adapter) is the next candidate to test whether the ceiling keeps climbing or starts to plateau.
4. **Judge-vs-human validation** — the cross-judge check passed (96.7% agreement); the next step is scoring the exported human label sheet against both judges, and growing the eval beyond 25 tasks, before quoting absolute quality numbers as precise. The v2 hard set (objective exact-match) is a start at reducing judge dependence.

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

## Honest limitations

- **The judge is validated, not proven.** LLM-as-judge scoring is now cross-checked by a second, independent judge from a different vendor (96.7% agreement, 0.010 mean difference) — but both are still LLMs and could share blind spots a human grader wouldn't. A human label sheet has been exported for this run; scoring it is the remaining step before quality numbers are trusted as ground truth.
- **25 tasks is a starting eval,** not a production-scale one. It's enough to demonstrate the method and the composition insight; it's not enough to certify a production savings figure.
- **Single-turn tasks only.** Real workloads include multi-turn and tool-use flows this benchmark doesn't cover.
- **The at-scale projection is directional.** It's a linear extrapolation from this run's per-task cost and assumes production traffic resembles this benchmark's task mix — real traffic will differ, sometimes substantially.

---

*Reproduce it: it works with any subset of provider keys. Add whichever of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `DEEPSEEK_API_KEY` you have to `.env` and run `python run_benchmark.py`. Any model whose provider key is missing falls back to a labeled mock adapter, so the harness always runs end-to-end. First run makes live calls (for whichever keys are present) and caches them; every run after is free and identical.*
