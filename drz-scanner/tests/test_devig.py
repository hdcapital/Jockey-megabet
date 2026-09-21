"""De-vig behaviour, including the property this whole tool depends on."""

from __future__ import annotations

import numpy as np
import pytest

from app.calibration import Calibration
from app.devig import beta_probabilities, power_devig, proportional_devig
from app.winprob import (
    WIN_MODEL_BETA,
    WIN_MODEL_POWER,
    sportsbet_win_probabilities,
)

# A realistic over-round Australian book: short favourite, long tail.
BOOK = [2.4, 4.0, 6.5, 9.0, 13.0, 21.0, 34.0, 51.0]


def test_power_sums_to_one():
    res = power_devig(BOOK)
    assert abs(sum(res.probabilities) - 1.0) < 1e-12
    assert res.overround > 1.0
    assert res.exponent > 1.0


def test_power_preserves_order():
    res = power_devig(BOOK)
    probs = list(res.probabilities)
    assert probs == sorted(probs, reverse=True)


def test_power_moves_probability_from_longshots_to_favourites():
    """The favourite-longshot-bias fix, stated as a test.

    Proportional de-vig scales every implied probability by the same number,
    so the longshot keeps its share of a margin it never had. Power de-vig
    must give the favourite *more* and the longshot *less*.
    """
    prop = proportional_devig(BOOK).probabilities
    pow_ = power_devig(BOOK).probabilities
    assert pow_[0] > prop[0], "favourite should gain under power de-vig"
    assert pow_[-1] < prop[-1], "longshot should lose under power de-vig"
    # And the effect should be monotone across the book.
    ratios = [p / q for p, q in zip(pow_, prop)]
    assert ratios == sorted(ratios, reverse=True)


def test_power_on_a_fair_book_is_near_identity():
    fair = [1 / p for p in (0.5, 0.3, 0.12, 0.08)]
    res = power_devig(fair)
    assert np.allclose(res.probabilities, [0.5, 0.3, 0.12, 0.08], atol=1e-6)


@pytest.mark.parametrize("bad", [[1.0, 2.0], [0.5, 3.0], [2.0, float("nan")]])
def test_rejects_invalid_odds(bad):
    with pytest.raises(ValueError):
        power_devig(bad)


def test_beta_recovers_a_known_exponent_on_simulated_races():
    """Simulate races whose winners follow p ~ raw**B, then recover B."""
    from scipy.optimize import minimize_scalar

    rng = np.random.default_rng(20260918)
    true_beta = 1.18
    n_races, n_runners = 4000, 9

    raw = rng.uniform(0.02, 0.45, size=(n_races, n_runners))
    p = raw**true_beta
    p = p / p.sum(axis=1, keepdims=True)
    winners = np.array([rng.choice(n_runners, p=row) for row in p])
    won = np.zeros((n_races, n_runners), dtype=bool)
    won[np.arange(n_races), winners] = True

    def nll(b: float) -> float:
        q = raw**b
        q = q / q.sum(axis=1, keepdims=True)
        return float(-np.log(np.maximum(q[won], 1e-300)).sum())

    fitted = minimize_scalar(nll, bracket=(0.8, 1.0, 1.5)).x
    assert abs(fitted - true_beta) < 0.06, f"recovered {fitted}, wanted {true_beta}"


def test_beta_probabilities_match_the_functional_form():
    res = beta_probabilities(BOOK, 1.2)
    raw = np.array([1 / o for o in BOOK])
    want = raw**1.2
    want = want / want.sum()
    assert np.allclose(res.probabilities, want)


def test_model_selection_prefers_beta_once_enough_races():
    cal = Calibration(beta=1.12, beta_n_races=2000)
    chosen = sportsbet_win_probabilities(BOOK, cal, beta_min_races=1500)
    assert chosen.win_model == WIN_MODEL_BETA
    assert not chosen.uncalibrated


def test_model_selection_falls_back_to_power_and_flags_uncalibrated():
    cal = Calibration(beta=1.12, beta_n_races=400)
    chosen = sportsbet_win_probabilities(BOOK, cal, beta_min_races=1500)
    assert chosen.win_model == WIN_MODEL_POWER
    assert chosen.uncalibrated
    assert "400" in chosen.detail

    chosen = sportsbet_win_probabilities(BOOK, Calibration(), beta_min_races=1500)
    assert chosen.win_model == WIN_MODEL_POWER
    assert "no fitted beta" in chosen.detail
