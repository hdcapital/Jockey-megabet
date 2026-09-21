"""The place engine against brute-force enumeration of every finishing order.

This is the test that matters most. The vectorised engine is a compact piece
of tensor arithmetic, and compact tensor arithmetic is exactly where an index
slip produces numbers that look plausible and are wrong. So the engine is
checked against a completely independent implementation: enumerate every
permutation of the field, weight each one by the model's own sequential
probabilities, and add up the orderings in which a runner finishes in the
paying positions.
"""

from __future__ import annotations

from itertools import permutations
from math import factorial

import numpy as np
import pytest

from app.place_model import (
    place_probabilities,
    place_probabilities_batch,
    place_probabilities_loop,
    places_for_field,
)

PARAM_SETS = [(1.0, 1.0), (0.71, 0.70), (0.76, 0.62), (0.5, 0.9), (0.9, 0.4)]

FIELDS = [
    [0.4, 0.3, 0.2, 0.1],
    [0.3, 0.25, 0.2, 0.15, 0.1],
    [0.35, 0.2, 0.15, 0.12, 0.1, 0.08],
    [0.30, 0.20, 0.15, 0.12, 0.10, 0.08, 0.05],
    [1 / 6] * 6,
]


def brute_force_place(q, places: int, lam: float, tau: float):
    """P(place) by enumerating every finishing order. O(n!) — small n only."""
    q = np.asarray(q, dtype=float)
    q = q / q.sum()
    n = q.size
    a = q**lam
    b = q**tau
    A, B = a.sum(), b.sum()
    out = np.zeros(n)
    for order in permutations(range(n)):
        j = order[0]
        p = q[j]
        if places >= 2:
            i = order[1]
            p *= a[i] / (A - a[j])
        if places >= 3:
            k = order[2]
            p *= b[k] / (B - b[j] - b[i])
        # Every permutation sharing the same first ``places`` finishers is
        # counted once by weighting with the remaining orderings' count.
        remaining = factorial(n - places)
        for slot in order[:places]:
            out[slot] += p / remaining
    return out


@pytest.mark.parametrize("q", FIELDS)
@pytest.mark.parametrize("lam,tau", PARAM_SETS)
@pytest.mark.parametrize("places", [2, 3])
def test_matches_brute_force(q, lam, tau, places):
    if len(q) <= places:
        pytest.skip("field too small for these terms")
    got = place_probabilities(q, places, lam, tau).p_place
    want = brute_force_place(q, places, lam, tau)
    assert np.allclose(got, want, atol=1e-12), f"{got} != {want}"


@pytest.mark.parametrize("q", FIELDS)
@pytest.mark.parametrize("lam,tau", PARAM_SETS)
@pytest.mark.parametrize("places", [2, 3])
def test_sums_to_number_of_places(q, lam, tau, places):
    if len(q) <= places:
        pytest.skip("field too small for these terms")
    result = place_probabilities(q, places, lam, tau)
    result.check_sum(tol=1e-10)
    assert abs(sum(result.p_place) - places) < 1e-10


@pytest.mark.parametrize("q", FIELDS)
@pytest.mark.parametrize("places", [2, 3])
def test_lam_tau_one_is_plain_harville(q, places):
    """lam = tau = 1 must reproduce the closed-form Harville expression."""
    if len(q) <= places:
        pytest.skip("field too small")
    arr = np.asarray(q, dtype=float)
    arr = arr / arr.sum()
    n = arr.size
    want = np.zeros(n)
    for j in range(n):
        want[j] += arr[j]
        for i in range(n):
            if i == j:
                continue
            p2 = arr[j] * arr[i] / (1 - arr[j])
            want[i] += p2
            if places < 3:
                continue
            for k in range(n):
                if k in (i, j):
                    continue
                want[k] += p2 * arr[k] / (1 - arr[j] - arr[i])
    got = place_probabilities(q, places, 1.0, 1.0).p_place
    assert np.allclose(got, want, atol=1e-12)


@pytest.mark.parametrize("q", FIELDS)
@pytest.mark.parametrize("lam,tau", PARAM_SETS)
@pytest.mark.parametrize("places", [2, 3])
def test_vectorised_equals_loop(q, lam, tau, places):
    if len(q) <= places:
        pytest.skip("field too small")
    got = place_probabilities(q, places, lam, tau).p_place
    want = place_probabilities_loop(q, places, lam, tau)
    assert np.allclose(got, want, atol=1e-12)


def test_batch_handles_padding_and_mixed_terms():
    """A padded batch of different-sized races must match one-at-a-time."""
    races = [
        ([0.4, 0.25, 0.2, 0.1, 0.05], 2),
        ([0.3, 0.2, 0.15, 0.12, 0.1, 0.08, 0.05], 3),
        ([0.25] * 4 + [0.0], 2),
    ]
    max_n = 7
    q = np.zeros((3, max_n))
    mask = np.zeros((3, max_n), dtype=bool)
    places = np.zeros(3, dtype=int)
    singles = []
    for r, (probs, pl) in enumerate(races):
        probs = [p for p in probs if p > 0]
        n = len(probs)
        arr = np.array(probs) / sum(probs)
        q[r, :n] = arr
        mask[r, :n] = True
        places[r] = pl
        singles.append(place_probabilities(arr, pl, 0.71, 0.70).p_place)
    batch = place_probabilities_batch(q, mask, places, 0.71, 0.70)
    for r, single in enumerate(singles):
        assert np.allclose(batch[r, : len(single)], single, atol=1e-12)
        assert np.allclose(batch[r, len(single):], 0.0)
        assert abs(batch[r].sum() - places[r]) < 1e-10


def test_discount_moves_probability_off_the_favourite():
    """The whole point: plain Harville over-states a favourite's place chance."""
    q = [0.5, 0.2, 0.12, 0.08, 0.06, 0.04]
    harville = place_probabilities(q, 3, 1.0, 1.0).p_place
    discounted = place_probabilities(q, 3, 0.71, 0.70).p_place
    assert discounted[0] < harville[0]
    assert discounted[-1] > harville[-1]


@pytest.mark.parametrize(
    "n,expected", [(4, 0), (5, 2), (7, 2), (8, 3), (12, 3), (24, 3)]
)
def test_places_for_field(n, expected):
    assert places_for_field(n) == expected


def test_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        place_probabilities([0.5, 0.5], 3, 0.71, 0.70)      # too few runners
    with pytest.raises(ValueError):
        place_probabilities([0.5, 0.3, 0.2], 4, 0.71, 0.70)  # unsupported terms
    with pytest.raises(ValueError):
        place_probabilities([0.5, 0.0, 0.5], 2, 0.71, 0.70)  # a zero probability
