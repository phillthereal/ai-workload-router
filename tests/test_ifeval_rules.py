import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import check_ifeval_rules as c  # noqa: E402


class RuleChecks(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(c.check("punctuation:no_comma", {}, "a b"))
        self.assertFalse(c.check("punctuation:no_comma", {}, "a, b"))
        rule = {"letter": "n", "let_relation": "less than", "let_frequency": 2}
        self.assertTrue(c.check("keywords:letter_frequency", rule, "n"))
        self.assertFalse(c.check("keywords:letter_frequency", rule, "nn"))
        self.assertIsNone(c.check("language:response_language", {}, "hola"))

    def test_counters(self):
        self.assertEqual(c.sentences("One. Two! Three?"), 3)
        self.assertEqual(c.bullets("* a\n* b\n- c"), 3)
        self.assertEqual(c.highlights("*a* **b**"), 2)
        self.assertEqual(len(c.paragraphs("a\n***\nb\n***\nc")), 3)
        self.assertTrue(c.is_json('```json\n{"a":1}\n```'))
        self.assertTrue(c.two("a\n******\nb"))


if __name__ == "__main__":
    unittest.main()
