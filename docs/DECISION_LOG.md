# Decision log

The product calls behind the AI Workload Router — what was decided, what it was traded
against, and the evidence. Numbers link back to the findings docs; every one is from a
live run or a disclosed simulation.

The through-line: **one invariant — routing must never trade quality away by default —
decided more of these than any metric did.**

---

### 1. Test one hypothesis; defer everything else
**Context.** The original brief was a multi-agent orchestrator across four models with a
web app and three build phases.
**Decision.** Cut it to one falsifiable claim: *routing to the cheapest capable model cuts
cost ≥40% at ≥95% of frontier quality.* Orchestration, UI and adaptive routing deferred.
**Trade-off.** A less impressive-looking v1, in exchange for knowing whether the core idea
pays before building machinery around it.
**Outcome.** Hypothesis passed (53.6%). Every deferred item was later built *because* the
evidence justified it — adaptive routing became v3.

### 2. Define "done" before building: a named baseline and a quality floor
**Decision.** Savings measured against *every task sent to the frontier model*; success
requires ≥95% of that baseline's quality, scored by a judge — not assumed.
**Why.** "Reduce cost" is meaningless without a baseline, and cheap wrong answers are
worthless. Fixing both up front stopped the results being argued after the fact.

### 3. Record-and-replay every API call
**Decision.** Cache every real response to disk, keyed on the full request.
**Trade-off.** Some engineering effort up front.
**Outcome.** The full benchmark re-runs for free and byte-identically. Most later analysis
— threshold sweeps, price re-costing, regression checks — cost $0 because of it.

### 4. Cross-check the judge with a rival vendor
**Context.** An Anthropic model judging answers raises an obvious same-family-bias question.
**Decision.** Re-score with an independent OpenAI judge before quoting quality.
**Outcome.** 96.7% agreement within 0.15, mean difference 0.010 (n=30). Quality numbers are
validated, not proven — a human label sheet is the remaining step, and the docs say so.

### 5. Price models at list, not promotional, rates
**Context.** Sonnet 5 launched with an introductory price.
**Decision.** Benchmark at list price.
**Why.** A promo-priced result silently becomes wrong when the promo ends.
**Outcome.** v5's live price tracker later confirmed the call: the aggregator still lists
the promo (−33%), while v1's 53.6% holds at today's market prices (53.58%).

### 6. Report savings net of the router's own cost
**Decision.** When a classifier or verifier is used, its cost is subtracted from the savings
and shown as "overhead as % of savings".
**Why.** A router that spends money to decide is not free; hiding that would overstate
every result. (v1 used free hand labels, so its headline is unchanged.)
**Outcome.** Exposed the real economics: the cross-vendor classifier costs ~1% of savings;
the within-vendor cascade ~31%.

### 7. Keep the conservative v1 number as the headline
**Context.** The v2 prompt classifier beat v1: 65.6% vs 53.6%.
**Decision.** Keep 53.6% as the headline; present 65.6% as a follow-up result.
**Why.** They are different claims (hand labels vs predicted labels), and the more
conservative, longer-validated number is the one to lead with.

### 8. Build a harder benchmark — and publish where routing loses
**Context.** The original 25 tasks score ~0.99 on every model tier, so they cannot show
whether routing holds quality when tasks are genuinely hard.
**Decision.** Build a 10-task hard set that separates the tiers (Haiku 0.70 / Sonnet 0.90 /
Opus 1.00) and publish the result even though it is unflattering.
**Outcome.** On hard work routing is cost-negative (−6% classifier, −20% cascade) while
quality holds at 100%. That became the project's central insight: **savings are a function
of how much of the workload is easy** — and the basis of the "do you need a router?"
calculator.

### 9. Overrule the cheaper verifier as the default
**Context.** Swapping the cascade's checker from Sonnet to Haiku looked like a clear win:
overhead 31% → 6% of savings, net savings 53.1% → 85.0% on the easy set.
**Decision.** Keep the independent (Sonnet) verifier as the default; the cheap one is opt-in.
**Evidence.** On the hard set, quality fell to 0.70: Haiku scored three of its own *wrong*
answers 0.95–1.0 and never escalated them — self-verification can't catch its own blind
spots.
**Why.** A default must uphold the quality guarantee on workloads nobody has measured yet.
The best number on one dataset is not a reason to weaken it.

### 10. Don't tune the knob that doesn't move the outcome
**Context.** The obvious fix for the cascade's 31% overhead was tuning its escalation threshold.
**Decision.** Measure first. A sweep over 0.5–0.9 (replayed from cache, $0) showed overhead
flat to rising: 91–94% of the overhead was the verifier being paid on every task, not
wasted escalations.
**Outcome.** Effort went to the real lever (the verifier model) instead of an inert one.

### 11. A learned router that only moves cheaper on strong evidence
**Decision.** v3's history-based router may downgrade a task only with ≥5 similar past
successes at the quality bar; thin evidence falls back to the classifier, and it never
uses history to justify a cheaper model on uncertain ground.
**Outcome.** 22.7% cheaper than the static router at identical quality on 14 held-out tasks,
with 3 downgrades correctly refused. The caveat travels with the number: the saving only
exists where task shapes repeat.

### 12. Ship a tool that sometimes says "don't buy this"
**Decision.** The ROI calculator's verdicts include *"Don't buy a router — you'd lose money
on this workload."*
**Why.** The benchmark proved routing can be cost-negative; a calculator that only ever
says yes would contradict the project's own evidence. Honesty is the product.

### 13. Product surface over more experiments
**Context (v4).** Routing only existed inside a benchmark script — nothing an app could call.
**Decision.** Pause new experiments and ship a usable surface: one `Router.complete()` API,
an OpenAI-compatible drop-in endpoint, and real failover to the next model instead of a
silent fake answer.
**Security calls.** Localhost-only by default, optional auth, a 1 MB request cap, and
request logging **off by default** because it stores users' prompts. Provider errors show
in the trace as status and type only, never the raw message, which can contain account ids.

### 14. Report the experiments that didn't work
**Jury of cheap models (v5).** Best balance on easy work (66.1% net, quality 0.99) but
unsafe on hard work (0.86): on one task three vendors agreed on the same wrong answer.
**Decision.** Not the default — and published as a finding: *agreement isn't correctness.*
**Red-team (v5).** Hardening stopped cost-inflation prompts (7/10 → 0/10), but disguising a
hard task as easy still works 2/9 times. **Decision.** Document it as the residual risk
rather than claim the router is secure; keep the pre-hardening prompts pinned so published
results still reproduce.

### 15. Guard against a provider's expensive defaults
**Context.** Moonshot's Kimi K3 defaults to maximum reasoning, billed as output tokens.
**Decision.** The router defaults Kimi to low effort; callers opt into more.
**Evidence.** An offline projection from real token counts showed that at Sonnet's
per-token price plus mandatory reasoning, Kimi can only compete on quality, never on price.
