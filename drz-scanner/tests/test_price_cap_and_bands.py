"""The win-price cap, the band correction and the exchange-confirmation gate.

The cap answers one question: out to what price are the win probabilities
trustworthy? That depends entirely on where the probabilities came from, so a
single global cutoff is the wrong shape. Exchange-derived probabilities stay
calibrated to about $51; bookmaker-derived ones carry the longshot overround
and are held at $9.

None of this is evidence that an edge exists at any price.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.calibration import Calibration
from app.config import Settings
from app.database import models as m
from app.database.repository import Repository, session_factory
from app.engine import band_source_for, value_race
from app.scoring import (
    REASON_BEYOND_CAP,
    REASON_NO_EXCHANGE,
    QUALITY_HIGH,
    TIER_BET,
    TIER_SUSPECT,
    TIER_WATCH,
    TierInputs,
    decide_tier,
)
from app.sources.base import MeetingInfo
from app.sources.sportsbet import parse_racecard
from app.winprob import (
    WIN_MODEL_BETA,
    WIN_MODEL_BETFAIR,
    WIN_MODEL_POWER,
    max_win_price_for,
)

NOW = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)


def row(**kw) -> TierInputs:
    """A row meeting every BET criterion, so each test can break exactly one."""
    args = dict(
        drz_by_model={"betfair": 1.15},
        win_price=4.0,
        price_age_seconds=10.0,
        quality=QUALITY_HIGH,
        uncalibrated=False,
        allow_uncalibrated=False,
        win_model=WIN_MODEL_BETFAIR,
        max_win_price=51.0,
        drz_exchange_place=1.14,
    )
    args.update(kw)
    return TierInputs(**args)


def has(res, token: str) -> bool:
    return any(token in r for r in res.reasons)


# --- the caps themselves ---------------------------------------------------

@pytest.mark.parametrize("model,delayed,expected", [
    (WIN_MODEL_BETFAIR, False, 51.0),
    (WIN_MODEL_BETFAIR, True, 21.0),
    (WIN_MODEL_BETA, False, 9.0),
    (WIN_MODEL_POWER, False, 9.0),
])
def test_cap_depends_on_the_win_model(model, delayed, expected):
    cap, reason = max_win_price_for(model, delayed, Settings())
    assert cap == expected
    assert reason


def test_a_betfair_runner_at_15_is_bet():
    """$15 is inside the range where exchange probabilities are calibrated."""
    assert decide_tier(row(win_price=15.0)).tier == TIER_BET


def test_the_same_runner_under_sportsbet_power_is_watch():
    res = decide_tier(row(
        win_price=15.0, win_model=WIN_MODEL_POWER, max_win_price=9.0,
        drz_by_model={"sportsbet_power": 1.15},
        uncalibrated=True, allow_uncalibrated=True,
    ))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_BEYOND_CAP)


def test_a_betfair_runner_at_60_is_watch():
    """$60 is past $51, where the bands stop holding."""
    res = decide_tier(row(win_price=60.0))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_BEYOND_CAP)


def test_a_delayed_key_runner_at_30_is_watch():
    res = decide_tier(row(
        win_price=30.0, max_win_price=21.0,
        max_win_price_reason="delayed exchange prices carry no matched volume",
    ))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_BEYOND_CAP)
    # ...and 15.0 is inside the delayed cap.
    assert decide_tier(row(win_price=15.0, max_win_price=21.0)).tier == TIER_BET


@pytest.mark.parametrize("model,cap,price", [
    (WIN_MODEL_BETFAIR, 51.0, 15.0),
    (WIN_MODEL_BETFAIR, 51.0, 60.0),
    (WIN_MODEL_POWER, 9.0, 4.0),
])
def test_suspect_still_dominates_the_cap(model, cap, price):
    """A data problem is a data problem at any price, under any model."""
    res = decide_tier(row(
        win_price=price, win_model=model, max_win_price=cap,
        drz_by_model={model: 1.45}, uncalibrated=(model == WIN_MODEL_POWER),
        allow_uncalibrated=True,
    ))
    assert res.tier == TIER_SUSPECT


# --- exchange confirmation -------------------------------------------------

def test_a_model_only_row_cannot_reach_bet():
    """Measured: model-only signals against exchange place prices returned
    0.80-0.88 per $1. So the exchange has to agree."""
    res = decide_tier(row(drz_exchange_place=None))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_NO_EXCHANGE)


def test_an_exchange_that_disagrees_blocks_bet():
    res = decide_tier(row(drz_exchange_place=0.95))
    assert res.tier == TIER_WATCH
    assert has(res, REASON_NO_EXCHANGE)
    assert "0.950" in " ".join(res.reasons)


def test_an_exchange_that_agrees_allows_bet():
    assert decide_tier(row(drz_exchange_place=1.11)).tier == TIER_BET


def test_confirmation_can_be_switched_off():
    res = decide_tier(row(drz_exchange_place=None,
                          require_exchange_confirmation=False))
    assert res.tier == TIER_BET


# --- the band correction ---------------------------------------------------

BANDS = [
    {"lo": 1.0, "hi": 2.0, "ratio": 0.97, "n": 3668},
    {"lo": 2.0, "hi": 3.0, "ratio": 0.98, "n": 10418},
    {"lo": 5.0, "hi": 9.0, "ratio": 1.00, "n": 46161},
    {"lo": 9.0, "hi": 15.0, "ratio": 1.03, "n": 44858},
    {"lo": 101.0, "hi": 1e9, "ratio": 0.82, "n": 34397},
]


@pytest.mark.parametrize("price,expected", [
    (1.5, 0.97), (2.5, 0.98), (7.0, 1.0),
    (12.0, 1.0),     # ratio 1.03 -> clamped, a no-op
    (500.0, 0.82),
    (4.0, 1.0),      # no band covers it
    (None, 1.0),
])
def test_shrink_factor_never_exceeds_one(price, expected):
    cal = Calibration(band_ratio={"betfair": BANDS})
    assert cal.shrink_factor("betfair", price) == pytest.approx(expected)


def test_a_band_above_one_leaves_the_probability_untouched():
    cal = Calibration(band_ratio={"betfair": BANDS})
    assert cal.band_ratio_for("betfair", 12.0) == 1.03
    assert cal.shrink_factor("betfair", 12.0) == 1.0


def test_no_table_for_a_source_is_a_no_op():
    cal = Calibration(band_ratio={"betfair": BANDS})
    assert cal.shrink_factor("sportsbet", 1.5) == 1.0
    assert Calibration().shrink_factor("betfair", 1.5) == 1.0


def test_band_source_is_only_claimed_where_it_was_measured():
    """The table was measured on exchange probabilities, so it is only applied
    to exchange probabilities."""
    assert band_source_for(WIN_MODEL_BETFAIR) == "betfair"
    assert band_source_for(WIN_MODEL_BETA) is None
    assert band_source_for(WIN_MODEL_POWER) is None


def test_the_shipped_bands_reproduce_the_measured_table(calibration):
    """The shipped calibration.json carries the table from the README."""
    bands = {(b["lo"], b["hi"]): b["ratio"] for b in calibration.band_ratio["betfair"]}
    expected = {
        (1.0, 2.0): 0.97, (2.0, 3.0): 0.98, (3.0, 5.0): 0.99, (5.0, 9.0): 1.00,
        (9.0, 15.0): 1.03, (15.0, 21.0): 1.02, (21.0, 31.0): 1.00,
        (31.0, 51.0): 1.00, (51.0, 101.0): 0.99, (101.0, 1e9): 0.84,
    }
    for key, want in expected.items():
        assert bands[key] == pytest.approx(want, abs=0.02), key


# --- the correction as the engine applies it -------------------------------

def _race(fixture, name="racecard_open_9_runners.json"):
    race = parse_racecard(fixture(name), event_id="900101", fetched_at=NOW)
    race.start_time = NOW + timedelta(minutes=15)
    return race


def _exchange_probs(race):
    active = race.active_runners()
    raw = [1.0 / (r.win_price * 0.95) for r in active]
    total = sum(raw)
    return [p / total for p in raw], [True] * len(active)


def test_corrected_place_probability_is_never_above_the_raw_one(fixture, settings,
                                                                calibration):
    race = _race(fixture)
    probs, reliable = _exchange_probs(race)
    vals = value_race(race, calibration, settings, now=NOW,
                      betfair_win_probs=probs, betfair_reliable=reliable)
    assert vals
    for v in vals:
        assert v.band_shrink <= 1.0
        assert v.p_place <= v.p_place_raw + 1e-12, v.horse_name
        assert v.p_place == pytest.approx(v.p_place_raw * v.band_shrink)


def test_short_priced_runners_are_trimmed_and_mid_range_is_untouched(
    fixture, settings, calibration
):
    race = _race(fixture)
    probs, reliable = _exchange_probs(race)
    vals = {v.saddlecloth: v for v in value_race(
        race, calibration, settings, now=NOW,
        betfair_win_probs=probs, betfair_reliable=reliable)}
    # #1 is $2.60 -> the (2,3] band, ratio 0.98: trimmed.
    assert vals[1].band_shrink < 1.0
    assert vals[1].p_place < vals[1].p_place_raw
    # #5 is $11.00 -> the (9,15] band, ratio 1.03: a no-op, never scaled up.
    assert vals[5].band_shrink == 1.0
    assert vals[5].p_place == pytest.approx(vals[5].p_place_raw)


def test_sportsbet_priced_rows_get_no_band_correction(fixture, settings, calibration):
    """The table was measured on exchange probabilities only."""
    race = _race(fixture)
    vals = value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True)
    assert all(v.win_model == WIN_MODEL_POWER for v in vals)
    assert all(v.band_shrink == 1.0 for v in vals)
    assert all(v.p_place == pytest.approx(v.p_place_raw) for v in vals)


# --- beyond-cap rows are still valued, stored and shown --------------------

def test_rows_beyond_the_cap_are_valued_stored_and_carry_the_reason(
    fixture, settings, calibration, monkeypatch
):
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.repository.get_settings", lambda: settings)
    race = _race(fixture)
    # Push the longest-priced runner clear of the 51.0 cap, and price its
    # place so it would otherwise qualify (drz inside [1.10, 1.30], below the
    # SUSPECT ceiling).
    outsider = max(race.active_runners(), key=lambda r: r.win_price)
    outsider.win_price = 80.0
    probs, reliable = _exchange_probs(race)
    outsider.place_price = 13.0
    place_probs = {r.source_id: 0.10 for r in race.active_runners()}

    vals = value_race(race, calibration, settings, now=NOW,
                      betfair_win_probs=probs, betfair_reliable=reliable,
                      betfair_place_probs=place_probs)
    beyond = [v for v in vals if v.beyond_price_cap]
    assert beyond, "the 80.00 runner should be beyond the 51.0 cap"
    assert all(v.tier != TIER_SUSPECT for v in beyond), "set up below the suspect cap"
    for v in beyond:
        assert v.win_price > v.max_win_price
        assert v.tier != TIER_BET
        assert any(REASON_BEYOND_CAP in r for r in v.tier_reasons)
        assert v.p_place is not None, "still valued"
        assert v.drz is not None

    Session = session_factory(settings.database_url)
    with Session() as session:
        repo = Repository(session)
        meeting = repo.upsert_meeting(MeetingInfo(
            source="sportsbet", source_id="5701", venue="Fixtureville",
            meeting_date=NOW.date()))
        race_row = repo.upsert_race(meeting, race)
        rows = {r.source_id: repo.upsert_runner(race_row, r) for r in race.runners}
        for v in vals:
            repo.record_valuation(v, race_row, rows[v.runner_source_id])
        session.commit()

        stored = session.query(m.PlaceValuation).filter(
            m.PlaceValuation.beyond_price_cap.is_(True)).all()
        assert len(stored) == len(beyond), "beyond-cap rows must be persisted"
        for s in stored:
            assert REASON_BEYOND_CAP in (s.tier_reasons or "")
            assert s.max_win_price == 51.0
            assert s.p_place is not None and s.p_place_raw is not None
            assert s.model_version == "1.1"


def test_the_cap_is_the_only_thing_holding_a_row_back_and_it_is_still_shown(
    fixture, settings, calibration, monkeypatch
):
    """A row that meets every other BET criterion, held at WATCH by the cap.

    The cap gates the BET tier; it must not make the row invisible, because
    the backtester needs those rows to test the cap itself later.
    """
    from rich.console import Console

    import app.reporting.tables as tables

    race = _race(fixture)
    outsider = max(race.active_runners(), key=lambda r: r.win_price)
    outsider.win_price = 80.0                 # clear of the 51.0 betfair cap
    probs, reliable = _exchange_probs(race)
    # Priced so BOTH models clear the threshold and the exchange agrees, so
    # the cap is genuinely the only thing left holding the row back.
    outsider.place_price = 19.0
    vals = value_race(race, calibration, settings, now=NOW,
                      betfair_win_probs=probs, betfair_reliable=reliable,
                      betfair_place_probs={r.source_id: 0.065
                                           for r in race.active_runners()},
                      allow_uncalibrated=True)
    capped = next(v for v in vals if v.beyond_price_cap)
    assert all(d >= settings.drz_min for d in capped.drz_by_model.values()), (
        "every model must clear, so the cap is the only blocker")
    assert capped.drz_exchange_place >= settings.drz_min, "the exchange agrees"
    assert capped.tier == TIER_WATCH, "WATCH at most"
    assert len(capped.tier_reasons) == 1 and REASON_BEYOND_CAP in capped.tier_reasons[0], (
        f"the cap should be the only blocker, got {capped.tier_reasons}")

    # Raise the cap past it and the same row becomes BET — which is what
    # "the cap gates the BET tier" means.
    lifted = Settings(**{**settings.model_dump(), "max_win_price_betfair": 101.0})
    promoted = value_race(race, calibration, lifted, now=NOW,
                          betfair_win_probs=probs, betfair_reliable=reliable,
                          betfair_place_probs={r.source_id: 0.065
                                               for r in race.active_runners()},
                          allow_uncalibrated=True)
    assert next(v for v in promoted
                if v.saddlecloth == capped.saddlecloth).tier == TIER_BET
    assert capped.suggested_stake is None, "no stake beyond the cap"

    recorder = Console(width=220, record=True, force_terminal=False)
    monkeypatch.setattr(tables, "console", recorder)
    tables.render_race(vals, show_all=False, now=NOW)
    out = recorder.export_text()
    assert capped.horse_name in out, "the cap must not hide the row"
    assert REASON_BEYOND_CAP in out, "and it must say why the row is capped"
