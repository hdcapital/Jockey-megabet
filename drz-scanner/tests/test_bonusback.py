"""The bonus-back-if-2nd-or-3rd model: probabilities, payoffs, hedges, pick."""

from __future__ import annotations

import argparse
import itertools
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.bonusback import (
    BonusBackTerms,
    ExchangeRunner,
    PromoRaceError,
    RunnerInputs,
    bonus_bet_conversion,
    finish_probabilities,
    kelly_fraction_promo,
    lay_win_stake,
    lock_stakes,
    pick_best,
    promo_p23,
    value_promo_race,
    value_runner,
)
from app.sources.base import RaceInfo, RunnerInfo


def brute_force(q, lam, tau):
    """P(1st), P(2nd), P(3rd) by enumerating every top-3 order."""
    q = np.asarray(q) / np.sum(q)
    n = len(q)
    a, b = q**lam, q**tau
    p = np.zeros((3, n))
    for j, i, k in itertools.permutations(range(n), 3):
        pr = q[j] * a[i] / (a.sum() - a[j]) * b[k] / (b.sum() - b[j] - b[i])
        p[0, j] += pr
        p[1, i] += pr
        p[2, k] += pr
    return p


@pytest.mark.parametrize("n", [3, 4, 5, 7])
@pytest.mark.parametrize("lam,tau", [(1.0, 1.0), (0.71, 0.70), (0.5, 0.9)])
def test_finish_probabilities_match_enumeration(n, lam, tau):
    rng = np.random.default_rng(n)
    q = rng.dirichlet(np.ones(n))
    fp = finish_probabilities(q, lam, tau)
    want = brute_force(q, lam, tau)
    np.testing.assert_allclose(fp.p_first, want[0], atol=1e-12)
    np.testing.assert_allclose(fp.p_second, want[1], atol=1e-12)
    np.testing.assert_allclose(fp.p_third, want[2], atol=1e-12)
    for pos in (fp.p_first, fp.p_second, fp.p_third):
        assert math.isclose(sum(pos), 1.0, abs_tol=1e-12)


def test_promo_p23_carries_place_corrections(calibration):
    q = [0.55, 0.15, 0.1, 0.08, 0.05, 0.04, 0.02, 0.01]
    fp, p23 = promo_p23(q, calibration)
    raw = fp.p_second_or_third
    # The strong favourite's top-3 chance is corrected down, so its 2nd/3rd
    # chance must be below the raw model's.
    assert p23[0] < raw[0]
    assert all(p >= 0 for p in p23)


def _inputs(**kw):
    base = dict(horse_name="A", saddlecloth=1, win_price=4.0, p_win=0.22,
                p_second=0.18, p_third=0.15, p23=0.33)
    base.update(kw)
    return RunnerInputs(**base)


def test_ev_and_breakeven():
    terms = BonusBackTerms(bonus_value=0.7)
    v = value_runner(_inputs(), terms, 1000, 0.25)
    assert math.isclose(v.ev_no_promo, 0.22 * 4 - 1)
    assert math.isclose(v.ev_back, 0.22 * 4 + 0.33 * 0.7 - 1)
    # At r = breakeven the bet is worth exactly nothing.
    r = v.breakeven_r
    assert math.isclose(0.22 * 4 + 0.33 * r - 1, 0.0, abs_tol=1e-12)


def test_lay_win_balances_win_and_loss():
    o, lay, c = 4.0, 4.2, 0.08
    s = lay_win_stake(o, lay, c)
    assert math.isclose((o - 1) - s * (lay - 1), -1 + s * (1 - c))


@pytest.mark.parametrize("o,lw,lp,r,c", [(4.0, 4.2, 1.6, 0.7, 0.08), (9.0, 10.0, 2.6, 0.75, 0.05)])
def test_lock_pays_the_same_in_every_outcome(o, lw, lp, r, c):
    sw, sp, profit = lock_stakes(o, lw, lp, r, c)
    win = (o - 1) - sw * (lw - 1) - sp * (lp - 1)
    placed = -1 + r + sw * (1 - c) - sp * (lp - 1)
    unplaced = -1 + sw * (1 - c) + sp * (1 - c)
    for x in (win, placed, unplaced):
        assert math.isclose(x, profit, abs_tol=1e-12)


def test_bonus_conversion_is_locked():
    o, lay, c = 8.0, 8.4, 0.08
    conv = bonus_bet_conversion(o, lay, c)
    s = (o - 1) / (lay - c)
    assert math.isclose((o - 1) - s * (lay - 1), conv)
    assert math.isclose(s * (1 - c), conv)


def test_kelly_reduces_to_textbook_without_bonus():
    p, o = 0.3, 4.0
    f = kelly_fraction_promo(p, 0.3, o, r=0.0)
    assert math.isclose(f, (p * o - 1) / (o - 1), rel_tol=1e-6)


def test_kelly_zero_when_negative_ev():
    assert kelly_fraction_promo(0.2, 0.1, 4.0, 0.5) == 0.0


def test_bonus_makes_kelly_positive():
    # -EV without the promo, +EV with it.
    assert kelly_fraction_promo(0.22, 0.33, 4.0, 0.0) == 0.0
    assert kelly_fraction_promo(0.22, 0.33, 4.0, 0.7) > 0.0


def test_stake_capped_at_promo_max():
    terms = BonusBackTerms(max_stake=50)
    v = value_runner(_inputs(p_win=0.3), terms, bankroll=1_000_000, kelly_multiplier=1.0)
    assert v.stake == 50.0


def test_pick_skips_ineligible_and_enforces_min_ev():
    terms = BonusBackTerms(bonus_value=0.7, min_ev=0.02)
    good = value_runner(_inputs(horse_name="good"), terms, 1000, 0.25)
    capped = value_runner(
        _inputs(horse_name="capped", win_price=60.0, p_win=0.05, p23=0.2, max_win_price=51),
        terms, 1000, 0.25,
    )
    assert capped.ev_back > good.ev_back and not capped.eligible
    assert pick_best([good, capped], "ev", terms).best is good
    poor = value_runner(_inputs(horse_name="poor", p_win=0.18, p23=0.2), terms, 1000, 0.25)
    assert pick_best([poor], "ev", terms).best is None


def test_conservative_ev_uses_worst_model():
    terms = BonusBackTerms()
    v = value_runner(_inputs(alt_models={"sportsbet_power": (0.15, 0.30)}), terms, 1000, 0.25)
    assert v.ev_back_conservative < v.ev_back
    assert math.isclose(v.ev_back_conservative, 0.15 * 4 + 0.30 * 0.7 - 1)


def _race(prices, start=None):
    now = datetime.now(timezone.utc)
    return RaceInfo(
        source="sportsbet", source_id="E1", race_number=7,
        start_time=start or now + timedelta(minutes=20), status="open", venue="Randwick",
        runners=[
            RunnerInfo(source="sportsbet", source_id=f"S{i}", horse_name=f"H{i}",
                       saddlecloth=i + 1, win_price=p, place_price=None,
                       price_timestamp=now)
            for i, p in enumerate(prices)
        ],
    )


PRICES = [2.6, 4.4, 6.0, 8.0, 11.0, 15.0, 21.0, 31.0, 41.0]


def test_value_promo_race_sportsbet_only(settings, calibration):
    pr = value_promo_race(_race(PRICES), calibration, settings, BonusBackTerms())
    assert pr.win_model.startswith("sportsbet")
    assert len(pr.valuations) == len(PRICES)
    assert pr.pick.best is not None
    # No exchange, so no hedges.
    assert all(v.lock_profit is None for v in pr.valuations)


def test_value_promo_race_with_exchange_lock(settings, calibration):
    race = _race(PRICES)
    implied = np.array([1 / p for p in PRICES])
    fair = implied / implied.sum()
    ex = [
        ExchangeRunner(win_probability=float(q), win_reliable=True,
                       win_lay=round(1 / q * 1.02, 2), win_lay_size=1000,
                       place_lay=round(max(1.05, 1 / min(0.95, 2.6 * q)), 2),
                       place_lay_size=1000)
        for q in fair
    ]
    pr = value_promo_race(race, calibration, settings, BonusBackTerms(), "lock",
                          exchange=ex, place_market_winners=3)
    assert pr.win_model == "betfair"
    assert all(v.lock_profit is not None for v in pr.valuations)
    best = max(pr.valuations, key=lambda v: v.lock_profit)
    if best.lock_profit >= BonusBackTerms().min_ev:
        assert pr.pick.best is best
    # Two-place exchange market: no exact lock.
    pr2 = value_promo_race(race, calibration, settings, BonusBackTerms(), "lock",
                           exchange=ex, place_market_winners=2)
    assert all(v.lock_profit is None for v in pr2.valuations)
    assert pr2.pick.best is None


def test_value_promo_race_rejects_unpriced(settings, calibration):
    race = _race(PRICES)
    race.runners[0].win_price = None
    with pytest.raises(PromoRaceError):
        value_promo_race(race, calibration, settings, BonusBackTerms())


def test_promo_stub_selection():
    from app.bonusback_scan import parse_promo, promo_stubs
    from app.sources.base import RaceStub

    now = datetime.now(timezone.utc)
    stubs = [
        RaceStub(event_id=str(i), race_number=n, start_time=now + timedelta(hours=1),
                 name=None, status_code="A", betting_status=None, meeting_id=m,
                 meeting_name=m, class_name="Horses - Aus/NZ")
        for i, (m, n) in enumerate([("Randwick", 7), ("Randwick", 8), ("Flemington", 4)])
    ]
    assert parse_promo("Randwick:7") == ("Randwick", 7)
    args = argparse.Namespace(all=False, meeting=None, race=None,
                              promo=["Randwick:7", "Flemington:4"])
    assert [s.event_id for s in promo_stubs(stubs, args)] == ["0", "2"]
    args = argparse.Namespace(all=False, meeting="rand", race=None, promo=[])
    assert [s.event_id for s in promo_stubs(stubs, args)] == ["0", "1"]


@pytest.mark.data
def test_p23_calibrated_out_of_sample(calibration):
    """On the last year of history, actual/model P(2nd or 3rd) by price band
    stays within 6% from $3 to $51, and beats plain Harville on log-loss."""
    from datetime import date

    from app.bonusback_backtest import build, calibration_rows, log_loss
    from app.history import load_history

    a = load_history(date(2025, 9, 1), date(2026, 8, 31))
    pr = build(a, calibration)
    assert log_loss(pr.p23, pr.y23, pr.mask) < log_loss(pr.p23_harville, pr.y23, pr.mask)
    for row in calibration_rows(pr, "price"):
        if 3 <= row["lo"] and row["hi"] <= 51:
            assert abs(row["actual"] / row["predicted"] - 1) < 0.06, row


class _FakeSportsbet:
    def __init__(self):
        now = datetime.now(timezone.utc)
        from app.sources.base import RaceStub

        self.stubs = [
            RaceStub(event_id=f"E{n}", race_number=n, start_time=now + timedelta(minutes=10 * n),
                     name=None, status_code="A", betting_status=None, meeting_id="M",
                     meeting_name="Randwick", class_name="Horses - Aus/NZ")
            for n in (6, 7)
        ]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def fetch_schedule(self, for_date):
        return [], self.stubs, None

    def fetch_racecard(self, event_id):
        race = _race(PRICES)
        race.source_id = event_id
        race.race_number = int(event_id[1:])
        return race


def test_menu_runs_without_typing_any_options(tmp_path, monkeypatch):
    from app import bonusback_menu as menu

    monkeypatch.setattr(menu, "PREFS_PATH", tmp_path / "prefs.json")
    monkeypatch.setattr(menu, "REPORT_PATH", tmp_path / "bonusback.html")
    monkeypatch.setattr("app.bonusback_scan.open_betfair", lambda *a, **k: None)
    answers = iter(["1", "25", "1", "2", "q"])   # promo, stake, meeting, race 7, quit
    opened = []
    assert menu.run_menu(ask=lambda _p: next(answers), sb_factory=_FakeSportsbet,
                         open_browser=opened.append) == 0
    page = (tmp_path / "bonusback.html").read_text()
    assert "Randwick Race 7" in page and "$25" in page
    assert opened and menu.load_stake(50) == 25.0

    # Enter at the race prompt means every race at the meeting; Enter at the
    # stake prompt keeps the remembered amount.
    answers = iter(["1", "", "1", "", "q"])
    assert menu.run_menu(ask=lambda _p: next(answers), sb_factory=_FakeSportsbet,
                         open_browser=lambda u: None) == 0
    page = (tmp_path / "bonusback.html").read_text()
    assert "Race 6" in page and "Race 7" in page and "$25" in page


def test_menu_shrugs_off_bad_input():
    from app.bonusback_menu import _choose, parse_money

    assert parse_money("$50") == 50.0 and parse_money("abc") is None
    assert _choose("", 3, lambda p: "9") is None
    assert _choose("", 3, lambda p: "Q") == "q"
    assert _choose("", 3, lambda p: "2") == 2


def _exchange_race(settings, calibration):
    race = _race(PRICES)
    imp = np.array([1 / p for p in PRICES])
    fair = imp ** 1.1 / (imp ** 1.1).sum()
    ex = [ExchangeRunner(win_probability=float(q), win_reliable=True,
                         win_lay=round(1 / q * 1.03, 2), win_lay_size=5000)
          for q in fair]
    return value_promo_race(race, calibration, settings, BonusBackTerms(), exchange=ex)


def test_conversion_lay_locks_the_same_cash_both_ways(settings, calibration):
    from app.bonusback import conversion_options

    pr = _exchange_race(settings, calibration)
    lay, keep = conversion_options([pr], 50.0, 0.08)
    best = lay[0]
    o = best.inputs.win_price
    win_side = 50 * (o - 1) - best.lay_stake * (best.lay_price - 1)
    lose_side = best.lay_stake * (1 - 0.08)
    assert math.isclose(win_side, best.cash) and math.isclose(lose_side, best.cash)
    assert all(best.per_dollar >= c.per_dollar for c in lay)
    assert keep and all(c.method == "keep" for c in keep)


def test_conversion_ranks_unfillable_lays_last(settings, calibration):
    from app.bonusback import conversion_options

    pr = _exchange_race(settings, calibration)
    lay, _ = conversion_options([pr], 50.0, 0.08)
    top = lay[0].inputs
    top.bf_win_lay_size = 1.0   # nothing on offer at that price
    lay2, _ = conversion_options([pr], 50.0, 0.08)
    assert lay2[0].inputs is not top and not lay2[-1].enough_on_offer


def test_conversion_screen_renders(settings, calibration, capsys):
    from app.bonusback_menu import show_conversion
    from app.reporting.tables import console

    pr = _exchange_race(settings, calibration)
    with console.capture() as cap:
        show_conversion([pr], 50.0, 0.08, betfair=True)
    out = cap.get()
    assert "LAY the same horse" in out and "lay stake" in out
    with console.capture() as cap:
        show_conversion([value_promo_race(_race(PRICES), calibration, settings,
                                          BonusBackTerms())], 50.0, 0.08, betfair=False)
    assert "JUST BET IT" in cap.get()


def test_menu_option_two_uses_a_bonus_bet(tmp_path, monkeypatch):
    from app import bonusback_menu as menu

    monkeypatch.setattr(menu, "PREFS_PATH", tmp_path / "prefs.json")
    monkeypatch.setattr(menu, "REPORT_PATH", tmp_path / "bonusback.html")
    monkeypatch.setattr("app.bonusback_scan.open_betfair", lambda *a, **k: None)
    shown = []
    monkeypatch.setattr(menu, "show_conversion",
                        lambda results, bonus, c, betfair: shown.append((len(results), bonus)))
    # use a bonus bet, $30, every race in the next hour, then quit
    answers = iter(["2", "30", "", "q"])
    assert menu.run_menu(ask=lambda _p: next(answers), sb_factory=_FakeSportsbet,
                         open_browser=lambda u: None) == 0
    assert shown == [(1, 30.0)]   # R6 is inside the hour, R7 is not


def test_bonus_value_prefers_a_fillable_lock(settings, calibration):
    from app.bonusback import conversion_options, estimate_bonus_value

    pr = _exchange_race(settings, calibration)
    est = estimate_bonus_value([pr], 0.08, fallback=0.70, haircut=0.95)
    lay, _ = conversion_options([pr], 1.0, 0.08)
    assert est.method == "lay"
    assert math.isclose(est.raw, lay[0].per_dollar)
    assert math.isclose(est.value, est.raw * 0.95)


def test_bonus_value_without_betfair_is_profit_only(settings, calibration):
    from app.bonusback import estimate_bonus_value

    pr = value_promo_race(_race(PRICES), calibration, settings, BonusBackTerms())
    est = estimate_bonus_value([pr], 0.08, fallback=0.70, haircut=1.0)
    assert est.method == "keep"
    # A bonus bet returns profit only: p * (O - 1), never p * O.
    want = max(v.inputs.p_win * (v.inputs.win_price - 1) for v in pr.valuations if v.eligible)
    assert math.isclose(est.value, want)


def test_bonus_value_falls_back_when_nothing_priced():
    from app.bonusback import estimate_bonus_value

    est = estimate_bonus_value([], 0.08, fallback=0.66)
    assert est.method == "fallback" and est.value == 0.66 and est.option is None


def test_later_stubs_skip_races_the_bonus_cannot_reach():
    from app.bonusback_scan import later_stubs

    stubs = _FakeSportsbet().stubs          # R6 at +60 min, R7 at +70 min
    after = stubs[0].start_time             # promo race is R6
    assert later_stubs(stubs, after, 8) == []           # R7 is only 10 min later
    assert [s.race_number for s in later_stubs(stubs, after - timedelta(minutes=10), 8)] == [7]


class _FakeSportsbetLater(_FakeSportsbet):
    def __init__(self):
        super().__init__()
        from app.sources.base import RaceStub

        self.stubs.append(RaceStub(
            event_id="E9", race_number=9,
            start_time=datetime.now(timezone.utc) + timedelta(minutes=150),
            name=None, status_code="A", betting_status=None, meeting_id="M",
            meeting_name="Randwick", class_name="Horses - Aus/NZ"))


def test_menu_values_the_bonus_on_later_races(tmp_path, monkeypatch):
    from app import bonusback_menu as menu

    monkeypatch.setattr(menu, "PREFS_PATH", tmp_path / "prefs.json")
    monkeypatch.setattr(menu, "REPORT_PATH", tmp_path / "bonusback.html")
    monkeypatch.setattr("app.bonusback_scan.open_betfair", lambda *a, **k: None)
    answers = iter(["1", "50", "1", "1", "q"])   # promo, $50, Randwick, R6, quit
    assert menu.run_menu(ask=lambda _p: next(answers), sb_factory=_FakeSportsbetLater,
                         open_browser=lambda u: None) == 0
    page = (tmp_path / "bonusback.html").read_text()
    assert "Randwick R9" in page          # measured on the later race, not the fallback
