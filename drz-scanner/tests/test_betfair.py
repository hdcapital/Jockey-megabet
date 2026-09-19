"""Betfair gates, delayed-key detection and place-market matching."""

from __future__ import annotations

import pytest

from app.sources.betfair import (
    BetfairMarket,
    BetfairRunnerQuote,
    derive_probability,
    market_is_delayed,
    place_market_for,
)
from app.winprob import betfair_win_probabilities


def test_midpoint_used_when_liquid_and_tight():
    p, reliable, detail = derive_probability(2.0, 2.1, 5000.0, 500.0, 0.25)
    assert p == pytest.approx(1 / 2.05)
    assert reliable
    assert "midpoint" in detail


def test_thin_market_is_priced_but_not_reliable():
    p, reliable, detail = derive_probability(2.0, 2.1, 50.0, 500.0, 0.25)
    assert p == pytest.approx(1 / 2.05)
    assert not reliable
    assert "thin" in detail


def test_wide_spread_falls_back_to_best_back():
    p, reliable, detail = derive_probability(2.0, 3.5, 9000.0, 500.0, 0.25)
    assert p == pytest.approx(0.5)
    assert not reliable
    assert "exceeds" in detail


def test_crossed_book_is_refused():
    p, reliable, _ = derive_probability(3.0, 2.0, 9000.0, 500.0, 0.25)
    assert p is None and not reliable


def test_no_prices_at_all():
    p, reliable, detail = derive_probability(None, None, 9000.0, 500.0, 0.25)
    assert p is None and not reliable and "no exchange prices" in detail


def test_one_sided_book_is_unreliable():
    p, reliable, detail = derive_probability(2.0, None, 9000.0, 500.0, 0.25)
    assert p == pytest.approx(0.5) and not reliable and "only best back" in detail


# -- delayed application key ------------------------------------------------

def test_delayed_key_detected_from_absent_matched_volume():
    assert market_is_delayed({"runners": [{"selectionId": 1}, {"selectionId": 2}]})
    assert market_is_delayed({"totalMatched": 0, "runners": [{"totalMatched": 0}]})
    assert not market_is_delayed({"totalMatched": 1200.0, "runners": []})
    assert not market_is_delayed({"runners": [{"totalMatched": 300.0}]})


def test_delayed_key_uses_a_spread_only_gate():
    """With no volume to check, a tight spread alone carries the decision."""
    p, reliable, detail = derive_probability(
        2.00, 2.10, None, 500.0, 0.25, delayed=True, delayed_max_relative_spread=0.10
    )
    assert p == pytest.approx(1 / 2.05)
    assert reliable, "a tight delayed market is usable"
    assert "DELAYED" in detail

    # The same market would have been rejected as unpriced under the normal
    # gate, because totalMatched is missing.
    _, normal_reliable, _ = derive_probability(2.00, 2.10, None, 500.0, 0.25)
    assert not normal_reliable


def test_delayed_gate_is_tighter_than_the_live_one():
    """A 15% spread passes the live gate and fails the delayed one."""
    _, live_ok, _ = derive_probability(2.0, 2.3, 9000.0, 500.0, 0.25)
    assert live_ok
    _, delayed_ok, _ = derive_probability(
        2.0, 2.3, None, 500.0, 0.25, delayed=True, delayed_max_relative_spread=0.10
    )
    assert not delayed_ok


def test_delayed_flag_reaches_the_probability_set():
    probs = betfair_win_probabilities([0.5, 0.3, 0.2], [True, True, True], delayed=True)
    assert probs is not None
    assert probs.betfair_delayed
    assert "DELAYED" in probs.detail
    assert sum(probs.probabilities) == pytest.approx(1.0)


def test_a_gap_in_the_exchange_book_refuses_the_whole_model():
    """A partial book cannot be normalised without inventing the missing share."""
    assert betfair_win_probabilities([0.5, None, 0.2], [True, False, True], False) is None
    assert betfair_win_probabilities([0.5, 0.3, 0.2], [True, False, True], False) is None


# -- place market matching --------------------------------------------------

def _market(mtype, venue, race_no, winners):
    return BetfairMarket(
        market_id=f"1.{race_no}{mtype}", market_name=f"R{race_no}", market_type=mtype,
        venue=venue, market_start=None, race_number=race_no,
        number_of_winners=winners,
    )


def test_place_market_must_match_our_place_terms():
    markets = [
        _market("WIN", "Flemington", 4, None),
        _market("PLACE", "Flemington", 4, 3),
        _market("PLACE", "Flemington", 5, 2),
    ]
    assert place_market_for(markets, "Flemington", 4, 3).number_of_winners == 3
    # A 3-place Sportsbet bet cannot be checked against a 2-winner exchange
    # market, so no second opinion is offered rather than a misleading one.
    assert place_market_for(markets, "Flemington", 5, 3) is None
    assert place_market_for(markets, "Flemington", 9, 3) is None
    assert place_market_for(markets, None, 4, 3) is None


# -- runner-name prefix and session expiry ------------------------------------

def test_market_books_parse_the_cloth_number_prefix(monkeypatch):
    """Betfair names horses "7. Zoustar"; the number is the join key."""
    from app.sources.betfair import BetfairClient

    client = BetfairClient.__new__(BetfairClient)
    from app.config import Settings

    client.settings = Settings()
    client.delayed_key_detected = False
    market = BetfairMarket("1.1", "R1", "WIN", "Flemington", None, race_number=1,
                           catalogue_runners={11: "7. Zoustar", 12: "Bare Name"})
    monkeypatch.setattr(client, "_rpc", lambda *a, **k: [{
        "marketId": "1.1", "status": "OPEN", "totalMatched": 5000.0,
        "runners": [
            {"selectionId": 11, "status": "ACTIVE", "totalMatched": 3000.0,
             "ex": {"availableToBack": [{"price": 3.0, "size": 50}],
                    "availableToLay": [{"price": 3.1, "size": 50}]}},
            {"selectionId": 12, "status": "ACTIVE", "totalMatched": 2000.0,
             "ex": {"availableToBack": [{"price": 6.0, "size": 50}],
                    "availableToLay": [{"price": 6.2, "size": 50}]}},
        ]}])
    client.fetch_market_books([market])
    by_id = {q.selection_id: q for q in market.runners}
    assert by_id[11].cloth_number == 7 and by_id[11].runner_name == "7. Zoustar"
    assert by_id[12].cloth_number is None


def test_an_expired_session_is_renewed_once(monkeypatch):
    from app.config import Settings
    from app.sources.betfair import BetfairClient

    client = BetfairClient.__new__(BetfairClient)
    client.settings = Settings()
    client._session_token = "stale"
    logins = []
    client.login = lambda: (logins.append(1), setattr(client, "_session_token", "fresh"))

    calls = []

    class Result:
        def __init__(self, body):
            self._body = body
            self.status_code = 200

        def json(self):
            return self._body

    def post_json(url, json_body=None, headers=None, data=None):
        calls.append(headers["X-Authentication"])
        if headers["X-Authentication"] == "stale":
            return Result({"error": {"data": {"APINGException": {
                "errorCode": "INVALID_SESSION_INFORMATION"}}}})
        return Result({"result": [{"ok": True}]})

    client.client = type("C", (), {"post_json": staticmethod(post_json)})()
    assert client._rpc("listMarketBook", {}) == [{"ok": True}]
    assert calls == ["stale", "fresh"]
    assert len(logins) == 1


def test_a_non_session_error_is_not_retried():
    from app.config import Settings
    from app.http import SourceUnavailableError
    from app.sources.betfair import BetfairClient

    client = BetfairClient.__new__(BetfairClient)
    client.settings = Settings()
    client._session_token = "t"
    client.login = lambda: (_ for _ in ()).throw(AssertionError("must not re-login"))

    class Result:
        status_code = 200

        def json(self):
            return {"error": {"code": -32099, "message": "TOO_MUCH_DATA"}}

    client.client = type("C", (), {"post_json": staticmethod(lambda *a, **k: Result())})()
    with pytest.raises(SourceUnavailableError):
        client._rpc("listMarketBook", {})


# -- delayed-key detection is a session-level decision ----------------------

def _client_with_books(monkeypatch, books, setting="auto"):
    from app.config import Settings
    from app.sources.betfair import BetfairClient

    client = BetfairClient.__new__(BetfairClient)
    client.settings = Settings(betfair_key_delayed=setting)
    client.delayed_key_detected = False
    monkeypatch.setattr(client, "_rpc", lambda *a, **k: books)
    return client


def _book(market_id, matched, runner_matched):
    return {"marketId": market_id, "status": "OPEN", "totalMatched": matched,
            "runners": [{"selectionId": 1, "status": "ACTIVE", "totalMatched": runner_matched,
                         "ex": {"availableToBack": [{"price": 3.0, "size": 50}],
                                "availableToLay": [{"price": 3.1, "size": 50}]}}]}


def _markets(n):
    return [BetfairMarket(f"1.{i}", "R1", "WIN", "V", None, race_number=1,
                          catalogue_runners={1: "1. Horse"}) for i in range(n)]


def test_one_thin_market_does_not_make_the_key_delayed(monkeypatch):
    """At 10am a live key has thin markets. Those must fail the liquidity
    gate, not be handed the delayed key's spread-only gate."""
    books = [_book("1.0", 0, 0), _book("1.1", 4000.0, 3000.0)]
    client = _client_with_books(monkeypatch, books)
    markets = _markets(2)
    client.fetch_market_books(markets)
    assert client.delayed_key_detected is False
    thin, liquid = markets[0].runners[0], markets[1].runners[0]
    assert not thin.delayed and not thin.reliable, "thin market: priced but unreliable"
    assert liquid.reliable


def test_zero_volume_everywhere_means_a_delayed_key(monkeypatch):
    books = [_book("1.0", 0, 0), _book("1.1", 0, 0)]
    client = _client_with_books(monkeypatch, books)
    markets = _markets(2)
    client.fetch_market_books(markets)
    assert client.delayed_key_detected is True
    assert all(q.delayed and q.reliable for m in markets for q in m.runners), (
        "under a delayed key a tight spread is the whole reliability test")


def test_the_setting_overrides_the_heuristic(monkeypatch):
    books = [_book("1.0", 4000.0, 3000.0)]
    client = _client_with_books(monkeypatch, books, setting="true")
    markets = _markets(1)
    client.fetch_market_books(markets)
    assert client.delayed_key_detected is True
    client = _client_with_books(monkeypatch, [_book("1.0", 0, 0)], setting="false")
    markets = _markets(1)
    client.fetch_market_books(markets)
    assert client.delayed_key_detected is False
