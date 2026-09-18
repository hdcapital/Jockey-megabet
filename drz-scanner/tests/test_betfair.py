"""Betfair gates, delayed-key detection and place-market matching."""

from __future__ import annotations

import pytest

from app.sources.betfair import (
    BetfairMarket,
    BetfairRunnerQuote,
    derive_probability,
    market_is_delayed,
    place_market_for,
)
from app.winprob import betfair_win_probabilities


def test_midpoint_used_when_liquid_and_tight():
    p, reliable, detail = derive_probability(2.0, 2.1, 5000.0, 500.0, 0.25)
    assert p == pytest.approx(1 / 2.05)
    assert reliable
    assert "midpoint" in detail


def test_thin_market_is_priced_but_not_reliable():
    p, reliable, detail = derive_probability(2.0, 2.1, 50.0, 500.0, 0.25)
    assert p == pytest.approx(1 / 2.05)
    assert not reliable
    assert "thin" in detail


def test_wide_spread_falls_back_to_best_back():
    p, reliable, detail = derive_probability(2.0, 3.5, 9000.0, 500.0, 0.25)
    assert p == pytest.approx(0.5)
    assert not reliable
    assert "exceeds" in detail


def test_crossed_book_is_refused():
    p, reliable, _ = derive_probability(3.0, 2.0, 9000.0, 500.0, 0.25)
    assert p is None and not reliable


def test_no_prices_at_all():
    p, reliable, detail = derive_probability(None, None, 9000.0, 500.0, 0.25)
    assert p is None and not reliable and "no exchange prices" in detail


def test_one_sided_book_is_unreliable():
    p, reliable, detail = derive_probability(2.0, None, 9000.0, 500.0, 0.25)
    assert p == pytest.approx(0.5) and not reliable and "only best back" in detail


# -- delayed application key ------------------------------------------------

def test_delayed_key_detected_from_absent_matched_volume():
    assert market_is_delayed({"runners": [{"selectionId": 1}, {"selectionId": 2}]})
    assert market_is_delayed({"totalMatched": 0, "runners": [{"totalMatched": 0}]})
    assert not market_is_delayed({"totalMatched": 1200.0, "runners": []})
    assert not market_is_delayed({"runners": [{"totalMatched": 300.0}]})


def test_delayed_key_uses_a_spread_only_gate():
    """With no volume to check, a tight spread alone carries the decision."""
    p, reliable, detail = derive_probability(
        2.00, 2.10, None, 500.0, 0.25, delayed=True, delayed_max_relative_spread=0.10
    )
    assert p == pytest.approx(1 / 2.05)
    assert reliable, "a tight delayed market is usable"
    assert "DELAYED" in detail

    # The same market would have been rejected as unpriced under the normal
    # gate, because totalMatched is missing.
    _, normal_reliable, _ = derive_probability(2.00, 2.10, None, 500.0, 0.25)
    assert not normal_reliable


def test_delayed_gate_is_tighter_than_the_live_one():
    """A 15% spread passes the live gate and fails the delayed one."""
    _, live_ok, _ = derive_probability(2.0, 2.3, 9000.0, 500.0, 0.25)
    assert live_ok
    _, delayed_ok, _ = derive_probability(
        2.0, 2.3, None, 500.0, 0.25, delayed=True, delayed_max_relative_spread=0.10
    )
    assert not delayed_ok


def test_delayed_flag_reaches_the_probability_set():
    probs = betfair_win_probabilities([0.5, 0.3, 0.2], [True, True, True], delayed=True)
    assert probs is not None
    assert probs.betfair_delayed
    assert "DELAYED" in probs.detail
    assert sum(probs.probabilities) == pytest.approx(1.0)


def test_a_gap_in_the_exchange_book_refuses_the_whole_model():
    """A partial book cannot be normalised without inventing the missing share."""
    assert betfair_win_probabilities([0.5, None, 0.2], [True, False, True], False) is None
    assert betfair_win_probabilities([0.5, 0.3, 0.2], [True, False, True], False) is None


# -- place market matching --------------------------------------------------

def _market(mtype, venue, race_no, winners):
    return BetfairMarket(
        market_id=f"1.{race_no}{mtype}", market_name=f"R{race_no}", market_type=mtype,
        venue=venue, market_start=None, race_number=race_no,
        number_of_winners=winners,
    )


def test_place_market_must_match_our_place_terms():
    markets = [
        _market("WIN", "Flemington", 4, None),
        _market("PLACE", "Flemington", 4, 3),
        _market("PLACE", "Flemington", 5, 2),
    ]
    assert place_market_for(markets, "Flemington", 4, 3).number_of_winners == 3
    # A 3-place Sportsbet bet cannot be checked against a 2-winner exchange
    # market, so no second opinion is offered rather than a misleading one.
    assert place_market_for(markets, "Flemington", 5, 3) is None
    assert place_market_for(markets, "Flemington", 9, 3) is None
    assert place_market_for(markets, None, 4, 3) is None
