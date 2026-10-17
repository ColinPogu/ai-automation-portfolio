"""Schema and confidence handling for classifier output.

These prove the agent fails closed: anything that is not a well-formed
ROUTINE object with adequate confidence escalates to a human instead of
triggering an auto-draft.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import triage  # noqa: E402


class TestParseClassification(unittest.TestCase):
    def test_clean_routine(self):
        out = triage.parse_classification(
            '{"category": "ROUTINE", "reason": "simple question", "confidence": 0.9}'
        )
        self.assertEqual(out["category"], "ROUTINE")
        self.assertAlmostEqual(out["confidence"], 0.9)

    def test_routine_with_surrounding_prose(self):
        out = triage.parse_classification(
            'Sure! Here is the result:\n'
            '{"category":"ROUTINE","reason":"status check","confidence":0.8}\n'
            'Hope that helps.'
        )
        self.assertEqual(out["category"], "ROUTINE")

    def test_non_json_fails_closed(self):
        out = triage.parse_classification("I think this is routine, probably.")
        self.assertEqual(out["category"], "SENSITIVE")
        self.assertEqual(out["confidence"], 0.0)

    def test_invalid_json_fails_closed(self):
        out = triage.parse_classification('{"category": "ROUTINE", bad json')
        self.assertEqual(out["category"], "SENSITIVE")

    def test_json_array_is_not_an_object(self):
        # Valid JSON, but a list. Must not be treated as a classification.
        out = triage.parse_classification('["ROUTINE", 0.99]')
        self.assertEqual(out["category"], "SENSITIVE")

    def test_json_scalar_is_not_an_object(self):
        out = triage.parse_classification('{"x": 1} but really "ROUTINE"')
        # First brace group is {"x":1}: no category -> SENSITIVE.
        self.assertEqual(out["category"], "SENSITIVE")

    def test_unknown_category_fails_closed(self):
        out = triage.parse_classification(
            '{"category": "MAYBE", "confidence": 0.95}'
        )
        self.assertEqual(out["category"], "SENSITIVE")

    def test_garbage_confidence_becomes_zero(self):
        out = triage.parse_classification(
            '{"category": "ROUTINE", "confidence": "high"}'
        )
        self.assertEqual(out["confidence"], 0.0)

    def test_confidence_is_clamped(self):
        out = triage.parse_classification(
            '{"category": "ROUTINE", "confidence": 7}'
        )
        self.assertLessEqual(out["confidence"], 1.0)
        self.assertGreaterEqual(out["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
