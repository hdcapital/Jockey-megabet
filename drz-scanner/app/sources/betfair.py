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

import json
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
    #: Saddlecloth parsed from the runner name, when Betfair supplied one.
    cloth_number: int | None
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
#: Betfair horse-racing runner names carry the saddlecloth: "7. Zoustar".
_CLOTH_PREFIX = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*$")
#: Sportsbet appends origin/apprentice markers: "Zoustar (NZ)", "Name (a3)".
_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def split_runner_name(raw: str) -> tuple[int | None, str]:
    """``"7. Zoustar"`` -> ``(7, "Zoustar")``; ``"Zoustar"`` -> ``(None, "Zoustar")``."""
    m = _CLOTH_PREFIX.match(raw or "")
    if m:
        return int(m.group(1)), m.group(2)
    return None, (raw or "").strip()


def normalise_runner_name(raw: str) -> str:
    """Lower-case alphanumerics only, cloth prefix and bracketed suffix gone.

    Both sides of the Sportsbet/Betfair join go through this, so
    ``"7. Zoustar"`` and ``"Zoustar (NZ)"`` meet at ``"zoustar"``.
    """
    _, name = split_runner_name(raw)
    name = _TRAILING_PAREN.sub("", name)
    return "".join(ch for ch in name.lower() if ch.isalnum())


def normalise_venue(raw: str | None) -> str:
    return "".join(ch for ch in (raw or "").lower() if ch.isalnum())


def venues_match(a: str | None, b: str | None) -> bool:
    """Equal after normalisation, or one is a prefix of the other.

    Sportsbet writes "Sandown Hillside" where Betfair writes "Sandown"; the
    race number and the day still have to agree, so a prefix match is safe.
    """
    na, nb = normalise_venue(a), normalise_venue(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = sorted((na, nb), key=len)
    return len(shorter) >= 4 and longer.startswith(shorter)


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

    def _rpc(self, method: str, params: dict[str, Any], _retry: bool = True) -> Any:
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
            # A session token lasts hours, not days. In a long loop the first
            # sign of expiry is this error code; one fresh login fixes it.
            text = json.dumps(body["error"])
            if _retry and ("INVALID_SESSION_INFORMATION" in text or "NO_SESSION" in text):
                log.warning("betfair: session expired, logging in again")
                self._session_token = None
                self.login()
                return self._rpc(method, params, _retry=False)
            raise SourceUnavailableError(
                SOURCE, s.betfair_api_url, f"{method} error: {body['error']}"
            )
        return body.get("result")

    # -- markets ---------------------------------------------------------
    #: listMarketCatalogue has a data-weight limit of 200 per request:
    #: RUNNER_DESCRIPTION weighs 1 per market, MARKET_DESCRIPTION another 1.
    #: A whole Australian day is ~100 races x 2 market types, so one request
    #: for everything comes back TOO_MUCH_DATA. Ask per market type, with the
    #: runner projection only (the type is known from the request), in time
    #: windows small enough to stay under the limit.
    CATALOGUE_WINDOW_HOURS = 6
    CATALOGUE_MAX_RESULTS = 200

    def list_au_markets(
        self, for_date: date, market_types: tuple[str, ...] = ("WIN", "PLACE")
    ) -> list[BetfairMarket]:
        """Australian thoroughbred WIN and PLACE markets for a date."""
        start = datetime.combine(for_date, datetime.min.time(), tzinfo=timezone.utc)
        window_from = start - timedelta(hours=14)
        window_to = start + timedelta(hours=38)
        seen: set[str] = set()
        markets: list[BetfairMarket] = []
        for market_type in market_types:
            cursor = window_from
            while cursor < window_to:
                chunk_to = min(cursor + timedelta(hours=self.CATALOGUE_WINDOW_HOURS), window_to)
                catalogue = self._rpc(
                    "listMarketCatalogue",
                    {
                        "filter": {
                            "eventTypeIds": ["7"],
                            "marketCountries": ["AU"],
                            "marketTypeCodes": [market_type],
                            "marketStartTime": {
                                "from": cursor.isoformat(),
                                "to": chunk_to.isoformat(),
                            },
                        },
                        "marketProjection": [
                            "EVENT", "MARKET_START_TIME", "RUNNER_DESCRIPTION",
                        ],
                        "sort": "FIRST_TO_START",
                        "maxResults": self.CATALOGUE_MAX_RESULTS,
                    },
                ) or []
                if len(catalogue) >= self.CATALOGUE_MAX_RESULTS:
                    log.warning(
                        "betfair: %s catalogue window %s..%s hit maxResults=%d; "
                        "some markets may be missing",
                        market_type, cursor, chunk_to, self.CATALOGUE_MAX_RESULTS,
                    )
                for m in catalogue:
                    if m["marketId"] in seen:
                        continue
                    seen.add(m["marketId"])
                    event = m.get("event") or {}
                    start_str = m.get("marketStartTime")
                    name = m.get("marketName", "")
                    mm = _RACE_NO.match(name)
                    markets.append(
                        BetfairMarket(
                            market_id=m["marketId"],
                            market_name=name,
                            market_type=market_type,
                            venue=event.get("venue") or event.get("name"),
                            market_start=(
                                datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                                if isinstance(start_str, str)
                                else None
                            ),
                            race_number=int(mm.group(1)) if mm else None,
                            number_of_winners=None,   # filled from the market book
                            catalogue_runners={
                                r["selectionId"]: r.get("runnerName", "")
                                for r in m.get("runners", [])
                            },
                        )
                    )
                cursor = chunk_to
        log.info(
            "betfair: %d AU markets in catalogue (%s)",
            len(markets),
            ", ".join(f"{t}={sum(1 for x in markets if x.market_type == t)}" for t in market_types),
        )
        return markets

    def fetch_market_books(self, markets: list[BetfairMarket]) -> None:
        """Populate runner quotes from ``listMarketBook``.

        Whether the key is delayed is decided once per sweep, over every
        book together: a delayed key returns no matched volume anywhere,
        while a live key at 10am simply has some thin markets. Judging each
        market alone would hand every thin market the delayed key's looser
        gate — exactly the markets that should fail the liquidity gate.
        """
        s = self.settings
        fetched_at = datetime.now(timezone.utc)
        books: list[tuple[BetfairMarket, dict[str, Any]]] = []
        for i in range(0, len(markets), 25):  # API weight limits
            chunk = markets[i : i + 25]
            result = self._rpc(
                "listMarketBook",
                {
                    "marketIds": [m.market_id for m in chunk],
                    "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
                },
            ) or []
            by_id = {b["marketId"]: b for b in result}
            for market in chunk:
                book = by_id.get(market.market_id)
                if book:
                    books.append((market, book))

        setting = (s.betfair_key_delayed or "auto").strip().lower()
        if setting in ("true", "yes", "1"):
            delayed = True
        elif setting in ("false", "no", "0"):
            delayed = False
        else:
            delayed = bool(books) and all(market_is_delayed(b) for _, b in books)
        if delayed and not self.delayed_key_detected:
            log.warning(
                "betfair: no matched volume on any of %d market books — treating "
                "the application key as DELAYED (BETFAIR_KEY_DELAYED=%s); liquidity "
                "gate replaced by a %.0f%% spread-only gate, rows tagged "
                "betfair_delayed, win-price cap %.0f",
                len(books), setting, s.betfair_delayed_max_relative_spread * 100,
                s.max_win_price_betfair_delayed,
            )
        self.delayed_key_detected = delayed

        for market, book in books:
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
                raw_name = market.catalogue_runners.get(r["selectionId"], "")
                cloth, _ = split_runner_name(raw_name)
                market.runners.append(
                    BetfairRunnerQuote(
                        market_id=market.market_id,
                        selection_id=r["selectionId"],
                        runner_name=raw_name,
                        cloth_number=cloth,
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
    for m in markets:
        if m.market_type != "PLACE":
            continue
        if not venues_match(m.venue, venue):
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
