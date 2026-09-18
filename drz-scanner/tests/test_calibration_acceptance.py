"""The acceptance test: refit from real Betfair history and check the numbers.

Marked ``data`` because it needs the Betfair Data Scientists CSVs (cached
under ``data/history/`` after the first run, ~80 MB). Run it with::

    python -m pytest -m data

These targets are an independent result over Jan 2024 - Aug 2026 (44,856
races). Missing them by more than the stated tolerance means something in the
pipeline is broken, not that the world changed.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from app.calibration import Calibration, CalibrationError, write_calibration
from app.calibrate import (
    binary_log_loss,
    evaluate,
    fit_bucket_correction,
    fit_lam_tau,
    fit_win_exponent,
    place_bsp_log_loss,
    time_split,
    _subset,
)
from app.history import HistoryUnavailableError, load_history

pytestmark = pytest.mark.data

START = date(2024, 1, 1)
END = date(2026, 8, 31)
EXPECTED_RACES = 44856


@pytest.fixture(scope="module")
def arrays():
    try:
        return load_history(START, END)
    except HistoryUnavailableError as exc:
        pytest.skip(f"Betfair history unavailable: {exc}")


def test_filters_select_the_expected_race_count(arrays):
    assert arrays.n_races == pytest.approx(EXPECTED_RACES, rel=0.01), (
        f"expected ~{EXPECTED_RACES} races after the documented filters, "
        f"got {arrays.n_races}"
    )
    assert str(arrays.dates.min()) == "2024-01-01"
    assert str(arrays.dates.max()) == "2026-08-31"
    assert set(np.unique(arrays.places)) <= {2, 3}
    assert arrays.n_runners.min() >= 5
    assert np.allclose(arrays.q.sum(axis=1), 1.0)
    assert (arrays.won.sum(axis=1) == 1).all()


def test_bsp_win_exponent_is_one(arrays):
    """Betfair BSP needs no favourite-longshot correction."""
    beta = fit_win_exponent(arrays)
    assert beta == pytest.approx(1.01, abs=0.04), f"got {beta}"


def test_fitted_lam_tau_match_the_independent_result(arrays):
    split = time_split(arrays)
    lam, tau, _ = fit_lam_tau(_subset(arrays, split.train))
    assert lam == pytest.approx(0.71, abs=0.04), f"lam={lam}"
    assert tau == pytest.approx(0.70, abs=0.04), f"tau={tau}"


def test_out_of_sample_log_loss_beats_plain_harville(arrays):
    split = time_split(arrays)
    train, test = _subset(arrays, split.train), _subset(arrays, split.test)
    lam, tau, _ = fit_lam_tau(train)

    ll_model = binary_log_loss(evaluate(test, lam, tau), test.placed, test.mask)
    ll_harville = binary_log_loss(evaluate(test, 1.0, 1.0), test.placed, test.mask)
    assert ll_model == pytest.approx(0.4999, abs=0.004), f"model {ll_model}"
    assert ll_harville == pytest.approx(0.5062, abs=0.004), f"harville {ll_harville}"
    assert ll_model < ll_harville


def test_surface_is_flat_between_the_two_reported_optima(arrays):
    split = time_split(arrays)
    test = _subset(arrays, split.test)
    a = binary_log_loss(evaluate(test, 0.71, 0.70), test.placed, test.mask)
    b = binary_log_loss(evaluate(test, 0.76, 0.62), test.placed, test.mask)
    assert abs(a - b) < 5e-4, f"0.71/0.70 -> {a}, 0.76/0.62 -> {b}"


def test_plain_harville_overstates_the_top_bucket(arrays):
    """Harville's top place-probability bucket predicts ~0.94 and gets ~0.86."""
    split = time_split(arrays)
    test = _subset(arrays, split.test)
    p = evaluate(test, 1.0, 1.0)
    sel = test.mask & (p >= 0.9)
    assert sel.sum() > 500
    assert p[sel].mean() == pytest.approx(0.94, abs=0.02)
    assert test.placed[sel].mean() == pytest.approx(0.86, abs=0.03)


def test_the_exchange_place_market_slightly_beats_the_model(arrays):
    split = time_split(arrays)
    train, test = _subset(arrays, split.train), _subset(arrays, split.test)
    lam, tau, _ = fit_lam_tau(train)
    ll_model = binary_log_loss(evaluate(test, lam, tau), test.placed, test.mask)
    ll_bsp = place_bsp_log_loss(test, evaluate(test, lam, tau))
    assert ll_bsp == pytest.approx(0.4991, abs=0.004), f"place BSP {ll_bsp}"
    assert ll_bsp < ll_model, "the exchange should still be a touch sharper"


def test_high_win_probability_runners_are_over_predicted(arrays):
    """The known residual: q > 0.5 predicts ~0.89 place and gets ~0.86."""
    split = time_split(arrays)
    train, test = _subset(arrays, split.train), _subset(arrays, split.test)
    lam, tau, _ = fit_lam_tau(train)
    p = evaluate(test, lam, tau)
    sel = test.mask & (test.q > 0.5)
    assert sel.sum() > 500
    predicted, actual = p[sel].mean(), test.placed[sel].mean()
    assert predicted == pytest.approx(0.89, abs=0.02)
    assert actual == pytest.approx(0.86, abs=0.02)
    assert predicted - actual == pytest.approx(0.03, abs=0.02)


def test_bucket_correction_is_adopted_only_when_it_helps(arrays):
    split = time_split(arrays)
    train, test = _subset(arrays, split.train), _subset(arrays, split.test)
    lam, tau, _ = fit_lam_tau(train)
    correction = fit_bucket_correction(train, lam, tau)

    base = binary_log_loss(evaluate(test, lam, tau), test.placed, test.mask)
    corrected = binary_log_loss(
        evaluate(test, lam, tau, correction), test.placed, test.mask
    )
    # The correction is tiny but must not be adopted unless it genuinely helps.
    assert corrected <= base + 1e-9
    top = next(b for b in correction if b["lo"] == 0.5)
    assert top["delta"] < -0.015, "the q>0.5 bucket should be corrected downward"


def test_global_logistic_recalibration_does_not_help(arrays):
    """A single slope/intercept fit finds nothing: slope 1.00, intercept 0.00."""
    from scipy.optimize import minimize

    split = time_split(arrays)
    train = _subset(arrays, split.train)
    lam, tau, _ = fit_lam_tau(train)
    p = np.clip(evaluate(train, lam, tau)[train.mask], 1e-9, 1 - 1e-9)
    y = train.placed[train.mask].astype(float)
    z = np.log(p / (1 - p))

    def nll(ab):
        a, b = ab
        t = a + b * z
        return float(-(y * -np.logaddexp(0, -t) + (1 - y) * -np.logaddexp(0, t)).sum())

    res = minimize(nll, [0.0, 1.0], method="Nelder-Mead")
    intercept, slope = res.x
    assert intercept == pytest.approx(0.0, abs=0.02)
    assert slope == pytest.approx(1.0, abs=0.02)


def test_shipped_calibration_matches_a_fresh_fit(arrays, calibration):
    """The file in data/ must be the file this code produces."""
    assert calibration.n_races == pytest.approx(EXPECTED_RACES, rel=0.01)
    assert calibration.lam == pytest.approx(0.71, abs=0.04)
    assert calibration.tau == pytest.approx(0.70, abs=0.04)
    oos = calibration.out_of_sample
    assert oos["logloss_model"] < oos["logloss_plain_harville"]
    assert oos["bsp_win_exponent"] == pytest.approx(1.01, abs=0.04)


def test_write_refuses_to_regress_out_of_sample_log_loss(tmp_path):
    path = tmp_path / "calibration.json"
    good = Calibration(lam=0.71, tau=0.70, out_of_sample={"logloss_model": 0.4997})
    write_calibration(good, path)
    worse = Calibration(lam=0.6, tau=0.6, out_of_sample={"logloss_model": 0.5100})
    with pytest.raises(CalibrationError, match="worse"):
        write_calibration(worse, path)
    # ...unless forced.
    write_calibration(worse, path, force=True)
