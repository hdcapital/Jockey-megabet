"""Live scanner: discover Jockey Megabets, price them, persist observations.

Run:  python -m app.scan [--meeting X] [--jockey Y] [--min-edge 0.05]
                         [--date YYYY-MM-DD] [--source sportsbet]
                         [--show-low] [--loop] [--no-db]

Every scan writes timestamped observations (prices + valuations) to the
database so the strategy can later be backtested against actual results.
If a source cannot be reached, the real error is reported and nothing is
fabricated.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone

from app.config import get_settings
from app.engine import MegabetValuation, value_offer
from app.http import SourceUnavailableError
from app.logging_setup import setup_logging
from app.matching.names import normalize_name, venue_names_match
from app.reporting import tables
from app.sources.base import MegabetOffer, RaceInfo, SchemaMismatchError
from app.sources.sportsbet import SportsbetClient, _first
from app.sources.betfair import (
    BetfairClient,
    BetfairMarket,
    BetfairNotConfiguredError,
)

log = logging.getLogger(__name__)


def gather_meeting_races(
    sb: SportsbetClient, offers: list[MegabetOffer], for_date: date
) -> dict[str, list[RaceInfo]]:
    """Fetch racecards for every meeting that has a Jockey Megabet offer.

    Returns {normalized meeting name: [RaceInfo, ...]}.
    """
    wanted = {normalize_name(o.meeting_name) for o in offers if o.meeting_name}
    if not wanted:
        log.warning("offers carry no meeting names; cannot locate racecards")
        return {}
    meetings_raw, _ = sb.fetch_meetings(for_date)
    races_by_meeting: dict[str, list[RaceInfo]] = {}
    for meeting in meetings_raw:
        name = _first(meeting, "name", "venueName", "meetingName")
        if not isinstance(name, str):
            continue
        if not any(venue_names_match(name, w) for w in wanted if w):
            continue
        key = normalize_name(name)
        race_nodes = _first(meeting, "races", "events") or []
        for rn in race_nodes:
            if not isinstance(rn, dict):
                continue
            event_id = _first(rn, "id", "eventId")
            if event_id is None:
                continue
            try:
                card = sb.fetch_racecard(str(event_id))
            except (SourceUnavailableError, SchemaMismatchError) as exc:
                log.error("racecard %s failed: %s", event_id, exc)
                continue
            races_by_meeting.setdefault(key, []).extend(card.races)
        log.info(
            "meeting %s: %d races retrieved", name, len(races_by_meeting.get(key, []))
        )
    return races_by_meeting


def fetch_betfair_markets(for_date: date) -> list[BetfairMarket] | None:
    """Betfair AU win markets with live books, or None when unavailable."""
    try:
        bf = BetfairClient()
    except BetfairNotConfiguredError as exc:
        log.info("betfair benchmark disabled: %s", exc)
        return None
    try:
        markets = bf.list_au_win_markets(for_date)
        bf.fetch_market_books(markets)
        return markets
    except SourceUnavailableError as exc:
        log.error("betfair unavailable: %s", exc)
        return None
    finally:
        bf.close()


def persist_scan(
    offers: list[MegabetOffer],
    races_by_meeting: dict[str, list[RaceInfo]],
    valuations: list[MegabetValuation],
    now: datetime,
) -> None:
    from app.database.repository import Repository, session_factory
    from app.engine import price_race_sportsbet
    from app.matching.names import jockey_names_match

    settings = get_settings()
    Session = session_factory()
    with Session() as s, s.begin():
        repo = Repository(s)
        race_rows = {}
        runner_rows = {}
        for key, races in races_by_meeting.items():
            meeting_date = next(
                (r.start_time.date() for r in races if r.start_time), None
            )
            for race in races:
                meeting_row = repo.upsert_meeting(
                    race.source, key or race.source_id, key, meeting_date, None
                )
                race_row = repo.upsert_race(
                    meeting_row, race.source, race.source_id,
                    race.race_number, race.start_time, race.status,
                )
                race_rows[race.source_id] = race_row
                dv = price_race_sportsbet(race, settings.devig_method)
                active_priced = [
                    r for r in race.active_runners() if r.win_odds and r.win_odds > 1.0
                ]
                fair_by_id = {}
                if dv:
                    fair_by_id = {
                        r.source_id: dv.fair_probabilities[i]
                        for i, r in enumerate(active_priced)
                    }
                for runner in race.runners:
                    row = repo.upsert_runner(
                        race_row, runner.source, runner.source_id,
                        runner.horse_name, runner.jockey_name,
                        runner.saddlecloth, runner.status,
                    )
                    runner_rows[(race.source_id, runner.source_id)] = row
                    if runner.win_odds:
                        repo.add_runner_price(
                            row, now, "sportsbet",
                            decimal_odds=runner.win_odds,
                            raw_probability=1.0 / runner.win_odds,
                            fair_probability=fair_by_id.get(runner.source_id),
                            devig_method=dv.method if dv else None,
                            race_overround=dv.overround if dv else None,
                        )
                if race.status == "resulted" and race.winner_names:
                    winner = race.winner_names[0]
                    wj = next(
                        (r.jockey_name for r in race.runners
                         if r.horse_name == winner), None,
                    )
                    repo.add_result(race_row, winner, wj)

        for v in valuations:
            meeting_row = None
            offer = v.offer
            megabet = repo.upsert_megabet(
                offer.source, offer.market_id or offer.selection_id or offer.market_name,
                meeting_row, offer.meeting_name, offer.meeting_date,
                offer.jockey_name, offer.threshold, offer.market_name,
            )
            if v.model == "consensus":  # store the SB price once per offer scan
                repo.add_megabet_price(
                    megabet, offer.odds, offer.market_status, now, offer.raw_sha256
                )
            ride_probs = [
                p for p in (r.probability_for(v.model) for r in v.rides)
                if p is not None
            ]
            repo.add_valuation(
                megabet, now, v.model,
                fair_probability=v.fair_probability, fair_odds=v.fair_odds,
                sportsbet_odds=offer.odds, expected_return=v.expected_return,
                number_of_rides=len(v.ride_card.rides), quality=v.quality,
                quality_detail=v.quality_detail,
                ride_probabilities=ride_probs or None,
            )
            if v.ride_card.match_status in ("unmatched", "ambiguous"):
                repo.add_unmatched(
                    "jockey", offer.jockey_name,
                    v.ride_card.normalized_jockey_name,
                    f"megabet {offer.market_name!r} at {offer.meeting_name}",
                    v.ride_card.match_status,
                )
            # Jockey changes may make the Megabet void under Sportsbet
            # settlement rules — recorded separately from fair value: flag
            # any Megabet whose jockey lost or gained a booked ride this scan.
            if any(
                jockey_names_match(offer.jockey_name, old)
                or jockey_names_match(offer.jockey_name, new)
                for _, old, new in repo.jockey_changes
            ):
                megabet.possible_void_on_jockey_change = True
                log.warning(
                    "megabet %s flagged possible-void: jockey change involving %s",
                    offer.market_name, offer.jockey_name,
                )
    log.info("scan persisted: %d valuations", len(valuations))


def jockey_names_match_safe(a: str, b: str) -> bool:
    from app.matching.names import jockey_names_match

    return jockey_names_match(a, b)


def run_scan(args: argparse.Namespace) -> int:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    scan_date = date.fromisoformat(args.date) if args.date else now.date()

    with SportsbetClient() as sb:
        try:
            offers = sb.discover_jockey_megabets()
        except SourceUnavailableError as exc:
            tables.print_source_unavailable("Sportsbet", str(exc))
            return 2
        except SchemaMismatchError as exc:
            tables.print_source_unavailable("Sportsbet (schema)", str(exc))
            return 3

        if args.meeting:
            offers = [
                o for o in offers
                if o.meeting_name and venue_names_match(o.meeting_name, args.meeting)
            ]
        if args.jockey:
            offers = [
                o for o in offers
                if normalize_name(args.jockey) in normalize_name(o.jockey_name)
                or jockey_names_match_safe(args.jockey, o.jockey_name)
            ]
        if not offers:
            tables.print_no_megabets(datetime.now(timezone.utc))
            return 0

        try:
            races_by_meeting = gather_meeting_races(sb, offers, scan_date)
        except (SourceUnavailableError, SchemaMismatchError) as exc:
            tables.print_source_unavailable("Sportsbet racecards", str(exc))
            return 2

    betfair_markets = None
    if args.source in ("all", "betfair"):
        betfair_markets = fetch_betfair_markets(scan_date)

    valuations: list[MegabetValuation] = []
    ride_cache: dict = {}
    for offer in offers:
        races = races_by_meeting.get(normalize_name(offer.meeting_name or ""), [])
        if not races:
            log.warning(
                "no racecards found for meeting %r (megabet: %s)",
                offer.meeting_name, offer.market_name,
            )
        valuations.extend(
            value_offer(offer, races, settings, betfair_markets, now=now,
                        ride_cache=ride_cache)
        )

    if args.min_edge is not None:
        keep_offers = {
            id(v.offer) for v in valuations
            if v.model == "consensus" and v.expected_return is not None
            and v.expected_return >= args.min_edge
        }
        valuations = [v for v in valuations if id(v.offer) in keep_offers]

    if not args.no_db:
        persist_scan(offers, races_by_meeting, valuations, now)

    tables.render_valuations(valuations, now, show_low_quality=args.show_low)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scan",
        description="Sportsbet Jockey Megabet fair-value scanner",
    )
    parser.add_argument("--meeting", help="filter by meeting/venue name")
    parser.add_argument("--jockey", help="filter by jockey name")
    parser.add_argument("--min-edge", type=float, default=None,
                        help="only show offers with consensus EV >= this (e.g. 0.05)")
    parser.add_argument("--date", help="scan date YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--source", choices=["all", "sportsbet", "betfair"],
                        default="all", help="probability sources to use")
    parser.add_argument("--show-low", action="store_true",
                        help="include LOW data-quality rows in the table")
    parser.add_argument("--no-db", action="store_true",
                        help="do not persist observations")
    parser.add_argument("--loop", action="store_true",
                        help="run continuously at SCAN_INTERVAL_SECONDS")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    settings = get_settings()

    if not args.loop:
        return run_scan(args)
    while True:
        try:
            run_scan(args)
        except Exception:
            log.exception("scan iteration failed; continuing")
        time.sleep(settings.scan_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
