"""Edge-triggered rule engine: a steady state must not re-alert."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

import monitor  # noqa: E402


def reading(value=None, raw_text=None, in_stock=None):
    return {"value": value, "raw_text": raw_text, "in_stock": in_stock}


class TestEvaluate(unittest.TestCase):
    def test_below_fires_once_on_crossing(self):
        prev = reading(value=25.0)
        curr = reading(value=19.0)
        fired, msg = monitor.evaluate("below", prev, curr, 20.0, None)
        self.assertTrue(fired)

    def test_below_does_not_refire_while_still_below(self):
        prev = reading(value=19.0)
        curr = reading(value=18.0)
        fired, _ = monitor.evaluate("below", prev, curr, 20.0, None)
        self.assertFalse(fired)

    def test_back_in_stock_edge(self):
        prev = reading(in_stock=0)
        curr = reading(in_stock=1)
        fired, _ = monitor.evaluate("back_in_stock", prev, curr, None, None)
        self.assertTrue(fired)
        # Still in stock next pass: no repeat alert.
        fired2, _ = monitor.evaluate("back_in_stock", curr, reading(in_stock=1),
                                     None, None)
        self.assertFalse(fired2)

    def test_contains_keyword_appears(self):
        prev = reading(raw_text="sold out")
        curr = reading(raw_text="buy now, available")
        fired, _ = monitor.evaluate("contains", prev, curr, None, "available")
        self.assertTrue(fired)

    def test_first_reading_below_fires_then_goes_quiet(self):
        # First observation already below threshold: alert (we just learned it).
        fired, _ = monitor.evaluate("below", None, reading(value=5.0), 20.0, None)
        self.assertTrue(fired)
        # Next pass still below: edge-triggered, so no repeat.
        fired2, _ = monitor.evaluate("below", reading(value=5.0),
                                     reading(value=4.0), 20.0, None)
        self.assertFalse(fired2)

    def test_decrease_needs_a_previous_reading(self):
        # A relative rule cannot fire without a baseline to compare against.
        fired, _ = monitor.evaluate("decrease", None, reading(value=5.0), 1.0, None)
        self.assertFalse(fired)


if __name__ == "__main__":
    unittest.main()
