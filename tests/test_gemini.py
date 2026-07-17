"""
Tests for the Google Gemini addition: MODELS registry entry, adapter
resolution via the shared OpenAI-compatible HTTP adapter, and the
`cross_vendor_4` roster's widened price range vs `cross_vendor`.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.adapters import MockAdapter, get_adapter  # noqa: E402
from router.config import MODELS, get_roster  # noqa: E402


class TestGeminiModelConfig(unittest.TestCase):
    def test_gemini_in_models_with_google_provider(self):
        self.assertIn("gemini-2.0-flash", MODELS)
        model = MODELS["gemini-2.0-flash"]
        self.assertEqual(model.provider, "google")
        self.assertEqual(model.name, "gemini-2.0-flash")
        self.assertGreater(model.cost_per_1m_input_tokens, 0)
        self.assertGreater(model.cost_per_1m_output_tokens, 0)
        self.assertGreater(model.context_window_tokens, 0)


class TestGeminiAdapter(unittest.TestCase):
    def test_get_adapter_returns_mock_under_force_mock(self):
        os.environ["AWR_FORCE_MOCK"] = "1"
        try:
            adapter = get_adapter("gemini-2.0-flash")
            self.assertIsInstance(adapter, MockAdapter)
        finally:
            del os.environ["AWR_FORCE_MOCK"]


class TestCrossVendor4Roster(unittest.TestCase):
    def test_cross_vendor_4_widens_price_range(self):
        cross_vendor = get_roster("cross_vendor")
        cross_vendor_4 = get_roster("cross_vendor_4")
        self.assertGreater(cross_vendor_4.price_range(), cross_vendor.price_range())


if __name__ == "__main__":
    unittest.main()
