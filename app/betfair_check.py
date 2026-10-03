"""Step-by-step Betfair connectivity diagnosis.

Run:  python -m app.betfair_check [--date YYYY-MM-DD] [--books N] [-v]

Walks the exact path the scanner takes — configuration, login, market
catalogue, market books, price reliability — and prints one line per step
with the real outcome, Betfair's own error code when a step is refused, and
what usually fixes it. Nothing secret is printed: the app key and username
are masked and the password never appears.

Exit codes: 0 everything reachable; 1 not configured; 2 login refused or
unreachable; 3 logged in but the betting API failed; 4 connected but no
usable prices were found (reported, not an error of this tool).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone

from app.config import PROJECT_ROOT, get_settings
from app.http import SourceUnavailableError
from app.logging_setup import setup_logging
from app.sources.betfair import (
    BetfairClient,
    BetfairLoginError,
    BetfairNotConfiguredError,
)

log = logging.getLogger(__name__)


def _mask(value: str | None, keep: int = 3) -> str:
    if not value:
        return "(unset)"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]} ({len(value)} chars)"


def _say(step: str, ok: bool | None, text: str) -> None:
    tag = {True: "OK  ", False: "FAIL", None: "INFO"}[ok]
    print(f"[{tag}] {step}: {text}")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.betfair_check",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--date", help="catalogue date YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--books", type=int, default=12,
                        help="how many markets to fetch books for (default 12)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.WARNING)

    s = get_settings()
    for_date = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()

    # 1. configuration -----------------------------------------------------
    env_path = PROJECT_ROOT / ".env"
    _say("config", None, f".env {'found' if env_path.exists() else 'NOT found'} at {env_path}")
    _say("config", bool(s.betfair_app_key), f"BETFAIR_APP_KEY = {_mask(s.betfair_app_key)}")
    _say("config", bool(s.betfair_username), f"BETFAIR_USERNAME = {_mask(s.betfair_username, 2)}")
    _say("config", bool(s.betfair_password or (s.betfair_cert_file and s.betfair_key_file)),
         "BETFAIR_PASSWORD = " + ("set" if s.betfair_password else "(unset)")
         + ("; certificate login configured" if s.betfair_cert_file and s.betfair_key_file else ""))
    try:
        bf = BetfairClient()
    except BetfairNotConfiguredError as exc:
        _say("config", False, f"{exc}. Put the three BETFAIR_* lines in {env_path}")
        return 1
    _say("config", None, "identity hosts to try: " + ", ".join(bf.identity_urls()))
    _say("config", None, f"betting API: {s.betfair_api_url}")

    # 2. login -------------------------------------------------------------
    try:
        bf.login()
    except BetfairLoginError as exc:
        _say("login", False, f"Betfair refused the login: {exc.code}")
        _say("login", None, f"what usually fixes it: {exc.hint}")
        if exc.body_snippet:
            _say("login", None, f"response: {exc.body_snippet}")
        return 2
    except SourceUnavailableError as exc:
        _say("login", False, f"could not reach the identity host: {exc.detail}")
        if exc.body_snippet:
            _say("login", None, f"response body: {exc.body_snippet}")
        _say("login", None,
             "an HTTP 403 with a Cloudflare/Akamai page means Betfair's edge blocks "
             "this network (datacenter/cloud IPs are refused); run from an ordinary "
             "Australian connection")
        return 2
    _say("login", True, f"session token obtained via {bf.status.identity_host}")

    # 3. catalogue ---------------------------------------------------------
    try:
        markets = bf.list_au_win_markets(for_date)
    except SourceUnavailableError as exc:
        _say("catalogue", False, f"listMarketCatalogue failed: {exc.detail}")
        if exc.body_snippet:
            _say("catalogue", None, f"response body: {exc.body_snippet}")
        return 3
    if not markets:
        _say("catalogue", False,
             f"no Australian WIN markets found around {for_date} UTC. Try "
             "--date on a racing day; if that also finds nothing the app key "
             "may lack market data access")
        return 4
    venues: dict[str, int] = {}
    for m in markets:
        venues[m.venue or "?"] = venues.get(m.venue or "?", 0) + 1
    _say("catalogue", True, f"{len(markets)} AU WIN markets: "
         + ", ".join(f"{v} ({n})" for v, n in sorted(venues.items())))
    first = markets[0]
    sample_runners = list(getattr(first, "_catalogue_runners", {}).values())[:3]
    _say("catalogue", None,
         f"first market {first.market_id} {first.venue} {first.market_name} at "
         f"{first.market_start}; runners look like: {sample_runners}")

    # 4. books -------------------------------------------------------------
    subset = markets[: max(1, args.books)]
    try:
        bf.fetch_market_books(subset)
    except SourceUnavailableError as exc:
        _say("books", False, f"listMarketBook failed: {exc.detail}")
        if exc.body_snippet:
            _say("books", None, f"response body: {exc.body_snippet}")
        return 3
    st = bf.status
    _say("books", st.books_returned > 0,
         f"{st.books_returned}/{len(subset)} books returned, {st.open_books} open"
         + ("; application key is DELAYED (no matched volume reported)" if st.delayed else ""))
    quotes = [q for m in subset for q in m.runners]
    priced = [q for q in quotes if q.probability is not None]
    reliable = [q for q in quotes if q.reliable]
    _say("prices", bool(priced),
         f"{len(quotes)} active runners quoted, {len(priced)} with a price, "
         f"{len(reliable)} reliable under the current gates "
         f"(min liquidity {s.betfair_min_liquidity:g}, max spread "
         f"{(s.betfair_delayed_max_relative_spread if st.delayed else s.betfair_max_relative_spread):.0%})")
    for q in quotes[:6]:
        _say("prices", None,
             f"{q.runner_name!r}: back {q.best_back} lay {q.best_lay} matched "
             f"{q.total_matched} -> p={q.probability and round(q.probability, 4)} "
             f"{'reliable' if q.reliable else 'unreliable'}: {q.detail}")
    if not priced:
        _say("prices", None,
             "books came back without prices: markets may be suspended/closed at "
             "this hour, or the key may not carry price data")
        return 4
    if not reliable:
        _say("prices", None,
             "connected and priced, but nothing passes the reliability gates yet; "
             "spreads tighten as the jump approaches. Lower BETFAIR_MIN_LIQUIDITY "
             "or raise the spread limits in .env if this persists all day")
    _say("result", True, st.summary())
    bf.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except Exception as exc:  # the diagnosis must end with a verdict, not a traceback
        log.exception("betfair_check crashed")
        _say("crash", False, f"{type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
