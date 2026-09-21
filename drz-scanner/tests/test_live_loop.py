"""The pieces of the live loop that a unit test can reach without a network.

These exist because the first review of the loop found it fetched Betfair
prices once at startup and reused them all afternoon while labelling rows
"betfair". Nothing here talks to an exchange; the session is driven with a
fake client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.database import models as m
from app.database.repository import Repository, session_factory
from app.drz import (
    BetfairSession,
    _betfair_inputs,
    _match_quote,
    _near_jump_ids,
    _persist,
)
from app.engine import RaceAlreadyJumpedError, value_race
from app.sources.base import RaceStub
from app.sources.betfair import BetfairMarket, BetfairRunnerQuote
from app.sources.sportsbet import parse_racecard

NOW = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)


def quote(name, cloth, prob, reliable=True, delayed=False):
    return BetfairRunnerQuote(
        market_id="1.1", selection_id=hash(name) & 0xFFFF, runner_name=name,
        cloth_number=cloth, best_back=None, best_lay=None, back_volume=None,
        lay_volume=None, total_matched=None, market_status="OPEN",
        fetched_at=NOW, probability=prob, reliable=reliable, delayed=delayed,
    )


class Runner:
    def __init__(self, name, saddle, source_id="r"):
        self.horse_name, self.saddlecloth, self.source_id = name, saddle, source_id


# --- runner matching --------------------------------------------------------

def test_cloth_number_is_the_join_key():
    quotes = [quote("7. Zoustar", 7, 0.3), quote("2. Fast Fixture", 2, 0.2)]
    assert _match_quote(quotes, Runner("Zoustar (NZ)", 7)).probability == 0.3
    assert _match_quote(quotes, Runner("Fast Fixture", 2)).probability == 0.2


def test_a_cloth_hit_with_a_different_name_is_refused():
    """That is what a late runner replacement looks like; never pair it."""
    quotes = [quote("7. Zoustar", 7, 0.3)]
    assert _match_quote(quotes, Runner("Some Other Horse", 7)) is None


def test_name_fallback_only_for_unprefixed_quotes():
    quotes = [quote("Zoustar", None, 0.3)]
    assert _match_quote(quotes, Runner("Zoustar", 7)).probability == 0.3
    assert _match_quote(quotes, Runner("Nope", 7)) is None


# --- exchange inputs --------------------------------------------------------

def _session_with(markets, fetched_at):
    s = Settings()
    bf = BetfairSession.__new__(BetfairSession)
    bf.settings = s
    bf.client = object()
    bf.markets = markets
    bf.books_fetched_at = fetched_at
    bf.reason_unavailable = None
    return bf


def _race(fixture):
    race = parse_racecard(fixture("racecard_open_9_runners.json"),
                          event_id="900101", fetched_at=NOW)
    race.start_time = NOW + timedelta(minutes=15)
    return race


def _markets_for(race, place_reliable=True):
    active = race.active_runners()
    win = BetfairMarket("1.w", "R4", "WIN", "Fixtureville", None, race_number=4,
                        runners=[quote(f"{r.saddlecloth}. {r.horse_name}", r.saddlecloth,
                                       1 / r.win_price) for r in active])
    place = BetfairMarket("1.p", "R4 To Be Placed", "PLACE", "Fixtureville", None,
                          race_number=4, number_of_winners=3,
                          runners=[quote(f"{r.saddlecloth}. {r.horse_name}", r.saddlecloth,
                                         0.4, reliable=place_reliable) for r in active])
    return [win, place]


def test_fresh_books_are_used(fixture):
    race = _race(fixture)
    bf = _session_with(_markets_for(race), NOW)
    probs, reliable, delayed, place = _betfair_inputs(bf, race, 3, NOW)
    assert probs and all(p is not None for p in probs)
    assert all(reliable)
    assert not delayed
    assert place and len(place) == len(race.active_runners())


def test_stale_books_are_refused_outright(fixture):
    """Old exchange prices must never be scored under the 'betfair' label."""
    race = _race(fixture)
    bf = _session_with(_markets_for(race), NOW - timedelta(hours=2))
    assert _betfair_inputs(bf, race, 3, NOW) == (None, None, False, None)


def test_unreliable_place_quotes_cannot_confirm(fixture):
    race = _race(fixture)
    bf = _session_with(_markets_for(race, place_reliable=False), NOW)
    _probs, _rel, _del, place = _betfair_inputs(bf, race, 3, NOW)
    assert place == {}, "a thin or wide place quote is an opinion, not evidence"


def test_place_market_with_wrong_terms_gives_no_confirmation(fixture):
    race = _race(fixture)
    markets = _markets_for(race)
    markets[1].number_of_winners = 2          # we are valuing 3 places
    bf = _session_with(markets, NOW)
    assert _betfair_inputs(bf, race, 3, NOW)[3] is None


def test_venue_prefix_still_matches(fixture):
    race = _race(fixture)
    race.venue = "Fixtureville Hillside"
    bf = _session_with(_markets_for(race), NOW)
    assert _betfair_inputs(bf, race, 3, NOW)[0] is not None


# --- the loop's clock -------------------------------------------------------

def stub(event_id, minutes, status="A"):
    return RaceStub(event_id=event_id, race_number=1,
                    start_time=NOW + timedelta(minutes=minutes), name="R1",
                    status_code=status, betting_status="PRICED", meeting_id="m",
                    meeting_name="M", class_name="Horses - Aus/NZ")


def test_near_jump_window_selects_the_right_races():
    settings = Settings()
    stubs = [stub("a", 3), stub("b", 9), stub("c", 11), stub("d", -0.5),
             stub("e", -5), stub("f", 4, status="R")]
    assert _near_jump_ids(stubs, settings, NOW) == {"a", "b", "d"}


def test_a_race_that_jumped_minutes_ago_is_not_valued(fixture, settings, calibration):
    race = _race(fixture)
    race.start_time = NOW - timedelta(minutes=5)
    with pytest.raises(RaceAlreadyJumpedError):
        value_race(race, calibration, settings, now=NOW)
    # ...but a race one minute past its advertised time is (they jump late).
    race.start_time = NOW - timedelta(seconds=60)
    assert value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True)


# --- persistence of what the scan saw --------------------------------------

def test_indicative_tote_quotes_are_stored_as_their_own_rows(
    fixture, settings, calibration, monkeypatch
):
    for target in ("app.config.get_settings", "app.drz.get_settings",
                   "app.database.repository.get_settings"):
        monkeypatch.setattr(target, lambda: settings)
    race = _race(fixture)
    vals = value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True)
    _persist(race, vals)

    Session = session_factory(settings.database_url)
    with Session() as session:
        rows = session.query(m.RunnerPrice).all()
        live = [r for r in rows if r.price_code == "L"]
        tote = [r for r in rows if r.price_type == "tote_indicative"]
        assert len(live) == len(race.runners)
        assert tote, "the MDP/TMD quotes the fixture carries must be kept"
        assert {r.price_code for r in tote} <= {"MDP", "TMD"}
        assert all(r.win_price for r in tote)
        assert all(r.place_price is None for r in tote), "no place price on those codes"
        # Repository session reuse: two persists, one engine.
        from app.database.repository import _ENGINES
        assert len([u for u in _ENGINES if settings.database_url in u]) == 1
