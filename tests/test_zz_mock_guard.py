"""Runs last (alphabetical discovery). Fails if any earlier test left the suite
out of mock mode — a leak that silently routes later tests to live, paid APIs
whenever real keys are present (it happened: test_gemini once `del`eted the var).
"""

import os
import unittest


class TestMockModeStillOn(unittest.TestCase):
    def test_force_mock_survived_the_suite(self):
        self.assertEqual(os.environ.get("AWR_FORCE_MOCK"), "1")


if __name__ == "__main__":
    unittest.main()
