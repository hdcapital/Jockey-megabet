"""Betfair runner/market matching and delayed-key handling.

Written against the shape seen live on 2026-09-19: the catalogue loaded
(127 AU win markets) yet whole fields logged "runner unmatched on Betfair"
and every book reported no matched volume.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.matching.meetings import find_betfair_market
from app.matching.runners import match_race_runners
from app.sources.base import RaceInfo, RunnerInfo
from app.sources.betfair import (
    BetfairClient,
    BetfairMarket,
    BetfairRunnerQuote,
    derive_probability,
    market_is_delayed,
    split_runner_name,
)

NOW = datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)


def quote(name: str, cloth: int | None = None, prob: float | None = 0.25) -> BetfairRunnerQuote:
    return BetfairRunnerQuote(
        market_id="1.1", selection_id=hash(name) & 0xFFFF, runner_name=name,
        best_back=4.0, best_lay=4.2, back_volume=100.0, lay_volume=100.0,
        total_matched=1000.0, market_status="OPEN", fetched_at=NOW,
        probability=prob, reliable=True, cloth_number=cloth,
    )


def runner(name: str, cloth: int | None = None) -> RunnerInfo:
    return RunnerInfo(source="sportsbet", source_id=name.lower(), horse_name=name,
                      saddlecloth=cloth, win_odds=4.0)


# --- runner matching ---------------------------------------------------------

def test_split_runner_name_handles_cloth_prefix():
    assert split_runner_name("7. Zoustar") == (7, "Zoustar")
    assert split_runner_name("12.Winx (NZ)") == (12, "Winx (NZ)")
    assert split_runner_name("Zoustar") == (None, "Zoustar")
    assert split_runner_name("") == (None, "")


def test_cloth_number_matches_before_name():
    """Same saddlecloth, name agrees -> matched via cloth even when the
    Betfair spelling differs only by the prefix."""
    quotes = [quote("1. Bombay Boom", 1), quote("2. Reds City", 2)]
    m = match_race_runners([runner("Bombay Boom", 1), runner("Reds City", 2)], quotes)
    assert [x.status for x in m] == ["matched", "matched"]
    assert m[0].betfair_quote.runner_name == "1. Bombay Boom"


def test_cloth_number_never_overrides_a_different_name():
    """Cloth 3 on Sportsbet is a different horse on Betfair: fall back to the
    name, and stay unmatched rather than take the wrong runner."""
    quotes = [quote("3. Puffin", 3), quote("4. Manoora", 4)]
    m = match_race_runners([runner("Cuban Cigar", 3), runner("Manoora", 9)], quotes)
    assert m[0].status == "unmatched"
    assert m[1].status == "matched" and m[1].betfair_quote.runner_name == "4. Manoora"


def test_name_only_match_when_no_cloth_on_either_side():
    m = match_race_runners([runner("It's Kaos")], [quote("Its Kaos")])
    assert m[0].status == "matched"


def test_unmatched_race_logs_one_summary_naming_what_betfair_lists(caplog):
    """When most of a field is unmatched, the log must say what Betfair
    actually lists for the market, so a wrong market is visible."""
    quotes = [quote("1. Other Horse", 1), quote("2. Another", 2)]
    with caplog.at_level(logging.INFO, logger="app.matching.runners"):
        m = match_race_runners(
            [runner("Bombay Boom", 1), runner("Reds City", 2)], quotes,
            race_label="Randwick R4",
        )
    assert [x.status for x in m] == ["unmatched", "unmatched"]
    summaries = [r for r in caplog.records if "betfair runners Randwick R4" in r.getMessage()]
    assert len(summaries) == 1 and summaries[0].levelno == logging.WARNING
    assert "1. Other Horse" in summaries[0].getMessage()
    assert not [r for r in caplog.records if r.levelno == logging.INFO
                and "runner unmatched" in r.getMessage()], "per-runner lines are DEBUG"


# --- market lookup -----------------------------------------------------------

def _race(number: int, start: datetime) -> RaceInfo:
    return RaceInfo(source="sportsbet", source_id=f"r{number}", race_number=number,
                    start_time=start, status="open")


def test_market_hours_away_is_rejected_not_taken():
    """Same venue and race number the next day (the catalogue spans 52h)
    was previously accepted when it was the only candidate."""
    tomorrow = BetfairMarket("1.9", "R4 1400m", "Randwick", NOW + timedelta(hours=26), 4)
    assert find_betfair_market("Randwick", _race(4, NOW), [tomorrow]) is None


def test_market_within_tolerance_is_taken():
    today = BetfairMarket("1.1", "R4 1400m", "Randwick", NOW + timedelta(minutes=5), 4)
    tomorrow = BetfairMarket("1.9", "R4 1400m", "Randwick", NOW + timedelta(hours=26), 4)
    assert find_betfair_market("Randwick", _race(4, NOW), [tomorrow, today]) is today


# --- delayed key -------------------------------------------------------------

def test_delayed_key_reliability_is_spread_only():
    p, reliable, detail = derive_probability(4.0, 4.2, 0.0, 500.0, 0.25, delayed=True)
    assert reliable and abs(p - 1 / 4.1) < 1e-9 and "delayed" in detail
    # tighter gate than the liquid rule: 20% is fine live, not on a delayed key
    p, reliable, _ = derive_probability(4.0, 4.8, 0.0, 500.0, 0.25, delayed=True)
    assert not reliable and abs(p - 0.25) < 1e-9


def test_market_is_delayed_needs_zero_everywhere():
    assert market_is_delayed({"totalMatched": 0, "runners": [{"totalMatched": 0}]})
    assert not market_is_delayed({"totalMatched": 0, "runners": [{"totalMatched": 12.5}]})
    assert not market_is_delayed({"totalMatched": 900.0, "runners": []})


class _Settings:
    betfair_min_liquidity = 500.0
    betfair_max_relative_spread = 0.25
    betfair_key_delayed = "auto"
    betfair_delayed_max_relative_spread = 0.10


def _book(market_id: str, matched: float) -> dict:
    return {
        "marketId": market_id, "status": "OPEN", "totalMatched": matched,
        "runners": [
            {"selectionId": 11, "status": "ACTIVE", "totalMatched": matched,
             "ex": {"availableToBack": [{"price": 4.0, "size": 50}],
                    "availableToLay": [{"price": 4.2, "size": 50}]}},
            {"selectionId": 12, "status": "REMOVED", "totalMatched": 0,
             "ex": {"availableToBack": [], "availableToLay": []}},
        ],
    }


def _client(books: list[dict]) -> BetfairClient:
    c = BetfairClient.__new__(BetfairClient)
    c.settings = _Settings()
    c._rpc = lambda method, params, **_: books
    return c


def _markets() -> list[BetfairMarket]:
    ms = [BetfairMarket("1.1", "R1", "Randwick", NOW, 1), BetfairMarket("1.2", "R2", "Randwick", NOW, 2)]
    for m in ms:
        m._catalogue_runners = {11: "7. Zoustar", 12: "8. Scratched One"}  # type: ignore[attr-defined]
    return ms


def test_delayed_is_decided_from_every_book_not_one_thin_market():
    ms = _markets()
    _client([_book("1.1", 0.0), _book("1.2", 2500.0)]).fetch_market_books(ms)
    assert not any(m.delayed for m in ms)
    r = ms[0].runners[0]
    assert not r.reliable and "thin market" in r.detail

    ms = _markets()
    _client([_book("1.1", 0.0), _book("1.2", 0.0)]).fetch_market_books(ms)
    assert all(m.delayed for m in ms)
    r = ms[1].runners[0]
    assert r.reliable and "delayed key" in r.detail
    assert r.cloth_number == 7 and r.runner_name == "7. Zoustar"
    assert len(ms[1].runners) == 1, "removed runners are not quoted"


def test_delayed_override_false_keeps_liquidity_rule():
    ms = _markets()
    c = _client([_book("1.1", 0.0), _book("1.2", 0.0)])
    c.settings.betfair_key_delayed = "false"
    c.fetch_market_books(ms)
    assert not any(m.delayed for m in ms) and not ms[0].runners[0].reliable


def test_missing_book_is_reported_not_silent(caplog):
    ms = _markets()
    with caplog.at_level(logging.WARNING, logger="app.sources.betfair"):
        _client([_book("1.1", 900.0)]).fetch_market_books(ms)
    assert ms[1].runners == [] and ms[1].book_status is None
    assert any("no book returned for Randwick R2" in r.getMessage() for r in caplog.records)


def test_engine_matches_each_race_once(monkeypatch):
    """A jockey with several rides in one race must not re-run the race
    match per ride (that is what multiplied the unmatched log lines)."""
    import app.engine as engine
    calls: list[str] = []
    real = engine.find_betfair_market

    def counting(venue, race, markets):
        calls.append(race.source_id)
        return real(venue, race, markets)

    monkeypatch.setattr(engine, "find_betfair_market", counting)
    from app.config import Settings
    from app.matching.jockeys import JockeyRideCard, RideMatch

    race = RaceInfo(source="sportsbet", source_id="r1", race_number=1, start_time=NOW,
                    status="open", runners=[runner("A", 1), runner("B", 2), runner("C", 3)])
    card = JockeyRideCard(
        source_jockey_name="X", normalized_jockey_name="x",
        rides=[RideMatch(race=race, runner=r) for r in race.runners[:2]],
        match_status="matched",
    )
    market = BetfairMarket("1.1", "R1", "Randwick", NOW, 1,
                           runners=[quote("1. A", 1), quote("2. B", 2), quote("3. C", 3)])
    rides = engine.build_ride_probabilities(card, "Randwick", Settings(), [market])
    assert calls == ["r1"], "one lookup for the race, not one per ride"
    assert all(r.betfair_p is not None for r in rides)
