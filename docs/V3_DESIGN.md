# V3 Design — the learned router

A design doc, not a build. Nothing here is implemented; this is the spec the
next phase would build against. The pitch in one line: **a router that gets
cheaper as it learns your workload**, instead of re-deriving the same
difficulty estimate from scratch on every call.

## The idea

v1's rules router and v2's classifier both predict difficulty *cold* — they
look at a prompt and guess `(task_type, difficulty)` with no memory of what
actually happened the last time a similar prompt came through. But every one
of those past calls got logged: model, cost, quality, success. That log is
sitting in `data/runs.db`, unused for routing decisions.

The learned router closes that loop. Instead of relying only on the classifier's
cold prediction, it also asks: **for tasks like this one, what's the cheapest
model that historically met the quality bar?** If the evidence says the budget
tier has handled this shape of task well 20 times in a row, route there instead
of paying for the classifier's more conservative guess.

Plainly: this is contextual-bandit-shaped routing. Each incoming task is a
context; each model tier is an arm; the reward is quality-at-cost. There's no
need to dress this up further — it's the standard shape of the problem, and
naming it correctly says what literature and failure modes apply (explore/
exploit, regret bounds, cold-start) without pretending this design does
anything more sophisticated than a fairly simple exploitation rule with a
conservative fallback.

## The training signal already exists

`src/router/db.py`'s `runs` table already logs everything a learned router
would need to condition on:

| Column | Use for the learned router |
|---|---|
| `task_type`, `difficulty` | The classifier's own prediction — a cheap, existing feature. |
| `model`, `roster` | Which tier handled it, and on what ladder. |
| `cost_usd`, `routing_cost_usd` | What it cost, including the classification tax. |
| `quality_score`, `success` | The outcome — the reward signal. |
| `verifier_scores` | Per-tier adequacy scores from cascade runs — a second, cheaper-to-collect quality signal where available. |
| `run_group` | Which benchmark invocation produced the row — the natural key for a train/held-out split (see Eval plan). |

So the reward data is not the gap. Two things are missing, and they're the
entire scope of this design:

1. **A similarity function** — given a *new* prompt, which logged rows count
   as "tasks like this one"?
2. **A decision rule over that evidence** — given a set of similar past
   outcomes, when is it safe to route cheaper than the classifier would?

## Similarity is the hard 80%

Exact prompt matching is useless — production prompts are unique strings by
construction (different names, numbers, phrasing), so an exact-match lookup
would almost never hit. The router needs a notion of "similar enough,"  and
that's where the real design risk lives.

| Approach | How it works | Trade-off |
|---|---|---|
| **Embedding nearest-neighbor** | Embed the new prompt, cosine-search against embedded logged prompts, pull the k nearest outcomes. | Most semantically accurate. Requires an embedding model call (cost, latency) and a new dependency (a vector store or at minimum a numpy-based kNN over a growing table) — the repo has stayed stdlib-only through v1 and v2, and this breaks that. Also non-deterministic-ish and harder to unit-test offline. |
| **Lightweight task features** | Bucket by `task_type` (from the classifier) × prompt length band × a small keyword/regex signal set (already similar to `heuristic_classify`'s logic in `classifier.py`). | Cheap (no extra model call), fully deterministic, testable offline with fixtures — matches this repo's stdlib-only, `AWR_FORCE_MOCK`-friendly test philosophy. Crude: two prompts with the same type and length can differ wildly in actual difficulty, so it will sometimes lump dissimilar tasks together. |
| **Hybrid** | Lightweight features for a fast pre-filter, embeddings only to re-rank within the pre-filtered set. | Gets most of the accuracy at a fraction of the embedding calls. Still introduces the dependency and non-determinism, just less of it. |

**Recommendation for the prototype: lightweight features first.** It keeps
the prototype offline-testable and dependency-free, which is what let v1 and
v2 both ship with `AWR_FORCE_MOCK=1` test coverage and no live-call
requirement in CI. Concretely: bucket on `(task_type, difficulty)` predicted
by the existing classifier, plus a coarse prompt-length bucket, plus whatever
keyword signals `heuristic_classify` already extracts. This is deliberately
close to "a slightly smarter classifier with a memory," not a new subsystem.

**Name embeddings as the production upgrade.** Once the feature-based version
is proven to move the needle at all, embedding similarity is the natural next
step for a real deployment with enough volume to amortize the embedding cost
and enough task diversity that length/keyword buckets stop discriminating.
That's a v4-shaped decision, not a v3 one — flagging it here so it isn't
rediscovered from scratch.

## Cold start

With no matching history, **the learned router must degrade exactly to the
static classifier.** No history is not evidence for a cheaper model — it's an
absence of evidence, and the entire design goal is to never let absence of
evidence look like a green light.

Concretely, define an evidence threshold before history is allowed to
*override* the classifier's tier recommendation toward something cheaper:

- **n ≥ k similar logged outcomes** at the recommended cheaper tier (a
  starting point: k = 5, tunable per task type once there's enough log volume
  to tune it against).
- **quality ≥ bar** across those outcomes — the same 95%-of-frontier retention
  bar the benchmark's hypothesis already uses elsewhere, not a new number
  invented for this feature.
- Both conditions on the *same* tier being proposed — five good outcomes at
  the mid tier are not evidence for routing to budget.

If either condition fails, the learned router emits exactly what the
classifier would have. **The floor is the classifier's own prediction, never
lower** — thin evidence can decline to move the router cheaper; it can never
mandate moving it cheaper. This is the one hard rule of the whole design, and
it's worth stating twice because everything else here is a heuristic.

## Evidence decay

Logged outcomes go stale for two independent reasons: **the model changes**
(a provider updates weights under a fixed name) and **the price changes**
(vendors reprice; the repo's own `data/runs.db` already contains rows priced
under terms that won't hold forever — Sonnet 5's introductory pricing that
this repo deliberately benchmarks against at list price expires 2026-08-31,
and any evidence collected before that date is priced evidence that will be
wrong afterward). Treating a six-month-old outcome as equally trustworthy as
yesterday's is a bug, not a simplification.

Two mechanisms, not mutually exclusive:

- **Time-decay weighting.** Weight each logged outcome's contribution to the
  evidence threshold by recency (e.g. exponential decay on age), so old rows
  count for less rather than dropping out in a hard cliff.
- **Re-verification.** Periodically re-run a small sample of stale "similar
  task" buckets against current pricing/model behavior and refresh the
  evidence, rather than trusting a decayed weight forever. Cheaper than a
  blanket re-run, and it catches silent model-quality drift that pure
  time-decay can't.

**A schema gap worth flagging now:** `runs` has no dedicated timestamp
column — freshness today can only be inferred from `id` (insertion order,
monotonic but not wall-clock) or by parsing the timestamp-looking prefix out
of `run_group` (a convention, not a guarantee — see the `20260717T082024`
format used in README's results table). Any decay implementation needs an
actual `created_at` column added to the schema before it can do real
time-based weighting. That's a `src/` change, out of scope for this doc, but
the prototype builder should treat it as a prerequisite, not an afterthought.

## The safety asymmetry

History may only move routing **down** the price ladder, and only under the
evidence threshold above. Anything uncertain — thin evidence, decayed
evidence, evidence that disagrees with itself — routes at least as
conservatively as the static classifier would. Moving **up** the ladder (to a
more expensive, safer tier) should have a much lower evidence bar than moving
down: if the log shows *any* concerning signal on a task shape, escalating
costs money; failing to escalate risks quality. Those two mistakes are not
symmetric, and the decision rule shouldn't treat them as if they were.

This is not a hypothetical caution — it's the direct lesson of the v2
verifier-economics finding
([docs/V2_FINDINGS.md#verifier-economics-does-a-cheaper-verifier-still-gate-correctly](V2_FINDINGS.md#verifier-economics-does-a-cheaper-verifier-still-gate-correctly)).
The cheap (Haiku) verifier was a clean win on easy workloads — overhead fell
from 31% to 6.2% of savings with no quality loss — but on hard tasks it
dropped quality from 1.00 to 0.70 because a self-verifying cheap model rated
its own wrong answers 0.95–1.0. The failure mode was trusting a cheap,
convenient signal exactly where it was least reliable — hard tasks, where the
model doing the verifying was also the model that might be wrong. A learned
router that leans on sparse or stale evidence for hard-looking tasks is the
same mistake wearing a different hat: cheap signal, high-stakes decision,
correlated blind spot. The mitigation is the same principle in both places —
require independent, sufficient evidence before trusting a cheap path, and
default to the safer, more expensive one when in doubt.

## Eval plan

The only credible claim for v3 is that it **beats the static classifier on
held-out tasks it did not train on** — beating the classifier on data it
already learned from is not a result, it's overfitting with extra steps.

- **Split.** Use `run_group` as the natural partition: train the learned
  router's evidence store on some run groups, evaluate on later ones it
  never saw. (Chronological split, not random — a learned router that peeks
  at "future" outcomes to route a "past" task is not testing what it claims
  to test.) At current volume (25 easy tasks, 10 hard tasks, a handful of
  run groups per experiment) this likely means training on the existing v1/v2
  run groups and evaluating fresh on a newly run, held-out task set — not
  slicing the existing 25/10 task lists thinner, which would leave too few
  examples per bucket to mean anything.
- **Metrics.** Net savings vs. the static classifier, at equal-or-better
  quality — the same framing the whole project has used since v1 ("cost
  reduction at a quality floor," not cost reduction alone). Report routing
  overhead the same way v2 does (routing cost as % of savings) so a learned
  router that requires its own expensive matching step doesn't get to hide
  that cost.
- **Honest expectation.** At this benchmark's task volume (n=25 easy, n=10
  hard), a null result is a real possibility — there may simply not be enough
  repeated task shapes yet for the evidence threshold to ever fire, in which
  case the learned router should show up as *statistically indistinguishable
  from the classifier* rather than worse. That itself is a legitimate, honest
  finding to report, not a failed experiment: it would say the same thing the
  buyer's-decision-rule finding elsewhere in this project says — the value of
  a technique is a function of workload volume and repetition, and this
  benchmark's task count may be below the threshold where it pays off.

## What it would take

Rough scope only — not a commitment to build this shape exactly:

- **`src/router/learned.py`** — new module, sibling to `classifier.py`. Owns
  the similarity lookup (lightweight-features version first) and the
  evidence-threshold decision rule. Takes a prompt and the classifier's
  `(task_type, difficulty)` prediction as input; returns either the
  classifier's tier unchanged, or a cheaper tier plus the evidence that
  justified the downgrade (for the report to display, the same way
  `classifier_agreed` and `verifier_scores` are surfaced today).
- **Plugs into `run_benchmark.py`** the way `--classify` and `--strategy
  cascade` do now — most plausibly a `--strategy learned` flag that wraps the
  classifier's output through `learned.py` before dispatching to a model,
  and logs an extra field (analogous to `classifier_agreed`) recording
  whether the learned router agreed with, or downgraded, the classifier's
  call.
- **Reads `runs.db`** at routing time via a new query in `db.py` (something
  like "similar outcomes for this feature bucket," parallel to the existing
  `summary_by_strategy` / `all_runs` queries) rather than loading the whole
  table per call.
- Needs the `created_at` schema addition noted above before evidence decay
  can be anything more than insertion-order ordering.

None of the above is implemented by this document. It's the spec for the
next phase to build against, deliberately scoped small enough to stay
offline-testable and additive to what v1/v2 already ship.
