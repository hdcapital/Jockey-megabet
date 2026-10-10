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
  total matched volume >= ``BETFAIR_MIN_LIQUIDITY``.
* one side missing or the spread too wide: probability derived from the back
  price alone and marked unreliable (kept for the record, excluded from
  consensus by default).
* no usable prices: probability None ("unavailable"), never invented.
* delayed application key (every book reports zero matched volume): the
  liquidity gate cannot be met, so a runner is reliable when the spread is
  within ``BETFAIR_DELAYED_MAX_RELATIVE_SPREAD`` and its detail says so.
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

# Betfair names Australian runners "7. Zoustar" (saddlecloth, dot, name).
_CLOTH_PREFIX = re.compile(r"^\s*(\d+)\s*[.)-]\s*(.+?)\s*$")


def split_runner_name(raw: str) -> tuple[int | None, str]:
    """``"7. Zoustar"`` -> ``(7, "Zoustar")``; ``"Zoustar"`` -> ``(None, "Zoustar")``."""
    m = _CLOTH_PREFIX.match(raw or "")
    if m:
        return int(m.group(1)), m.group(2)
    return None, (raw or "").strip()


def market_is_delayed(book: dict[str, Any]) -> bool:
    """True when a market book carries no matched volume anywhere.

    A real Australian win market always has *some* matched volume once it
    is open. None at all, on the market and on every runner, is the
    signature of a delayed application key rather than of an empty market.
    """
    if book.get("totalMatched"):
        return False
    for r in book.get("runners", []) or []:
        if r.get("totalMatched"):
            return False
    return True


class BetfairNotConfiguredError(Exception):
    """Raised when Betfair credentials are absent — a normal, reported state."""


class BetfairLoginError(SourceUnavailableError):
    """Betfair answered the login call but refused it.

    ``code`` is Betfair's own ``error``/``loginStatus`` value (for example
    ``INVALID_APP_KEY``) and ``hint`` says what usually fixes it.
    """

    def __init__(self, url: str, code: str, hint: str, status: int | None = None):
        self.code = code
        self.hint = hint
        super().__init__(SOURCE, url, f"login refused: {code} — {hint}", status)


#: Betfair interactive-login error codes and what each one usually means.
#: A code outside this table is still reported verbatim.
LOGIN_HINTS: dict[str, str] = {
    "INVALID_USERNAME_OR_PASSWORD": (
        "Betfair rejected the username/password on this identity host. Check "
        "BETFAIR_USERNAME / BETFAIR_PASSWORD (no quotes or trailing spaces in "
        ".env); Australian/NZ accounts log in at identitysso.betfair.com.au; "
        "with two-factor auth enabled, append the current 2FA code to the "
        "password"
    ),
    "INVALID_APP_KEY": (
        "BETFAIR_APP_KEY is not a key Betfair recognises. Copy it from "
        "developer.betfair.com -> My Account -> API keys (the Delayed key "
        "works at once; the Live key only after Betfair activates it)"
    ),
    "ACCOUNT_NOW_LOCKED": "the account is locked after failed logins; unlock it on the Betfair site",
    "ACCOUNT_ALREADY_LOCKED": "the account is locked; unlock it on the Betfair site",
    "PENDING_AUTH": "the account needs a verification step on the Betfair site before the API will log it in",
    "TEMPORARY_BAN_TOO_MANY_REQUESTS": "too many login attempts; wait 20+ minutes before trying again",
    "SECURITY_RESTRICTED_LOCATION": "Betfair refuses logins from this location/IP (VPN, datacenter or geo-restricted); run from an ordinary Australian connection",
    "BETTING_RESTRICTED_LOCATION": "betting is restricted from this location; the API still refuses the login",
    "ACCOUNT_PENDING_PASSWORD_CHANGE": "Betfair requires a password change on the website first",
    "CHANGE_PASSWORD_REQUIRED": "Betfair requires a password change on the website first",
    "SECURITY_QUESTION_WRONG_3X": "security question failed three times; resolve on the Betfair site",
    "KYC_SUSPEND": "the account is suspended pending identity verification (KYC)",
    "SUSPENDED": "the account is suspended; contact Betfair",
    "CLOSED": "the account is closed",
    "SELF_EXCLUDED": "the account is self-excluded",
    "INVALID_CONNECTIVITY_TO_REGULATOR_DK": "Betfair cannot reach the regulator; try again later",
    "INVALID_CONNECTIVITY_TO_REGULATOR_IT": "Betfair cannot reach the regulator; try again later",
    "NOT_AUTHORIZED_BY_REGULATOR_DK": "the account is not authorised by its regulator",
    "NOT_AUTHORIZED_BY_REGULATOR_IT": "the account is not authorised by its regulator",
    "DANISH_AUTHORIZATION_REQUIRED": "Danish authorisation is required for this account",
    "SPAIN_MIGRATION_REQUIRED": "the account must be migrated on the Betfair site",
    "DENMARK_MIGRATION_REQUIRED": "the account must be migrated on the Betfair site",
    "SPANISH_TERMS_ACCEPTANCE_REQUIRED": "terms must be accepted on the Betfair site",
    "ITALIAN_CONTRACT_ACCEPTANCE_REQUIRED": "terms must be accepted on the Betfair site",
    "CERT_AUTH_REQUIRED": "this account must use certificate login; set BETFAIR_CERT_FILE / BETFAIR_KEY_FILE",
    "ITALIAN_PROFILING_ACCEPTANCE_REQUIRED": "profiling consent must be given on the Betfair site",
    "AUTHORIZED_ONLY_FOR_DOMAIN_RO": "the account is authorised only on betfair.ro",
    "AUTHORIZED_ONLY_FOR_DOMAIN_SE": "the account is authorised only on betfair.se",
    "INPUT_VALIDATION_ERROR": "the login request was malformed (empty username or password?)",
    "INTERNATIONAL_TERMS_ACCEPTANCE_REQUIRED": "terms must be accepted on the Betfair site",
    "EMAIL_LOGIN_NOT_ALLOWED": "log in with the Betfair username, not the email address",
    "MULTIPLE_USERS_WITH_SAME_CREDENTIAL": "the credentials match more than one account; contact Betfair",
    "ACCOUNT_PENDING_DISPLAY_NAME_CHANGE": "a display name change is pending on the Betfair site",
    "ACTIONS_REQUIRED": "Betfair requires an action on the website before the API will log in",
    "SWEDEN_BANK_ID_VERIFICATION_REQUIRED": "BankID verification is required",
    "SWEDEN_NATIONAL_IDENTIFIER_REQUIRED": "a national identifier is required",
    "TRADING_MASTER": "a trading master account cannot log in here",
    "TRADING_MASTER_SUSPENDED": "the trading master account is suspended",
    "AGENT_CLIENT_MASTER": "an agent client master account cannot log in here",
    "AGENT_CLIENT_MASTER_SUSPENDED": "the agent client master account is suspended",
    "NOT_WHITELISTED": "this API client is not whitelisted; contact Betfair",
}

#: Login failures that should NOT be retried on the other identity host,
#: because another attempt could make the account's situation worse.
_NO_FALLBACK_CODES = {
    "ACCOUNT_NOW_LOCKED", "ACCOUNT_ALREADY_LOCKED", "TEMPORARY_BAN_TOO_MANY_REQUESTS",
    "PENDING_AUTH", "SECURITY_RESTRICTED_LOCATION", "SECURITY_QUESTION_WRONG_3X",
    "INVALID_APP_KEY", "INPUT_VALIDATION_ERROR",
}


@dataclass
class BetfairStatus:
    """What the exchange side of a scan actually did, for the status line."""

    configured: bool = False
    logged_in: bool = False
    identity_host: str | None = None
    catalogue_markets: int = 0
    books_returned: int = 0
    open_books: int = 0
    delayed: bool = False
    error: str | None = None
    hint: str | None = None

    @property
    def ok(self) -> bool:
        return self.configured and self.logged_in and self.error is None

    def summary(self) -> str:
        if not self.configured:
            return "Betfair: not configured (BETFAIR_APP_KEY / BETFAIR_USERNAME unset) — Sportsbet-only"
        if self.error:
            return f"Betfair: FAILED — {self.error}"
        parts = [
            f"Betfair: connected via {self.identity_host}",
            f"{self.catalogue_markets} AU win markets",
            f"{self.books_returned} books ({self.open_books} open)",
        ]
        if self.delayed:
            parts.append("DELAYED key (spread-only reliability)")
        return "; ".join(parts)


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
    cloth_number: int | None = None  # parsed from "7. Zoustar", None if absent


@dataclass
class BetfairMarket:
    market_id: str
    market_name: str
    venue: str | None
    market_start: datetime | None
    race_number: int | None = None
    runners: list[BetfairRunnerQuote] = field(default_factory=list)
    book_status: str | None = None  # None until a book was fetched
    delayed: bool = False  # prices came from a delayed application key
    # Runners the exchange has REMOVED (scratched), by catalogue name, so a
    # Sportsbet runner that is still active can be reported as such.
    removed_names: list[str] = field(default_factory=list)
    inplay: bool = False  # the race is running; prices are in-running
    total_matched: float | None = None  # market's matched volume (AUD)
    fetched_at: datetime | None = None  # when the book was retrieved


def derive_probability(
    best_back: float | None,
    best_lay: float | None,
    total_matched: float | None,
    min_liquidity: float,
    max_relative_spread: float,
    delayed: bool = False,
    delayed_max_relative_spread: float = 0.10,
) -> tuple[float | None, bool, str]:
    """(probability, reliable, detail) from exchange prices — see module doc.

    ``delayed`` switches to the spread-only rule: matched volume is not
    reported by a delayed key, so it cannot be a condition of reliability.
    """
    if delayed:
        max_relative_spread = delayed_max_relative_spread
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
            if delayed:
                return 1.0 / mid, True, "midpoint of best back/lay; delayed key, spread-only"
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


#: APING error codes that are worth a plain-English line.
API_ERROR_HINTS: dict[str, str] = {
    "INVALID_APP_KEY": "the application key is not recognised",
    "INVALID_SESSION_INFORMATION": "the session token is missing or expired",
    "NO_SESSION": "no session token was sent",
    "NO_APP_KEY": "no application key was sent",
    "TOO_MUCH_DATA": "the request exceeded Betfair's data-weight limit (200)",
    "TOO_MANY_REQUESTS": "request rate limit hit; slow down",
    "SERVICE_BUSY": "Betfair is busy; retry shortly",
    "TIMEOUT_ERROR": "Betfair timed out; retry shortly",
    "UNEXPECTED_ERROR": "Betfair reported an internal error",
    "ACCESS_DENIED": "this application key is not allowed to call this method",
    "INVALID_INPUT_DATA": "the request parameters were rejected",
    "REQUEST_SIZE_EXCEEDS_LIMIT": "the request was too large",
}


def describe_api_error(error: Any) -> str:
    """Betfair's JSON-RPC error with its APING code and a hint if known."""
    code = None
    if isinstance(error, dict):
        data = error.get("data") or {}
        exc = data.get("APINGException") if isinstance(data, dict) else None
        if isinstance(exc, dict):
            code = exc.get("errorCode")
        if code is None:
            code = error.get("message")
    text = json.dumps(error) if not isinstance(error, str) else error
    if code and code in API_ERROR_HINTS:
        return f"{code} ({API_ERROR_HINTS[code]}): {text}"
    return text


class BetfairClient:
    def __init__(self, client: ArchivingClient | None = None, settings: Any = None):
        self.settings = settings or get_settings()
        if not (self.settings.betfair_app_key and self.settings.betfair_username):
            raise BetfairNotConfiguredError(
                "BETFAIR_APP_KEY / BETFAIR_USERNAME not set; Betfair benchmark disabled"
            )
        self.status.configured = True
        self.client = client or ArchivingClient(SOURCE)
        self._session_token: str | None = None

    @property
    def status(self) -> BetfairStatus:
        """Outcome of this client's calls so far (created on first use)."""
        st = self.__dict__.get("_status")
        if st is None:
            st = self.__dict__["_status"] = BetfairStatus()
        return st

    def close(self) -> None:
        self.client.close()

    # -- auth -----------------------------------------------------------
    def _uses_cert_login(self) -> bool:
        s = self.settings
        return bool(s.betfair_cert_file and s.betfair_key_file)

    def identity_urls(self) -> list[str]:
        """Login URLs to try, configured host first, then the other region.

        Betfair keeps Australian/NZ accounts on identitysso.betfair.com.au
        and everyone else on identitysso.betfair.com; a username that
        exists on one is "invalid" on the other. Trying both (and saying
        which one worked) beats failing on a regional default.
        """
        s = self.settings
        if self._uses_cert_login():
            hosts = [s.betfair_identity_cert_url, s.betfair_identity_cert_url_au]
            path = "/api/certlogin"
        else:
            hosts = [s.betfair_identity_url, s.betfair_identity_url_au]
            path = "/api/login"
        urls: list[str] = []
        for h in hosts:
            if h:
                u = h.rstrip("/") + path
                if u not in urls:
                    urls.append(u)
        return urls

    def _login_once(self, url: str) -> str:
        """POST the credentials to one identity URL; return the session token."""
        s = self.settings
        result = self.client.post_json(
            url,
            data={"username": s.betfair_username, "password": s.betfair_password or ""},
            headers={
                "X-Application": s.betfair_app_key or "",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            body = result.json()
        except ValueError:
            snippet = " ".join(result.body[:300].decode("utf-8", "replace").split())
            raise SourceUnavailableError(
                SOURCE, url,
                "login answered with something other than JSON (a block page or "
                "challenge rather than the API)",
                result.status_code, snippet or None,
            ) from None
        if not isinstance(body, dict):
            raise SourceUnavailableError(SOURCE, url, f"unexpected login body: {body!r}")
        token = body.get("token") or body.get("sessionToken")
        status = body.get("status") or body.get("loginStatus") or ""
        code = body.get("error") or status or "UNKNOWN"
        if token and status in ("SUCCESS", "", None):
            return token
        if token:
            # SUCCESS is the only status that comes with a usable token.
            log.warning("betfair: login returned a token with status %s", status)
            return token
        hint = LOGIN_HINTS.get(code, "see Betfair's login error documentation")
        raise BetfairLoginError(url, code, hint, result.status_code)

    def login(self) -> None:
        """Obtain a session token, trying the regional identity hosts in turn.

        Raises :class:`BetfairLoginError` (with Betfair's code and a hint) or
        :class:`SourceUnavailableError` carrying every attempt's outcome.
        """
        attempts: list[str] = []
        last: SourceUnavailableError | None = None
        for url in self.identity_urls():
            try:
                token = self._login_once(url)
            except BetfairLoginError as exc:
                attempts.append(f"{url}: {exc.code}")
                last = exc
                log.warning("betfair: login at %s refused: %s", url, exc.code)
                if exc.code in _NO_FALLBACK_CODES:
                    break
                continue
            except SourceUnavailableError as exc:
                attempts.append(f"{url}: {exc.detail}")
                last = exc
                log.warning("betfair: login at %s failed: %s", url, exc.detail)
                continue
            self._session_token = token
            self.status.logged_in = True
            self.status.identity_host = url.split("/api/")[0].replace("https://", "")
            log.info("betfair: login ok via %s", url)
            return
        assert last is not None
        if len(attempts) > 1 and isinstance(last, BetfairLoginError):
            raise BetfairLoginError(
                last.url, last.code,
                last.hint + " (tried " + "; ".join(attempts) + ")", last.status,
            )
        raise last

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
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            body = result.json()
        except ValueError:
            snippet = " ".join(result.body[:300].decode("utf-8", "replace").split())
            raise SourceUnavailableError(
                SOURCE, s.betfair_api_url,
                f"{method} answered with something other than JSON (a block page "
                "or challenge rather than the API)",
                result.status_code, snippet or None,
            ) from None
        if not isinstance(body, dict):
            raise SourceUnavailableError(
                SOURCE, s.betfair_api_url, f"{method}: unexpected body {body!r}"
            )
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
                SOURCE, s.betfair_api_url, f"{method} error: {describe_api_error(body['error'])}"
            )
        return body.get("result")

    # -- markets ---------------------------------------------------------
    #: listMarketCatalogue has a data-weight limit of 200 per request:
    #: RUNNER_DESCRIPTION weighs 1 per market and MARKET_DESCRIPTION another
    #: 1. A normal Australian day is ~110 WIN markets, so asking for both in
    #: one call (222) comes back TOO_MUCH_DATA and the exchange never engages
    #: (observed live 2026-09-19). MARKET_DESCRIPTION was never read by the
    #: parser; drop it, and ask in time windows so a big day stays under the
    #: limit too.
    CATALOGUE_WINDOW_HOURS = 6
    CATALOGUE_MAX_RESULTS = 200

    def list_au_win_markets(self, for_date: date) -> list[BetfairMarket]:
        """Australian thoroughbred WIN markets starting on the given date."""
        start = datetime.combine(for_date, datetime.min.time(), tzinfo=timezone.utc)
        markets = self.list_win_markets(
            start - timedelta(hours=14), start + timedelta(hours=38)
        )
        if not markets:
            log.warning(
                "betfair: the catalogue has no Australian WIN markets starting "
                "within 14h before / 38h after %s UTC — nothing for the exchange "
                "to price (wrong --date, or no AU racing in that span)", for_date,
            )
        return markets

    def list_win_markets(
        self,
        window_from: datetime,
        window_to: datetime,
        countries: tuple[str, ...] = ("AU",),
    ) -> list[BetfairMarket]:
        """Thoroughbred WIN markets in ``countries`` starting in the window."""
        import re as _re

        markets: list[BetfairMarket] = []
        seen: set[str] = set()
        cursor = window_from
        while cursor < window_to:
            chunk_to = min(cursor + timedelta(hours=self.CATALOGUE_WINDOW_HOURS), window_to)
            catalogue = self._rpc(
                "listMarketCatalogue",
                {
                    "filter": {
                        "eventTypeIds": ["7"],  # horse racing
                        "marketCountries": list(countries),
                        "marketTypeCodes": ["WIN"],
                        "marketStartTime": {
                            "from": cursor.isoformat(),
                            "to": chunk_to.isoformat(),
                        },
                    },
                    "marketProjection": ["EVENT", "MARKET_START_TIME", "RUNNER_DESCRIPTION"],
                    "sort": "FIRST_TO_START",
                    "maxResults": self.CATALOGUE_MAX_RESULTS,
                },
            ) or []
            if len(catalogue) >= self.CATALOGUE_MAX_RESULTS:
                log.warning(
                    "betfair: catalogue window %s..%s hit maxResults=%d; some "
                    "markets may be missing", cursor, chunk_to, self.CATALOGUE_MAX_RESULTS,
                )
            for m in catalogue:
                if m["marketId"] in seen:
                    continue
                seen.add(m["marketId"])
                event = m.get("event") or {}
                start_str = m.get("marketStartTime")
                start_dt = (
                    datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    if isinstance(start_str, str)
                    else None
                )
                name = m.get("marketName", "")
                mm = _re.match(r"\s*R(\d+)", name)
                bm = BetfairMarket(
                    market_id=m["marketId"],
                    market_name=name,
                    venue=event.get("venue") or event.get("name"),
                    market_start=start_dt,
                    race_number=int(mm.group(1)) if mm else None,
                )
                bm._catalogue_runners = {  # type: ignore[attr-defined]
                    r["selectionId"]: r.get("runnerName", "")
                    for r in m.get("runners", [])
                }
                markets.append(bm)
            cursor = chunk_to
        self.status.catalogue_markets = len(markets)
        log.info(
            "betfair: %d %s win markets in catalogue", len(markets), "/".join(countries)
        )
        return markets

    def fetch_market_books(self, markets: list[BetfairMarket]) -> None:
        """Populate runner quotes with live best back/lay via listMarketBook.

        Whether the key is delayed is decided once per call from every book
        together (see :func:`market_is_delayed`), never from a single thin
        market, unless ``BETFAIR_KEY_DELAYED`` forces it.
        """
        s = self.settings
        fetched_at = datetime.now(timezone.utc)
        books_by_id: dict[str, dict[str, Any]] = {}
        for i in range(0, len(markets), 25):  # API weight limits
            chunk = markets[i : i + 25]
            books = self._rpc(
                "listMarketBook",
                {
                    "marketIds": [m.market_id for m in chunk],
                    "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
                },
            ) or []
            for b in books:
                books_by_id[b["marketId"]] = b

        forced = (s.betfair_key_delayed or "auto").strip().lower()
        if forced in ("true", "1", "yes"):
            delayed = True
        elif forced in ("false", "0", "no"):
            delayed = False
        else:
            open_books = [b for b in books_by_id.values() if b.get("status") == "OPEN"]
            delayed = bool(open_books) and all(market_is_delayed(b) for b in open_books)
        if delayed:
            log.warning(
                "betfair: no matched volume on any of %d open books — treating the "
                "application key as DELAYED (spread-only reliability, max %.0f%%). "
                "Set BETFAIR_KEY_DELAYED=false to override.",
                len(books_by_id), s.betfair_delayed_max_relative_spread * 100,
            )
        self.status.books_returned = len(books_by_id)
        self.status.open_books = sum(
            1 for b in books_by_id.values() if b.get("status") == "OPEN"
        )
        self.status.delayed = delayed

        missing = 0
        for market in markets:
            book = books_by_id.get(market.market_id)
            market.delayed = delayed
            if not book:
                missing += 1
                log.warning(
                    "betfair: no book returned for %s %s (%s)",
                    market.venue, market.market_name, market.market_id,
                )
                continue
            status = book.get("status", "UNKNOWN")
            market.book_status = status
            market.inplay = bool(book.get("inplay"))
            market.total_matched = book.get("totalMatched")
            market.fetched_at = fetched_at
            market.runners = []  # a re-fetched book replaces the old quotes
            names = getattr(market, "_catalogue_runners", {})
            active = [r for r in book.get("runners", []) if r.get("status") in (None, "ACTIVE")]
            market.removed_names = [
                names.get(r["selectionId"], str(r["selectionId"]))
                for r in book.get("runners", [])
                if str(r.get("status", "")).startswith("REMOVED")
            ]
            if not active:
                log.warning(
                    "betfair: %s %s book is %s with no active runners "
                    "(%d in book, %d in catalogue)",
                    market.venue, market.market_name, status,
                    len(book.get("runners", [])), len(names),
                )
            for r in active:
                ex = r.get("ex") or {}
                backs = ex.get("availableToBack") or []
                lays = ex.get("availableToLay") or []
                best_back = backs[0]["price"] if backs else None
                back_vol = backs[0]["size"] if backs else None
                best_lay = lays[0]["price"] if lays else None
                lay_vol = lays[0]["size"] if lays else None
                # The liquidity gate is on the market's matched volume (as
                # documented): a $500 gate on each runner's own volume would
                # exclude most outsiders all morning, and a Megabet needs
                # every ride reliable before the exchange model engages.
                runner_total = r.get("totalMatched")
                market_total = book.get("totalMatched")
                total = market_total if market_total else runner_total
                prob, reliable, detail = derive_probability(
                    best_back,
                    best_lay,
                    total,
                    s.betfair_min_liquidity,
                    s.betfair_max_relative_spread,
                    delayed=delayed,
                    delayed_max_relative_spread=s.betfair_delayed_max_relative_spread,
                )
                raw_name = names.get(r["selectionId"], "")
                cloth, _ = split_runner_name(raw_name)
                market.runners.append(
                    BetfairRunnerQuote(
                        market_id=market.market_id,
                        selection_id=r["selectionId"],
                        runner_name=raw_name,
                        best_back=best_back,
                        best_lay=best_lay,
                        back_volume=back_vol,
                        lay_volume=lay_vol,
                        total_matched=runner_total if runner_total is not None else total,
                        market_status=status,
                        fetched_at=fetched_at,
                        probability=prob,
                        reliable=reliable,
                        detail=detail,
                        cloth_number=cloth,
                    )
                )
        if missing:
            log.warning("betfair: %d of %d markets came back without a book", missing, len(markets))
