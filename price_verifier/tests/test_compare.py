"""Offline price-tolerance tests. Run:
  python -m unittest price_verifier.tests.test_compare
"""

from __future__ import annotations

import unittest

from price_verifier.pipeline.compare import prices_match


class ToleranceTests(unittest.TestCase):
    def test_exact_match_no_tolerance(self):
        self.assertTrue(prices_match(1499.0, 1499.0, tolerance_abs=0, tolerance_pct=0))

    def test_mismatch_no_tolerance(self):
        self.assertFalse(prices_match(1499.0, 1500.0, tolerance_abs=0, tolerance_pct=0))

    def test_within_absolute_tolerance(self):
        self.assertTrue(prices_match(1499.0, 1500.0, tolerance_abs=1.0, tolerance_pct=0))

    def test_outside_absolute_tolerance(self):
        self.assertFalse(prices_match(1499.0, 1501.0, tolerance_abs=1.0, tolerance_pct=0))

    def test_within_percent_tolerance(self):
        # 0.5% of 1000 = 5
        self.assertTrue(prices_match(1000.0, 1004.0, tolerance_abs=0, tolerance_pct=0.5))

    def test_outside_percent_tolerance(self):
        self.assertFalse(prices_match(1000.0, 1010.0, tolerance_abs=0, tolerance_pct=0.5))

    def test_either_tolerance_passing_is_enough(self):
        # Fails percent (way outside %), but within absolute
        self.assertTrue(prices_match(10.0, 10.9, tolerance_abs=1.0, tolerance_pct=0.1))

    def test_actual_lower_than_expected_still_compared_by_abs_diff(self):
        self.assertTrue(prices_match(1500.0, 1499.5, tolerance_abs=1.0, tolerance_pct=0))


if __name__ == "__main__":
    unittest.main()
