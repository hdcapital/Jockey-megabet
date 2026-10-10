"""Race value scanner: comparison rules and one full run end to end.

The end-to-end test drives the real Sportsbet and Betfair clients against
fake servers through two scans (signal, then confirmation), the jump
(closing price) and the settlement (result, BSP, results CSV, report).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from rich.console import Console

from app.http import ArchivingClient
from app.sources.base import RaceInfo, RunnerInfo
from app.sources.betfair import BetfairMarket, BetfairRunnerQuote
from app.sources.sportsbet import SportsbetClient
from app.value.compare import compare_race
from app.value.display import render
from app.value.report import load_results, render_report, results_line
from app.value.scanner import ValueScanner, listing_dates, place_market_for, upcoming_races
from app.value.tracker import Tracker
from tests.test_betfair_end_to_end import NOW, FakeBetfair, client, settings


def quote(sid, name, back, lay, cloth=None, size=1000.0):
    return BetfairRunnerQuote(
        market_id="1.1", selection_id=sid, runner_name=name, best_back=back,
        best_lay=lay, back_volume=size, lay_volume=size, total_matched=None,
        market_status="OPEN", fetched_at=NOW, cloth_number=cloth,
        back_ladder=[(back, size)] if back else [], lay_ladder=[(lay, size)] if lay else [],
    )


def sb_runner(rid, horse, odds, cloth, status="active", place=None):
    return RunnerInfo(source="sportsbet", source_id=rid, horse_name=horse,
                      saddlecloth=cloth, status=status, win_odds=odds,
                      odds_timestamp=NOW, place_odds=place)


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


def mid(back, lay):
    return (1 / back + 1 / lay) / 2


# -- race selection -------------------------------------------------------------

def test_upcoming_races_window_grace_and_status():
    ts = lambda m: int((NOW + timedelta(minutes=m)).timestamp())
    meetings = [{"name": "Randwick", "events": [
        {"id": 1, "raceNumber": 1, "startTime": ts(-1), "statusCode": "A"},   # late jump
        {"id": 2, "raceNumber": 2, "startTime": ts(12), "statusCode": "A"},
        {"id": 3, "raceNumber": 3, "startTime": ts(35), "statusCode": "A"},   # too far
        {"id": 4, "raceNumber": 4, "startTime": ts(5), "statusCode": "R"},    # resulted
        {"id": 5, "raceNumber": 5, "startTime": ts(-10), "statusCode": "A"},  # long gone
    ]}]
    ups = upcoming_races(meetings, NOW, timedelta(minutes=20), timedelta(minutes=2))
    assert [u.race_number for u in ups] == [1, 2]


def test_listing_dates_include_yesterday_before_6am_sydney():
    assert len(listing_dates(datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc))) == 1
    early = listing_dates(datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc))  # 3am AEST
    assert [d.isoformat() for d in early] == ["2026-10-02", "2026-10-03"]


def test_place_market_is_paired_by_event_and_start():
    w = BetfairMarket("1.1", "R1 1200m", "Randwick", NOW, 1, event_id="e1")
    other = BetfairMarket("1.9", "To Be Placed", "Randwick", NOW + timedelta(minutes=30),
                          None, market_type="PLACE", event_id="e1")
    p = BetfairMarket("1.2", "To Be Placed", "Randwick", NOW, None,
                      market_type="PLACE", event_id="e1")
    assert place_market_for(w, [w, other, p]) is p
    assert place_market_for(w, [w, other]) is None


# -- win comparison ------------------------------------------------------------

def test_win_ev_against_normalised_exchange():
    qs = [quote(1, "1. Alpha", 2.0, 2.02, 1), quote(2, "2. Bravo", 3.0, 3.05, 2),
          quote(3, "3. Charlie", 6.0, 6.2, 3)]
    rc = race([sb_runner("a", "Alpha", 2.2, 1), sb_runner("b", "Bravo", 2.9, 2),
               sb_runner("c", "Charlie", 5.5, 3)])
    rows, notes = compare_race("Randwick", rc, market(qs), settings())
    book = mid(2.0, 2.02) + mid(3.0, 3.05) + mid(6.0, 6.2)
    by = {r.horse: r for r in rows}
    assert by["Alpha"].ev == pytest.approx(mid(2.0, 2.02) / book * 2.2 - 1)
    assert by["Alpha"].ev > 0 > by["Bravo"].ev
    assert by["Alpha"].ev_at_lay == pytest.approx(2.2 / 2.02 - 1)
    assert by["Alpha"].kelly == pytest.approx(by["Alpha"].ev / 1.2)
    assert by["Alpha"].spread_ticks == 1 and by["Charlie"].spread_ticks == 1
    assert all(r.reliable and r.kind == "WIN" for r in rows) and notes == []


def test_sportsbet_scratching_is_removed_from_the_exchange_book():
    qs = [quote(1, "1. Alpha", 2.0, 2.02, 1), quote(2, "2. Bravo", 3.0, 3.05, 2),
          quote(3, "3. Charlie", 6.0, 6.2, 3)]
    rc = race([sb_runner("a", "Alpha", 2.0, 1), sb_runner("b", "Bravo", 2.8, 2),
               sb_runner("c", "Charlie", None, 3, status="scratched")])
    rows, notes = compare_race("Randwick", rc, market(qs), settings())
    assert {r.horse for r in rows} == {"Alpha", "Bravo"}
    assert sum(r.p_fair for r in rows) == pytest.approx(1.0)
    assert all("RENORM" in r.flags for r in rows)
    assert any("Charlie scratched on Sportsbet" in n for n in notes)


def test_runner_removed_on_betfair_is_reported_not_valued():
    qs = [quote(1, "1. Alpha", 2.0, 2.02, 1), quote(2, "2. Bravo", 2.0, 2.02, 2)]
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
    rows, notes = compare_race("Randwick", rc,
                               market([quote(1, "1. Alpha", 2.0, 2.02, 1)], **kw), settings())
    assert rows == [] and expected in notes[0]


def test_suspended_sportsbet_market_is_skipped():
    rc = race([sb_runner("a", "Alpha", 2.1, 1)], win_market_status="S")
    rows, notes = compare_race("Randwick", rc, market([quote(1, "1. Alpha", 2.0, 2.02, 1)]),
                               settings())
    assert rows == [] and "suspended" in notes[0]


def test_gates_flag_wide_thin_shallow_delayed_and_suspect_rows():
    qs = [quote(1, "1. Alpha", 2.0, 2.02, 1), quote(2, "2. Long", 20.0, 30.0, 2),
          quote(3, "3. Thin", 4.0, 4.1, 3, size=10.0)]
    rc = race([sb_runner("a", "Alpha", 2.0, 1), sb_runner("b", "Long", 40.0, 2),
               sb_runner("c", "Thin", 4.0, 3)])
    rows, _ = compare_race("Randwick", rc, market(qs, matched=300.0), settings())
    by = {r.horse: r for r in rows}
    assert any(f.startswith("THIN") for f in by["Alpha"].flags)
    assert "SPREAD 10 ticks" in by["Long"].flags
    assert "SUSPECT" in by["Long"].flags and not by["Long"].reliable
    assert "SHALLOW" in by["Thin"].flags
    rows, _ = compare_race("Randwick", rc, market(qs, matched=0.0, delayed=True), settings())
    assert all("DELAYED key" in r.flags and not r.reliable for r in rows)


# -- place comparison ------------------------------------------------------------

def place_case(n_runners=5, sb_places=2, bf_places=2):
    win = [quote(i, f"{i}. H{i}", o, round(o * 1.02, 2), i)
           for i, o in zip(range(1, n_runners + 1), (2.5, 4.0, 6.0, 8.0, 12.0, 15.0, 20.0, 30.0))]
    place = [quote(100 + i, f"{i}. H{i}", o, round(o + 0.02, 2), i)
             for i, o in zip(range(1, n_runners + 1), (1.3, 1.7, 2.2, 2.8, 3.8, 4.4, 5.5, 7.0))]
    runners = [sb_runner(str(i), f"H{i}", 2.0, i, place=1.9) for i in range(1, n_runners + 1)]
    pm = market(place, matched=5000.0, market_id="1.2", market_type="PLACE",
                number_of_winners=bf_places)
    return race(runners, places=sb_places), market(win), pm


def test_place_rows_use_the_place_market_scaled_to_the_places():
    rc, wm, pm = place_case()
    rows, notes = compare_race("Randwick", rc, wm, settings(), place_market=pm)
    places = [r for r in rows if r.kind == "PLACE"]
    assert len(places) == 5 and notes == []
    assert sum(r.p_fair for r in places) == pytest.approx(2.0)
    raw = {i: mid(o, round(o + 0.02, 2)) for i, o in zip(range(1, 6), (1.3, 1.7, 2.2, 2.8, 3.8))}
    h1 = next(r for r in places if r.horse == "H1")
    assert h1.p_fair == pytest.approx(raw[1] * 2 / sum(raw.values()))
    assert h1.ev == pytest.approx(h1.p_fair * 1.9 - 1)
    assert h1.places == 2 and h1.market_id == "1.2"


def test_place_terms_must_match():
    rc, wm, pm = place_case(sb_places=2, bf_places=3)
    rows, notes = compare_race("Randwick", rc, wm, settings(), place_market=pm)
    assert all(r.kind == "WIN" for r in rows)
    assert "place terms differ" in notes[0]


def test_eight_runner_place_terms_are_flagged_fragile():
    rc, wm, pm = place_case(n_runners=8, sb_places=3, bf_places=3)
    rows, _ = compare_race("Randwick", rc, wm, settings(), place_market=pm)
    assert all("8 RUNNERS" in r.flags for r in rows if r.kind == "PLACE")
    only_fragile = [r for r in rows if r.flags == ["8 RUNNERS"]]
    assert only_fragile and all(r.reliable for r in only_fragile)  # a warning, not a gate


# -- end to end -------------------------------------------------------------------

START = NOW + timedelta(minutes=5)
WIN_PX = {1: (3.0, 3.05), 2: (4.0, 4.1), 3: (5.0, 5.1), 4: (8.0, 8.2), 5: (12.0, 12.5)}
PLACE_PX = {1: (1.62, 1.64), 2: (1.92, 1.94), 3: (2.3, 2.36), 4: (3.5, 3.6), 5: (5.0, 5.2)}
SB_WIN = {1: 3.4, 2: 3.8, 3: 4.6, 4: 7.5, 5: 11.0}
SB_PLACE = {1: 1.55, 2: 2.1, 3: 2.2, 4: 3.3, 5: 4.6}


class FakeExchange(FakeBetfair):
    """FakeBetfair's login and JSON-RPC, with a win and a place market whose
    state the test moves through open -> in-play -> settled."""

    def __init__(self):
        super().__init__()
        self.state = "OPEN"  # OPEN | INPLAY | CLOSED
        self.markets = {
            "1.100": ("WIN", "R1 1200m Hcap", WIN_PX, 50_000.0, 1),
            "1.101": ("PLACE", "To Be Placed", PLACE_PX, 6_000.0, 2),
        }

    def _catalogue(self, params):
        f = params["filter"]
        frm = datetime.fromisoformat(f["marketStartTime"]["from"])
        to = datetime.fromisoformat(f["marketStartTime"]["to"])
        if not frm <= START < to:
            return []
        return [{
            "marketId": mid_, "marketName": name,
            "marketStartTime": START.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "event": {"id": "e1", "venue": "Randwick", "countryCode": "AU"},
            "runners": [{"selectionId": (0 if kind == "WIN" else 100) + n,
                         "runnerName": f"{n}. Horse {'ABCDE'[n - 1]}"} for n in px],
        } for mid_, (kind, name, px, _, _) in self.markets.items()
            if kind in f["marketTypeCodes"]]

    def _books(self, params):
        out = []
        for mid_ in params["marketIds"]:
            kind, _, px, matched, nwin = self.markets[mid_]
            base = 0 if kind == "WIN" else 100
            winners = {1} if kind == "WIN" else {1, 2}
            runners = []
            for n, (b, l) in px.items():
                r = {"selectionId": base + n, "status": "ACTIVE", "totalMatched": 100.0,
                     "ex": {"availableToBack": [{"price": b, "size": 900.0}],
                            "availableToLay": [{"price": l, "size": 900.0}]}}
                if self.state == "CLOSED":
                    r = {"selectionId": base + n,
                         "status": "WINNER" if n in winners else "LOSER",
                         "sp": {"actualSP": round(b * 1.05, 2)}}
                runners.append(r)
            out.append({"marketId": mid_, "status": "CLOSED" if self.state == "CLOSED" else "OPEN",
                        "inplay": self.state == "INPLAY", "totalMatched": matched,
                        "numberOfWinners": nwin, "runners": runners})
        return out


class FakeSportsbet:
    def __init__(self):
        self.paths: list[str] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if "/AllRacing/" in path:
            return httpx.Response(200, json={"dates": [{"sections": [{
                "raceType": "Horses", "meetings": [{
                    "id": 1, "name": "Randwick", "className": "Horses - Aus/NZ",
                    "events": [
                        {"id": 101, "raceNumber": 1, "startTime": int(START.timestamp()),
                         "statusCode": "A"},
                        {"id": 102, "raceNumber": 2,
                         "startTime": int((NOW + timedelta(minutes=40)).timestamp()),
                         "statusCode": "A"},
                    ]}]}]}]})
        if path.endswith("/Events/101/Racecard"):
            return httpx.Response(200, json={
                "competitionName": "Randwick", "raceNumber": 1,
                "startTime": int(START.timestamp()), "statusCode": "A",
                "bettingStatus": "PRICED",
                "markets": [{"name": "Win or Place", "statusCode": "A", "numPlaces": 2,
                             "selections": [
                    {"id": 500 + n, "name": f"Horse {'ABCDE'[n - 1]}", "runnerNumber": n,
                     "jockey": "J Rider", "statusCode": "A",
                     "prices": [{"priceCode": "L", "winPrice": SB_WIN[n],
                                 "placePrice": SB_PLACE[n]}]}
                    for n in SB_WIN]}]})
        return httpx.Response(404, text="not found")


def make_scanner(tmp_path, **over):
    cfg = settings(value_betfair_countries="AU", **over)
    fake_bf, fake_sb = FakeExchange(), FakeSportsbet()
    sb = SportsbetClient(ArchivingClient("sportsbet", archive=False,
                                         transport=fake_sb.transport, settings=cfg))
    tracker = Tracker(cfg, tmp_path / "state.json", tmp_path / "results.csv")
    scanner = ValueScanner(cfg, sportsbet=sb, betfair=client(fake_bf, **over), tracker=tracker)
    return scanner, fake_bf, fake_sb, cfg


def test_full_run_signal_confirm_close_and_settle(tmp_path):
    scanner, fake_bf, fake_sb, cfg = make_scanner(tmp_path)

    first = scanner.scan_once(NOW)
    assert first.error is None and first.races_in_window == 1 and first.races_compared == 1
    assert not any("/Events/102/" in p for p in fake_sb.paths)
    by = {(r.kind, r.horse): r for r in first.rows}
    assert len(first.rows) == 10

    # Expected values from the prices by hand.
    wbook = sum(mid(*v) for v in WIN_PX.values())
    pbook = sum(mid(*v) for v in PLACE_PX.values())
    a_win = mid(*WIN_PX[1]) / wbook * SB_WIN[1] - 1
    b_place = mid(*PLACE_PX[2]) * 2 / pbook * SB_PLACE[2] - 1
    assert by[("WIN", "Horse A")].ev == pytest.approx(a_win)
    assert by[("PLACE", "Horse B")].ev == pytest.approx(b_place)
    assert first.rows[0].ev == max(a_win, b_place)
    value = {(r.kind, r.horse) for r in first.rows if r.reliable and r.ev >= cfg.value_min_ev}
    assert value == {("WIN", "Horse A"), ("PLACE", "Horse B")}
    assert all(r.streak == 1 for r in first.rows if (r.kind, r.horse) in value)
    assert all(s.confirmed_at is None for s in scanner.tracker.signals.values())

    second = scanner.scan_once(NOW + timedelta(seconds=60))
    assert {(r.kind, r.horse) for r in second.rows if r.streak >= 2} == value
    assert all(s.confirmed_at for s in scanner.tracker.signals.values())

    # The screen renders the confirmed bets in words.
    con = Console(record=True, width=160)
    render(second, cfg, pending=2, console=con)
    text = con.export_text()
    assert "BACK  Horse A (#1)  to WIN" in text and "Horse B (#2)  to PLACE (top 2)" in text

    # A restart keeps everything.
    reloaded = Tracker(cfg, tmp_path / "state.json", tmp_path / "results.csv")
    assert set(reloaded.signals) == set(scanner.tracker.signals)

    # Near the jump the markets are polled; the first in-play sighting fixes
    # the closing price at the last open snapshot.
    scanner.between_scans(START - timedelta(seconds=30))
    fake_bf.state = "INPLAY"
    scanner._last_close_poll = None
    scanner.between_scans(START + timedelta(seconds=20))
    sigs = list(scanner.tracker.signals.values())
    assert all(s.close_p is not None for s in sigs)

    # Settlement: Betfair's runner status and BSP.
    fake_bf.state = "CLOSED"
    settled = scanner.between_scans(START + timedelta(minutes=4))
    assert {(s.kind, s.horse, s.result) for s in settled} == {
        ("WIN", "Horse A", "won"), ("PLACE", "Horse B", "placed")}
    assert scanner.tracker.signals == {} and scanner.tracker.markets == {}

    rows = {r["kind"]: r for r in load_results(tmp_path / "results.csv")}
    assert float(rows["WIN"]["pnl"]) == pytest.approx(SB_WIN[1] - 1)
    assert float(rows["WIN"]["bsp"]) == pytest.approx(round(3.0 * 1.05, 2))
    assert float(rows["WIN"]["clv"]) == pytest.approx(a_win, rel=1e-3)
    assert rows["WIN"]["confirmed"] == "True"
    assert float(rows["PLACE"]["pnl"]) == pytest.approx(SB_PLACE[2] - 1)

    con = Console(record=True, width=160)
    render_report(tmp_path / "results.csv", con)
    assert "Too early to tell: 2 settled" in con.export_text()
    assert results_line(tmp_path / "results.csv", START).startswith("Today: 2 bet(s) settled")


def test_login_refusal_backs_off_instead_of_retrying_every_minute(tmp_path):
    scanner, fake_bf, _, _ = make_scanner(tmp_path, betfair_password="wrong")
    first = scanner.scan_once(NOW)
    logins = sum(1 for _, p in fake_bf.calls if p == "/api/login")
    second = scanner.scan_once(NOW + timedelta(minutes=1))
    assert "INVALID_USERNAME_OR_PASSWORD" in first.error
    assert "trying again at" in second.error
    assert sum(1 for _, p in fake_bf.calls if p == "/api/login") == logins
    con = Console(record=True, width=120)
    render(first, scanner.settings, console=con)
    assert "re-enter" in con.export_text()


def test_places_can_be_switched_off(tmp_path):
    scanner, _, _, _ = make_scanner(tmp_path, value_include_places=False)
    res = scanner.scan_once(NOW)
    assert res.rows and all(r.kind == "WIN" for r in res.rows)
