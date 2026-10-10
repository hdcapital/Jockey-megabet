"""Race value scanner: Sportsbet fixed odds against Betfair as the truth.

The maths is checked by hand, and one end-to-end pass runs the real
Sportsbet and Betfair clients against fake servers (the Betfair fake is the
one from test_betfair_end_to_end) so race selection, market pairing, the
fetch order and the comparison are all exercised together.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.http import ArchivingClient
from app.race_value import (
    ValueScanner,
    append_csv,
    compare_race,
    exchange_fair_probabilities,
    lay_lock_profit,
    listing_dates,
    midpoint_probability,
    upcoming_races,
)
from app.sources.base import RaceInfo, RunnerInfo
from app.sources.betfair import BetfairMarket, BetfairRunnerQuote
from app.sources.sportsbet import SportsbetClient
from tests.test_betfair_end_to_end import NOW, FakeBetfair, client, settings


def quote(sid, name, back, lay, cloth=None):
    return BetfairRunnerQuote(
        market_id="1.1", selection_id=sid, runner_name=name, best_back=back,
        best_lay=lay, back_volume=100.0, lay_volume=50.0, total_matched=None,
        market_status="OPEN", fetched_at=NOW, cloth_number=cloth,
    )


def sb_runner(rid, horse, odds, cloth, status="active"):
    return RunnerInfo(source="sportsbet", source_id=rid, horse_name=horse,
                      saddlecloth=cloth, status=status, win_odds=odds,
                      odds_timestamp=NOW)


def market(quotes, matched=50_000.0, **kw):
    m = BetfairMarket(market_id="1.1", market_name="R1 1200m", venue="Randwick",
                      market_start=NOW, race_number=1, runners=quotes,
                      book_status="OPEN", total_matched=matched, fetched_at=NOW)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def race(runners, **kw):
    r = RaceInfo(source="sportsbet", source_id="e1", race_number=1,
                 start_time=NOW + timedelta(minutes=8), status="open", runners=runners)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


# -- maths -------------------------------------------------------------------

def test_midpoint_is_taken_in_probability_space():
    assert midpoint_probability(2.0, 2.5) == pytest.approx((0.5 + 0.4) / 2)
    assert midpoint_probability(None, 4.0) == pytest.approx(0.25)
    assert midpoint_probability(None, None) is None


def test_exchange_book_is_normalised_and_excluded_runners_leave_the_field():
    qs = [quote(1, "1. A", 2.0, 2.04), quote(2, "2. B", 3.0, 3.1), quote(3, "3. C", 8.0, 8.4)]
    fair, book = exchange_fair_probabilities(qs)
    assert sum(fair.values()) == pytest.approx(1.0)
    assert book == pytest.approx(sum(midpoint_probability(q.best_back, q.best_lay) for q in qs))
    fair2, _ = exchange_fair_probabilities(qs, exclude={3})
    assert set(fair2) == {1, 2} and sum(fair2.values()) == pytest.approx(1.0)
    assert fair2[1] > fair[1]


def test_lay_lock_equalises_both_outcomes_after_commission():
    b, l, c = 3.0, 2.8, 0.08
    lock = lay_lock_profit(b, l, c)
    lay_stake = b / (l - c)
    wins = (b - 1) - lay_stake * (l - 1)
    loses = lay_stake * (1 - c) - 1
    assert wins == pytest.approx(loses) == pytest.approx(lock)
    assert lay_lock_profit(2.5, 2.6, 0.08) < 0  # no lock when lay > back


# -- race selection -----------------------------------------------------------

def meetings_payload(now):
    ts = lambda m: int((now + timedelta(minutes=m)).timestamp())
    return [{
        "name": "Randwick", "className": "Horses - Aus/NZ",
        "events": [
            {"id": 1, "raceNumber": 1, "startTime": ts(-1), "statusCode": "A"},   # late jump
            {"id": 2, "raceNumber": 2, "startTime": ts(12), "statusCode": "A"},
            {"id": 3, "raceNumber": 3, "startTime": ts(35), "statusCode": "A"},   # too far
            {"id": 4, "raceNumber": 4, "startTime": ts(5), "statusCode": "R"},    # resulted
            {"id": 5, "raceNumber": 5, "startTime": ts(-10), "statusCode": "A"},  # long gone
        ],
    }]


def test_upcoming_races_window_grace_and_status():
    ups = upcoming_races(meetings_payload(NOW), NOW, timedelta(minutes=20), timedelta(minutes=2))
    assert [u.race_number for u in ups] == [1, 2]


def test_listing_dates_include_yesterday_before_6am_sydney():
    assert len(listing_dates(datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc))) == 1
    early = listing_dates(datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc))  # 3am AEST
    assert [d.isoformat() for d in early] == ["2026-10-02", "2026-10-03"]


# -- comparison ---------------------------------------------------------------

def test_compare_race_ev_against_normalised_exchange():
    qs = [quote(1, "1. Alpha", 2.0, 2.04, 1), quote(2, "2. Bravo", 3.0, 3.1, 2),
          quote(3, "3. Charlie", 6.0, 6.2, 3)]
    rc = race([sb_runner("a", "Alpha", 2.2, 1), sb_runner("b", "Bravo", 2.9, 2),
               sb_runner("c", "Charlie", 5.5, 3)])
    rows, notes = compare_race("Randwick", rc, market(qs), settings())
    fair, _ = exchange_fair_probabilities(qs)
    by = {r.horse: r for r in rows}
    assert by["Alpha"].ev == pytest.approx(fair[1] * 2.2 - 1)
    assert by["Alpha"].ev > 0 > by["Bravo"].ev
    assert by["Alpha"].ev_at_lay == pytest.approx(2.2 / 2.04 - 1)
    assert by["Alpha"].kelly == pytest.approx(by["Alpha"].ev / 1.2)
    assert by["Bravo"].kelly == 0
    assert by["Alpha"].sb_overround == pytest.approx(1 / 2.2 + 1 / 2.9 + 1 / 5.5)
    assert all(r.reliable for r in rows) and notes == []


def test_sportsbet_scratching_is_removed_from_the_exchange_book():
    qs = [quote(1, "1. Alpha", 2.0, 2.04, 1), quote(2, "2. Bravo", 3.0, 3.1, 2),
          quote(3, "3. Charlie", 6.0, 6.2, 3)]
    rc = race([sb_runner("a", "Alpha", 2.0, 1), sb_runner("b", "Bravo", 2.8, 2),
               sb_runner("c", "Charlie", None, 3, status="scratched")])
    rows, notes = compare_race("Randwick", rc, market(qs), settings())
    assert {r.horse for r in rows} == {"Alpha", "Bravo"}
    assert sum(r.p_fair for r in rows) == pytest.approx(1.0)
    assert all("RENORM" in r.flags for r in rows)
    assert any("Charlie scratched on Sportsbet" in n for n in notes)


def test_runner_removed_on_betfair_is_reported_not_valued():
    qs = [quote(1, "1. Alpha", 2.0, 2.04, 1), quote(2, "2. Bravo", 2.0, 2.04, 2)]
    rc = race([sb_runner("a", "Alpha", 2.1, 1), sb_runner("b", "Bravo", 2.1, 2),
               sb_runner("c", "Charlie", 9.0, 3)])
    rows, notes = compare_race("Randwick", rc, market(qs, removed_names=["3. Charlie"]),
                               settings())
    assert {r.horse for r in rows} == {"Alpha", "Bravo"}
    assert any("still prices Charlie" in n for n in notes)


@pytest.mark.parametrize("kw,expected", [
    (dict(inplay=True), "in-play"),
    (dict(book_status="SUSPENDED"), "SUSPENDED"),
])
def test_unbettable_exchange_markets_are_skipped(kw, expected):
    rc = race([sb_runner("a", "Alpha", 2.1, 1)])
    rows, notes = compare_race("Randwick", rc, market([quote(1, "1. Alpha", 2.0, 2.04, 1)], **kw),
                               settings())
    assert rows == [] and expected in notes[0]


def test_suspended_sportsbet_market_is_skipped():
    rc = race([sb_runner("a", "Alpha", 2.1, 1)], win_market_status="S")
    rows, notes = compare_race("Randwick", rc, market([quote(1, "1. Alpha", 2.0, 2.04, 1)]),
                               settings())
    assert rows == [] and "suspended" in notes[0]


def test_gates_flag_wide_thin_delayed_and_suspect_rows():
    qs = [quote(1, "1. Alpha", 2.0, 2.04, 1), quote(2, "2. Long", 20.0, 30.0, 2)]
    rc = race([sb_runner("a", "Alpha", 2.0, 1), sb_runner("b", "Long", 40.0, 2)])
    rows, _ = compare_race("Randwick", rc, market(qs, matched=300.0), settings())
    by = {r.horse: r for r in rows}
    assert any(f.startswith("THIN") for f in by["Alpha"].flags)
    assert any(f.startswith("SPREAD") for f in by["Long"].flags)
    assert "SUSPECT" in by["Long"].flags and not by["Long"].reliable
    rows, _ = compare_race("Randwick", rc, market(qs, matched=0.0, delayed=True), settings())
    assert all("DELAYED key" in r.flags and not r.reliable for r in rows)


# -- end to end ---------------------------------------------------------------

class FakeSportsbet:
    """AllRacing listing + racecards for Randwick, in the live-verified shapes."""

    ODDS = {1: 2.3, 2: 3.9, 3: 4.1}  # vs Betfair 2.0/2.1, 4.0/4.2, 4.0/4.4

    def __init__(self):
        self.paths: list[str] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if "/AllRacing/" in path:
            ts = lambda m: int((NOW + timedelta(minutes=m)).timestamp())
            return httpx.Response(200, json={"dates": [{"sections": [{
                "raceType": "Horses", "meetings": [{
                    "id": 1, "name": "Randwick", "className": "Horses - Aus/NZ",
                    "events": [
                        {"id": 101, "raceNumber": 1, "startTime": ts(0), "statusCode": "A"},
                        {"id": 102, "raceNumber": 2, "startTime": ts(35), "statusCode": "A"},
                    ]}]}]}]})
        if path.endswith("/Events/101/Racecard"):
            return httpx.Response(200, json={
                "competitionName": "Randwick", "raceNumber": 1,
                "startTime": int(NOW.timestamp()), "statusCode": "A",
                "bettingStatus": "PRICED",
                "markets": [{"name": "Win or Place", "statusCode": "A", "selections": [
                    {"id": 500 + n, "name": f"Horse {'ABC'[n - 1]}", "runnerNumber": n,
                     "jockey": "J Rider", "statusCode": "A",
                     "prices": [{"priceCode": "L", "winPrice": o}]}
                    for n, o in self.ODDS.items()]}]})
        return httpx.Response(404, text="not found")


def test_scan_once_end_to_end(tmp_path):
    cfg = settings(value_betfair_countries="AU", value_min_matched=1000.0)
    fake_bf, fake_sb = FakeBetfair(), FakeSportsbet()
    sb = SportsbetClient(ArchivingClient("sportsbet", archive=False,
                                         transport=fake_sb.transport, settings=cfg))
    scanner = ValueScanner(cfg, sportsbet=sb, betfair=client(fake_bf, value_betfair_countries="AU"))
    res = scanner.scan_once(NOW + timedelta(seconds=30))

    assert res.error is None
    assert res.races_in_window == 1 and res.races_compared == 1
    # Only the race in the window had its racecard fetched.
    assert not any("/Events/102/" in p for p in fake_sb.paths)
    # Books were fetched after the catalogue, once.
    rpcs = [p for h, p in fake_bf.calls if h == "api.betfair.com"]
    assert len(rpcs) == 2

    qs = {1: (2.0, 2.1), 2: (4.0, 4.2), 3: (4.0, 4.4)}
    mids = {n: midpoint_probability(*bl) for n, bl in qs.items()}
    book = sum(mids.values())
    expected = {f"Horse {'ABC'[n - 1]}": mids[n] / book * FakeSportsbet.ODDS[n] - 1 for n in qs}
    assert [r.horse for r in res.rows] == sorted(expected, key=expected.get, reverse=True)
    for r in res.rows:
        assert r.ev == pytest.approx(expected[r.horse])

    path = append_csv(res, tmp_path)
    lines = path.read_text().splitlines()
    assert lines[0].startswith("scanned_at,venue,race") and len(lines) == 4


def test_login_refusal_backs_off_instead_of_retrying_every_minute():
    cfg = settings(value_betfair_countries="AU", betfair_password="wrong")
    fake_bf, fake_sb = FakeBetfair(), FakeSportsbet()
    sb = SportsbetClient(ArchivingClient("sportsbet", archive=False,
                                         transport=fake_sb.transport, settings=cfg))
    scanner = ValueScanner(cfg, sportsbet=sb,
                           betfair=client(fake_bf, betfair_password="wrong"))
    first = scanner.scan_once(NOW)
    logins = sum(1 for _, p in fake_bf.calls if p == "/api/login")
    second = scanner.scan_once(NOW + timedelta(minutes=1))
    assert "INVALID_USERNAME_OR_PASSWORD" in first.error
    assert "next attempt" in second.error
    assert sum(1 for _, p in fake_bf.calls if p == "/api/login") == logins
