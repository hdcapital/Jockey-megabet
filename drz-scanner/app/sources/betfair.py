"""Betfair Exchange adapter: win market (Model A) and place market (second opinion).

Credentials come from environment variables only; nothing is written to the
repository or the logs.

Delayed application keys
------------------------
Betfair issues every account a *delayed* application key alongside the live
one. A delayed key returns prices, but the market book comes back with no
matched volume — ``totalMatched`` is absent or zero on every runner. That
breaks the liquidity gate, which would otherwise silently pass every market
as "thin but usable" or fail every market as unpriced.

So the adapter detects the condition, falls back to a **spread-only**
reliability gate (a tighter maximum spread, since spread is now the only
evidence of a real market), tags every quote ``delayed=True``, and the
scorer refuses to let a delayed-only price promote a row to BET inside the
final five minutes before the jump — exactly when a delay matters most.

The place market as a second opinion
------------------------------------
Where a "To Be Placed" market exists whose ``numberOfWinners`` equals the
place terms we are valuing, its midpoint gives an independent place
probability. Measured over 44,856 races, betting wherever
``model_p * place_BSP > 1.0`` returned 0.98 gross and ``> 1.2`` returned
0.90: when the model and the exchange disagree, the exchange has been right.
Disagreement is therefore displayed as a warning, never as an edge.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.http import ArchivingClient, SourceUnavailableError

log = logging.getLogger(__name__)

SOURCE = "betfair"


class BetfairNotConfiguredError(Exception):
    """Credentials absent — a normal, reported state, not an error condition."""


@dataclass
class BetfairRunnerQuote:
    market_id: str
    selection_id: int
    runner_name: str
    best_back: float | None
    best_lay: float | None
    back_volume: float | None
    lay_volume: float | None
    total_matched: float | None
    market_status: str
    fetched_at: datetime
    probability: float | None = None
    reliable: bool = False
    delayed: bool = False
    detail: str = ""


@dataclass
class BetfairMarket:
    market_id: str
    market_name: str
    market_type: str
    venue: str | None
    market_start: datetime | None
    race_number: int | None = None
    number_of_winners: int | None = None
    total_matched: float | None = None
    runners: list[BetfairRunnerQuote] = field(default_factory=list)
    catalogue_runners: dict[int, str] = field(default_factory=dict)


def market_is_delayed(book: dict[str, Any]) -> bool:
    """True when a market book carries no matched volume anywhere.

    A real Australian win market minutes from the jump always has *some*
    matched volume. None at all, across the market and every runner, is the
    signature of a delayed application key rather than of an empty market.
    """
    if book.get("totalMatched"):
        return False
    for r in book.get("runners", []) or []:
        if r.get("totalMatched"):
            return False
    return True


def derive_probability(
    best_back: float | None,
    best_lay: float | None,
    total_matched: float | None,
    min_liquidity: float,
    max_relative_spread: float,
    delayed: bool = False,
    delayed_max_relative_spread: float | None = None,
) -> tuple[float | None, bool, str]:
    """``(probability, reliable, detail)`` from exchange prices.

    With a delayed key the liquidity half of the gate is unavailable, so the
    spread half is tightened and carries the decision alone.
    """
    if best_back is not None and best_back <= 1.0:
        best_back = None
    if best_lay is not None and best_lay <= 1.0:
        best_lay = None
    if best_back is None and best_lay is None:
        return None, False, "no exchange prices available"

    spread_limit = max_relative_spread
    if delayed:
        spread_limit = (
            delayed_max_relative_spread
            if delayed_max_relative_spread is not None
            else max_relative_spread / 2.0
        )

    if best_back is not None and best_lay is not None:
        spread = (best_lay - best_back) / best_back
        if spread < 0:
            return None, False, f"crossed book (back {best_back} > lay {best_lay})"
        if spread <= spread_limit:
            mid = (best_back + best_lay) / 2.0
            if delayed:
                return (
                    1.0 / mid,
                    True,
                    f"midpoint; DELAYED key, spread {spread:.1%} within "
                    f"{spread_limit:.0%} spread-only gate",
                )
            liquid = total_matched is not None and total_matched >= min_liquidity
            detail = "midpoint of best back/lay" + (
                "" if liquid else f"; thin market (matched {total_matched})"
            )
            return 1.0 / mid, liquid, detail
        return (
            1.0 / best_back,
            False,
            f"spread {spread:.1%} exceeds {spread_limit:.0%}; using best back",
        )
    side = best_back if best_back is not None else best_lay
    name = "back" if best_back is not None else "lay"
    return 1.0 / side, False, f"only best {name} available"


_RACE_NO = re.compile(r"\s*R(\d+)")


class BetfairClient:
    def __init__(self, client: ArchivingClient | None = None):
        self.settings = get_settings()
        if not (self.settings.betfair_app_key and self.settings.betfair_username):
            raise BetfairNotConfiguredError(
                "BETFAIR_APP_KEY / BETFAIR_USERNAME not set; "
                "the Betfair win model and place second opinion are disabled"
            )
        self.client = client or ArchivingClient(SOURCE)
        self._session_token: str | None = None
        #: Set once a market book has shown the delayed-key signature.
        self.delayed_key_detected = False

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "BetfairClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth -----------------------------------------------------------
    def login(self) -> None:
        s = self.settings
        if s.betfair_cert_file and s.betfair_key_file:
            url = f"{s.betfair_identity_cert_url}/api/certlogin"
        else:
            url = f"{s.betfair_identity_url}/api/login"
        result = self.client.post_json(
            url,
            data={"username": s.betfair_username, "password": s.betfair_password or ""},
            headers={
                "X-Application": s.betfair_app_key or "",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        body = result.json()
        token = body.get("token") or body.get("sessionToken")
        if not token:
            status = body.get("status") or body.get("loginStatus") or body
            raise SourceUnavailableError(
                SOURCE, url, f"login failed: {status}", result.status_code
            )
        self._session_token = token
        log.info("betfair: login ok")

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        if self._session_token is None:
            self.login()
        s = self.settings
        result = self.client.post_json(
            s.betfair_api_url,
            json_body={
                "jsonrpc": "2.0",
                "method": f"SportsAPING/v1.0/{method}",
                "params": params,
                "id": 1,
            },
            headers={
                "X-Application": s.betfair_app_key or "",
                "X-Authentication": self._session_token or "",
                "Content-Type": "application/json",
            },
        )
        body = result.json()
        if "error" in body:
            raise SourceUnavailableError(
                SOURCE, s.betfair_api_url, f"{method} error: {body['error']}"
            )
        return body.get("result")

    # -- markets ---------------------------------------------------------
    def list_au_markets(
        self, for_date: date, market_types: tuple[str, ...] = ("WIN", "PLACE")
    ) -> list[BetfairMarket]:
        """Australian thoroughbred WIN and PLACE markets for a date."""
        start = datetime.combine(for_date, datetime.min.time(), tzinfo=timezone.utc)
        catalogue = self._rpc(
            "listMarketCatalogue",
            {
                "filter": {
                    "eventTypeIds": ["7"],
                    "marketCountries": ["AU"],
                    "marketTypeCodes": list(market_types),
                    "marketStartTime": {
                        "from": (start - timedelta(hours=14)).isoformat(),
                        "to": (start + timedelta(hours=38)).isoformat(),
                    },
                },
                "marketProjection": [
                    "EVENT", "MARKET_START_TIME", "RUNNER_DESCRIPTION",
                    "MARKET_DESCRIPTION",
                ],
                "sort": "FIRST_TO_START",
                "maxResults": 1000,
            },
        ) or []
        markets: list[BetfairMarket] = []
        for m in catalogue:
            event = m.get("event") or {}
            desc = m.get("description") or {}
            start_str = m.get("marketStartTime")
            name = m.get("marketName", "")
            mm = _RACE_NO.match(name)
            markets.append(
                BetfairMarket(
                    market_id=m["marketId"],
                    market_name=name,
                    market_type=str(desc.get("marketType") or "").upper()
                    or ("PLACE" if "plac" in name.lower() else "WIN"),
                    venue=event.get("venue") or event.get("name"),
                    market_start=(
                        datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                        if isinstance(start_str, str)
                        else None
                    ),
                    race_number=int(mm.group(1)) if mm else None,
                    number_of_winners=(
                        int(m["numberOfWinners"])
                        if isinstance(m.get("numberOfWinners"), (int, float))
                        else None
                    ),
                    catalogue_runners={
                        r["selectionId"]: r.get("runnerName", "")
                        for r in m.get("runners", [])
                    },
                )
            )
        log.info("betfair: %d AU markets in catalogue", len(markets))
        return markets

    def fetch_market_books(self, markets: list[BetfairMarket]) -> None:
        """Populate runner quotes from ``listMarketBook``."""
        s = self.settings
        fetched_at = datetime.now(timezone.utc)
        for i in range(0, len(markets), 25):  # API weight limits
            chunk = markets[i : i + 25]
            books = self._rpc(
                "listMarketBook",
                {
                    "marketIds": [m.market_id for m in chunk],
                    "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
                },
            ) or []
            by_id = {b["marketId"]: b for b in books}
            for market in chunk:
                book = by_id.get(market.market_id)
                if not book:
                    continue
                delayed = market_is_delayed(book)
                if delayed and not self.delayed_key_detected:
                    self.delayed_key_detected = True
                    log.warning(
                        "betfair: no matched volume on market %s — treating this "
                        "as a DELAYED application key; liquidity gate replaced by "
                        "a %.0f%% spread-only gate and rows tagged betfair_delayed",
                        market.market_id, s.betfair_delayed_max_relative_spread * 100,
                    )
                market.total_matched = book.get("totalMatched")
                status = book.get("status", "UNKNOWN")
                if isinstance(book.get("numberOfWinners"), (int, float)):
                    market.number_of_winners = int(book["numberOfWinners"])
                for r in book.get("runners", []):
                    if r.get("status") not in (None, "ACTIVE"):
                        continue
                    ex = r.get("ex") or {}
                    backs = ex.get("availableToBack") or []
                    lays = ex.get("availableToLay") or []
                    best_back = backs[0]["price"] if backs else None
                    back_vol = backs[0]["size"] if backs else None
                    best_lay = lays[0]["price"] if lays else None
                    lay_vol = lays[0]["size"] if lays else None
                    total = r.get("totalMatched") or book.get("totalMatched")
                    prob, reliable, detail = derive_probability(
                        best_back,
                        best_lay,
                        total,
                        s.betfair_min_liquidity,
                        s.betfair_max_relative_spread,
                        delayed=delayed,
                        delayed_max_relative_spread=s.betfair_delayed_max_relative_spread,
                    )
                    market.runners.append(
                        BetfairRunnerQuote(
                            market_id=market.market_id,
                            selection_id=r["selectionId"],
                            runner_name=market.catalogue_runners.get(r["selectionId"], ""),
                            best_back=best_back,
                            best_lay=best_lay,
                            back_volume=back_vol,
                            lay_volume=lay_vol,
                            total_matched=total,
                            market_status=status,
                            fetched_at=fetched_at,
                            probability=prob,
                            reliable=reliable,
                            delayed=delayed,
                            detail=detail,
                        )
                    )


def place_market_for(
    markets: list[BetfairMarket], venue: str | None, race_number: int | None, places: int
) -> BetfairMarket | None:
    """The "To Be Placed" market matching a race *and* our place terms.

    A Betfair place market with two winners tells us nothing about a
    three-dividend Sportsbet bet, so a mismatch returns ``None`` rather than
    a number that looks comparable and is not.
    """
    if venue is None or race_number is None:
        return None
    target = venue.strip().lower()
    for m in markets:
        if m.market_type != "PLACE":
            continue
        if (m.venue or "").strip().lower() != target:
            continue
        if m.race_number != race_number:
            continue
        if m.number_of_winners != places:
            log.info(
                "betfair: place market %s for %s R%s pays %s — not the %s "
                "dividends being valued; skipping as a second opinion",
                m.market_id, venue, race_number, m.number_of_winners, places,
            )
            continue
        return m
    return None
