"""Tier logic: the SUSPECT cap, the uncalibrated gate, fragile place terms."""

from __future__ import annotations

import pytest

from app.scoring import (
    REASON_BEYOND_CAP,
    REASON_DELAYED,
    REASON_INDICATIVE,
    REASON_MODELS_SHORT,
    REASON_NO_EXCHANGE,
    REASON_STALE,
    REASON_UNCALIBRATED,
    QUALITY_HIGH,
    QUALITY_LOW,
    QUALITY_MEDIUM,
    TIER_BET,
    TIER_NONE,
    TIER_SUSPECT,
    TIER_WATCH,
    TierInputs,
    apply_race_stake_cap,
    decide_tier,
    drz_score,
    fair_place_odds,
    kelly_fraction,
    quality_for,
    suggest_stake,
)


def base(**kw) -> TierInputs:
    """A row that satisfies every BET criterion, so each test can break one."""
    args = dict(
        drz_by_model={"betfair": 1.15, "sportsbet_beta": 1.12},
        win_price=4.0,
        price_age_seconds=10.0,
        quality=QUALITY_HIGH,
        uncalibrated=False,
        allow_uncalibrated=False,
        win_model="betfair",
        max_win_price=51.0,
        drz_exchange_place=1.14,      # the exchange agrees
    )
    args.update(kw)
    return TierInputs(**args)


def has(res, token: str) -> bool:
    """True when any blocker carries this reason token, whatever the order."""
    return any(token in r for r in res.reasons)


def test_bet_requires_every_model_to_clear():
    assert decide_tier(base()).tier == TIER_BET


def test_one_model_short_is_watch_not_bet():
    res = decide_tier(base(drz_by_model={"betfair": 1.15, "sportsbet_power": 1.05}))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_MODELS_SHORT)
    assert "1/2 models" in " ".join(res.reasons)


def test_watch_band():
    assert decide_tier(base(drz_by_model={"betfair": 1.05})).tier == TIER_WATCH
    assert decide_tier(base(drz_by_model={"betfair": 1.01})).tier == TIER_NONE


def test_suspect_cap_is_never_a_bet():
    """A Dr Z above 1.30 is evidence of a data problem, not of value."""
    res = decide_tier(base(drz_by_model={"betfair": 1.45, "sportsbet_beta": 1.44}))
    assert res.tier == TIER_SUSPECT
    assert "stale price" in res.reasons[0]


def test_suspect_dominates_everything_else():
    res = decide_tier(base(drz_by_model={"betfair": 2.0}, quality=QUALITY_LOW,
                           price_age_seconds=9999, uncalibrated=True))
    assert res.tier == TIER_SUSPECT


def test_win_price_cap_blocks_bet():
    res = decide_tier(base(win_price=60.0))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_BEYOND_CAP)
    assert "51.0 cap" in " ".join(res.reasons)
    assert decide_tier(base(win_price=60.0, max_win_price=101.0)).tier == TIER_BET


def test_stale_price_blocks_bet():
    res = decide_tier(base(price_age_seconds=75.0))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_STALE)
    assert "75s old" in " ".join(res.reasons)
    assert decide_tier(base(price_age_seconds=None)).tier == TIER_WATCH


def test_quality_must_be_high():
    assert decide_tier(base(quality=QUALITY_MEDIUM)).tier == TIER_WATCH


def test_uncalibrated_model_is_gated_unless_allowed():
    res = decide_tier(base(drz_by_model={"sportsbet_power": 1.20}, uncalibrated=True))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_UNCALIBRATED)
    ok = decide_tier(base(drz_by_model={"sportsbet_power": 1.20},
                          uncalibrated=True, allow_uncalibrated=True))
    assert ok.tier == TIER_BET


def test_tote_indicative_price_is_never_a_bet():
    res = decide_tier(base(price_type="tote_indicative"))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_INDICATIVE)


def test_delayed_betfair_alone_cannot_bet_near_the_jump():
    near = decide_tier(base(drz_by_model={"betfair": 1.20},
                            betfair_delayed_only=True, seconds_to_jump=120))
    assert near.tier == TIER_WATCH
    assert has(near, REASON_DELAYED)
    far = decide_tier(base(drz_by_model={"betfair": 1.20},
                           betfair_delayed_only=True, seconds_to_jump=900))
    assert far.tier == TIER_BET


def test_no_model_means_no_tier():
    assert decide_tier(base(drz_by_model={})).tier == TIER_NONE


def test_exactly_eight_runners_is_flagged_fragile():
    quality, notes = quality_for(10.0, 60, 1, False, terms_fragile=True)
    assert quality == QUALITY_HIGH
    assert any("one scratching" in n for n in notes)
    _, clean = quality_for(10.0, 60, 1, False, terms_fragile=False)
    assert not any("scratching" in n for n in clean)


def test_quality_degrades_with_price_age_and_delay():
    assert quality_for(10.0, 60, 1, False, False)[0] == QUALITY_HIGH
    assert quality_for(90.0, 60, 1, False, False)[0] == QUALITY_MEDIUM
    assert quality_for(600.0, 60, 1, False, False)[0] == QUALITY_LOW
    assert quality_for(10.0, 60, 1, True, False)[0] == QUALITY_MEDIUM
    assert quality_for(None, 60, 1, False, False)[0] == QUALITY_LOW


def test_score_arithmetic():
    assert drz_score(0.55, 2.0) == pytest.approx(1.10)
    assert fair_place_odds(0.5) == pytest.approx(2.0)
    assert fair_place_odds(0.0) is None


def test_kelly_and_caps():
    # drz 1.10 at a place price of 2.0: f = 0.10/1.0 = 0.10 full Kelly.
    assert kelly_fraction(1.10, 2.0) == pytest.approx(0.10)
    assert kelly_fraction(0.9, 2.0) == 0.0
    # Quarter-Kelly 0.025 is above the 1% per-bet cap, so the cap binds.
    assert suggest_stake(1.10, 2.0, 1000.0, 0.25, 0.01) == pytest.approx(10.0)
    # A thin edge sizes below the cap.
    assert suggest_stake(1.02, 2.0, 1000.0, 0.25, 0.01) == pytest.approx(5.0)


def test_per_race_stake_cap_scales_proportionally():
    stakes = [10.0, 10.0, 10.0]          # 3% of a 1000 bankroll
    capped = apply_race_stake_cap(stakes, 1000.0, 0.02)
    assert sum(capped) <= 20.0, "the per-race cap must never be exceeded by rounding"
    assert capped == [pytest.approx(6.66)] * 3
    under = apply_race_stake_cap([5.0, 5.0], 1000.0, 0.02)
    assert under == [5.0, 5.0]
