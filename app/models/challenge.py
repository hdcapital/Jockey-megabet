"""Jockey Challenge (most winners at a meeting) via Monte Carlo simulation.

Inputs are the per-race fair win probabilities of each jockey's ride (from
the de-vigged win markets). Each simulated meeting samples one winner per
race — so two jockeys are correctly *negatively* correlated (they cannot
both win the same race) — then counts wins per jockey.

Settlement model (documented assumption, recorded with every valuation):
"most winners, dead-heats divided". If Sportsbet settles its challenge on a
points system (e.g. 3-2-1 for placings) rather than raw wins, these fair
values do NOT apply — the assumption is stored so backtests can test it.

The RNG is seeded, so a stored result is exactly reproducible from the
stored per-race probabilities plus (seed, n_sims).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

DEFAULT_SIMS = 100_000
DEFAULT_SEED = 1701

OTHER = "__other__"  # aggregate for jockeys not individually priced


@dataclass(frozen=True)
class ChallengeResult:
    # competitor -> dead-heat-adjusted probability of winning the challenge
    probabilities: dict[str, float]
    other_probability: float  # P(a non-listed jockey has the most wins)
    no_winner_probability: float  # P(zero wins recorded at all)
    n_sims: int
    seed: int


def simulate_most_wins(
    race_probs: list[dict[str, float]],
    n_sims: int = DEFAULT_SIMS,
    seed: int = DEFAULT_SEED,
) -> ChallengeResult:
    """Simulate the meeting; race_probs[i] maps jockey -> P(win race i).

    Within each race the probabilities must not exceed 1; residual mass is
    "no listed jockey wins this race" (an unmodelled runner/jockey wins).
    """
    competitors: set[str] = set()
    for rp in race_probs:
        total = sum(rp.values())
        if total > 1.0 + 1e-9:
            raise ValueError(f"race win probabilities sum to {total:.4f} > 1")
        competitors.update(rp)
    rng = random.Random(seed)
    shares: dict[str, float] = {c: 0.0 for c in competitors}
    other_share = 0.0
    no_winner = 0

    # Precompute cumulative distributions per race.
    cum: list[list[tuple[float, str]]] = []
    for rp in race_probs:
        acc, dist = 0.0, []
        for name, p in rp.items():
            acc += p
            dist.append((acc, name))
        cum.append(dist)

    for _ in range(n_sims):
        counts: dict[str, int] = {}
        other_count = 0
        for dist in cum:
            u = rng.random()
            winner = None
            for edge, name in dist:
                if u < edge:
                    winner = name
                    break
            if winner is None:
                other_count += 1  # a jockey outside the modelled set won
            else:
                counts[winner] = counts.get(winner, 0) + 1
        best = max(counts.values(), default=0)
        # 'other' is many different jockeys, not one competitor; count it as
        # beating the field only when no listed jockey wins a race at all —
        # conservative, and flagged via other_probability for transparency.
        if best == 0:
            if other_count > 0:
                other_share += 1.0
            else:
                no_winner += 1
            continue
        leaders = [c for c, n in counts.items() if n == best]
        for c in leaders:
            shares[c] += 1.0 / len(leaders)

    return ChallengeResult(
        probabilities={c: s / n_sims for c, s in shares.items()},
        other_probability=other_share / n_sims,
        no_winner_probability=no_winner / n_sims,
        n_sims=n_sims,
        seed=seed,
    )
