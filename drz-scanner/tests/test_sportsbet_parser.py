"""Parser behaviour against archived-shape Sportsbet payloads.

These fixtures reproduce the racecard shape that was verified against live
Sportsbet responses on 2026-08-22 by the Jockey-Megabet scanner, with one
documented exception: the name of the *place* price field inside the
priceCode=L entry could not be re-verified from this build (no egress to
sportsbet.com.au). See BUILD_STATUS.md. The parser accepts a candidate list
and fails loudly when none of them is present, which is what these tests pin
down.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.sources.base import SchemaMismatchError
from app.sources.sportsbet import (
    LIVE_PRICE_CODE,
    PRICE_CODE_NOTES,
    classify_price_code,
    extract_prices,
    parse_all_racing,
    parse_racecard,
    parse_result_placings,
)


def test_extracts_win_and_place_from_the_live_code_only(fixture):
    race = parse_racecard(fixture("racecard_win_or_place.json"), event_id="820001")
    by_name = {r.horse_name: r for r in race.runners}

    fast = by_name["Fast Fixture"]
    assert fast.status == "active"
    assert fast.win_price == 2.3       # priceCode L, not MDP 3.0 or TMD 3.2
    assert fast.place_price == 1.3
    assert fast.price_type == "fixed"

    second = by_name["Second Sample"]
    assert (second.win_price, second.place_price) == (4.6, 1.8)


def test_never_uses_mdp_or_tmd_as_a_live_price(fixture):
    race = parse_racecard(fixture("racecard_win_or_place.json"), event_id="820001")
    fast = next(r for r in race.runners if r.horse_name == "Fast Fixture")
    assert fast.prices_by_code["MDP"].win_price == 3.0
    assert fast.prices_by_code["TMD"].win_price == 3.2
    # ...but the runner's own price is the L one.
    assert fast.win_price == 2.3
    assert fast.prices_by_code["MDP"].price_type == "tote_indicative"
    assert fast.prices_by_code["TMD"].price_type == "tote_indicative"
    assert fast.tote_indicative() is not None


def test_scratched_runner_has_no_price_and_is_excluded(fixture):
    race = parse_racecard(fixture("racecard_win_or_place.json"), event_id="820001")
    scratched = next(r for r in race.runners if r.horse_name == "Scratchy Example")
    assert scratched.status == "scratched"
    assert scratched.win_price is None
    assert scratched not in race.active_runners()
    # ...even though a stale MDP price is still sitting in the payload.
    assert "MDP" in scratched.prices_by_code


def test_scratching_detected_from_a_missing_live_price_alone():
    """statusCode can lag; an absent live win price is enough on its own."""
    payload = {
        "id": 1, "statusCode": "A", "bettingStatus": "PRICED",
        "markets": [{"name": "Win or Place", "selections": [
            {"id": 1, "name": "Alpha", "runnerNumber": 1, "statusCode": "A",
             "prices": [{"priceCode": "L", "winPrice": 2.0, "placePrice": 1.3}]},
            {"id": 2, "name": "Beta", "runnerNumber": 2, "statusCode": "A",
             "prices": [{"priceCode": "MDP", "winPrice": 9.0}]},
        ]}],
    }
    race = parse_racecard(payload, event_id="1")
    assert [r.status for r in race.runners] == ["active", "scratched"]


def test_missing_place_price_raises_rather_than_guessing(fixture):
    with pytest.raises(SchemaMismatchError) as exc:
        parse_racecard(fixture("racecard_no_place_price.json"), event_id="900105")
    assert "place price" in str(exc.value)
    assert "placePrice" in str(exc.value)


def test_missing_win_market_raises():
    with pytest.raises(SchemaMismatchError):
        parse_racecard({"id": 5, "markets": [{"name": "Top 3", "selections": [{}]}]},
                       event_id="5")


def test_nine_runner_race_parses_with_one_scratching(fixture):
    race = parse_racecard(fixture("racecard_open_9_runners.json"), event_id="900101")
    assert len(race.runners) == 10
    assert len(race.active_runners()) == 9
    assert race.status == "open"
    assert race.venue == "Fixtureville"
    assert race.race_number == 4
    assert all(r.place_price for r in race.active_runners())
    assert all(r.jockey_name for r in race.active_runners())


def test_result_placings_captured_in_full(fixture):
    race = parse_racecard(fixture("racecard_resulted_dead_heat.json"), event_id="900104")
    assert race.status == "resulted"
    assert race.result_placings == [2, 5, 3, 9]
    assert race.result_raw == "2,5,3,9"


@pytest.mark.parametrize("raw,expected", [
    ("1,16,18", [1, 16, 18]),
    ("2,5,3,9", [2, 5, 3, 9]),
    (" 4 , 11 ", [4, 11]),
    ("", []),
    (None, []),
    ("abandoned", []),
    ("1,DH,3", []),      # unparseable fragment -> refuse the whole string
])
def test_parse_result_placings(raw, expected):
    assert parse_result_placings(raw) == expected


def test_schedule_is_horse_only_and_carries_open_flags(fixture):
    meetings, stubs = parse_all_racing(fixture("all_racing_schedule.json"),
                                       date(2026, 9, 18))
    assert [m.venue for m in meetings] == ["Fixtureville"]
    assert {s.race_number for s in stubs} == {4, 7}
    open_races = [s for s in stubs if s.is_open]
    assert [s.race_number for s in open_races] == [4]
    resulted = next(s for s in stubs if s.race_number == 7)
    assert not resulted.is_open and resulted.result == "2,5,3,9"


def test_price_code_classification_is_conservative():
    assert classify_price_code("L") == "fixed"
    assert classify_price_code("l") == "fixed"
    for code in ("MDP", "TMD", "SOMETHING_NEW", ""):
        assert classify_price_code(code) == "tote_indicative"


def test_price_code_notes_admit_what_is_not_established():
    assert PRICE_CODE_NOTES["L"]["established"] is True
    for code in ("MDP", "TMD"):
        assert PRICE_CODE_NOTES[code]["established"] is False
        assert "NOT ESTABLISHED" in PRICE_CODE_NOTES[code]["meaning"]


def test_extract_prices_drops_empty_entries():
    quotes = extract_prices({"prices": [
        {"priceCode": "L"},                      # a withdrawn live price
        {"priceCode": "MDP", "winPrice": 6.0},
        {"priceCode": "TMD"},
        {"noCode": True, "winPrice": 3.0},
    ]})
    assert set(quotes) == {"MDP"}
    assert LIVE_PRICE_CODE not in quotes


def test_extract_prices_accepts_a_nested_decimal():
    quotes = extract_prices({"prices": [
        {"priceCode": "L", "winPrice": {"decimal": 3.25},
         "placePrice": {"decimal": 1.55}},
    ]})
    assert quotes["L"].win_price == 3.25
    assert quotes["L"].place_price == 1.55
