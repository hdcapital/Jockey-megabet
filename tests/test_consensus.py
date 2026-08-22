"""Consensus model tests (synthetic probability fixtures)."""

import pytest

from app.models.consensus import SourceProbability, consensus

WEIGHTS = {"betfair": 0.7, "sportsbet": 0.3}


def test_blend_when_both_available():
    res = consensus(
        [SourceProbability("betfair", 0.30), SourceProbability("sportsbet", 0.20)],
        WEIGHTS,
    )
    assert res.probability == pytest.approx(0.7 * 0.30 + 0.3 * 0.20)
    assert res.used_sources == ("betfair", "sportsbet")
    assert not res.fallback


def test_fallback_to_sportsbet_when_betfair_missing():
    res = consensus(
        [
            SourceProbability("betfair", None, detail="market unavailable"),
            SourceProbability("sportsbet", 0.22),
        ],
        WEIGHTS,
    )
    assert res.probability == pytest.approx(0.22)
    assert res.used_sources == ("sportsbet",)
    assert res.fallback
    assert "betfair" in res.detail


def test_unreliable_betfair_excluded():
    res = consensus(
        [
            SourceProbability("betfair", 0.5, reliable=False, detail="thin market"),
            SourceProbability("sportsbet", 0.2),
        ],
        WEIGHTS,
    )
    assert res.probability == pytest.approx(0.2)
    assert res.fallback


def test_no_sources_returns_none():
    res = consensus(
        [SourceProbability("betfair", None), SourceProbability("sportsbet", None)],
        WEIGHTS,
    )
    assert res.probability is None
    assert res.fallback


def test_weights_renormalised():
    res = consensus(
        [SourceProbability("betfair", 0.4), SourceProbability("sportsbet", 0.1)],
        {"betfair": 2.0, "sportsbet": 6.0},
    )
    assert res.probability == pytest.approx((2 * 0.4 + 6 * 0.1) / 8)
    assert res.weights["sportsbet"] == pytest.approx(0.75)


def test_zero_weight_source_ignored():
    res = consensus(
        [SourceProbability("betfair", 0.4), SourceProbability("sportsbet", 0.1)],
        {"betfair": 0.0, "sportsbet": 1.0},
    )
    assert res.probability == pytest.approx(0.1)
