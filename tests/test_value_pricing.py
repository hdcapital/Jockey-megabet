"""Exchange pricing maths, checked by hand."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.sources.betfair import BetfairRunnerQuote
from app.value.pricing import (
    depth_probability,
    fair_probabilities,
    fill_price,
    kelly_fraction,
    lay_lock_profit,
    places_for_field,
    spread_ticks,
    tick_index,
)

NOW = datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc)


def q(sid, backs, lays):
    return BetfairRunnerQuote(
        market_id="1.1", selection_id=sid, runner_name=f"{sid}. X",
        best_back=backs[0][0] if backs else None, best_lay=lays[0][0] if lays else None,
        back_volume=backs[0][1] if backs else None, lay_volume=lays[0][1] if lays else None,
        total_matched=None, market_status="OPEN", fetched_at=NOW,
        back_ladder=list(backs), lay_ladder=list(lays),
    )


def test_tick_ladder_matches_betfair_increments():
    assert tick_index(1.01) == 0
    assert tick_index(2.0) == 99
    assert tick_index(3.0) == 149
    assert tick_index(1000) == 349  # Betfair's ladder has 350 prices
    assert spread_ticks(2.0, 2.02) == 1
    assert spread_ticks(1.98, 2.02) == 3  # crosses the 2.0 band edge
    assert spread_ticks(3.0, 3.05) == 1
    assert spread_ticks(10.0, 11.0) == 2
    assert spread_ticks(50.0, 55.0) == 1


def test_fill_price_walks_the_ladder():
    ladder = [(3.0, 40.0), (2.9, 40.0), (2.8, 100.0)]
    avg, filled = fill_price(ladder, 100.0)
    assert filled == 100.0
    assert avg == pytest.approx((3.0 * 40 + 2.9 * 40 + 2.8 * 20) / 100)
    avg, filled = fill_price([(3.0, 30.0)], 100.0)
    assert (avg, filled) == (3.0, 30.0)
    assert fill_price([], 100.0) == (None, 0.0)


def test_token_money_at_the_top_cannot_move_the_price():
    # $2 at 3.5 on top of real money at 3.0 / 3.05.
    thin_top = q(1, [(3.5, 2.0), (3.0, 500.0)], [(3.05, 500.0)])
    p, shallow = depth_probability(thin_top, 100.0)
    vb = (3.5 * 2 + 3.0 * 98) / 100
    assert p == pytest.approx((1 / vb + 1 / 3.05) / 2)
    assert not shallow
    p2, shallow2 = depth_probability(q(2, [(3.0, 20.0)], [(3.05, 500.0)]), 100.0)
    assert shallow2 and p2 == pytest.approx((1 / 3.0 + 1 / 3.05) / 2)
    p3, shallow3 = depth_probability(q(3, [], [(4.0, 500.0)]), 100.0)
    assert shallow3 and p3 == pytest.approx(0.25)


def test_fair_probabilities_scale_to_places_and_exclude():
    quotes = [q(i, [(o, 1000.0)], [(o * 1.02, 1000.0)]) for i, o in
              ((1, 1.5), (2, 2.0), (3, 3.0), (4, 5.0), (5, 8.0))]
    win, book, _ = fair_probabilities(quotes, 100.0, places=1)
    assert sum(win.values()) == pytest.approx(1.0)
    place, _, _ = fair_probabilities(quotes, 100.0, places=2)
    assert all(v <= 0.995 for v in place.values())
    assert sum(place.values()) <= 2.0 + 1e-9
    ex, _, _ = fair_probabilities(quotes, 100.0, places=1, exclude={5})
    assert 5 not in ex and sum(ex.values()) == pytest.approx(1.0)


def test_lock_kelly_and_place_terms():
    b, l, c = 3.0, 2.8, 0.08
    lay_stake = b / (l - c)
    assert lay_lock_profit(b, l, c) == pytest.approx((b - 1) - lay_stake * (l - 1))
    assert lay_lock_profit(b, l, c) == pytest.approx(lay_stake * (1 - c) - 1)
    assert kelly_fraction(0.5, 2.2) == pytest.approx((0.5 * 2.2 - 1) / 1.2)
    assert kelly_fraction(0.3, 3.0) == 0.0
    assert [places_for_field(n) for n in (4, 5, 7, 8, 16)] == [0, 2, 2, 3, 3]
