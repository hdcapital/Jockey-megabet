"""Official Betfair Exchange API adapter (external probability benchmark).

Uses the documented Betting API (JSON-RPC) with an application key and a
session token obtained from the identity SSO endpoint. Credentials come from
environment variables only (see ``.env.example``); nothing is ever written
to the repository or logs.

Probability methodology (Model B)
---------------------------------
For each runner we record best available back price, best available lay
price and visible available volume, then derive:

* both sides present and relative spread <= ``BETFAIR_MAX_RELATIVE_SPREAD``:
  probability = 1 / midpoint(back, lay), marked reliable when the market's
  total available-to-back volume >= ``BETFAIR_MIN_LIQUIDITY``.
* one side missing or the spread too wide: probability derived from the back
  price alone and marked unreliable (kept for the record, excluded from
  consensus by default).
* no usable prices: probability None ("unavailable"), never invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.http import ArchivingClient, SourceUnavailableError

log = logging.getLogger(__name__)

SOURCE = "betfair"


class BetfairNotConfiguredError(Exception):
    """Raised when Betfair credentials are absent — a normal, reported state."""


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
    detail: str = ""


@dataclass
class BetfairMarket:
    market_id: str
    market_name: str
    venue: str | None
    market_start: datetime | None
    race_number: int | None = None
    runners: list[BetfairRunnerQuote] = field(default_factory=list)


def derive_probability(
    best_back: float | None,
    best_lay: float | None,
    total_matched: float | None,
    min_liquidity: float,
    max_relative_spread: float,
) -> tuple[float | None, bool, str]:
    """(probability, reliable, detail) from exchange prices — see module doc."""
    if best_back is not None and best_back <= 1.0:
        best_back = None
    if best_lay is not None and best_lay <= 1.0:
        best_lay = None
    if best_back is None and best_lay is None:
        return None, False, "no exchange prices available"
    if best_back is not None and best_lay is not None:
        spread = (best_lay - best_back) / best_back
        if spread < 0:
            return None, False, f"crossed book (back {best_back} > lay {best_lay})"
        if spread <= max_relative_spread:
            mid = (best_back + best_lay) / 2.0
            liquid = total_matched is not None and total_matched >= min_liquidity
            detail = "midpoint of best back/lay" + (
                "" if liquid else f"; thin market (matched {total_matched})"
            )
            return 1.0 / mid, liquid, detail
        return (
            1.0 / best_back,
            False,
            f"spread {spread:.1%} too wide for midpoint; using best back",
        )
    side = best_back if best_back is not None else best_lay
    name = "back" if best_back is not None else "lay"
    return 1.0 / side, False, f"only best {name} available"


class BetfairClient:
    def __init__(self, client: ArchivingClient | None = None):
        self.settings = get_settings()
        if not (self.settings.betfair_app_key and self.settings.betfair_username):
            raise BetfairNotConfiguredError(
                "BETFAIR_APP_KEY / BETFAIR_USERNAME not set; Betfair benchmark disabled"
            )
        self.client = client or ArchivingClient(SOURCE)
        self._session_token: str | None = None

    def close(self) -> None:
        self.client.close()

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
        status = body.get("status") or body.get("loginStatus")
        if not token:
            raise SourceUnavailableError(
                SOURCE, url, f"login failed: {status or body}", result.status_code
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
    def list_au_win_markets(self, for_date: date) -> list[BetfairMarket]:
        """Australian thoroughbred WIN markets starting on the given date."""
        start = datetime.combine(for_date, datetime.min.time(), tzinfo=timezone.utc)
        catalogue = self._rpc(
            "listMarketCatalogue",
            {
                "filter": {
                    "eventTypeIds": ["7"],  # horse racing
                    "marketCountries": ["AU"],
                    "marketTypeCodes": ["WIN"],
                    "marketStartTime": {
                        "from": (start - timedelta(hours=14)).isoformat(),
                        "to": (start + timedelta(hours=38)).isoformat(),
                    },
                },
                "marketProjection": [
                    "EVENT", "MARKET_START_TIME", "RUNNER_DESCRIPTION", "MARKET_DESCRIPTION",
                ],
                "sort": "FIRST_TO_START",
                "maxResults": 200,
            },
        ) or []
        markets: list[BetfairMarket] = []
        for m in catalogue:
            event = m.get("event") or {}
            start_str = m.get("marketStartTime")
            start_dt = (
                datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if isinstance(start_str, str)
                else None
            )
            name = m.get("marketName", "")
            race_no = None
            import re as _re

            mm = _re.match(r"\s*R(\d+)", name)
            if mm:
                race_no = int(mm.group(1))
            bm = BetfairMarket(
                market_id=m["marketId"],
                market_name=name,
                venue=event.get("venue") or event.get("name"),
                market_start=start_dt,
                race_number=race_no,
            )
            bm._catalogue_runners = {  # type: ignore[attr-defined]
                r["selectionId"]: r.get("runnerName", "")
                for r in m.get("runners", [])
            }
            markets.append(bm)
        log.info("betfair: %d AU win markets in catalogue", len(markets))
        return markets

    def fetch_market_books(self, markets: list[BetfairMarket]) -> None:
        """Populate runner quotes with live best back/lay via listMarketBook."""
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
                status = book.get("status", "UNKNOWN")
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
                    )
                    names = getattr(market, "_catalogue_runners", {})
                    market.runners.append(
                        BetfairRunnerQuote(
                            market_id=market.market_id,
                            selection_id=r["selectionId"],
                            runner_name=names.get(r["selectionId"], ""),
                            best_back=best_back,
                            best_lay=best_lay,
                            back_volume=back_vol,
                            lay_volume=lay_vol,
                            total_matched=total,
                            market_status=status,
                            fetched_at=fetched_at,
                            probability=prob,
                            reliable=reliable,
                            detail=detail,
                        )
                    )
