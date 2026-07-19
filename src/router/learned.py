"""
Learned router (v3) — routes on outcome history, not just a cold prediction.

Implements docs/V3_DESIGN.md. Read that doc first; this module is deliberately
a thin, mechanical translation of it, not a reinterpretation. The one-line
pitch: v1's rules router and v2's classifier both predict (task_type,
difficulty) COLD, from the prompt alone, every single time. But `data/runs.db`
already logs what actually happened the last time a similar task came through
— this module is what asks that log a question before trusting the cold guess.

THE FLOOR IS THE CLASSIFIER'S OWN PREDICTION, NEVER LOWER. This module never
invents a route; it only asks whether logged evidence justifies moving
CHEAPER than what `router.router.DEFAULT_ROUTING_RULES` would already have
picked for the classifier's (task_type, difficulty), and it only does that
under a strict evidence threshold (n >= k similar outcomes, quality at or
above the same 95%-of-frontier retention bar the benchmark's hypothesis uses
everywhere else). Absence of history is not evidence for a cheaper model —
it is an absence of evidence — so with no matching history this module
degrades EXACTLY to the classifier's tier. That is cold start, and it is not
a special case in the code below: an empty evidence pool simply never clears
the threshold, so the same code path handles it.

THE SAFETY ASYMMETRY. Moving cheaper requires strong evidence (n >= k,
quality >= bar). Moving more expensive requires almost none: a single
recent, un-decayed outcome at the currently-chosen tier that looks like a
clear failure (success=False, or quality below a low floor) is enough to
escalate one tier, regardless of how many good outcomes exist alongside it.
Thin evidence can decline to move the router cheaper; it can never mandate
moving it cheaper — but thin BAD evidence is exactly enough to move it
pricier, because escalating costs money and failing to escalate risks
quality, and those two mistakes are not symmetric (see docs/V3_DESIGN.md's
"safety asymmetry" section, and the v2 verifier-economics finding it draws
the analogy from).

FEATURES ARE THE LIGHTWEIGHT-TASK-FEATURES OPTION FROM THE DESIGN DOC, NOT
EMBEDDINGS: (task_type, difficulty, prompt-length bucket, keyword-signal
group) — task_type/difficulty come from the caller (the classifier's own
prediction, real or heuristic), length bucket and keyword signal are
computed here from the prompt text, reusing router.classifier's own marker
logic (see router.classifier.keyword_signal_groups). No embedding model, no
new dependency, fully offline-testable — the same trade-off v1 and v2 made.

WHERE THE EVIDENCE COMES FROM. `runs` stores task_type/difficulty/model/
quality per row but NOT the prompt text (only `response_text`, the model's
OUTPUT). So the finer part of the feature bucket (length, keyword signal)
can't be recovered from the row alone — it has to be recomputed from the
task's ORIGINAL prompt, looked up by `task_id` in the known task registries
(data/tasks*.json). A historical row whose task_id isn't in any known
registry contributes nothing (documented limitation, not a crash): this
prototype's evidence pool is bounded by "tasks this benchmark has run
before," which is exactly the repeated-task-shape assumption the whole
design rests on.
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import db
from .classifier import keyword_signal_groups
from .config import TIER_ORDER, get_roster
from .report import QUALITY_RETENTION_TARGET_PCT
from .router import DEFAULT_ROUTING_RULES

# --- Evidence-threshold constants (tunable; see docs/V3_DESIGN.md) ---------

MIN_EVIDENCE_N: int = 5
"""k in "n >= k similar logged outcomes". The design doc's own starting
point, flagged there as "tunable per task type once there's enough log
volume to tune it against" — this prototype does not yet have that volume
broken out per task_type, so one global constant is the honest amount of
sophistication to ship."""

MIN_EFFECTIVE_N_RATIO: float = 0.5
"""effective_n (decay-weighted count) must be at least this fraction of
MIN_EVIDENCE_N, not the full MIN_EVIDENCE_N itself. Decay weight is
STRICTLY less than 1.0 for any nonzero age (see decay_weight), so requiring
effective_n >= MIN_EVIDENCE_N outright would fail even microseconds-old
evidence by a floating-point hair and could never realistically be cleared.
The ratio is the actual "hasn't decayed away" gate: on average, evidence
must retain at least half its original weight (roughly "no older than one
DECAY_HALF_LIFE_DAYS on average") to still count as enough."""

DEFAULT_FRONTIER_QUALITY: float = 1.0
"""Used when a bucket has no logged frontier evidence yet to compare
against. Assuming the frontier would score at the ceiling is the STRICTEST
possible bar (any real frontier quality < 1.0 would only make the bar easier
to clear), which keeps "no frontier evidence" from ever being a loophole
that makes the cheaper-tier bar easier to pass by omission."""

CONCERN_QUALITY_FLOOR: float = 0.5
"""A logged outcome at or below this quality is a "clear failure" for the
purposes of the safety asymmetry's escalation check — deliberately low
(clearly wrong, not just imperfect) so escalation triggers on real distress
signals, not on ordinary judge-score noise."""

CONCERN_MIN_WEIGHT: float = 0.05
"""A concerning outcome only counts if its decay weight is STRICTLY above
this floor — i.e. it is not so old as to be functionally erased already.
Without this, a single ancient failure could escalate a bucket forever.
The comparison is strict (>) rather than >= deliberately: NULL-created_at
rows (all pre-v3 history) are assigned exactly NULL_CREATED_AT_WEIGHT ==
this floor, and "maximally stale" has to mean maximally stale in BOTH
directions — undated history is too decayed to justify a downgrade AND too
decayed to mandate an escalation. With the gate in place, the escalation
check still fires far more readily than the n>=5 downgrade check on any
dated evidence (one qualifying row is enough, vs. five), which is the
asymmetry the design doc calls for."""

DECAY_HALF_LIFE_DAYS: float = 30.0
"""Exponential half-life for time-decay weighting of logged outcomes. At
this benchmark's actual data (a few days of history as of writing — see
docs/V3_FINDINGS.md), a 30-day half-life makes almost every row's weight
close to 1.0; it exists as a real, working mechanism for when the log
starts spanning months, not as a knob that changes today's numbers much.
Tunable; see docs/V3_DESIGN.md's evidence-decay section (the model-repricing
argument for why staleness matters at all)."""

NULL_CREATED_AT_WEIGHT: float = 0.05
"""Decay weight assigned to a row with NULL created_at (every row logged
before the v3 schema migration — see router.db's _MIGRATIONS). Treated as
MAXIMALLY STALE rather than "now" (which would silently trust undated
history as if it were fresh) or a hard 0.0 (which would make an all-NULL
bucket divide-by-zero when computing a weighted mean, and would also erase
history that a human could otherwise still audit). A small positive floor
is the documented middle ground the design doc calls for: "fully decayed or
minimum weight"."""

_LENGTH_SHORT_MAX = 120
_LENGTH_MEDIUM_MAX = 220
"""Same breakpoints as router.classifier.heuristic_classify's own
difficulty-from-length heuristic — reused deliberately (see
docs/V3_DESIGN.md: "a coarse prompt-length bucket ... already similar to
heuristic_classify's logic") rather than inventing a second set of
thresholds with no basis."""


def length_bucket(prompt: str) -> str:
    """Coarse prompt-length band: short / medium / long."""
    length = len(prompt)
    if length < _LENGTH_SHORT_MAX:
        return "short"
    if length < _LENGTH_MEDIUM_MAX:
        return "medium"
    return "long"


@dataclass(frozen=True)
class FeatureBucket:
    """The lightweight-features similarity key. Two tasks are "similar
    enough" for evidence purposes iff every field here matches exactly."""

    task_type: str
    difficulty: str
    length_bucket: str
    keyword_signal: str
    """Comma-joined, sorted keyword_signal_groups(prompt), or "none" if no
    marker group matched. A string (not a tuple) so it round-trips cleanly
    through JSON when logged as part of learned_evidence."""


def extract_features(prompt: str, task_type: str, difficulty: str) -> FeatureBucket:
    """
    Build the similarity key for one task.

    Args:
        prompt: Raw prompt text.
        task_type: The classifier's predicted task_type (real model or
            heuristic fallback — this module does not care which).
        difficulty: The classifier's predicted difficulty.

    Returns:
        FeatureBucket combining the classifier's own prediction with two
        signals computed independently from the prompt text.
    """
    groups = keyword_signal_groups(prompt)
    return FeatureBucket(
        task_type=task_type,
        difficulty=difficulty,
        length_bucket=length_bucket(prompt),
        keyword_signal=",".join(groups) if groups else "none",
    )


def decay_weight(created_at: Optional[str], now: Optional[datetime] = None) -> float:
    """
    Exponential recency weight in (0, 1] for one logged outcome.

    Args:
        created_at: ISO-8601 timestamp string, or None/unparseable (every
            pre-v3 row — see router.db's _MIGRATIONS note on why `runs` has
            no real timestamp for history that predates the column).
        now: Reference time to measure age against. Defaults to the real
            current time; tests pass a fixed value for determinism.

    Returns:
        1.0 for age=0, halving every DECAY_HALF_LIFE_DAYS, floored at
        NULL_CREATED_AT_WEIGHT for missing/unparseable timestamps (never
        raises on bad input — a decay function that can crash a routing
        decision is worse than one that is merely conservative).
    """
    if not created_at:
        return NULL_CREATED_AT_WEIGHT
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return NULL_CREATED_AT_WEIGHT
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = max((reference - parsed).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)


# --- Task registry: resolving a historical row's prompt by task_id ---------

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_registry_cache: Optional[dict[str, str]] = None


def _discover_task_files() -> list[Path]:
    """Every data/tasks*.json file this repo ships — the full known pool of
    task shapes a historical run_group could have drawn its task_id from."""
    return sorted(Path(p) for p in glob.glob(str(_DATA_DIR / "tasks*.json")))


def _load_task_registry() -> dict[str, str]:
    """Lazily build and cache task_id -> prompt across every known task
    file. Cached at module level because it is read on every evidence
    lookup and the underlying files do not change during a process
    lifetime."""
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    registry: dict[str, str] = {}
    for path in _discover_task_files():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for task in data.get("tasks", []):
            task_id = task.get("id")
            prompt = task.get("prompt")
            if task_id and prompt and task_id not in registry:
                registry[task_id] = prompt
    _registry_cache = registry
    return registry


@dataclass(frozen=True)
class Evidence:
    """What the log shows for one (feature bucket, tier) pair."""

    tier: str
    n: int
    """Raw count of matching logged outcomes (before decay weighting) — the
    literal "n" the n >= k threshold is checked against."""
    effective_n: float
    """Decay-weighted count. Also gated at >= MIN_EVIDENCE_N: five
    outcomes from six months ago should count for less than five from
    yesterday, including toward whether there's "enough" evidence at all,
    not just toward the quality average."""
    weighted_quality: float
    """Decay-weighted mean quality_score across matching outcomes."""
    frontier_reference_quality: float
    """What the retention bar is computed against for this bucket."""
    quality_bar: float
    """frontier_reference_quality * (QUALITY_RETENTION_TARGET_PCT / 100)."""
    meets_threshold: bool
    """True iff both n and effective_n clear MIN_EVIDENCE_N AND
    weighted_quality clears quality_bar. The gate a cheaper tier must clear
    to be eligible for a downgrade."""
    concerning: bool
    """True iff at least one matching outcome at this tier is a recent
    (decay weight >= CONCERN_MIN_WEIGHT), clear failure (success=False or
    quality <= CONCERN_QUALITY_FLOOR). The much-lower-bar signal that can
    trigger escalation regardless of meets_threshold."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe summary for persisting via db.log_run(learned_evidence=...)."""
        return {
            "tier": self.tier,
            "n": self.n,
            "effective_n": round(self.effective_n, 4),
            "weighted_quality": round(self.weighted_quality, 4),
            "frontier_reference_quality": round(self.frontier_reference_quality, 4),
            "quality_bar": round(self.quality_bar, 4),
            "meets_threshold": self.meets_threshold,
            "concerning": self.concerning,
        }


@dataclass(frozen=True)
class LearnedDecision:
    """The learned router's output: a tier, and the evidence that justifies it."""

    task_id: str
    feature_bucket: FeatureBucket
    classifier_tier: str
    """What router.router.DEFAULT_ROUTING_RULES would have picked for the
    classifier's (task_type, difficulty) — the floor this module can never
    go below without evidence."""
    chosen_tier: str
    direction: str
    """"unchanged" | "downgraded" | "escalated"."""
    reason: str
    evidence: list[Evidence] = field(default_factory=list)
    """Every tier's Evidence this decision considered, cheapest-checked
    first, in the order it was evaluated."""

    def to_log_dict(self) -> dict[str, Any]:
        """JSON-safe payload for db.log_run(learned_evidence=...)."""
        return {
            "feature_bucket": {
                "task_type": self.feature_bucket.task_type,
                "difficulty": self.feature_bucket.difficulty,
                "length_bucket": self.feature_bucket.length_bucket,
                "keyword_signal": self.feature_bucket.keyword_signal,
            },
            "classifier_tier": self.classifier_tier,
            "chosen_tier": self.chosen_tier,
            "direction": self.direction,
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
        }


def _tier_for(task_type: str, difficulty: str, routing_rules: Optional[dict]) -> str:
    rules = routing_rules or DEFAULT_ROUTING_RULES
    return rules[task_type][difficulty]


def _gather_evidence(
    tier: str,
    features: FeatureBucket,
    roster,
    before_run_group: Optional[str],
    db_path,
    task_registry: dict[str, str],
    now: Optional[datetime],
) -> Evidence:
    """Query + refine + weigh the evidence for one (feature bucket, tier)."""
    model = roster.model_for_tier(tier)
    rows = db.outcomes_for_bucket(
        features.task_type, features.difficulty,
        before_run_group=before_run_group, db_path=db_path,
    )

    matched: list[dict[str, Any]] = []
    for row in rows:
        # Match on the roster's EXACT model for this tier, not on tier label.
        # MODEL_TIER maps gpt-4o-mini and claude-haiku-4-5 both to "budget",
        # but evidence that one vendor's budget model handled a task shape is
        # not evidence that a different vendor's will — the same reasoning as
        # the design doc's "five good outcomes at the mid tier are not
        # evidence for routing to budget", applied across rosters instead of
        # across tiers.
        if row["model"] != model:
            continue
        prompt = task_registry.get(row["task_id"])
        if prompt is None:
            # Can't recompute the fine-grained bucket without the original
            # prompt — this row is invisible to the learned router, not an
            # error. See module docstring: the evidence pool is bounded by
            # known task registries.
            continue
        row_features = extract_features(prompt, features.task_type, features.difficulty)
        if row_features != features:
            continue
        matched.append(row)

    weights = [decay_weight(r["created_at"], now) for r in matched]
    n = len(matched)
    effective_n = sum(weights)
    weighted_quality = (
        sum(w * r["quality_score"] for w, r in zip(weights, matched)) / effective_n
        if effective_n > 0 else 0.0
    )

    frontier_reference = db.frontier_reference_quality(
        features.task_type, features.difficulty, roster.frontier,
        before_run_group=before_run_group, db_path=db_path,
    )
    if frontier_reference is None:
        frontier_reference = DEFAULT_FRONTIER_QUALITY
    quality_bar = frontier_reference * (QUALITY_RETENTION_TARGET_PCT / 100)

    meets_threshold = (
        n >= MIN_EVIDENCE_N
        and effective_n >= MIN_EVIDENCE_N * MIN_EFFECTIVE_N_RATIO
        and weighted_quality >= quality_bar
    )
    concerning = any(
        (not r["success"] or r["quality_score"] <= CONCERN_QUALITY_FLOOR) and w > CONCERN_MIN_WEIGHT
        for r, w in zip(matched, weights)
    )

    return Evidence(
        tier=tier,
        n=n,
        effective_n=effective_n,
        weighted_quality=weighted_quality,
        frontier_reference_quality=frontier_reference,
        quality_bar=quality_bar,
        meets_threshold=meets_threshold,
        concerning=concerning,
    )


def evaluate(
    task: dict[str, Any],
    classifier_task_type: str,
    classifier_difficulty: str,
    roster_name: Optional[str] = None,
    routing_rules: Optional[dict[str, dict[str, str]]] = None,
    before_run_group: Optional[str] = None,
    db_path: Optional[Any] = None,
    task_registry: Optional[dict[str, str]] = None,
    now: Optional[datetime] = None,
) -> LearnedDecision:
    """
    Decide a tier for `task`, consulting outcome history over the classifier's
    cold prediction.

    Args:
        task: Task dict with at least `id` and `prompt`.
        classifier_task_type: The classifier's predicted task_type (real
            model or heuristic — this module treats it as an opaque input).
        classifier_difficulty: The classifier's predicted difficulty.
        roster_name: Which ladder to route across. Defaults to the published
            v1 roster, same convention as router.router.route_task.
        routing_rules: Override for DEFAULT_ROUTING_RULES (mirrors
            router.router.route_task's parameter of the same name).
        before_run_group: Chronological cutoff — only evidence from
            run_groups strictly earlier than this one is considered. Pass
            the run_group about to be logged so a task never sees evidence
            from its own (or a later) benchmark invocation, including
            sibling tasks in the same batch.
        db_path: Optional db path override, for tests.
        task_registry: Override for the task_id -> prompt map used to
            resolve historical rows' prompts. Defaults to every known
            data/tasks*.json file. Tests should pass a small fixture dict
            here rather than relying on the real files.
        now: Reference time for decay weighting. Defaults to real "now";
            tests pass a fixed value for determinism.

    Returns:
        LearnedDecision. `chosen_tier` is never cheaper than
        `classifier_tier` unless the cheaper tier's own evidence clears
        MIN_EVIDENCE_N at the quality bar; it can be MORE expensive than
        classifier_tier if the chosen tier's evidence looks concerning.
    """
    roster = get_roster(roster_name)
    classifier_tier = _tier_for(classifier_task_type, classifier_difficulty, routing_rules)
    features = extract_features(task.get("prompt", ""), classifier_task_type, classifier_difficulty)
    registry = task_registry if task_registry is not None else _load_task_registry()

    considered: list[Evidence] = []

    # 1. Look for the CHEAPEST tier, strictly below the classifier's tier,
    # whose own evidence clears the downgrade threshold. Cheapest-first,
    # because the question this module answers is "what's the cheapest
    # model that historically met the quality bar" — not "is the next tier
    # down good enough", which would leave savings on the table when a
    # tier two steps down already has strong direct evidence.
    chosen_tier = classifier_tier
    direction = "unchanged"
    reason = (
        f"{classifier_task_type}/{classifier_difficulty} -> {classifier_tier} tier "
        f"per classifier; no cheaper tier had sufficient evidence to override"
    )
    for tier in TIER_ORDER[: TIER_ORDER.index(classifier_tier)]:
        ev = _gather_evidence(tier, features, roster, before_run_group, db_path, registry, now)
        considered.append(ev)
        if ev.meets_threshold:
            chosen_tier = tier
            direction = "downgraded"
            reason = (
                f"{classifier_task_type}/{classifier_difficulty} -> {classifier_tier} tier "
                f"per classifier; downgraded to {tier} on {ev.n} similar logged outcomes "
                f"(effective n={ev.effective_n:.1f}, weighted quality={ev.weighted_quality:.3f} "
                f">= bar {ev.quality_bar:.3f})"
            )
            break

    # 2. Safety asymmetry: does the tier we're about to route to show a
    # recent, clear failure? One qualifying row is enough — far weaker than
    # the n>=5 bar above — because escalating costs money but failing to
    # escalate risks quality, and those mistakes are not symmetric.
    chosen_evidence = next((e for e in considered if e.tier == chosen_tier), None)
    if chosen_evidence is None:
        chosen_evidence = _gather_evidence(
            chosen_tier, features, roster, before_run_group, db_path, registry, now
        )
        considered.append(chosen_evidence)

    if chosen_evidence.concerning and chosen_tier != TIER_ORDER[-1]:
        next_tier = TIER_ORDER[TIER_ORDER.index(chosen_tier) + 1]
        reason = (
            f"escalated from {chosen_tier} to {next_tier}: logged evidence at "
            f"{chosen_tier} includes a recent outcome at or below "
            f"{CONCERN_QUALITY_FLOOR} quality (or a failure) — thin bad evidence "
            f"is enough to escalate, unlike the n>=5 bar required to go cheaper"
        )
        chosen_tier = next_tier
        direction = "escalated"

    return LearnedDecision(
        task_id=task.get("id", ""),
        feature_bucket=features,
        classifier_tier=classifier_tier,
        chosen_tier=chosen_tier,
        direction=direction,
        reason=reason,
        evidence=considered,
    )
