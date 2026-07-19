"""Tests for router.gates — hard capability constraints."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import gates  # noqa: E402
from router.config import get_model  # noqa: E402


class TestTokenEstimate(unittest.TestCase):
    def test_estimate_scales_with_length(self):
        short = gates.estimate_prompt_tokens("hello")
        long = gates.estimate_prompt_tokens("hello " * 1000)
        self.assertLess(short, long)

    def test_estimate_is_pessimistic(self):
        """The estimate must be biased HIGH (fewer chars/token than English
        averages), because over-estimating causes a needless escalation while
        under-estimating causes a context-overflow API failure."""
        text = "a" * 4000  # ~1000 tokens at the common 4-chars/token rule
        self.assertGreater(gates.estimate_prompt_tokens(text), 1000)


class TestContextGate(unittest.TestCase):
    def test_short_prompt_fits_everywhere(self):
        for model in ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"):
            self.assertTrue(gates.fits_context(model, "What is 2 + 2?"))

    def test_oversized_prompt_gated_off_haiku_but_not_opus(self):
        """The load-bearing case: Haiku's 200K context is 5x smaller than its
        stablemates'. A prompt in that gap is routable to Sonnet/Opus and
        physically impossible on Haiku, at any difficulty or effort."""
        # ~250K tokens: over Haiku's usable budget, well under Opus's.
        prompt = "word " * 200_000
        self.assertFalse(gates.fits_context("claude-haiku-4-5", prompt))
        self.assertTrue(gates.fits_context("claude-opus-4-8", prompt))

    def test_gate_reserves_headroom_for_output(self):
        """A prompt that exactly fills the context window leaves no room to
        answer, so it must NOT pass the gate."""
        window = get_model("claude-haiku-4-5").context_window_tokens
        prompt = "a" * int(window * 3.5)  # ~= window tokens, 100% full
        self.assertFalse(gates.fits_context("claude-haiku-4-5", prompt))

    def test_check_reports_reason_on_failure(self):
        result = gates.check("claude-haiku-4-5", "word " * 200_000)
        self.assertFalse(result.eligible)
        self.assertIn("exceeds", result.reason)

    def test_prompt_exactly_at_usable_budget_fits(self):
        """Boundary case: fits_context compares with <=, so a prompt whose
        estimated token count lands EXACTLY on the usable-budget line must
        still pass — the gate is pessimistic about the estimate, not about
        the comparison operator."""
        window = get_model("claude-haiku-4-5").context_window_tokens
        budget = window * 0.70  # _INPUT_BUDGET_FRACTION
        # 489997 chars -> estimate_prompt_tokens == int(budget) exactly.
        prompt = "a" * 489_997
        self.assertEqual(gates.estimate_prompt_tokens(prompt), int(budget))
        self.assertTrue(gates.fits_context("claude-haiku-4-5", prompt))

    def test_prompt_one_estimated_token_over_budget_does_not_fit(self):
        """The other side of the same boundary: crossing the usable-budget
        line by a single estimated token must flip the gate to ineligible,
        not just 'close enough'."""
        window = get_model("claude-haiku-4-5").context_window_tokens
        budget = window * 0.70
        prompt = "a" * 490_000
        self.assertEqual(gates.estimate_prompt_tokens(prompt), int(budget) + 1)
        self.assertFalse(gates.fits_context("claude-haiku-4-5", prompt))

    def test_check_reports_reason_for_unsupported_effort_not_just_context(self):
        """check() must surface the EFFORT reason (not silently reuse the
        context-fit message) when a short prompt fits fine but the
        (model, effort) pairing itself is the failure."""
        result = gates.check("claude-haiku-4-5", "What is 2 + 2?", effort="high")
        self.assertFalse(result.eligible)
        self.assertIn("does not support effort", result.reason)
        self.assertNotIn("exceeds", result.reason)


class TestEffortGate(unittest.TestCase):
    def test_none_effort_supported_everywhere(self):
        """effort=None means 'send no config', which every model accepts."""
        for model in ("claude-haiku-4-5", "gpt-4o-mini", "claude-opus-4-8"):
            self.assertTrue(gates.effort_supported(model, None))

    def test_haiku_rejects_any_real_effort(self):
        """Haiku 4.5 predates output_config.effort and 400s on it."""
        for effort in ("off", "low", "high", "max"):
            self.assertFalse(gates.effort_supported("claude-haiku-4-5", effort))

    def test_sonnet_and_opus_accept_effort(self):
        for model in ("claude-sonnet-5", "claude-opus-4-8"):
            self.assertTrue(gates.effort_supported(model, "high"))

    def test_non_anthropic_models_reject_effort(self):
        for model in ("gpt-4o-mini", "deepseek-chat"):
            self.assertFalse(gates.effort_supported(model, "low"))


class TestEligibleModels(unittest.TestCase):
    def test_filters_to_passing_models(self):
        eligible = gates.eligible_models(
            "word " * 200_000,
            candidates=["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"],
        )
        self.assertNotIn("claude-haiku-4-5", eligible)
        self.assertIn("claude-opus-4-8", eligible)

    def test_preserves_input_order(self):
        candidates = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"]
        self.assertEqual(gates.eligible_models("hi", candidates=candidates), candidates)


if __name__ == "__main__":
    unittest.main()
