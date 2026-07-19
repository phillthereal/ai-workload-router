"""
Tests for router.adapters.cache — the record/replay disk cache's key scheme.

THE LOAD-BEARING GUARANTEE (see cache._cache_key's docstring): the key must
change whenever any request field that changes the response changes, EXCEPT
that the (effort=None, max_tokens=None) case degrades to the exact v1 legacy
key so previously-recorded, already-published cache entries stay valid.
effort=None ("send no thinking config") and effort="off" ("explicitly disable
thinking") are different requests to the API (see
router.adapters.anthropic_adapter) and so MUST hash to different keys, even
though both eventually mean "no visible thinking" — conflating them would
silently replay one request shape's cached answer for the other.

All tests here point CACHE_DIR at a tmp directory so they never touch (or
depend on) the real gitignored `.cache/` populated by live runs.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.adapters import cache  # noqa: E402
from router.adapters.base import Response  # noqa: E402


class TestCacheKeyStability(unittest.TestCase):
    def test_default_key_matches_v1_legacy_scheme(self):
        """effort=None, max_tokens=None (the v1 request shape) must degrade
        to the original sha256(model\\nprompt) key so every already-recorded
        v1 cache entry stays a hit after the effort/max_tokens plumbing was
        added."""
        import hashlib

        model, prompt = "claude-haiku-4-5", "What is 2 + 2?"
        legacy = hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()
        self.assertEqual(cache._cache_key(model, prompt), legacy)
        self.assertEqual(cache._cache_key(model, prompt, None, None), legacy)

    def test_effort_off_key_differs_from_effort_none_key(self):
        """The core distinction this module exists to preserve: None ('send
        nothing') and 'off' ('explicitly disable') are different requests
        and must never collide on the same cache file."""
        model, prompt = "claude-sonnet-5", "Summarize this."
        key_none = cache._cache_key(model, prompt, None, None)
        key_off = cache._cache_key(model, prompt, "off", None)
        self.assertNotEqual(key_none, key_off)

    def test_effort_levels_each_get_a_distinct_key(self):
        model, prompt = "claude-sonnet-5", "Summarize this."
        keys = {cache._cache_key(model, prompt, e, None) for e in ("off", "low", "high", "max")}
        self.assertEqual(len(keys), 4, "distinct effort levels collided on the same cache key")

    def test_max_tokens_changes_the_key_even_with_effort_none(self):
        model, prompt = "claude-haiku-4-5", "What is 2 + 2?"
        key_default = cache._cache_key(model, prompt, None, None)
        key_capped = cache._cache_key(model, prompt, None, 32)
        self.assertNotEqual(key_default, key_capped)

    def test_key_is_deterministic_for_identical_inputs(self):
        model, prompt = "gpt-4o-mini", "Classify this."
        self.assertEqual(
            cache._cache_key(model, prompt, "high", 100),
            cache._cache_key(model, prompt, "high", 100),
        )


class TestCacheRoundTrip(unittest.TestCase):
    """save_cache/load_cached against a tmp CACHE_DIR, keyed through the same
    effort/max_tokens-sensitive scheme."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = patch.object(cache, "CACHE_DIR", Path(self._tmp.name))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_round_trip_preserves_response_fields(self):
        response = Response(
            text="4", input_tokens=10, output_tokens=2, latency_ms=123.0,
            model="claude-haiku-4-5", simulated=False, success=True, effort=None,
        )
        cache.save_cache("claude-haiku-4-5", "What is 2 + 2?", response)
        loaded = cache.load_cached("claude-haiku-4-5", "What is 2 + 2?")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.text, "4")
        self.assertEqual(loaded.input_tokens, 10)
        self.assertTrue(loaded.success)
        self.assertFalse(loaded.simulated)  # replays are marked real, not simulated

    def test_effort_none_and_effort_off_do_not_share_a_cache_slot(self):
        """Saving a response under effort='off' must not satisfy a later
        lookup for the SAME prompt under effort=None (or vice versa) — they
        are different requests per the module docstring."""
        response = Response(
            text="thought-off answer", input_tokens=10, output_tokens=2,
            latency_ms=50.0, model="claude-sonnet-5", simulated=False,
            success=True, effort="off",
        )
        cache.save_cache("claude-sonnet-5", "Summarize this.", response, effort="off")

        self.assertIsNone(cache.load_cached("claude-sonnet-5", "Summarize this.", effort=None))
        hit = cache.load_cached("claude-sonnet-5", "Summarize this.", effort="off")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.text, "thought-off answer")

    def test_cache_miss_returns_none_not_an_error(self):
        self.assertIsNone(cache.load_cached("claude-opus-4-8", "never cached"))


if __name__ == "__main__":
    unittest.main()
