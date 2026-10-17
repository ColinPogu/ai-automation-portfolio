"""Per-run spending and delegation ceilings."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import limits  # noqa: E402


class _Usage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class TestRunBudget(unittest.TestCase):
    def test_message_ceiling_stops_the_run(self):
        b = limits.RunBudget(max_messages=2, max_usd=100, price_in_per_mtok=3,
                             price_out_per_mtok=15)
        b.check(); b.record_message()
        b.check(); b.record_message()
        with self.assertRaises(limits.BudgetExceeded):
            b.check()

    def test_dollar_ceiling_stops_the_run(self):
        b = limits.RunBudget(max_messages=1000, max_usd=0.01,
                             price_in_per_mtok=3, price_out_per_mtok=15)
        b.check()  # fine at zero spend
        b.record_usage(_Usage(1_000_000, 1_000_000))  # $3 + $15 = $18
        with self.assertRaises(limits.BudgetExceeded):
            b.check()

    def test_cost_math(self):
        b = limits.RunBudget(10, 100, 3, 15)
        b.record_usage(_Usage(500_000, 100_000))  # $1.5 + $1.5 = $3.0
        self.assertAlmostEqual(b.usd_spent, 3.0, places=6)

    def test_none_usage_is_safe(self):
        b = limits.RunBudget(10, 100, 3, 15)
        b.record_usage(None)
        self.assertEqual(b.usd_spent, 0.0)


if __name__ == "__main__":
    unittest.main()
