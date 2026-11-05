"""Request shapes and gates for the newest Claude models, which differ on turning thinking off."""
import unittest

from router import gates
from router.adapters.anthropic_adapter import AnthropicAdapter
from router.config import EFFORT_OFF, MODEL_TIER, MODELS


class TestThinkingOff(unittest.TestCase):
    def test_sonnet_5_5_uses_between_tools(self):
        kwargs = AnthropicAdapter("claude-sonnet-5-5", "k")._request_kwargs(EFFORT_OFF, None)
        self.assertEqual(kwargs["thinking"], {"type": "between_tools"})

    def test_other_models_keep_disabled(self):
        for model in ("claude-haiku-5-5", "claude-sonnet-5", "claude-opus-4-8"):
            kwargs = AnthropicAdapter(model, "k")._request_kwargs(EFFORT_OFF, None)
            self.assertEqual(kwargs["thinking"], {"type": "disabled"})

    def test_opus_5_5_cannot_turn_thinking_off(self):
        self.assertFalse(gates.effort_supported("claude-opus-5-5", EFFORT_OFF))
        self.assertTrue(gates.effort_supported("claude-opus-5-5", "low"))

    def test_every_model_has_a_tier(self):
        self.assertEqual(set(MODELS) - set(MODEL_TIER), set())


if __name__ == "__main__":
    unittest.main()
