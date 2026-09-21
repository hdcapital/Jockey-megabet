"""The model's parameters must come from data/calibration.json, not from code.

This is the regression test the audit found missing. Every other test asserts
the valuation *echoes* whatever the Calibration object holds, or that the
shipped file's numbers are near the expected ones — a hard-coded constant
would pass both. These tests change the file and require the output to move.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.calibration import (
    DEFAULT_LAM,
    DEFAULT_TAU,
    CalibrationError,
    load_calibration,
)
from app.engine import value_race
from app.sources.sportsbet import parse_racecard

NOW = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)


def _value(fixture, settings, calibration):
    race = parse_racecard(
        fixture("racecard_open_9_runners.json"), event_id="x", fetched_at=NOW
    )
    race.start_time = NOW + timedelta(minutes=15)
    return {
        v.saddlecloth: v
        for v in value_race(race, calibration, settings, now=NOW,
                            allow_uncalibrated=True)
    }


def _write(tmp_path, base_path, **overrides):
    raw = json.loads(base_path.read_text())
    raw.update(overrides)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(raw))
    return path


@pytest.fixture
def shipped_path():
    from pathlib import Path

    return Path(__file__).parent.parent / "data" / "calibration.json"


def test_changing_lam_and_tau_changes_every_place_probability(
    fixture, settings, calibration, tmp_path, shipped_path
):
    """Set the file to plain Harville and the whole field must move."""
    harville_path = _write(tmp_path, shipped_path, lam=1.0, tau=1.0,
                           win_prob_bucket_correction=[])
    harville = load_calibration(harville_path)
    assert (harville.lam, harville.tau) == (1.0, 1.0)

    base = _value(fixture, settings, calibration)
    moved = _value(fixture, settings, harville)

    assert base.keys() == moved.keys()
    for saddle in base:
        assert base[saddle].p_place != pytest.approx(moved[saddle].p_place), (
            f"runner {saddle} did not move — lam/tau may be hard-coded"
        )
    # And in the documented direction: plain Harville over-states the
    # favourite's place chance and under-states the outsider's.
    assert moved[1].p_place > base[1].p_place
    assert moved[9].p_place < base[9].p_place
    # The row records the values it actually used.
    assert all(v.lam == 1.0 and v.tau == 1.0 for v in moved.values())


def test_changing_the_band_table_changes_the_correction(
    fixture, settings, calibration, tmp_path, shipped_path
):
    from app.sources.base import RunnerInfo  # noqa: F401  (clarity of intent)

    aggressive = _write(
        tmp_path, shipped_path,
        band_ratio={"betfair": [{"lo": 1.0, "hi": 1000.0, "ratio": 0.5, "n": 9999}]},
    )
    cal = load_calibration(aggressive)
    assert cal.shrink_factor("betfair", 4.0) == 0.5
    # Sportsbet-priced rows are untouched: the table was measured on exchange
    # probabilities, so it is not applied to anything else.
    rows = _value(fixture, settings, cal)
    assert all(v.band_shrink == 1.0 for v in rows.values())


def test_a_missing_file_falls_back_to_the_documented_defaults(tmp_path, caplog):
    cal = load_calibration(tmp_path / "nope.json")
    assert (cal.lam, cal.tau) == (DEFAULT_LAM, DEFAULT_TAU)
    assert not cal.loaded_from_file
    assert cal.version == "shipped-default"
    assert cal.band_ratio == {}


def test_a_corrupt_file_falls_back_rather_than_crashing(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text("{not json at all")
    cal = load_calibration(path)
    assert (cal.lam, cal.tau) == (DEFAULT_LAM, DEFAULT_TAU)
    assert not cal.loaded_from_file


@pytest.mark.parametrize("bad", [
    {"lam": "nope"}, {"tau": None}, {"lam": 0.0}, {"tau": 3.5}, {"lam": -1.0},
])
def test_an_unusable_parameter_is_refused_not_guessed(tmp_path, shipped_path, bad):
    path = _write(tmp_path, shipped_path, **bad)
    with pytest.raises(CalibrationError):
        load_calibration(path)


def test_the_shipped_file_is_actually_loaded(calibration, shipped_path):
    assert calibration.loaded_from_file
    assert calibration.source_path == shipped_path
    assert calibration.version != "shipped-default"
    assert calibration.n_races and calibration.n_races > 40_000
    assert calibration.band_ratio.get("betfair")
