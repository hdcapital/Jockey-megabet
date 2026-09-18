"""``python -m app.drz`` — the scanner.

One pass: pull today's Australian thoroughbred schedule from Sportsbet, fetch
the racecard of every open race, score every active runner, print one table
per race ordered by time to the jump, and persist what was seen.

In ``--loop`` mode the full schedule is re-swept every ``SCAN_INTERVAL_SECONDS``
(180 by default) and races inside ten minutes of their jump are refreshed
every ``NEAR_JUMP_INTERVAL_SECONDS`` (40). Both go through the same per-host
throttle as everything else, so the politeness budget is enforced in one
place and cannot be accidentally raised by a caller.

Nothing here places a bet.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone

from app.calibration import load_calibration
from app.config import get_settings
from app.engine import NoPlaceMarketError, PlaceValuation, value_race
from app.http import SourceUnavailableError
from app.logging_setup import setup_logging
from app.place_model import places_for_field
from app.reporting.tables import console, render_scan
from app.sources.base import RaceStub, SchemaMismatchError
from app.sources.betfair import (
    BetfairClient,
    BetfairNotConfiguredError,
    place_market_for,
)
from app.sources.sportsbet import SportsbetClient

log = logging.getLogger("app.drz")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.drz",
        description="Dr Z place-value scanner for Australian thoroughbreds (display only).",
    )
    p.add_argument("--date", type=date.fromisoformat, default=None,
                   help="meeting date (default: today, Australian schedule)")
    p.add_argument("--meeting", default=None,
                   help="only meetings whose name contains this (case-insensitive)")
    p.add_argument("--race", type=int, default=None, help="only this race number")
    p.add_argument("--min-drz", type=float, default=None,
                   help="override DRZ_MIN, the BET threshold")
    p.add_argument("--max-win-odds", type=float, default=None,
                   help="override the Ziemba win-price filter")
    p.add_argument("--show-all", action="store_true",
                   help="show every runner, not just WATCH and above")
    p.add_argument("--no-db", action="store_true", help="do not write to the database")
    p.add_argument("--loop", action="store_true", help="keep scanning until interrupted")
    p.add_argument("--allow-uncalibrated", action="store_true",
                   help="let rows priced by an unvalidated win model reach BET")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def _apply_overrides(settings, args) -> None:
    if args.min_drz is not None:
        settings.drz_min = args.min_drz
    if args.max_win_odds is not None:
        settings.max_win_odds = args.max_win_odds


def select_stubs(stubs: list[RaceStub], args) -> list[RaceStub]:
    out = [s for s in stubs if s.is_open]
    if args.meeting:
        needle = args.meeting.lower()
        out = [s for s in out if needle in s.meeting_name.lower()]
    if args.race is not None:
        out = [s for s in out if s.race_number == args.race]
    return sorted(
        out,
        key=lambda s: (s.start_time is None, s.start_time or datetime.max.replace(tzinfo=timezone.utc)),
    )


def _betfair_inputs(bf_markets, race, places):
    """Exchange win probabilities + place second opinion for one race.

    Returns ``(win_probs, reliable, delayed, place_probs)``; all ``None``
    when no matching exchange market exists. Runner matching is by name only
    after normalisation — an unmatched runner leaves a gap, which the win
    model treats as "unusable", rather than being paired to a near-miss.
    """
    if not bf_markets:
        return None, None, False, None

    def norm(s: str) -> str:
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    active = race.active_runners()
    win_market = next(
        (
            m for m in bf_markets
            if m.market_type == "WIN"
            and m.race_number == race.race_number
            and norm(m.venue or "") == norm(race.venue or "")
        ),
        None,
    )
    if win_market is None:
        return None, None, False, None
    by_name = {norm(q.runner_name): q for q in win_market.runners}
    quotes = [by_name.get(norm(r.horse_name)) for r in active]
    probs = [q.probability if q else None for q in quotes]
    reliable = [bool(q and q.reliable) for q in quotes]
    delayed = any(q.delayed for q in quotes if q)

    place_probs = None
    pm = place_market_for(bf_markets, race.venue, race.race_number, places)
    if pm is not None:
        pmap = {norm(q.runner_name): q for q in pm.runners}
        place_probs = {
            r.source_id: pmap[norm(r.horse_name)].probability
            for r in active
            if norm(r.horse_name) in pmap
            and pmap[norm(r.horse_name)].probability is not None
        }
    return probs, reliable, delayed, place_probs


def scan_once(args, settings, calibration, sb: SportsbetClient, bf_markets=None):
    """One full pass. Returns ``(valuations_by_race, skipped_messages)``."""
    for_date = args.date or datetime.now(timezone.utc).date()
    _meetings, stubs, _raw = sb.fetch_schedule(for_date)
    selected = select_stubs(stubs, args)
    log.info("valuing %d open races", len(selected))

    by_race: list[list[PlaceValuation]] = []
    skipped: list[str] = []
    for stub in selected:
        try:
            race = sb.fetch_racecard(stub.event_id)
        except (SourceUnavailableError, SchemaMismatchError) as exc:
            skipped.append(f"{stub.meeting_name} R{stub.race_number}: {exc}")
            log.error("racecard %s failed: %s", stub.event_id, exc)
            continue
        race.venue = race.venue or stub.meeting_name
        race.race_number = race.race_number or stub.race_number
        race.start_time = race.start_time or stub.start_time
        if race.status != "open":
            skipped.append(f"{race.venue} R{race.race_number}: {race.status}")
            continue

        places = places_for_field(len(race.active_runners()))
        bf_win, bf_reliable, bf_delayed, bf_place = _betfair_inputs(
            bf_markets, race, places
        )
        try:
            valuations = value_race(
                race,
                calibration,
                settings,
                betfair_win_probs=bf_win,
                betfair_reliable=bf_reliable,
                betfair_delayed=bf_delayed,
                betfair_place_probs=bf_place,
                allow_uncalibrated=args.allow_uncalibrated,
            )
        except NoPlaceMarketError as exc:
            skipped.append(str(exc))
            continue
        by_race.append(valuations)
        if not args.no_db:
            _persist(race, valuations)
    return by_race, skipped


def _persist(race, valuations: list[PlaceValuation]) -> None:
    from app.database.repository import Repository, session_factory
    from app.sources.base import MeetingInfo

    Session = session_factory()
    with Session() as session:
        repo = Repository(session)
        meeting = repo.upsert_meeting(
            MeetingInfo(
                source=race.source,
                source_id=race.meeting_source_id or f"{race.source_id}-meeting",
                venue=race.venue or "",
                meeting_date=race.start_time.date() if race.start_time else None,
            )
        )
        race_row = repo.upsert_race(meeting, race)
        by_source_id = {}
        for runner in race.runners:
            row = repo.upsert_runner(race_row, runner)
            by_source_id[runner.source_id] = row
            repo.record_price(
                race_row, row, runner,
                observed_at=race.fetched_at or datetime.now(timezone.utc),
                seconds_to_jump=(
                    (race.start_time - (race.fetched_at or datetime.now(timezone.utc))).total_seconds()
                    if race.start_time else None
                ),
                raw_sha256=race.raw_sha256,
            )
        for v in valuations:
            runner_row = by_source_id.get(v.runner_source_id)
            if runner_row is not None:
                repo.record_valuation(v, race_row, runner_row)
        session.commit()


def _settled_bet_count(no_db: bool) -> int:
    if no_db:
        return 0
    try:
        from app.database.repository import Repository, session_factory

        Session = session_factory()
        with Session() as session:
            return Repository(session).settled_bet_count()
    except Exception as exc:  # a reporting nicety must never break a scan
        log.debug("settled BET count unavailable: %s", exc)
        return 0


def _betfair_markets(for_date: date):
    """Exchange markets for the day, or ``None`` with the reason logged."""
    try:
        client = BetfairClient()
    except BetfairNotConfiguredError as exc:
        log.info("betfair: %s", exc)
        return None, None
    try:
        markets = client.list_au_markets(for_date)
        client.fetch_market_books(markets)
        return markets, client
    except SourceUnavailableError as exc:
        log.error("betfair unavailable, continuing on Sportsbet alone: %s", exc)
        client.close()
        return None, None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    settings = get_settings()
    _apply_overrides(settings, args)
    calibration = load_calibration()
    for_date = args.date or datetime.now(timezone.utc).date()

    bf_markets, bf_client = _betfair_markets(for_date)
    exit_code = 0
    try:
        with SportsbetClient() as sb:
            while True:
                started = time.monotonic()
                try:
                    by_race, skipped = scan_once(args, settings, calibration, sb, bf_markets)
                except (SourceUnavailableError, SchemaMismatchError) as exc:
                    console.print(f"[bold red]Scan failed:[/bold red] {exc}")
                    console.print(
                        "[dim]Nothing is displayed rather than anything invented. "
                        "The raw payload path above is the evidence.[/dim]"
                    )
                    if not args.loop:
                        return 2
                    exit_code = 2
                    by_race, skipped = [], []
                else:
                    exit_code = 0
                    render_scan(
                        by_race,
                        calibration,
                        _settled_bet_count(args.no_db),
                        settings.proven_signal_threshold,
                        datetime.now(timezone.utc),
                        show_all=args.show_all,
                        skipped=skipped,
                    )
                if not args.loop:
                    return exit_code

                # Races inside the near-jump window get the short interval.
                soonest = min(
                    (
                        v.seconds_to_jump
                        for race in by_race for v in race
                        if v.seconds_to_jump is not None and v.seconds_to_jump > 0
                    ),
                    default=None,
                )
                interval = (
                    settings.near_jump_interval_seconds
                    if soonest is not None and soonest <= settings.near_jump_window_seconds
                    else settings.scan_interval_seconds
                )
                elapsed = time.monotonic() - started
                wait = max(5.0, interval - elapsed)
                console.print(f"[dim]next sweep in {wait:.0f}s (Ctrl-C to stop)[/dim]\n")
                time.sleep(wait)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
        return 0
    finally:
        if bf_client is not None:
            bf_client.close()


if __name__ == "__main__":
    sys.exit(main())
