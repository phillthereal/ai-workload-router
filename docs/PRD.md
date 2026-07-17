# PRD — AI Workload Router

**Author:** Fil Ivanov · **Status:** Draft v1 · **Last updated:** 2026-07-14

> **Portfolio note.** This document is written as a real PRD, but its purpose is to demonstrate product-management judgment: framing a fuzzy idea, isolating the riskiest assumption, cutting scope to test it, and defining success in measurable terms. The build that accompanies it (a "Prove-it MVP") exists to generate the evidence this document cites — not to be a production system.

---

## TL;DR

Teams building on LLMs default to sending *every* task to one expensive frontier model, because comparing models per-task is tedious and there's no easy way to know which model is "good enough" for a given job. The **AI Workload Router** is a thin layer that classifies each incoming task and routes it to the cheapest model capable of handling it — logging cost, latency, and quality for every run so the routing improves over time.

**The one hypothesis this project exists to test:** *For a representative mix of tasks, routing to cheaper models where appropriate cuts cost by ~40% while holding output quality within an acceptable band of the frontier-only baseline.*

If that's true, the router is worth building. If it's not, no amount of orchestration UI matters. So v1 proves it on a benchmark before anything else gets built.

---

## Problem Statement

Teams shipping LLM features overpay because they route all traffic to a single premium model "to be safe." A large share of real tasks — classification, extraction, short rewrites, simple Q&A — are handled just as well by models that cost 5–20× less, but teams have no cheap, trustworthy way to know *which* tasks those are. The cost of not solving this is a directly inflated API bill (often the largest variable cost of an AI product) and no visibility into the price/quality tradeoff they're implicitly making on every call.

The people who feel this: **the engineer or founder who owns the API bill** and watches it scale linearly with usage, with no lever to pull other than "use a cheaper model everywhere and hope quality holds."

---

## Goals

1. **Prove the core savings hypothesis.** On a fixed benchmark of representative tasks, demonstrate ≥40% cost reduction versus a frontier-only baseline, with quality held at ≥95% of the baseline's quality score. *(This is the make-or-break goal.)*
2. **Make the price/quality tradeoff visible.** For any task, show what each candidate model would cost, how fast it responds, and how its output scores — turning an invisible decision into an explicit, data-backed one.
3. **Build a routing decision that improves with data.** Every run is logged (task type, model, tokens, cost, latency, quality). The routing logic reads from that log, so more usage produces better routing rather than a static config.
4. **Demonstrate strategy-to-shipped execution** *(portfolio goal).* Take the idea from problem statement → scoped MVP → benchmarked result, and tell that story clearly enough that a hiring manager sees the PM reasoning, not just the code.

---

## Non-Goals

1. **Not a multi-agent orchestration platform (yet).** The original brief imagined an orchestrator that decomposes tasks and coordinates four specialist agents. That's deferred — it multiplies cost and complexity *before* the core savings claim is proven. Decomposition is a Phase 2 question.
2. **Not a production, multi-tenant service.** No auth, billing, SLAs, or horizontal scaling in v1. The deliverable is a benchmark harness plus a thin demo, not a hosted product.
3. **Not a full frontend in v1.** A polished Next.js UI is only worth building once the router earns it. v1 is backend + a results view; a richer UI is Phase 2.
4. **Not "support every provider."** v1 uses a small, deliberate model set (2–3 models spanning the price/capability range). Adding more providers is trivial later and irrelevant to proving the hypothesis now.
5. **Not automated quality scoring we blindly trust.** v1 will *use* LLM-as-judge scoring, but treats it as a known-imperfect instrument, validated against a small human-labeled set — not as ground truth. Building a rigorous eval system is its own initiative.

---

## Users & User Stories

**Primary persona — "Dana," the engineer/founder who owns the AI bill.** Technical, cost-conscious, ships fast, doesn't have time to manually A/B every model.

- As an engineer, I want to submit a task and have it routed to the cheapest capable model, so that I cut cost without hand-picking a model per call.
- As an engineer, I want to see, for a batch of tasks, the total cost and quality under "router" vs "frontier-only," so that I can decide whether the router is worth adopting.
- As an engineer, I want every run logged with cost/latency/quality, so that I can audit *why* a task went to a given model and trust the routing.
- As an engineer, I want to set a quality floor (e.g. "never drop below X"), so that cost savings never come at unacceptable quality on critical tasks.

**Secondary persona — "Sam," the reviewer of this portfolio (hiring manager).**

- As a hiring manager, I want to see the reasoning from problem → scope → metrics → result, so that I can judge product judgment, not just engineering.

---

## Requirements

### Must-Have (P0) — the Prove-it MVP

| # | Requirement | Acceptance criteria |
|---|-------------|---------------------|
| P0-1 | **Provider adapter layer** — one internal interface that normalizes calls to 2–3 models across ≥2 providers. | Given a task in the internal format, when routed to any supported model, then the call succeeds and returns normalized output + token counts + latency, regardless of provider API differences. |
| P0-2 | **Benchmark task set** — ~25–30 labeled tasks spanning task types (classification, extraction, short-form generation, reasoning) and difficulty. | Task set is version-controlled, each task has an input and a reference/rubric for scoring. |
| P0-3 | **Rules-based router** — classifies each task and picks a model from a documented default mapping. | Given a task, when routed, then the chosen model + the reason is recorded; a quality-floor override forces a stronger model when configured. |
| P0-4 | **Quality scoring harness** — scores each output (LLM-as-judge + rubric), validated against a small human-labeled subset. | Judge scores correlate with human labels on the validation subset above an agreed threshold; disagreements are logged. |
| P0-5 | **Performance log (DB)** — every run persists task type, model, tokens in/out, cost, latency, quality score, success/fail. | Schema is defined and populated; a query returns total cost & mean quality grouped by strategy (router vs frontier-only). |
| P0-6 | **Benchmark report** — runs the full task set under router vs frontier-only and outputs the cost/quality comparison. | Report shows % cost delta and quality delta vs the defined baseline; this is the headline number for the case study. |

### Nice-to-Have (P1) — fast follows if the hypothesis holds

- Simple results UI (table + charts) instead of a CLI/report file.
- Confidence-based escalation: cheap model first, auto-retry on a stronger model if the judge score is low.
- Prompt-caching and semantic caching to cut repeat-task cost further.
- Fallback handling when a provider errors or times out.

### Future Considerations (P2) — design so we don't block these

- Task **decomposition** and the multi-agent orchestration from the original brief.
- **Adaptive routing** that updates the model mapping automatically from the performance log.
- Multi-tenant, hosted service with auth and per-user cost tracking.
- Broader provider set and a model-capability registry.

---

## Success Metrics

**Baseline (defined explicitly, because "reduce cost 40%" is meaningless without it):** every benchmark task sent to a single frontier model (e.g. the most capable Claude model). All deltas are measured against this.

**Leading indicators (available the moment the benchmark runs):**
- **Cost reduction vs baseline** — target **≥40%**, stretch **≥55%**. *Measured: total API cost of router run ÷ baseline run, over the full task set.*
- **Quality retention vs baseline** — target **≥95%** of baseline mean quality score, floor: no P0-critical task type drops below an agreed absolute score.
- **Routing accuracy** — % of tasks where the router's model choice matched or beat the cost-optimal-at-acceptable-quality choice in hindsight. Target **≥80%**.

**Lagging indicators (portfolio-relevant, over weeks):**
- The case study is used in ≥N job applications and referenced in interviews (qualitative signal that the artifact does its job).

---

## Open Questions

- **[Data]** What's the acceptable "quality band"? Is 95% of baseline the right floor, or does it differ by task type (extraction can tolerate more slippage than reasoning)? — *needs a judgment call before P0-6 is meaningful.*
- **[Eng]** Which 2–3 models for v1? Proposed: one frontier (Claude), one mid-tier, one budget (e.g. DeepSeek/Qwen or a small OpenAI/Gemini model). Final pick depends on which API keys are actually available. — *blocking for P0-1.*
- **[Data]** How big must the human-labeled validation set be to trust the LLM judge? Start at ~8–10 tasks and expand if judge/human disagreement is high. — *non-blocking; resolve during P0-4.*
- **[Product]** Does the demo need a live "submit a task, watch it route" moment, or is the batch benchmark report enough for the portfolio story? — *affects whether the P1 UI becomes P0.*

---

## Timeline & Phasing

- **Phase 1 — Prove-it MVP (this PRD's P0):** adapter layer, benchmark set, rules router, scoring harness, performance log, benchmark report. Ends with the headline cost/quality number.
- **Phase 2 — Make it legible:** results UI, escalation/fallback, caching. Turns the proof into a demo-able product.
- **Phase 3 — Make it smart:** adaptive routing from the performance log; revisit task decomposition and multi-agent orchestration *only if* the data justifies the added cost.

**Hard constraint:** this is a portfolio piece on a job-search timeline, so Phase 1 is scoped to be finishable fast and to produce one clear, defensible number. Everything that doesn't serve that number is deferred by design — which is itself the point the case study makes.
