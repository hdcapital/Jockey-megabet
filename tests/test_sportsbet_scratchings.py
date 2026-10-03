"""Scratchings in the live Sportsbet racecard shape (verified 2026-10-03).

A scratched runner keeps its selection in the "Win or Place" market with
statusCode "S" while the market stays "A"; its price list still carries
NTP/NTS entries and may carry a stale live price. The parser must mark it
scratched and never price it. A suspended market ("S" on the market and
every selection) is not a scratching.
"""

from datetime import datetime, timezone

from app.sources.sportsbet import SportsbetClient, _extract_price

NOW = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


def _sel(sid, name, number, code, jockey, prices):
    return {"id": sid, "name": name, "runnerNumber": number, "statusCode": code,
            "isOut": False, "jockey": jockey, "prices": prices}


def _card(market_code="A", scratched_prices=None):
    if scratched_prices is None:
        scratched_prices = [
            {"priceCode": "NTP", "winPrice": 163.9},
            {"priceCode": "NTS", "winPrice": 163.9},
            {"priceCode": "L", "winPrice": 26.0},  # stale live price left behind
        ]
    return {
        "competitionName": "Randwick", "raceNumber": 1, "statusCode": "A",
        "bettingStatus": "PRICED",
        "markets": [
            {"id": 255342883, "name": "Win", "statusCode": market_code, "selections": [
                {"id": 1261729836, "name": "Just In Time", "statusCode": "S", "isOut": False,
                 "prices": [{"priceCode": "L"}]},
            ]},
            {"id": 255342878, "name": "Top 2", "statusCode": market_code, "selections": [
                {"id": 1261729778, "name": "Just In Time", "statusCode": "S", "isOut": False,
                 "prices": [{"priceCode": "L", "winPrice": 8.25}]},
            ]},
            {"id": 255342874, "name": "Win or Place", "statusCode": market_code, "selections": [
                _sel(1261729743, "Just In Time", 3, "S", "Regan Bayliss", scratched_prices),
                _sel(1261729701, "Columbus", 1, "A", "J McDonald",
                     [{"priceCode": "L", "winPrice": 3.0}, {"priceCode": "NTP", "winPrice": 3.2}]),
                _sel(1261729702, "Doradus", 2, "A", "T Berry",
                     [{"priceCode": "L", "winPrice": 4.0}]),
            ]},
        ],
    }


def _parse(payload):
    return SportsbetClient.parse_racecard(SportsbetClient.__new__(SportsbetClient),
                                          payload, event_id="10997245", fetched_at=NOW)


def test_selection_s_in_open_market_is_scratched_even_with_prices():
    card = _parse(_card())
    runners = {r.horse_name: r for r in card.races[0].runners}
    jit = runners["Just In Time"]
    assert jit.status == "scratched" and jit.win_odds is None
    assert jit.saddlecloth == 3 and jit.jockey_name == "Regan Bayliss"
    assert [r.horse_name for r in card.races[0].active_runners()] == ["Columbus", "Doradus"]
    assert runners["Columbus"].win_odds == 3.0, "live price, not NTP"


def test_selection_s_with_withdrawn_price_is_scratched():
    card = _parse(_card(scratched_prices=[{"priceCode": "L"}]))
    jit = next(r for r in card.races[0].runners if r.horse_name == "Just In Time")
    assert jit.status == "scratched" and jit.win_odds is None


def test_suspended_market_is_not_a_scratching():
    payload = _card(market_code="S")
    for sel in payload["markets"][2]["selections"]:
        sel["statusCode"] = "S"
    card = _parse(payload)
    runners = {r.horse_name: r for r in card.races[0].runners}
    assert runners["Columbus"].status == "active" and runners["Columbus"].win_odds == 3.0
    # The scratched one still has a live price in this shape, so under a
    # suspended market it cannot be told apart; the Betfair REMOVED
    # cross-check names it in the log instead.
    assert runners["Just In Time"].status == "active"


def test_tagged_price_list_only_uses_live_entry():
    assert _extract_price({"prices": [{"priceCode": "NTP", "winPrice": 9.0},
                                      {"winPrice": 7.0}]}) is None
    assert _extract_price({"prices": [{"priceCode": "NTP", "winPrice": 9.0},
                                      {"priceCode": "L", "winPrice": 7.0}]}) == 7.0
    assert _extract_price({"prices": [{"winPrice": 7.0}]}) == 7.0, "untagged legacy shape"
