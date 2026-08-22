"""Poisson-binomial distribution tests.

All probability vectors here are SYNTHETIC TEST FIXTURES for validating the
mathematics — they are not real racing data and never appear in production
output.
"""

import itertools
import math

import pytest

from app.models.poisson_binomial import (
    edge_pct,
    expected_return,
    fair_odds,
    poisson_binomial,
)


def brute_force_pmf(probs: list[float]) -> list[float]:
    """Independent O(2^n) enumeration used to validate the DP implementation."""
    n = len(probs)
    pmf = [0.0] * (n + 1)
    for outcome in itertools.product([0, 1], repeat=n):
        p = 1.0
        for win, pi in zip(outcome, probs):
            p *= pi if win else (1.0 - pi)
        pmf[sum(outcome)] += p
    return pmf


# The seven-ride example vector from the specification (logic example only).
SPEC_EXAMPLE = [0.38, 0.24, 0.17, 0.135, 0.095, 0.30, 0.12]


class TestAgainstIndependentComputation:
    @pytest.mark.parametrize(
        "probs",
        [
            SPEC_EXAMPLE,
            [0.5],
            [0.9, 0.1],
            [0.2, 0.2, 0.2, 0.2],
            [0.01, 0.99, 0.5, 0.33, 0.66],
        ],
    )
    def test_pmf_matches_brute_force(self, probs):
        dist = poisson_binomial(probs)
        expected = brute_force_pmf(probs)
        assert len(dist.pmf) == len(probs) + 1
        for got, want in zip(dist.pmf, expected):
            assert got == pytest.approx(want, abs=1e-12)

    def test_spec_example_cumulative(self):
        dist = poisson_binomial(SPEC_EXAMPLE)
        bf = brute_force_pmf(SPEC_EXAMPLE)
        for k in range(len(SPEC_EXAMPLE) + 2):
            assert dist.prob_at_least(k) == pytest.approx(
                sum(bf[k:]) if k <= len(SPEC_EXAMPLE) else 0.0, abs=1e-12
            )


class TestDistributionProperties:
    def test_pmf_sums_to_one(self):
        dist = poisson_binomial(SPEC_EXAMPLE)
        assert sum(dist.pmf) == pytest.approx(1.0, abs=1e-12)

    def test_cumulative_monotonic(self):
        dist = poisson_binomial(SPEC_EXAMPLE)
        cum = [dist.prob_at_least(k) for k in range(len(SPEC_EXAMPLE) + 1)]
        assert all(a >= b - 1e-15 for a, b in zip(cum, cum[1:]))

    def test_at_least_zero_is_one(self):
        assert poisson_binomial([0.3, 0.4]).prob_at_least(0) == 1.0

    def test_beyond_n_is_zero(self):
        assert poisson_binomial([0.3, 0.4]).prob_at_least(3) == 0.0

    def test_expected_wins(self):
        dist = poisson_binomial(SPEC_EXAMPLE)
        mean_from_pmf = sum(j * p for j, p in enumerate(dist.pmf))
        assert dist.expected_wins() == pytest.approx(sum(SPEC_EXAMPLE))
        assert mean_from_pmf == pytest.approx(sum(SPEC_EXAMPLE), abs=1e-12)


class TestDegenerateCases:
    def test_no_rides(self):
        dist = poisson_binomial([])
        assert dist.pmf == (1.0,)
        assert dist.prob_at_least(0) == 1.0
        assert dist.prob_at_least(1) == 0.0

    def test_one_ride(self):
        dist = poisson_binomial([0.25])
        assert dist.prob_exactly(0) == pytest.approx(0.75)
        assert dist.prob_exactly(1) == pytest.approx(0.25)

    def test_all_zero(self):
        dist = poisson_binomial([0.0, 0.0, 0.0])
        assert dist.prob_exactly(0) == pytest.approx(1.0)
        assert dist.prob_at_least(1) == pytest.approx(0.0)

    def test_one_certain(self):
        dist = poisson_binomial([1.0, 0.5])
        assert dist.prob_at_least(1) == pytest.approx(1.0)
        assert dist.prob_exactly(0) == pytest.approx(0.0)

    def test_all_certain(self):
        dist = poisson_binomial([1.0, 1.0, 1.0])
        assert dist.prob_exactly(3) == pytest.approx(1.0)
        assert dist.prob_at_least(3) == pytest.approx(1.0)

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            poisson_binomial([0.5, 1.5])
        with pytest.raises(ValueError):
            poisson_binomial([-0.1])


class TestFairValueFormulas:
    def test_fair_odds_reciprocal(self):
        assert fair_odds(0.25) == pytest.approx(4.0)
        assert fair_odds(0.0) is None

    def test_expected_return(self):
        # p * odds - 1: at fair odds EV is exactly zero.
        assert expected_return(0.25, 4.0) == pytest.approx(0.0)
        assert expected_return(0.25, 5.0) == pytest.approx(0.25)
        assert expected_return(0.25, 3.0) == pytest.approx(-0.25)

    def test_edge_pct_equals_expected_return(self):
        p, odds = 0.31, 3.8
        assert edge_pct(odds, 1.0 / p) == pytest.approx(expected_return(p, odds))

    def test_numerical_stability_many_rides(self):
        probs = [0.05] * 40  # more rides than realistic, stresses the DP
        dist = poisson_binomial(probs)
        assert sum(dist.pmf) == pytest.approx(1.0, abs=1e-9)
        # Compare P(X=0) to the closed form (1-p)^n.
        assert dist.prob_exactly(0) == pytest.approx(0.95**40, rel=1e-9)
        assert not any(math.isnan(x) or x < 0 for x in dist.pmf)
