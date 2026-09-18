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


def test_beta_fit_joins_real_races_and_recovers_a_known_exponent(tmp_path):
    """The whole `sportsbet_beta` path, end to end, against real history.

    Stored Sportsbet prices are invented — but they are invented from real
    races, so the join (date + track + race number + TAB number) is exercised
    against genuine track names and dates rather than against a mock. The
    invented book is built so that its implied probabilities raised to a
    known exponent reproduce the real BSP probabilities; the fit must find
    that exponent back.
    """
    from datetime import datetime, timezone

    import pandas as pd

    from app.calibrate import fit_beta_from_stored_prices
    from app.database.repository import Repository, session_factory
    from app.sources.base import MeetingInfo, RaceInfo, RunnerInfo

    try:
        month = load_history(date(2026, 8, 1), date(2026, 8, 31),
                             download_missing=False)
    except HistoryUnavailableError as exc:
        pytest.skip(f"history unavailable: {exc}")
    assert month.n_races > 500

    true_beta = 1.15
    db_url = f"sqlite:///{tmp_path / 'beta.db'}"
    Session = session_factory(db_url)
    with Session() as session:
        repo = Repository(session)
        for ri in range(month.n_races):
            key = month.keys.iloc[ri]
            n = int(month.n_runners[ri])
            q = month.q[ri][:n]
            raw = q ** (1.0 / true_beta)
            raw = raw / raw.sum() * 1.15          # add a realistic overround
            prices = 1.0 / raw
            meeting_date = pd.to_datetime(key["meeting_date"]).date()
            start = datetime.combine(
                meeting_date, datetime.min.time(), tzinfo=timezone.utc
            )
            meeting = repo.upsert_meeting(MeetingInfo(
                source="sportsbet",
                source_id=f"mt-{key['TRACK']}-{meeting_date}",
                venue=str(key["TRACK"]), meeting_date=meeting_date,
            ))
            race_row = repo.upsert_race(meeting, RaceInfo(
                source="sportsbet", source_id=f"race-{ri}",
                race_number=int(key["RACE_NO"]), start_time=start,
                status="open", fetched_at=start,
            ))
            for i in range(n):
                # Real TAB numbers are NOT 1..n — scratchings leave gaps — so
                # the saddlecloth has to come from the data, not from the
                # loop index. (Assuming otherwise is exactly the mis-pairing
                # the name check exists to catch, and it does catch it.)
                info = RunnerInfo(
                    source="sportsbet", source_id=f"race-{ri}-{i}",
                    horse_name=str(month.names[ri][i]),
                    saddlecloth=int(month.tab_numbers[ri][i]),
                    win_price=float(prices[i]), place_price=2.0,
                )
                repo.record_price(
                    race_row, repo.upsert_runner(race_row, info), info,
                    observed_at=start, seconds_to_jump=60.0, raw_sha256=None,
                )
        session.commit()

    fit = fit_beta_from_stored_prices(month, db_url=db_url)
    assert fit.beta is not None, fit.detail
    assert fit.n_races == month.n_races, "every stored race should have joined"
    assert fit.n_unmatched_races == 0
    assert fit.n_name_mismatches == 0, "every runner's name should have matched"
    assert fit.beta == pytest.approx(true_beta, abs=0.02), (
        f"recovered {fit.beta}, planted {true_beta}"
    )


def test_beta_fit_drops_runners_whose_name_does_not_match(tmp_path):
    """The name check is a real check, not a field that is always zero."""
    from datetime import datetime, timezone

    import pandas as pd

    from app.calibrate import fit_beta_from_stored_prices
    from app.database.repository import Repository, session_factory
    from app.sources.base import MeetingInfo, RaceInfo, RunnerInfo

    try:
        month = load_history(date(2026, 8, 1), date(2026, 8, 31),
                             download_missing=False)
    except HistoryUnavailableError as exc:
        pytest.skip(f"history unavailable: {exc}")

    db_url = f"sqlite:///{tmp_path / 'mismatch.db'}"
    Session = session_factory(db_url)
    n_races = 40
    with Session() as session:
        repo = Repository(session)
        for ri in range(n_races):
            key = month.keys.iloc[ri]
            n = int(month.n_runners[ri])
            meeting_date = pd.to_datetime(key["meeting_date"]).date()
            start = datetime.combine(
                meeting_date, datetime.min.time(), tzinfo=timezone.utc
            )
            meeting = repo.upsert_meeting(MeetingInfo(
                source="sportsbet",
                source_id=f"mt-{key['TRACK']}-{meeting_date}",
                venue=str(key["TRACK"]), meeting_date=meeting_date,
            ))
            race_row = repo.upsert_race(meeting, RaceInfo(
                source="sportsbet", source_id=f"race-{ri}",
                race_number=int(key["RACE_NO"]), start_time=start,
                status="open", fetched_at=start,
            ))
            for i in range(n):
                # Deliberately wrong names: the join key still matches, so
                # only the name check can catch this.
                info = RunnerInfo(
                    source="sportsbet", source_id=f"race-{ri}-{i}",
                    horse_name=f"Not The Right Horse {i}", saddlecloth=i + 1,
                    win_price=4.0, place_price=2.0,
                )
                repo.record_price(
                    race_row, repo.upsert_runner(race_row, info), info,
                    observed_at=start, seconds_to_jump=60.0, raw_sha256=None,
                )
        session.commit()

    fit = fit_beta_from_stored_prices(month, db_url=db_url)
    assert fit.n_name_mismatches > 0, "mismatched names must be counted"
    assert fit.beta is None, "a fit must not be produced from mis-paired runners"


def test_optimisers_do_not_depend_on_a_lucky_bracket(arrays):
    """A bracketed minimize_scalar raises when the bracket misses the minimum.

    Both fits use bounded optimisation instead, so a degenerate input returns
    a number at the boundary rather than aborting the whole calibration.
    """
    import numpy as np

    from app.calibrate import fit_win_exponent
    from app.history import RaceArrays

    flat = RaceArrays(
        q=np.tile([0.25, 0.25, 0.25, 0.25, 0.0], (10, 1)),
        mask=np.tile([True] * 4 + [False], (10, 1)),
        won=np.tile([True] + [False] * 4, (10, 1)),
        placed=np.tile([True, True, False, False, False], (10, 1)),
        place_bsp=np.full((10, 5), np.nan),
        names=np.full((10, 5), "", dtype=object),
        tab_numbers=np.tile([1, 2, 3, 4, -1], (10, 1)),
        n_runners=np.full(10, 4),
        places=np.full(10, 2),
        dates=arrays.dates[:10],
        keys=arrays.keys.iloc[:10].reset_index(drop=True),
    )
    value = fit_win_exponent(flat)   # must not raise
    assert np.isfinite(value)
