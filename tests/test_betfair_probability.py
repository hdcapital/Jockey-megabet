"""Betfair probability derivation tests (synthetic price fixtures)."""

import pytest

from app.sources.betfair import derive_probability

MIN_LIQ = 500.0
MAX_SPREAD = 0.25


def test_midpoint_when_tight_and_liquid():
    p, reliable, detail = derive_probability(4.0, 4.2, 10000.0, MIN_LIQ, MAX_SPREAD)
    assert p == pytest.approx(1.0 / 4.1)
    assert reliable
    assert "midpoint" in detail


def test_thin_market_not_reliable():
    p, reliable, _ = derive_probability(4.0, 4.2, 50.0, MIN_LIQ, MAX_SPREAD)
    assert p == pytest.approx(1.0 / 4.1)
    assert not reliable


def test_wide_spread_uses_back_not_reliable():
    p, reliable, detail = derive_probability(4.0, 8.0, 10000.0, MIN_LIQ, MAX_SPREAD)
    assert p == pytest.approx(1.0 / 4.0)
    assert not reliable
    assert "spread" in detail


def test_back_only():
    p, reliable, detail = derive_probability(6.0, None, 1000.0, MIN_LIQ, MAX_SPREAD)
    assert p == pytest.approx(1.0 / 6.0)
    assert not reliable
    assert "only best back" in detail


def test_no_prices_unavailable():
    p, reliable, detail = derive_probability(None, None, None, MIN_LIQ, MAX_SPREAD)
    assert p is None
    assert not reliable
    assert "no exchange prices" in detail


def test_crossed_book_rejected():
    p, reliable, _ = derive_probability(5.0, 4.0, 10000.0, MIN_LIQ, MAX_SPREAD)
    assert p is None
    assert not reliable


def test_invalid_prices_at_or_below_one_ignored():
    p, _, _ = derive_probability(1.0, None, None, MIN_LIQ, MAX_SPREAD)
    assert p is None


def test_catalogue_is_fetched_in_light_windows(monkeypatch):
    """One request for a day with MARKET_DESCRIPTION + RUNNER_DESCRIPTION is
    ~222 weight against Betfair's limit of 200 (TOO_MUCH_DATA, seen live
    2026-09-19). The catalogue must be asked for in windows, runner
    projection only, and deduplicated across windows."""
    from datetime import date

    from app.sources.betfair import BetfairClient

    client = BetfairClient.__new__(BetfairClient)
    calls = []

    def fake_rpc(method, params, **_):
        calls.append(params)
        frm = params["filter"]["marketStartTime"]["from"]
        # The same market straddles two windows; it must appear once.
        return [{"marketId": "1.1", "marketName": "R1 1200m", "marketStartTime": frm,
                 "event": {"venue": "Flemington"}, "runners": [{"selectionId": 1, "runnerName": "1. A"}]}]

    client._rpc = fake_rpc
    markets = client.list_au_win_markets(date(2026, 9, 19))
    assert len(calls) >= 8, "a 52-hour span in 6-hour windows"
    assert all("MARKET_DESCRIPTION" not in c["marketProjection"] for c in calls)
    assert all(c["maxResults"] <= 200 for c in calls)
    assert [m.market_id for m in markets] == ["1.1"], "deduplicated across windows"
    assert markets[0].race_number == 1 and markets[0].venue == "Flemington"
