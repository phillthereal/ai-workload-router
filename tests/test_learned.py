"""
Tests for router.learned — the v3 learned router (docs/V3_DESIGN.md).

Runs entirely offline against tmp SQLite fixtures (no AWR_FORCE_MOCK needed:
this module makes no LLM calls at all — its lookup is pure SQL + dict logic,
which is the whole point of it being free routing overhead). A fixed `now`
and an injected `task_registry` keep decay weighting and prompt resolution
deterministic without touching the real data/tasks*.json files or the real
clock.
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import db  # noqa: E402
from router import learned  # noqa: E402

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)

EXTRACTION_PROMPT = "Extract the company name from this sentence about a widget maker."
REASONING_PROMPT = "Work out how many widgets remain, step by step."
CLASSIFICATION_PROMPT = "Classify this review as positive or negative: 'great product'."

REGISTRY = {
    "t1": EXTRACTION_PROMPT,
    "t2": REASONING_PROMPT,
    "t3": CLASSIFICATION_PROMPT,
}


class _DbCase(unittest.TestCase):
    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.db_path = Path(fd.name)
        db.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _log(self, task_id, model, quality, task_type="extraction", difficulty="hard",
              success=True, created_at=None, run_group="g0"):
        return db.log_run(
            run_group=run_group, strategy="router", task_id=task_id, task_type=task_type,
            difficulty=difficulty, model=model, input_tokens=10, output_tokens=5,
            cost_usd=0.001, latency_ms=10.0, quality_score=quality, success=success,
            created_at=created_at, db_path=self.db_path,
        )

    def _log_legacy_null_created_at(self, task_id, model, quality, task_type="extraction",
                                     difficulty="hard", success=True, run_group="g0"):
        """Simulate a genuinely pre-v3 row: log_run always stamps a real
        timestamp when created_at isn't given (see its docstring), so a NULL
        row can only exist from before the column was added — reproduce
        that directly rather than relying on a code path that can't
        actually produce one."""
        row_id = self._log(task_id, model, quality, task_type, difficulty, success, run_group=run_group)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE runs SET created_at = NULL WHERE id = ?", (row_id,))
            conn.commit()
        finally:
            conn.close()
        return row_id


class TestFeatureBucketing(unittest.TestCase):
    def test_length_bucket_boundaries(self):
        self.assertEqual(learned.length_bucket("x" * 50), "short")
        self.assertEqual(learned.length_bucket("x" * 119), "short")
        self.assertEqual(learned.length_bucket("x" * 120), "medium")
        self.assertEqual(learned.length_bucket("x" * 219), "medium")
        self.assertEqual(learned.length_bucket("x" * 220), "long")

    def test_features_carry_the_callers_task_type_and_difficulty(self):
        features = learned.extract_features(EXTRACTION_PROMPT, "extraction", "hard")
        self.assertEqual(features.task_type, "extraction")
        self.assertEqual(features.difficulty, "hard")

    def test_keyword_signal_reflects_matched_marker_groups(self):
        features = learned.extract_features(EXTRACTION_PROMPT, "extraction", "hard")
        self.assertEqual(features.keyword_signal, "extraction")

    def test_keyword_signal_none_when_nothing_matches(self):
        features = learned.extract_features("A generic prompt with no signal words.", "short_generation", "easy")
        self.assertEqual(features.keyword_signal, "none")

    def test_ambiguous_prompt_reports_multiple_groups(self):
        """A prompt that trips more than one marker group is a genuinely
        different shape from one that only trips one — the whole reason
        keyword_signal is tracked separately from task_type."""
        prompt = "Classify each item, then explain your reasoning for the ordering."
        features = learned.extract_features(prompt, "reasoning", "easy")
        self.assertIn("reasoning", features.keyword_signal)
        self.assertIn("classification", features.keyword_signal)

    def test_features_are_deterministic(self):
        a = learned.extract_features(EXTRACTION_PROMPT, "extraction", "hard")
        b = learned.extract_features(EXTRACTION_PROMPT, "extraction", "hard")
        self.assertEqual(a, b)


class TestDecayWeight(unittest.TestCase):
    def test_fresh_outcome_weight_is_near_one(self):
        self.assertAlmostEqual(learned.decay_weight(NOW.isoformat(), now=NOW), 1.0, places=6)

    def test_weight_halves_at_one_half_life(self):
        aged = (NOW - timedelta(days=learned.DECAY_HALF_LIFE_DAYS)).isoformat()
        self.assertAlmostEqual(learned.decay_weight(aged, now=NOW), 0.5, places=6)

    def test_older_outcome_weighs_less_than_newer(self):
        recent = (NOW - timedelta(days=1)).isoformat()
        old = (NOW - timedelta(days=90)).isoformat()
        self.assertGreater(learned.decay_weight(recent, now=NOW), learned.decay_weight(old, now=NOW))

    def test_null_created_at_is_minimum_weight_not_a_crash(self):
        self.assertEqual(learned.decay_weight(None, now=NOW), learned.NULL_CREATED_AT_WEIGHT)

    def test_unparseable_created_at_is_minimum_weight_not_a_crash(self):
        self.assertEqual(learned.decay_weight("not-a-timestamp", now=NOW), learned.NULL_CREATED_AT_WEIGHT)


class TestColdStart(_DbCase):
    def test_no_evidence_degrades_exactly_to_classifier_tier(self):
        """No matching history at all — absence of evidence must never look
        like a green light. The learned router must emit exactly what
        router.router's default policy would have."""
        task = {"id": "t2", "prompt": REASONING_PROMPT}
        decision = learned.evaluate(
            task, "reasoning", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "unchanged")
        self.assertEqual(decision.chosen_tier, "frontier")
        self.assertEqual(decision.classifier_tier, "frontier")

    def test_already_cheapest_tier_has_nothing_to_downgrade_to(self):
        task = {"id": "t3", "prompt": CLASSIFICATION_PROMPT}
        decision = learned.evaluate(
            task, "classification", "easy", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.chosen_tier, "budget")
        self.assertEqual(decision.direction, "unchanged")


class TestThresholdGating(_DbCase):
    """The load-bearing test in this file: n=4 similar outcomes must never
    override, n=5 must (given quality clears the bar)."""

    def _seed_frontier_reference(self):
        for _ in range(3):
            self._log("t1", "claude-opus-4-8", 0.97, created_at=NOW.isoformat())

    def test_four_similar_outcomes_do_not_override(self):
        self._seed_frontier_reference()
        for _ in range(4):
            self._log("t1", "claude-haiku-4-5", 0.96, created_at=NOW.isoformat())
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "unchanged")
        self.assertEqual(decision.chosen_tier, "frontier")

    def test_five_similar_outcomes_at_quality_do_override(self):
        self._seed_frontier_reference()
        for _ in range(5):
            self._log("t1", "claude-haiku-4-5", 0.96, created_at=NOW.isoformat())
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "downgraded")
        self.assertEqual(decision.chosen_tier, "budget")

    def test_five_outcomes_below_the_quality_bar_do_not_override(self):
        """n >= k alone is not sufficient — quality must also clear the
        95%-of-frontier bar, or thin-but-plentiful bad evidence could still
        smuggle a downgrade through."""
        self._seed_frontier_reference()
        for _ in range(5):
            self._log("t1", "claude-haiku-4-5", 0.5, created_at=NOW.isoformat())
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "unchanged")

    def test_other_vendors_budget_model_is_not_evidence_for_this_roster(self):
        """gpt-4o-mini and claude-haiku-4-5 are both 'budget' in MODEL_TIER,
        but five good gpt-4o-mini outcomes must not justify downgrading a
        claude_tiers task to Haiku — evidence is per-model, not per-tier-label."""
        self._seed_frontier_reference()
        for _ in range(5):
            self._log("t1", "gpt-4o-mini", 0.96, created_at=NOW.isoformat())
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "unchanged")

    def test_effective_n_exactly_at_ratio_threshold_counts(self):
        """MIN_EFFECTIVE_N_RATIO's gate is >=, not >. Five raw outcomes each
        aged exactly one DECAY_HALF_LIFE_DAYS carry weight 0.5 apiece, so
        effective_n lands EXACTLY on MIN_EVIDENCE_N * MIN_EFFECTIVE_N_RATIO
        (5 * 0.5 = 2.5) — that boundary value must still clear the gate, not
        fall just short of it by a hair."""
        from datetime import timedelta

        self._seed_frontier_reference()
        aged = (NOW - timedelta(days=learned.DECAY_HALF_LIFE_DAYS)).isoformat()
        for _ in range(5):
            self._log("t1", "claude-haiku-4-5", 0.99, created_at=aged)
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        ev = next(e for e in decision.evidence if e.tier == "budget")
        self.assertAlmostEqual(ev.effective_n, 2.5, places=6)
        self.assertTrue(ev.meets_threshold)
        self.assertEqual(decision.direction, "downgraded")

    def test_weighted_quality_exactly_at_bar_counts(self):
        """quality_bar's gate is also >=: five fresh outcomes scoring EXACTLY
        the retention bar (frontier_reference * 95%) must still qualify,
        not be treated as just missing it."""
        self._seed_frontier_reference()  # frontier_reference_quality == 0.97
        bar = 0.97 * (learned.QUALITY_RETENTION_TARGET_PCT / 100)
        for _ in range(5):
            self._log("t1", "claude-haiku-4-5", bar, created_at=NOW.isoformat())
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        ev = next(e for e in decision.evidence if e.tier == "budget")
        self.assertAlmostEqual(ev.weighted_quality, ev.quality_bar, places=6)
        self.assertTrue(ev.meets_threshold)
        self.assertEqual(decision.direction, "downgraded")

    def test_chronological_cutoff_hides_future_evidence(self):
        """Evidence logged in a run_group at or after the cutoff must not be
        visible — a learned router that peeks at the future is not testing
        what it claims to."""
        self._seed_frontier_reference()
        for _ in range(5):
            self._log("t1", "claude-haiku-4-5", 0.96, created_at=NOW.isoformat(), run_group="z-future")
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "unchanged", "future run_group evidence leaked into the decision")


class TestSafetyAsymmetry(_DbCase):
    def test_thin_evidence_never_routes_cheaper(self):
        """The one hard rule stated twice in the design doc: absence of
        strong evidence must never be read as a green light to go cheaper."""
        for _ in range(2):
            self._log("t1", "claude-haiku-4-5", 0.99, created_at=NOW.isoformat())
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.chosen_tier, "frontier")
        self.assertNotEqual(decision.direction, "downgraded")

    def test_one_recent_failure_escalates_even_with_lots_of_good_evidence(self):
        """The asymmetry: n>=5 good outcomes is required to go cheaper, but
        a single recent, clear failure is enough to escalate — even amid 20
        good outcomes that would otherwise clear the downgrade bar easily."""
        for _ in range(3):
            self._log("t1", "claude-opus-4-8", 0.97, created_at=NOW.isoformat())
        for _ in range(20):
            self._log("t1", "claude-haiku-4-5", 0.97, created_at=NOW.isoformat())
        self._log("t1", "claude-haiku-4-5", 0.1, success=False, created_at=NOW.isoformat())

        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "escalated")
        self.assertEqual(decision.chosen_tier, "mid")

    def test_stale_failure_does_not_escalate(self):
        """A concerning outcome so old its decay weight has fallen below
        CONCERN_MIN_WEIGHT must not trigger escalation forever."""
        for _ in range(3):
            self._log("t1", "claude-opus-4-8", 0.97, created_at=NOW.isoformat())
        for _ in range(5):
            self._log("t1", "claude-haiku-4-5", 0.97, created_at=NOW.isoformat())
        ancient = (NOW - timedelta(days=365 * 5)).isoformat()
        self._log("t1", "claude-haiku-4-5", 0.0, success=False, created_at=ancient)

        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "downgraded")
        self.assertEqual(decision.chosen_tier, "budget")


class TestNullCreatedAtDecay(_DbCase):
    def test_null_created_at_rows_do_not_crash_evaluation(self):
        for _ in range(3):
            self._log("t1", "claude-opus-4-8", 0.97, created_at=NOW.isoformat())
        for _ in range(5):
            self._log_legacy_null_created_at("t1", "claude-haiku-4-5", 0.96)
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertIsInstance(decision, learned.LearnedDecision)

    def test_null_created_at_failure_does_not_escalate(self):
        """Maximally stale must mean maximally stale in BOTH directions: an
        undated (pre-v3) failure row must not escalate a bucket forever, the
        same way undated successes can't justify a downgrade."""
        for _ in range(3):
            self._log("t1", "claude-opus-4-8", 0.97, created_at=NOW.isoformat())
        for _ in range(5):
            self._log("t1", "claude-haiku-4-5", 0.97, created_at=NOW.isoformat())
        self._log_legacy_null_created_at("t1", "claude-haiku-4-5", 0.0, success=False)
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "downgraded")
        self.assertEqual(decision.chosen_tier, "budget")

    def test_null_created_at_evidence_is_too_decayed_to_qualify(self):
        """Five NULL-timestamp outcomes pass the raw n>=5 count but must
        fail the effective-n (decay-weighted) gate — pre-v3 rows (which are
        all NULL) must not be trusted as if they were fresh."""
        for _ in range(3):
            self._log("t1", "claude-opus-4-8", 0.97, created_at=NOW.isoformat())
        for _ in range(5):
            self._log_legacy_null_created_at("t1", "claude-haiku-4-5", 0.99)
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        self.assertEqual(decision.direction, "unchanged")


class TestLearnedDecisionSerialization(_DbCase):
    def test_to_log_dict_round_trips_through_json(self):
        import json

        for _ in range(3):
            self._log("t1", "claude-opus-4-8", 0.97, created_at=NOW.isoformat())
        for _ in range(5):
            self._log("t1", "claude-haiku-4-5", 0.96, created_at=NOW.isoformat())
        task = {"id": "t1", "prompt": EXTRACTION_PROMPT}
        decision = learned.evaluate(
            task, "extraction", "hard", roster_name="claude_tiers",
            before_run_group="z", db_path=self.db_path, task_registry=REGISTRY, now=NOW,
        )
        payload = json.dumps(decision.to_log_dict())
        restored = json.loads(payload)
        self.assertEqual(restored["chosen_tier"], "budget")
        self.assertEqual(restored["direction"], "downgraded")


if __name__ == "__main__":
    unittest.main()
