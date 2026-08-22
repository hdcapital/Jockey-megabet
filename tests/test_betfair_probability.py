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
