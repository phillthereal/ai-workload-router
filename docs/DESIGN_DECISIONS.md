# Design decisions

This doc covers the design trade-offs in the AI Workload Router: what I chose, what it cost, what I rejected, and what I would change if real users depended on it. [`DECISION_LOG.md`](DECISION_LOG.md) records what was decided and the evidence. This doc goes further on the alternatives and on what would break at scale.

**Status.** This is a personal project. It has served no customer or company traffic. It has been tested on benchmark tasks, public datasets and my own emails, from a personal setup. Every "At production scale" paragraph means "if this served real users". It is a plan, not experience.

Every benchmark number here is from July or October 2026, on small task sets. A larger public run is planned in [`BENCHMARK_PLAN.md`](BENCHMARK_PLAN.md) and is not reflected here. When it is published, run A would replace the 25-task evidence in sections 1.1, 2.1 and 3.1, and run B would give the first hard-set result on the `cross_vendor` roster.

## Runs cited

Cost cut is net of routing cost. It is compared with sending every task to Claude Opus 4.8. The ranges in brackets are 95% ranges from [`ANALYSIS.md`](ANALYSIS.md). Where no range is shown, `ANALYSIS.md` has none. Quality is the router's quality as a share of frontier-only quality.

Task sets: easy is `data/tasks.json` (25 tasks), hard is `data/tasks_hard.json` (10 tasks), held-out is `data/tasks_v3.json` (14 tasks).

| ID | Run | Roster | Method | Task set | Cost cut [95% range] | Quality vs frontier-only | Report |
|---|---|---|---|---|---|---|---|
| R1 | `20260717T082024-098a7982` | cross_vendor | Hand labels | Easy | 53.6% [29.9, 82.6] | 100.4% [99.0, 102.0] | [report](../data/benchmark_report.md) |
| R2 | `20260717T164636-ee1ddc99` | cross_vendor | Real classifier | Easy | 65.6% [40.3, 97.7] | 99.8% [97.8, 101.6] | [report](../data/benchmark_report_cross_vendor_classified.md) |
| R3 | `20260717T164758-d2d01eaa` | claude_tiers | Classifier | Easy | 42.5% [23.4, 66.8] | 100.3% [98.9, 101.9] | [report](../data/benchmark_report_claude_tiers_classified.md) |
| R4 | `20260717T171702-e98b914b` | claude_tiers | Cascade, Sonnet verifier | Easy | 53.1% [25.1, 72.1] | 99.6% [98.0, 101.4] | [report](../data/benchmark_report_claude_tiers_cascade.md) |
| R5 | `20260717T172816-428aceeb` | claude_tiers | Classifier | Hard | -6.3% [-21.6, 6.7] | 100.0% [100.0, 100.0] | [report](../data/benchmark_report_claude_tiers_classified_hard.md) |
| R6 | `20260717T172831-e7fe3e42` | claude_tiers | Cascade, Sonnet verifier | Hard | -19.9% [-63.4, 7.1] | 100.0% [100.0, 100.0] | [report](../data/benchmark_report_claude_tiers_cascade_hard.md) |
| R7 | `20260717T204417-eb79487b` | claude_tiers | Cascade, Haiku verifier | Easy | 85.0% | 99.6% | [report](../data/benchmark_report_claude_tiers_cascade_verifier-claude-haiku-4-5.md) |
| R8 | `20260717T204417-7af1dd0c` | claude_tiers | Cascade, Haiku verifier | Hard | 73.8% | 70.0% | [report](../data/benchmark_report_claude_tiers_cascade_verifier-claude-haiku-4-5_hard.md) |
| R9 | `20261020T080621-38d36e09` | claude_tiers | Jury, unanimous | Easy | 66.1% | 99.8% | [report](../data/benchmark_report_claude_tiers_jury_unanimous.md) |
| R10 | `20261020T080527-be9155da` | claude_tiers | Jury, unanimous | Hard | 22.4% | 86.0% | [report](../data/benchmark_report_claude_tiers_jury_unanimous_hard.md) |
| R11 | `20260719T113230-91e5c641` | claude_tiers | Static router on labels | Held-out | 4.4% | 100.0% | [report](../data/benchmark_report_claude_tiers_v3.md) |
| R12 | `20260719T113315-b7ff3a62` | claude_tiers | Learned router | Held-out | 26.1% | 100.0% | [report](../data/benchmark_report_claude_tiers_learned_v3.md) |

Notes on single runs:

- R2: router total $0.043299 against frontier-only $0.125980. Classifier spend was $0.000822, which is 1.0% of savings. The classifier agreed with my hand labels on 68.0% of tasks. That is a sanity check against one person's labels, not an accuracy score.
- R4: cascade overhead was 31.1% of savings. It escalated 2 of 25 tasks.
- R6: overhead was 163.7% of savings. It escalated 5 of 10.
- R7: overhead 6.2%, escalated 0 of 25.
- R8: overhead 14.7%, escalated 1 of 10. Haiku rated three of its own wrong answers 0.95 to 1.0 ([`V2_FINDINGS.md`](V2_FINDINGS.md)).
- R9: jurors were gpt-4o-mini, deepseek-chat and claude-haiku-4-5, with Opus on disagreement.
- R10: all three vendors agreed on one wrong answer (DECISION_LOG #14).
- R11 and R12: compared directly, the learned router was 22.7% cheaper than the static one [7.1, 61.7]. These are different comparisons. 26.1% and 4.4% are against frontier-only. 22.7% is learned against static. It only works where task shapes repeat ([`V3_FINDINGS.md`](V3_FINDINGS.md)).

PR numbers below are GitHub numbers (#2 to #9).

---

## 1. Routing logic

### 1.1 Predict labels, then use a fixed table

**Choice.** A budget-tier model predicts (task_type, difficulty) from the prompt, capped at 32 output tokens. A rules table (`DEFAULT_ROUTING_RULES` in `src/router/router.py`) maps that pair to budget, mid or frontier. A free capability gate then moves the task up a tier if the prompt would not fit the chosen model's context window.

**Trade-off.** Routing becomes a guess. It can be wrong and it costs money, though only 1.0% of savings [R2]. It saved more than hand labels did (65.6% against 53.6%), but not for free. Short writing quality was 0.975 frontier-only, 0.983 with hand labels [R1] and 0.958 with the classifier [R2]. The mean hides that dip.

**Rejected.**
- Hand labels as a product. A real system only gets a prompt.
- The free keyword heuristic as the main classifier. On 150 of my own emails it sent 100% to the top tier, because every prompt was at least 3,700 characters (`REAL_TRAFFIC_ASSESSMENT.md`).
- Not tried: a small trained classifier. There is no large labelled traffic to train on.

**Red-team note.** Words in a prompt could steer routing. Cost-inflation attacks fell from 7/10 to 0/10 on the keyword classifier and from 3/10 to 0/10 on the LLM classifier. Making a hard task look easy still works 2 of 9 times ([`V5_REDTEAM.md`](V5_REDTEAM.md), LLM classifier on claude-haiku-4-5).

**A gap I should state plainly.** `run_benchmark.py` pins prompt version v2 (`_PV = "v2"`) so published runs replay. `Router` defaults to the hardened v3. So the benchmark numbers measure the older classifier prompt, and the planned public run will too, unless that is changed.

**At production scale.** Train on logged outcomes. Measure accuracy on a held-out sample. Watch the tier mix over time. Run a sample of traffic on the frontier as a check. The only evidence for that last idea is a simulation: checking 5% caught a quality drop in 83.3% of runs, for 6.1% extra cost ([`V5_BUDGET_SHADOW.md`](V5_BUDGET_SHADOW.md)).

### 1.2 Guess first by default; cascade, jury and learned are opt-in

**Choice.** The web app uses `RulesPolicy(use_labels=False)`: classify, then look up the table. A cascade tries the cheap model, a verifier scores the answer, and it escalates below 0.7. The jury and the learned router are options.

**Trade-off.** On the same roster and tasks the cascade saved more than the classifier [R4 vs R3], but it paid 31.1% of its savings in overhead and lost more on hard tasks [R6 vs R5].

**Rejected as defaults.**
- Haiku as verifier. Best easy number (85.0% [R7]), but quality was 70.0% on hard [R8]. It cannot catch its own blind spots.
- The jury. 66.1% on easy [R9], but 86.0% quality on hard [R10]. Agreement is not correctness.

The rule I used: a default must hold on work nobody has measured yet. The learned router stays opt-in for a similar reason: it may only move a task cheaper with at least 5 similar past successes at the quality bar, so it needs logged history first [R11, R12].

**At production scale.** Choose the policy per workload, from the measured share of easy work. Use the cascade where task difficulty cannot be predicted.

### 1.3 Failures climb, never drop

**Choice.** An unreadable classifier reply becomes reasoning/hard, so it goes to the frontier. An unreadable verifier score counts as 0.0, so it escalates. A provider error fails over up the ladder and is recorded in the trace. Failing costs money rather than quality.

**Trade-off.** Outages and bad replies raise cost. The worst case is paying twice (section 7, smell 3). One exception: if the classifier call itself fails, it silently uses the keyword heuristic (smell 1).

**Rejected.** Turning a provider failure into a simulated answer for real callers. `Router(allow_mock_fallback=False)` is the default (see the docstring in `src/router/api.py`). The benchmark still allows the mock fallback, unless a spend cap is set.

**At production scale.** Backoff, a circuit breaker per provider, and alerts on the failover rate.

---

## 2. Quality threshold and scoring

### 2.1 The pass bar was set first

**Choice.** At least 40% cheaper while keeping at least 95% of frontier-only quality (`PRD.md`, kept unchanged for `BENCHMARK_PLAN.md`).

**Trade-off.** A mean over all tasks can hide a drop in one task type, as the short writing case shows (section 1.1). The easy set scores about 0.99 on every tier, so it cannot show that routing protects quality. The hard set can: Haiku 0.70, Sonnet 0.90, Opus 1.00 (DECISION_LOG #8).

**Rejected.** Moving the bar after seeing results.

**Not built.** A per-task-type floor: the PRD raised it as an open question. `route_task` does have a `quality_floor` argument (0.85 forces mid, 0.95 forces frontier) and it is tested, but no policy or entry point passes it, so a caller of `Router` cannot use it today.

**At production scale.** Per-task-type and per-customer floors, agreed with the business and checked on live samples.

### 2.2 The cascade threshold stays at 0.7

**Choice.** Leave `escalate_threshold` at 0.7. DECISION_LOG #10 says a sweep over 0.5 to 0.9, replayed from cache, showed overhead flat to rising. 91 to 94% of the overhead was the verifier being paid on every task.

**Trade-off.** This rests on the decision log text and `tune_cascade.py`. No sweep report is committed, and re-running it needs the local `.cache/`. You cannot check it from the repo alone.

**Rejected.** Tuning that knob. The real lever was the verifier model.

**At production scale.** Re-sweep per workload on logged scores.

### 2.3 Where a quality score comes from

| Source | What it is | Where used | How far to trust it |
|---|---|---|---|
| `exact_match` | Reference found anywhere in the reply | July reports: 10 of 25 easy tasks, 8 of 10 hard | Loose. See 2.4 |
| `final_answer` | Only the last `Answer:` line | Planned public run: 228 of 300 mixed tasks, 38 of 40 hard | Strict, see 2.4 |
| `rubric_judge` | Claude Opus 4.8 scores against a rubric | July: 15 of 25 easy, 2 of 10 hard. Public plan: 72 of 300, 2 of 40 | Cross-checked once, see below |
| `verifier` | The mid model scores with no answer key | Paid web mode (`quality_source: verifier`) | An opinion. It grades itself when the mid model wrote the answer |
| `simulated` | Placeholder table in `router.scoring` | Demo mode | Not a measurement |
| `unavailable` | The verifier call failed | Paid web mode | Score is null, answer is kept |

The learned router weights evidence like this: judge 1.0, user 1.0, verifier 0.3 (`src/router/learned.py`).

On the judge check: a second judge, GPT-4o mini, re-scored the 30 rubric-judged answers from R1. 96.7% were within 0.15, mean difference 0.010. That figure exists only as text in `CASE_STUDY.md`. No raw output is committed. No human has scored them, though `BENCHMARK_PLAN.md` plans 30 hand-scored answers. Opus is also the frontier baseline, which is why the check matters.

**Choice.** Use the best score available per task, and label the source.

**Trade-off.** The benchmark's score needs an answer key. A live request has none. So the API labels the source rather than pretending (#3).

**Rejected.** Reusing the mock verifier in demo mode. Its text parses as 0.0, which would show a false "0.00" (#3).

**At production scale.** A human-scored sample per workload, user feedback as a signal, and judges from a different vendor than the frontier model.

### 2.4 The `final_answer` scorer

**Choice.** `exact_match` accepted a right number anywhere in the working, so a wrong conclusion could pass. `final_answer` reads only the last `Answer:` line (#8).

**Trade-off.** It is strict about format. `Answer - 18` and `\boxed{18}` score 0 on purpose, for every model. A cut-off answer loses its line and scores 0, since answers are capped at 1,024 tokens.

**Rejected.** Changing `exact_match` itself, because published reports must not change. So the July reports still use the looser scorer, and they have not been re-scored.

**At production scale.** Structured output, so a format slip is not read as a wrong answer.

---

## 3. Model tiers

### 3.1 Three tiers, with the roster as config

**Choice.** The default `cross_vendor` roster is GPT-4o mini, DeepSeek Chat and Claude Opus 4.8. The output price gap is 41.7x (from `Roster.price_range()`). The `claude_tiers` roster is Haiku 4.5, Sonnet 5 and Opus 4.8, a gap of 5.0x. Same tasks, same method: 65.6% [R2] against 42.5% [R3]. The classifying model is not the same, because each roster uses its own budget model, so the price gap is not the only difference. Savings depend on the price gap and on how much work is easy.

**Trade-off.** Three vendors means three keys, three failure modes and different behaviour. No committed report runs the hard set on `cross_vendor`, so the hard rows (R5, R6, R8, R10) do not compare directly with the headline runs.

**Rejected.** Not tried: more than three tiers. There is no measurement that says a fourth would help.

**At production scale.** More than three tiers only if measured. Per-tenant rosters. Prices fetched rather than hand-kept.

### 3.2 Reasoning always goes to the frontier

**Choice.** Any task classified as reasoning goes to the top tier.

**Trade-off.** Reasoning tasks do not get cheaper. They are 16% of tasks and 33% of the baseline bill in the easy set (README), which caps the saving.

**Rejected.** Not tried: a cheaper tier for easy reasoning as a rule. I ruled it out by judgement, not by a test: a wrong logic step costs more than the saving.

**At production scale.** Test this per workload. The learned router showed that some hard shapes can move down when there is evidence [R12].

### 3.3 List prices, not promotions

**Choice.** Sonnet 5 is costed at list price, not its intro rate, so a saving does not silently go stale (`src/router/config.py`, DECISION_LOG #5). Re-priced at OpenRouter's list prices, R1 came out at 53.58% ([`V5_PRICES.md`](V5_PRICES.md)). The router also defaults Kimi to low effort, because Moonshot's default is max.

**Trade-off.** List prices are kept by hand in `config.py` and can go stale.

**Rejected.** Kimi K3 as a cheaper mid tier. It has Sonnet's list price plus billed reasoning. That comes from an offline projection with no live quality run ([`V5_KIMI_OFFLINE.md`](V5_KIMI_OFFLINE.md)).

**At production scale.** Pull prices automatically and re-cost past runs.

---

## 4. MCP server

### 4.1 Advice by default, stdio only

**Choice.** Five tools answer "what would you do and what would it cost" at $0, using the free heuristic ([`MCP_SERVER.md`](MCP_SERVER.md)).

**Trade-off.** Free and safe to install, but the heuristic is rough, and each result says so. None of the savings measured elsewhere apply here.

**Rejected.** An HTTP or SSE transport: there is no login, and with stdio the client launches the process itself. The LLM classifier in the free tools: it would stop being free.

**At production scale.** A network transport with authentication, a per-client identity, and the real classifier behind it.

### 4.2 Paid calls are the server owner's choice, never the client's

**Choice.** `route_request(dry_run=False)` is refused unless the server's own environment has `AI_ROUTER_MCP_ALLOW_PAID=1`. A client can be talked into anything by text it reads. When on, the defaults are $0.50 a day, 1,000 output tokens per call, 10 calls a minute and frontier off. It checks today's spend plus the worst case for this call before calling. Answers come back in `untrusted_model_output`. Nothing is stored.

**Trade-off.** The limits are per process and reset on restart.

**Rejected.** A client-side flag.

**At production scale.** A shared spend store and per-client budgets.

---

## 5. RAG design

The corpus is three UK government documents: 491 chunks (#5). Chunks are about 800 characters with 150 overlap. Embeddings use `BAAI/bge-small-en-v1.5`. The top 4 passages are returned. Hybrid search uses reciprocal rank fusion with k=60.

Results from [`examples/rag/README.md`](../examples/rag/README.md). The first set has 13 questions, the second has 66. "Right page first" means the correct page ranked 1st. "Top 4" means it was among the 4 passages returned.

| Method | Set 1: right page first | Set 1: top 4 | Set 2: right page first | Set 2: top 4 |
|---|---|---|---|---|
| Embeddings | 6/13 | 11/13 | 20/66 | 34/66 |
| Keywords | 9/13 | 12/13 | 19/66 | 36/66 |
| Hybrid | 10/13 | 12/13 | 27/66 | 44/66 |

I wrote both sets. The first is too kind and the second may be too hard. The 95% range on the second set is about 30 to 53% for first place and 55 to 77% for the top 4.

### 5.1 Hybrid search, added because of a measurement

**Choice.** Embeddings alone ranked the page defining a Should Cost Model 31st on the eval question (#5). Hybrid is best on both sets.

**Trade-off.** The fix is partial. That question still ranks 5th, outside the top 4. Two rankings also means more moving parts. On the second set, a third of questions still miss the top 4. I stopped tuning there because it would fit the test. Answer quality has not been measured.

**Rejected.** Embeddings alone. Tuning on 13 questions.

**At production scale.** A reranker, a "not found" threshold, and an eval with answers written in advance.

### 5.2 Route on the question, not the padded prompt

**Choice.** Classify the question alone. In the free classifier, length decides difficulty: under 120 characters is easy, under 220 is medium, anything longer is hard. So the padded prompt (question plus passages) would be called hard and go to the most expensive model. The same effect showed up on real emails (section 1.1). A test covers it (`test_routing_uses_the_question_not_the_padded_prompt` in `tests/test_rag.py`), and #5 says it fails when the behaviour is reverted.

To be exact: `rag.py` uses the free `heuristic_classify` on the question and passes those labels to `RulesPolicy`, even with `--paid`. So RAG routing does not use the LLM classifier.

**Trade-off.** A simple question over hard passages can go to a cheap model.

**Rejected.** Classifying the whole prompt.

**At production scale.** Use the LLM classifier on the question, plus a signal from how strong the retrieved passages are.

### 5.3 No vector database, local embeddings

**Choice.** 491 chunks fit in one JSON file with an exact scan in plain Python. A code comment marks the ceiling at about 1M chunks. Local embeddings are free and document text never leaves the machine.

**Trade-off.** Simple, but it rebuilds and scans everything. The embedding model is small.

**Rejected.** Not tried: a hosted vector store or hosted embeddings. They would add cost and send document text off the machine, and 491 chunks do not need them.

**At production scale.** A vector index, incremental updates, and access control per document.

### 5.4 Documents are public and treated as untrusted

**Choice.** PDFs are downloaded and refused unless the SHA-256 matches. They are not committed. PDF text is fenced as untrusted with `router.untrusted.fence`.

**Trade-off.** Fencing reduces prompt injection but does not stop it. No live test has been run, because the `--paid` path has never been run.

**Rejected.** BREEAM documents (BRE keeps copyright and restricts other uses) and an Environment Agency summary (all rights reserved).

**At production scale.** Scan documents on ingest, and log what was retrieved for each answer.

---

## 6. Running it safely

### 6.1 Demo mode is the default, even with real keys

**Choice.** Answers, cost and quality are simulated and the page says so (#3). Real calls need `AWR_WEB_ALLOW_PAID=1` in the environment, not read from `.env`. Then the limits are $0.50 a day and 30 a minute. There is no login, so it is localhost only and other `Host` headers are refused.

**Trade-off.** A first-time visitor sees no real answer. The routing decision is real, but it uses the free keyword classifier, not the LLM classifier measured above. Paid mode has only been tested with stubs.

**Rejected.** Going paid whenever keys exist.

**At production scale.** Login, per-client limits, and a shared spend store.

### 6.2 Editable Docker install, as a workaround

**Choice.** The router works out `.cache/` and `data/` from its own source path (section 7, smell 6). A normal install would put them under site-packages. So the image uses an editable install (#4). This works around the smell. It does not fix it. It is also one container, not two, because the API already serves the UI. It is published on 127.0.0.1 only. The image is 279 MB.

**Trade-off.** The image depends on the source layout staying as it is.

**Rejected.** A two-service setup. Fixing the paths in that PR, because it was out of scope.

**At production scale.** Paths from config, a normal install, a pinned base image digest, and secrets from a secret store.

### 6.3 Spend limits differ by entry point

**Choice.** The web app, the OpenAI-compatible server and the MCP server share `SpendGuard`, and the MCP server adds a worst-case check. The benchmark has its own meter, because it needs to stop a run rather than refuse one request.

| Path | When it checks | Limits | Gap |
|---|---|---|---|
| Web `/route` | Before the call, against today's spend | $0.50 a day, 30 a minute | Requests in flight can overshoot |
| OpenAI-compatible server | Same check | $1.00 a day default, 30 a minute, 8 concurrent | Same overshoot (smell 4) |
| MCP | Today's spend plus the worst case for this call | $0.50 a day, 10 a minute | Per process |
| Benchmark `--max-spend` (#8) | Before each task | Stops when less than $0.10 of the cap is left | Per process |

The benchmark cap counts real calls only, so cache replays are free. It writes no report for a stopped run. It also stops on a classifier fallback, a failed or unreadable judge reply, a simulated answer or a provider error. It sends one tiny test call per model first. Because it is per process, a restart must be given only what is left. The plan is an $8 hard cap across both runs, with an estimate of about $4.30 (`BENCHMARK_PLAN.md`).

**Trade-off.** Only the MCP check is safe against overshoot, because it reserves the worst case. For the benchmark, a stopped run produces nothing, rather than a patched-up result.

**Rejected.** Quietly carrying on.

**At production scale.** One shared store, with a reservation made before each call.

---

## 7. Known design problems

These were found in #2. I listed them rather than fixed them, so that PR stayed free of behaviour changes. This table keeps the original numbers and shows the ones that matter most. The full list of 14 is in #2.

| # | Smell | Why it matters | Status on this branch |
|---|---|---|---|
| 1 | Silent classifier downgrade | With no key or a failed call, the classifier quietly uses the keyword heuristic, and `Result.simulated` stays False | `Result.classifier_fell_back` records it, and a capped benchmark stops on it (#8). Not fixed for other callers |
| 2 | Failed calls are not costed | A refusal has real token counts but `Ctx.call` drops them, so reported spend can be lower than the bill | Not fixed |
| 3 | Cascade can pay for the frontier twice | If the budget call fails over to a higher tier, that answer is still treated as a non-final attempt. If the verifier rejects it, the ladder calls the frontier again | Not fixed |
| 4 | Daily budget can overshoot under concurrent requests | Several requests pass the check before any spend is recorded | Not fixed on the HTTP server or the web app. The MCP server checks a worst case first |
| 5 | `assert` used for request validation in `server.py` | Asserts vanish under `python -O` | Not fixed |
| 6 | Paths depend on the source tree | A normal install would put `data/` and `.cache/` under site-packages | Worked around in Docker (6.2) |
| 7 | The server builds a new `Router` per request | Stateful policies such as `BudgetPolicy` cannot work behind it | Not fixed |
| 8 | Disk cache on by default for the HTTP server | Answers are written to `.cache/` even without `--log-db`, and an identical prompt replays while still counting as spend | Not fixed. The web app and MCP paid paths turn it off for answers, though in paid web mode the classifier still goes through it (#3) |
| 10 | Two sources of truth for tiers | `MODEL_TIER` and `MODELS` can disagree, and an unknown model silently becomes `budget` | Not fixed |

Smells 1 to 5 can give a wrong result or a wrong cost, so they matter most. `README.md` "What I'd do next" lists the fixes I would make first.
