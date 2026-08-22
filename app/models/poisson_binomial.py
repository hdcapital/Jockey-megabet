"""Exact Poisson-binomial distribution of a jockey's daily win count.

Given independent win probabilities p_1..p_n for the jockey's rides, the
number of wins X follows a Poisson-binomial distribution. We compute the
exact probability mass function with the standard O(n^2) dynamic programme:

    dp_k(j) = P(exactly j wins among the first k rides)
    dp_k(j) = dp_{k-1}(j) * (1 - p_k) + dp_{k-1}(j-1) * p_k

This is numerically stable for realistic n (a jockey rarely has more than
~12 rides at a meeting) because it only ever adds and multiplies numbers in
[0, 1] — no subtractive cancellation.

The independence assumption is a modelling choice; see README ("Correlation
investigation") for how it is monitored rather than arbitrarily adjusted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WinDistribution:
    """PMF and survival function of the number of wins."""

    probabilities: tuple[float, ...]  # the input p_i, kept for auditability
    pmf: tuple[float, ...]  # pmf[j] = P(X = j), j = 0..n

    @property
    def n_rides(self) -> int:
        return len(self.probabilities)

    def prob_exactly(self, k: int) -> float:
        if k < 0 or k >= len(self.pmf):
            return 0.0
        return self.pmf[k]

    def prob_at_least(self, k: int) -> float:
        """P(X >= k). P(X >= 0) is 1 by construction; beyond n it is 0."""
        if k <= 0:
            return 1.0
        if k > self.n_rides:
            return 0.0
        return min(1.0, sum(self.pmf[k:]))

    def expected_wins(self) -> float:
        return sum(self.probabilities)


def poisson_binomial(probabilities: list[float] | tuple[float, ...]) -> WinDistribution:
    """Compute the exact Poisson-binomial PMF via dynamic programming."""
    for p in probabilities:
        if not (0.0 <= p <= 1.0):
            raise ValueError(f"win probability out of range [0, 1]: {p}")
    pmf = [1.0]
    for p in probabilities:
        q = 1.0 - p
        nxt = [0.0] * (len(pmf) + 1)
        for j, mass in enumerate(pmf):
            nxt[j] += mass * q
            nxt[j + 1] += mass * p
        pmf = nxt
    # Guard against accumulated floating-point drift.
    total = sum(pmf)
    if total > 0:
        pmf = [m / total for m in pmf]
    return WinDistribution(probabilities=tuple(probabilities), pmf=tuple(pmf))


def fair_odds(probability: float) -> float | None:
    """Reciprocal fair decimal odds; None when the event is impossible."""
    if probability <= 0.0:
        return None
    return 1.0 / probability


def expected_return(fair_probability: float, bookmaker_odds: float) -> float:
    """Expected profit per unit staked: p * odds - 1."""
    return fair_probability * bookmaker_odds - 1.0


def edge_pct(bookmaker_odds: float, fair: float) -> float:
    """(bookmaker odds / fair odds) - 1; identical to expected return."""
    return bookmaker_odds / fair - 1.0
