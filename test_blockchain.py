"""Exact schedule and payout arithmetic, without real Bitcoin services."""

import unittest

from blockchain import HALVING, MAX_SUPPLY, SATOSHIS, shares, subsidy


class ChainArithmeticTests(unittest.TestCase):
    def test_halving_boundaries_and_finite_integer_supply(self):
        self.assertEqual(subsidy(1), 50 * SATOSHIS)
        self.assertEqual(subsidy(HALVING), 50 * SATOSHIS)
        self.assertEqual(subsidy(HALVING + 1), 25 * SATOSHIS)
        self.assertEqual(subsidy(33 * HALVING), 1)
        self.assertEqual(subsidy(33 * HALVING + 1), 0)
        total = sum(subsidy(era * HALVING + 1) * HALVING for era in range(34))
        self.assertEqual(total, 2_099_999_997_690_000)
        self.assertLess(total, MAX_SUPPLY)
        for invalid in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                subsidy(invalid)

    def test_exact_proportions_remainders_and_ties(self):
        self.assertEqual(shares(10, {3: 1, 2: 1, 1: 1}), {3: 3, 2: 3, 1: 4})
        self.assertEqual(shares(7, {1: 1, 2: 2}), {1: 2, 2: 5})
        self.assertEqual(shares(50, {}), {})
        self.assertEqual(shares(50, {1: 0}), {})
        self.assertEqual(shares(0, {1: 100}), {1: 0})
        for amount in (1, 99, 5_000_000_000, MAX_SUPPLY):
            weights = {p: p ** 3 for p in range(1, 37)}
            result = shares(amount, weights)
            self.assertEqual(sum(result.values()), amount)
            total = sum(weights.values())
            self.assertTrue(all(abs(result[p] * total - amount * weights[p]) < total
                                for p in weights))


if __name__ == "__main__":
    unittest.main()
