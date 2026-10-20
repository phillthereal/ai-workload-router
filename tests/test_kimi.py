import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from unittest import mock

from router import gates
from router.adapters import _PROVIDER_BASE_URL, _PROVIDER_ENV_VAR, get_adapter
from router.adapters.cache import _cache_key
from router.adapters.openai_compatible import OpenAICompatibleAdapter, _billed_output_tokens
from router.api import Router, RulesPolicy
from router.config import DEFAULT_ROSTER, MODEL_TIER, ROSTERS, get_model


class KimiTests(unittest.TestCase):
    def test_config_facts(self):
        m = get_model("kimi-k3")
        self.assertEqual((m.cost_per_1m_input_tokens, m.cost_per_1m_output_tokens), (3.0, 15.0))
        self.assertEqual(m.context_window_tokens, 1_048_576)
        self.assertEqual(MODEL_TIER["kimi-k3"], "mid")
        self.assertEqual(_PROVIDER_ENV_VAR["moonshot"], "MOONSHOT_API_KEY")
        self.assertEqual(_PROVIDER_BASE_URL["moonshot"], "https://api.moonshot.ai/v1/chat/completions")

    def test_roster(self):
        r = ROSTERS["kimi_mid"]
        self.assertEqual((r.budget, r.mid, r.frontier), ("gpt-4o-mini", "kimi-k3", "claude-opus-4-8"))
        self.assertEqual(DEFAULT_ROSTER, "cross_vendor")

    def test_effort_gating(self):
        for e in ("low", "high", "max", None):
            self.assertTrue(gates.effort_supported("kimi-k3", e))
        for e in ("medium", "xhigh", "off"):
            self.assertFalse(gates.effort_supported("kimi-k3", e))
        self.assertTrue(gates.effort_supported("claude-opus-4-8", "xhigh"))
        self.assertFalse(gates.effort_supported("gpt-4o-mini", "low"))

    def test_request_body(self):
        a = OpenAICompatibleAdapter("kimi-k3", "k", "u", provider="moonshot")
        self.assertEqual(a.build_body("p", "low", None)["reasoning_effort"], "low")
        b = a.build_body("p", None, None)
        self.assertNotIn("reasoning_effort", b)
        self.assertNotIn("thinking", b)
        o = OpenAICompatibleAdapter("gpt-4o-mini", "k", "u", provider="openai")
        self.assertNotIn("reasoning_effort", o.build_body("p", "low", None))

    def test_usage_parse(self):
        self.assertEqual(_billed_output_tokens({"completion_tokens": 50}), 50)
        self.assertEqual(_billed_output_tokens(
            {"completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 40}}), 50)
        self.assertEqual(_billed_output_tokens(
            {"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 40}}), 50)

    def test_cache_key_per_effort(self):
        keys = {_cache_key("kimi-k3", "p", e, None) for e in ("low", "high", "max", None)}
        self.assertEqual(len(keys), 4)

    def test_router_mock(self):
        res = Router(RulesPolicy(use_labels=True), roster_name="kimi_mid").complete("Classify the sentiment of: I love this.")
        self.assertTrue(res.response.success)
        self.assertTrue(get_adapter("kimi-k3").complete("hi", effort="max").simulated)


if __name__ == "__main__":
    unittest.main()
