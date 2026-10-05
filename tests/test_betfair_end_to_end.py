"""The whole exchange path over HTTP against a fake Betfair.

A fake identity host and betting API (httpx MockTransport, documented
request/response shapes) are served to the real ``ArchivingClient`` so the
test covers what the unit tests bypass: form-encoded login with the
X-Application header, the JSON-RPC envelope, session handling, the regional
identity-host fallback, non-JSON block pages, and finally a Sportsbet-shaped
racecard valued with a Betfair fair price.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings
from app.engine import value_offer
from app.http import ArchivingClient, SourceUnavailableError
from app.sources.base import MegabetOffer, RaceInfo, RunnerInfo
from app.sources.betfair import (
    SOURCE,
    BetfairClient,
    BetfairLoginError,
    BetfairStatus,
)

NOW = datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc)
APP_KEY = "abcdefgh12345678"
API = "https://api.betfair.com/exchange/betting/json-rpc/v1"


class FakeBetfair:
    """Minimal but faithful Betfair: login + listMarketCatalogue + listMarketBook."""

    def __init__(self, accounts_host: str = "identitysso.betfair.com",
                 matched: float = 1500.0, block_page_host: str | None = None,
                 token_ttl_calls: int | None = None):
        self.accounts_host = accounts_host  # host that knows the username
        self.block_page_host = block_page_host
        self.matched = matched
        self.token_ttl_calls = token_ttl_calls
        self.calls: list[tuple[str, str]] = []
        self.tokens_issued = 0
        self.rpc_since_login = 0
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append((host, path))
        if path == "/api/login":
            return self._login(request)
        if request.url == httpx.URL(API):
            return self._rpc(request)
        return httpx.Response(404, text="not found")

    def _login(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == self.block_page_host:
            return httpx.Response(403, text="<html>Attention Required! | Cloudflare</html>")
        assert request.headers["X-Application"] == APP_KEY
        assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        assert "application/json" in request.headers["Accept"]
        form = parse_qs(request.content.decode())
        if form.get("username") != ["punter"] or form.get("password") != ["hunter2"]:
            return httpx.Response(200, json={"token": "", "product": APP_KEY,
                                             "status": "FAIL",
                                             "error": "INVALID_USERNAME_OR_PASSWORD"})
        if host != self.accounts_host:
            return httpx.Response(200, json={"token": "", "product": APP_KEY,
                                             "status": "FAIL",
                                             "error": "INVALID_USERNAME_OR_PASSWORD"})
        self.tokens_issued += 1
        self.rpc_since_login = 0
        return httpx.Response(200, json={"token": f"tok{self.tokens_issued}",
                                         "product": APP_KEY, "status": "SUCCESS",
                                         "error": ""})

    def _rpc(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.headers.get("X-Application") != APP_KEY:
            return httpx.Response(200, json=self._error("INVALID_APP_KEY", body["id"]))
        if request.headers.get("X-Authentication") != f"tok{self.tokens_issued}":
            return httpx.Response(200, json=self._error("INVALID_SESSION_INFORMATION", body["id"]))
        self.rpc_since_login += 1
        if self.token_ttl_calls and self.rpc_since_login > self.token_ttl_calls:
            return httpx.Response(200, json=self._error("INVALID_SESSION_INFORMATION", body["id"]))
        method = body["method"].rsplit("/", 1)[-1]
        params = body["params"]
        if method == "listMarketCatalogue":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": self._catalogue(params)})
        if method == "listMarketBook":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": self._books(params)})
        return httpx.Response(200, json=self._error("INVALID_INPUT_DATA", body["id"]))

    @staticmethod
    def _error(code: str, rid: int) -> dict:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32099, "message": "ANGX-0003",
                          "data": {"APINGException": {"requestUUID": "x",
                                                      "errorCode": code,
                                                      "errorDetails": ""}}}}

    MARKETS = {
        "1.100": ("Randwick", "R1 1200m Hcap", NOW,
                  {11: "1. Horse A", 12: "2. Horse B", 13: "3. Horse C"}),
        "1.101": ("Randwick", "R2 1400m Hcap", NOW + timedelta(minutes=35),
                  {21: "1. Horse D", 22: "2. Horse E", 23: "3. Horse F"}),
        # Same venue and race numbers, the next day: must be rejected on time.
        "1.200": ("Randwick", "R1 1000m Mdn", NOW + timedelta(days=1),
                  {31: "1. Horse A", 32: "2. Horse B"}),
    }

    def _catalogue(self, params: dict) -> list[dict]:
        f = params["filter"]
        assert f["eventTypeIds"] == ["7"] and f["marketCountries"] == ["AU"]
        assert f["marketTypeCodes"] == ["WIN"]
        frm = datetime.fromisoformat(f["marketStartTime"]["from"])
        to = datetime.fromisoformat(f["marketStartTime"]["to"])
        assert "MARKET_DESCRIPTION" not in params["marketProjection"]
        out = []
        for mid, (venue, name, start, runners) in self.MARKETS.items():
            if frm <= start < to:
                out.append({
                    "marketId": mid, "marketName": name,
                    "marketStartTime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "totalMatched": self.matched,
                    "event": {"id": "e1", "name": "Randwick (AUS) 3rd Oct",
                              "venue": venue, "countryCode": "AU",
                              "timezone": "Australia/Sydney"},
                    "runners": [{"selectionId": sid, "runnerName": rn,
                                 "handicap": 0.0, "sortPriority": i + 1}
                                for i, (sid, rn) in enumerate(runners.items())],
                })
        return out

    PRICES = {11: (2.0, 2.1), 12: (4.0, 4.2), 13: (4.0, 4.4),
              21: (3.0, 3.1), 22: (3.0, 3.2), 23: (3.0, 3.1),
              31: (2.0, 2.2), 32: (2.0, 2.2)}

    def _books(self, params: dict) -> list[dict]:
        assert params["priceProjection"]["priceData"] == ["EX_BEST_OFFERS"]
        assert len(params["marketIds"]) <= 40, "listMarketBook weight limit"
        out = []
        for mid in params["marketIds"]:
            venue, name, start, runners = self.MARKETS[mid]
            out.append({
                "marketId": mid, "isMarketDataDelayed": self.matched == 0,
                "status": "OPEN", "betDelay": 0, "inplay": False,
                "totalMatched": self.matched,
                "runners": [
                    {"selectionId": sid, "handicap": 0.0, "status": "ACTIVE",
                     "totalMatched": self.matched / 10,
                     "ex": {"availableToBack": [{"price": b, "size": 120.0}],
                            "availableToLay": [{"price": l, "size": 80.0}],
                            "tradedVolume": []}}
                    for sid in runners for (b, l) in [self.PRICES[sid]]
                ],
            })
        return out


def settings(**over) -> Settings:
    base = dict(_env_file=None, archive_raw_responses=False,
                betfair_app_key=APP_KEY, betfair_username="punter",
                betfair_password="hunter2", http_min_request_interval_seconds=0.0)
    base.update(over)
    return Settings(**base)


def client(fake: FakeBetfair, **over) -> BetfairClient:
    cfg = settings(**over)
    return BetfairClient(
        client=ArchivingClient(SOURCE, archive=False, transport=fake.transport, settings=cfg),
        settings=cfg,
    )


def runner(rid, horse, jockey, odds, cloth):
    return RunnerInfo(source="sportsbet", source_id=rid, horse_name=horse,
                      jockey_name=jockey, status="active", win_odds=odds,
                      odds_timestamp=NOW, saddlecloth=cloth)


def sportsbet_races() -> list[RaceInfo]:
    r1 = RaceInfo(source="sportsbet", source_id="r1", race_number=1, start_time=NOW,
                  status="open", runners=[
                      runner("1", "Horse A", "Alpha Rider", 2.0, 1),
                      runner("2", "Horse B", "Other Jockey", 4.0, 2),
                      runner("3", "Horse C", "Third Jockey", 4.0, 3)])
    r2 = RaceInfo(source="sportsbet", source_id="r2", race_number=2,
                  start_time=NOW + timedelta(minutes=35), status="open", runners=[
                      runner("4", "Horse D", "A Rider", 3.0, 1),
                      runner("5", "Horse E", "Other Jockey", 3.0, 2),
                      runner("6", "Horse F", "Third Jockey", 3.0, 3)])
    return [r1, r2]


def offer(threshold=1, odds=2.5):
    return MegabetOffer(source="sportsbet", market_id="m1", selection_id="s1",
                        meeting_name="Royal Randwick", meeting_source_id=None,
                        meeting_date=NOW.date(), jockey_name="Alpha Rider",
                        threshold=threshold, odds=odds,
                        market_name="Alpha Rider to Ride 1+ Winners", fetched_at=NOW)


# ---------------------------------------------------------------------------

def test_full_path_login_catalogue_books_and_valuation():
    fake = FakeBetfair()
    bf = client(fake)
    markets = bf.list_au_win_markets(NOW.date())
    assert [m.market_id for m in markets] == ["1.100", "1.101", "1.200"]
    assert fake.tokens_issued == 1, "one login for the whole catalogue"
    assert markets[0].venue == "Randwick" and markets[0].race_number == 1
    bf.fetch_market_books(markets)
    st = bf.status
    assert st.ok and st.identity_host == "identitysso.betfair.com"
    assert st.catalogue_markets == 3 and st.books_returned == 3 and st.open_books == 3
    assert not st.delayed
    q = markets[0].runners[0]
    assert q.cloth_number == 1 and q.runner_name == "1. Horse A"
    assert q.reliable and q.probability == pytest.approx(1 / 2.05)
    assert q.total_matched == 150.0, "runner volume kept for the record"

    vals = value_offer(offer(), sportsbet_races(), settings(), markets, now=NOW)
    by_model = {v.model: v for v in vals}
    bf_val = by_model["betfair"]
    assert bf_val.fair_probability is not None, bf_val.quality_detail
    # P(>=1 win) = 1 - (1-1/2.05)(1-1/3.05)
    assert bf_val.fair_probability == pytest.approx(1 - (1 - 1 / 2.05) * (1 - 1 / 3.05))
    assert bf_val.quality == "HIGH", bf_val.quality_detail
    assert by_model["consensus"].alt_fair_odds["betfair"] == pytest.approx(1 / bf_val.fair_probability)
    assert "Betfair: connected via identitysso.betfair.com; 3 AU win markets; 3 books (3 open)" == st.summary()


def test_market_level_liquidity_gate_not_runner_level():
    """A $1500 market whose runners each matched $150 is reliable, as the
    documented gate is on the market's matched volume."""
    fake = FakeBetfair(matched=1500.0)
    bf = client(fake, betfair_min_liquidity=500.0)
    markets = bf.list_au_win_markets(NOW.date())
    bf.fetch_market_books(markets)
    assert all(q.reliable for q in markets[0].runners)
    fake = FakeBetfair(matched=300.0)
    bf = client(fake, betfair_min_liquidity=500.0)
    markets = bf.list_au_win_markets(NOW.date())
    bf.fetch_market_books(markets)
    assert not any(q.reliable for q in markets[0].runners)
    assert "thin market" in markets[0].runners[0].detail


def test_delayed_key_detected_over_http():
    fake = FakeBetfair(matched=0.0)
    bf = client(fake)
    markets = bf.list_au_win_markets(NOW.date())
    bf.fetch_market_books(markets)
    assert bf.status.delayed and all(m.delayed for m in markets)
    assert markets[0].runners[0].reliable  # 2.0/2.1 is a 5% spread
    assert "DELAYED" in bf.status.summary()


def test_australian_account_logs_in_on_the_au_host_after_com_refuses():
    fake = FakeBetfair(accounts_host="identitysso.betfair.com.au")
    bf = client(fake)
    bf.login()
    assert bf.status.identity_host == "identitysso.betfair.com.au"
    hosts = [h for h, p in fake.calls if p == "/api/login"]
    assert hosts == ["identitysso.betfair.com", "identitysso.betfair.com.au"]


def test_configured_au_host_is_tried_first():
    fake = FakeBetfair(accounts_host="identitysso.betfair.com.au")
    bf = client(fake, betfair_identity_url="https://identitysso.betfair.com.au")
    bf.login()
    hosts = [h for h, p in fake.calls if p == "/api/login"]
    assert hosts == ["identitysso.betfair.com.au"]


def test_wrong_password_reports_betfair_code_and_hint_for_both_hosts():
    fake = FakeBetfair()
    bf = client(fake, betfair_password="nope")
    with pytest.raises(BetfairLoginError) as ei:
        bf.login()
    assert ei.value.code == "INVALID_USERNAME_OR_PASSWORD"
    assert "identitysso.betfair.com.au" in ei.value.hint
    assert "tried" in str(ei.value) and ".com.au/api/login" in str(ei.value)


def test_invalid_app_key_is_not_retried_on_the_other_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "", "status": "FAIL", "error": "INVALID_APP_KEY"})
    fake = FakeBetfair()
    fake.transport = httpx.MockTransport(lambda r: (fake.calls.append((r.url.host, r.url.path)), handler(r))[1])
    bf = client(fake)
    with pytest.raises(BetfairLoginError) as ei:
        bf.login()
    assert ei.value.code == "INVALID_APP_KEY" and "developer.betfair.com" in ei.value.hint
    assert len(fake.calls) == 1


def test_block_page_on_com_falls_through_to_au_host():
    fake = FakeBetfair(accounts_host="identitysso.betfair.com.au",
                       block_page_host="identitysso.betfair.com")
    bf = client(fake)
    bf.login()
    assert bf.status.identity_host == "identitysso.betfair.com.au"


def test_block_page_everywhere_is_reported_with_the_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html>Attention Required! | Cloudflare</html>")
    fake = FakeBetfair()
    fake.transport = httpx.MockTransport(handler)
    bf = client(fake)
    with pytest.raises(SourceUnavailableError) as ei:
        bf.login()
    assert ei.value.status == 403 and "Cloudflare" in (ei.value.body_snippet or "")


def test_non_json_200_from_api_is_an_error_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"token": "t", "status": "SUCCESS", "error": ""})
        return httpx.Response(200, text="<html>challenge</html>")
    fake = FakeBetfair()
    fake.transport = httpx.MockTransport(handler)
    bf = client(fake)
    with pytest.raises(SourceUnavailableError) as ei:
        bf.list_au_win_markets(NOW.date())
    assert "other than JSON" in ei.value.detail


def test_expired_session_is_renewed_once_mid_loop():
    fake = FakeBetfair(token_ttl_calls=3)
    bf = client(fake)
    markets = bf.list_au_win_markets(NOW.date())  # ~9 windows, token expires inside
    assert markets and fake.tokens_issued >= 2


def test_api_error_names_the_aping_code():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"token": "t", "status": "SUCCESS", "error": ""})
        return httpx.Response(200, json=FakeBetfair._error("TOO_MUCH_DATA", 1))
    fake = FakeBetfair()
    fake.transport = httpx.MockTransport(handler)
    bf = client(fake)
    with pytest.raises(SourceUnavailableError) as ei:
        bf.list_au_win_markets(NOW.date())
    assert "TOO_MUCH_DATA (the request exceeded" in ei.value.detail


def test_scan_status_line_and_check_command(monkeypatch, capsys):
    """fetch_betfair_markets returns a status the scan prints; betfair_check
    walks the same path and ends with a verdict."""
    import app.betfair_check as check
    import app.scan as scan
    from app.reporting import tables

    fake = FakeBetfair()
    cfg = settings()
    monkeypatch.setattr(scan, "BetfairClient", lambda: client(fake))
    markets, status = scan.fetch_betfair_markets(NOW.date())
    assert markets and status.ok
    tables.print_betfair_status(status)
    assert "Betfair: connected" in capsys.readouterr().out

    monkeypatch.setattr(check, "get_settings", lambda: cfg)
    monkeypatch.setattr(check, "BetfairClient", lambda: client(fake))
    rc = check.main(["--date", NOW.date().isoformat()])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[OK  ] login" in out and "[OK  ] catalogue" in out and "[OK  ] result" in out
    assert "hunter2" not in out and APP_KEY not in out, "secrets never printed"

    # And the failure path ends in a verdict with Betfair's code.
    bad = FakeBetfair()
    monkeypatch.setattr(check, "BetfairClient", lambda: client(bad, betfair_password="nope"))
    rc = check.main([])
    out = capsys.readouterr().out
    assert rc == 2 and "INVALID_USERNAME_OR_PASSWORD" in out and "what usually fixes it" in out

    monkeypatch.setattr(scan, "BetfairClient", lambda: client(bad, betfair_password="nope"))
    markets, status = scan.fetch_betfair_markets(NOW.date())
    assert markets is None and not status.ok
    tables.print_betfair_status(status)
    out = capsys.readouterr().out
    assert "Betfair: FAILED" in out and "betfair_check" in out


def test_status_summary_when_not_configured():
    assert "not configured" in BetfairStatus().summary()


def test_probe_reports_reachable_api_without_credentials(capsys):
    from app.betfair_check import probe_reachability

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"token": "", "status": "FAIL", "error": "INVALID_APP_KEY"})
        return httpx.Response(200, json=FakeBetfair._error("NO_SESSION", 1))
    cfg = settings(betfair_app_key=None)
    ok = probe_reachability(cfg, ArchivingClient(SOURCE, archive=False,
                                                 transport=httpx.MockTransport(handler), settings=cfg))
    out = capsys.readouterr().out
    assert ok and out.count("[OK  ] reach") == 3 and "NO_SESSION" in out


def test_probe_reports_block_page(capsys):
    from app.betfair_check import probe_reachability

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html>Attention Required! | Cloudflare</html>")
    cfg = settings(betfair_app_key=None)
    ok = probe_reachability(cfg, ArchivingClient(SOURCE, archive=False,
                                                 transport=httpx.MockTransport(handler), settings=cfg))
    out = capsys.readouterr().out
    assert not ok and out.count("[FAIL] reach") == 3 and "Cloudflare" in out
    assert "refusing this network" in out


def test_betfair_scratching_is_named_not_matched(caplog):
    """A runner Betfair has REMOVED but Sportsbet still prices is reported
    in one warning and left out of the exchange match."""
    import logging
    from app.matching.runners import match_race_runners

    fake = FakeBetfair()
    bf = client(fake)
    markets = bf.list_au_win_markets(NOW.date())
    # Make the fake book REMOVE "2. Horse B" in market 1.100.
    orig = fake._books

    def books(params):
        out = orig(params)
        for b in out:
            if b["marketId"] == "1.100":
                for r in b["runners"]:
                    if r["selectionId"] == 12:
                        r["status"] = "REMOVED"
        return out
    fake._books = books
    bf.fetch_market_books(markets)
    m = markets[0]
    assert m.removed_names == ["2. Horse B"]
    assert [q.runner_name for q in m.runners] == ["1. Horse A", "3. Horse C"]
    with caplog.at_level(logging.WARNING, logger="app.matching.runners"):
        matches = match_race_runners(sportsbet_races()[0].active_runners(), m.runners,
                                     race_label="Randwick R1",
                                     removed_on_betfair=m.removed_names)
    by_name = {mt.sportsbet_runner.horse_name: mt for mt in matches}
    assert by_name["Horse B"].status == "removed_on_betfair"
    assert by_name["Horse A"].status == "matched"
    assert any("Horse B still priced by Sportsbet but REMOVED" in r.getMessage()
               for r in caplog.records)


def test_each_race_is_matched_once_per_scan_not_per_jockey(monkeypatch):
    import app.engine as engine
    calls: list[str] = []
    real = engine.match_race_runners

    def counting(*a, **k):
        calls.append(k.get("race_label", ""))
        return real(*a, **k)
    monkeypatch.setattr(engine, "match_race_runners", counting)
    fake = FakeBetfair()
    bf = client(fake)
    markets = bf.list_au_win_markets(NOW.date())
    bf.fetch_market_books(markets)
    races = sportsbet_races()
    cache: dict = {}
    other = MegabetOffer(source="sportsbet", market_id="m2", selection_id="s2",
                         meeting_name="Royal Randwick", meeting_source_id=None,
                         meeting_date=NOW.date(), jockey_name="Other Jockey",
                         threshold=1, odds=3.0, market_name="Other Jockey 1+", fetched_at=NOW)
    value_offer(offer(), races, settings(), markets, now=NOW, ride_cache=cache)
    value_offer(offer(threshold=2, odds=9.0), races, settings(), markets, now=NOW, ride_cache=cache)
    value_offer(other, races, settings(), markets, now=NOW, ride_cache=cache)
    assert sorted(calls) == ["Royal Randwick R1", "Royal Randwick R2"], calls


def test_table_shows_ride_coverage_when_betfair_model_unavailable(capsys):
    from app.reporting import tables

    fake = FakeBetfair()
    fake.PRICES = dict(fake.PRICES)
    fake.PRICES[21] = (3.0, 6.0)  # Horse D: 100% spread -> unreliable
    bf = client(fake)
    markets = bf.list_au_win_markets(NOW.date())
    bf.fetch_market_books(markets)
    vals = value_offer(offer(), sportsbet_races(), settings(), markets, now=NOW)
    assert next(v for v in vals if v.model == "betfair").fair_odds is None
    consensus_row = next(v for v in vals if v.model == "consensus")
    assert tables._fmt_betfair(consensus_row) == "[dim]1/2 rides[/dim]"
    tables.console.width = 200  # keep the cell on one line for the text check
    tables.render_valuations(vals, NOW)
    out = capsys.readouterr().out
    assert "1/2 rides" in out
