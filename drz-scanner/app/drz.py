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
from zoneinfo import ZoneInfo

from app import MODEL_VERSION
from app.calibration import load_calibration
from app.config import get_settings
from app.engine import NoPlaceMarketError, PlaceValuation, value_race
from app.http import SourceUnavailableError, prune_archive
from app.logging_setup import setup_logging
from app.place_model import places_for_field
from app.reporting.html_report import write_report
from app.reporting.tables import console, render_scan
from app.sources.base import RaceStub, SchemaMismatchError
from app.sources.betfair import (
    BetfairClient,
    BetfairMarket,
    BetfairNotConfiguredError,
    normalise_runner_name,
    place_market_for,
    venues_match,
)
from app.sources.sportsbet import SportsbetClient

log = logging.getLogger("app.drz")

#: Sportsbet's racing day is the Australian calendar day, not the UTC one.
#: AEST is UTC+10 and AEDT UTC+11, so for the first ten or eleven hours of
#: every Australian day the UTC date is still yesterday — a scanner keyed on
#: the UTC date would spend every Australian morning fetching the wrong day's
#: schedule entirely.
RACING_TZ = ZoneInfo("Australia/Sydney")


def racing_today() -> date:
    """Today's date in the Australian racing timezone."""
    return datetime.now(RACING_TZ).date()


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
    p.add_argument("--max-win-odds", "--max-win-price", type=float, default=None,
                   dest="max_win_odds",
                   help="override the maximum win price for EVERY model. It may "
                        "only LOWER a model's cap; raising one needs --i-know")
    p.add_argument("--i-know", action="store_true",
                   help="allow --max-win-odds to RAISE a cap above the measured "
                        "calibrated range. The band table says the place model "
                        "is only trustworthy to about $51 on exchange prices "
                        "and $9 on bookmaker prices; beyond that you are "
                        "betting on probabilities nothing has validated")
    p.add_argument("--no-exchange-confirmation", action="store_true",
                   help="drop the requirement that a Betfair place market with "
                        "matching terms confirms a BET row")
    p.add_argument("--show-all", action="store_true",
                   help="show every runner, not just WATCH and above")
    p.add_argument("--no-db", action="store_true", help="do not write to the database")
    p.add_argument("--loop", action="store_true", help="keep scanning until interrupted")
    p.add_argument("--allow-uncalibrated", action="store_true",
                   help="let rows priced by an unvalidated win model reach BET")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="terminal shows only tables and warnings; the log file "
                        "still gets everything")
    p.add_argument("--open", action="store_true",
                   help="open the live report (data/latest.html) in your browser "
                        "after the first sweep")
    p.add_argument("--no-report", action="store_true",
                   help="do not write data/latest.html")
    p.add_argument("--day", action="store_true",
                   help="the whole Australian day card in one pass: every race, "
                        "every runner, no time horizon, written to data/daycard.html, "
                        "then exit. A snapshot of morning prices — see QUICKSTART")
    return p


def _apply_overrides(settings, args) -> None:
    """Apply CLI overrides, refusing to silently widen a measured limit.

    The per-model win-price caps come from a measurement, so a single CLI
    number may tighten them but not loosen them. Raising one takes --i-know,
    and says so on the way past.
    """
    if args.min_drz is not None:
        settings.drz_min = args.min_drz
    if getattr(args, "no_exchange_confirmation", False):
        settings.require_exchange_confirmation = False
        log.warning(
            "exchange confirmation disabled: BET rows will no longer require a "
            "Betfair place market to agree with the model"
        )
    if args.max_win_odds is None:
        return
    requested = args.max_win_odds
    fields = (
        "max_win_price_betfair",
        "max_win_price_betfair_delayed",
        "max_win_price_sportsbet",
    )
    raised = [f for f in fields if requested > getattr(settings, f)]
    if raised and not getattr(args, "i_know", False):
        for f in raised:
            log.warning(
                "--max-win-odds %.1f would RAISE %s from %.1f; ignored for that "
                "model (pass --i-know to allow it)",
                requested, f, getattr(settings, f),
            )
    for f in fields:
        current = getattr(settings, f)
        if requested <= current or getattr(args, "i_know", False):
            setattr(settings, f, requested)
    if raised and getattr(args, "i_know", False):
        log.warning(
            "--i-know: win-price cap raised to %.1f, beyond the range the band "
            "table validates. Rows above the measured range rest on "
            "probabilities nothing has checked.", requested,
        )


#: Event ids found (from their racecard) to be outside the allowed countries
#: today. The schedule stub cannot tell NZ from AU — both sit in "Aus/NZ" —
#: so without this the near-jump loop would refetch an Ellerslie racecard
#: every 40 seconds just to skip it again.
EXCLUDED_EVENT_IDS: set[str] = set()


def _matches_filters(stub: RaceStub, args) -> bool:
    if stub.event_id in EXCLUDED_EVENT_IDS:
        return False
    if args.meeting and args.meeting.lower() not in stub.meeting_name.lower():
        return False
    if args.race is not None and stub.race_number != args.race:
        return False
    return True


def select_stubs(stubs: list[RaceStub], args) -> list[RaceStub]:
    """Open races to value, ordered by time to the jump."""
    out = [s for s in stubs if s.is_open and _matches_filters(s, args)]
    return sorted(
        out,
        key=lambda s: (s.start_time is None, s.start_time or datetime.max.replace(tzinfo=timezone.utc)),
    )


def select_resulted_stubs(stubs: list[RaceStub], args) -> list[RaceStub]:
    """Races that have resolved and carry a result, for settlement capture.

    These are deliberately *not* the races we value — they are the races we
    valued earlier today and now need an outcome for. Without this pass
    nothing ever writes a result, so the backtester can never settle a single
    signal and the UNPROVEN banner can never come down.
    """
    return [
        s for s in stubs
        if not s.is_open and s.result and _matches_filters(s, args)
    ]


class BetfairSession:
    """The exchange connection for one day: catalogue once, books every sweep.

    The catalogue (which markets exist) changes rarely; the books (prices)
    change constantly. Fetching the books once and reusing them for a whole
    afternoon — which is what a naive "fetch at startup" does — scores every
    later race on prices that are hours old while labelling them "betfair".
    """

    def __init__(self, for_date: date, settings):
        self.settings = settings
        self.for_date = for_date
        self.client: BetfairClient | None = None
        self.markets: list[BetfairMarket] = []
        self.books_fetched_at: datetime | None = None
        self.reason_unavailable: str | None = None
        try:
            self.client = BetfairClient()
        except BetfairNotConfiguredError as exc:
            self.reason_unavailable = str(exc)

    @property
    def configured(self) -> bool:
        return self.client is not None

    def refresh(self) -> None:
        """Fetch the catalogue if we have none, then fresh books."""
        if self.client is None:
            return
        try:
            if not self.markets:
                self.markets = self.client.list_au_markets(self.for_date)
            for market in self.markets:
                market.runners = []
            self.client.fetch_market_books(self.markets)
            self.books_fetched_at = datetime.now(timezone.utc)
            self.reason_unavailable = None
        except SourceUnavailableError as exc:
            # Keep the previous books; the staleness gate below will refuse
            # them once they are too old rather than silently reusing them.
            self.reason_unavailable = str(exc)
            log.error("betfair refresh failed, continuing on Sportsbet alone: %s", exc)

    def stale(self, now: datetime) -> bool:
        if self.books_fetched_at is None:
            return True
        age = (now - self.books_fetched_at).total_seconds()
        return age > self.settings.max_price_age_seconds * 3

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


def _match_quote(quotes, runner):
    """The exchange quote for a Sportsbet runner: cloth number first, then name.

    The saddlecloth is the join key both sides actually agree on. Names are
    the fallback for a market whose runner names carry no prefix, and a
    check when they do: a cloth-number hit with a different name is refused,
    because that is what a late runner replacement looks like.
    """
    target_name = normalise_runner_name(runner.horse_name)
    if runner.saddlecloth is not None:
        for q in quotes:
            if q.cloth_number == runner.saddlecloth:
                if normalise_runner_name(q.runner_name) == target_name:
                    return q
                return None
    for q in quotes:
        if q.cloth_number is None and normalise_runner_name(q.runner_name) == target_name:
            return q
    return None


def _betfair_inputs(bf: BetfairSession | None, race, places, now: datetime):
    """Exchange win probabilities + place confirmation for one race.

    Returns ``(win_probs, reliable, delayed, place_probs)``; all ``None``
    when there is no usable exchange market. A runner that cannot be matched
    leaves a gap, which the win model treats as "unusable" rather than
    pairing it to a near miss. Books older than three price-age limits are
    refused outright.
    """
    if bf is None or not bf.markets:
        return None, None, False, None
    if bf.stale(now):
        log.warning("betfair books are stale (fetched %s); not used this sweep",
                    bf.books_fetched_at)
        return None, None, False, None

    active = race.active_runners()
    win_market = next(
        (
            m for m in bf.markets
            if m.market_type == "WIN"
            and m.race_number == race.race_number
            and venues_match(m.venue, race.venue)
        ),
        None,
    )
    if win_market is None:
        return None, None, False, None
    quotes = [_match_quote(win_market.runners, r) for r in active]
    probs = [q.probability if q else None for q in quotes]
    reliable = [bool(q and q.reliable) for q in quotes]
    delayed = any(q.delayed for q in quotes if q)

    place_probs = None
    pm = place_market_for(bf.markets, race.venue, race.race_number, places)
    if pm is not None:
        place_probs = {}
        for r in active:
            q = _match_quote(pm.runners, r)
            # Only a quote that passed the spread/liquidity gate may confirm
            # a bet. A thin or wide place quote is an opinion, not evidence.
            if q is not None and q.probability is not None and q.reliable:
                place_probs[r.source_id] = q.probability
    return probs, reliable, delayed, place_probs


def capture_results(stubs: list[RaceStub], sb: SportsbetClient, no_db: bool) -> int:
    """Write outcomes for races we valued earlier and that have now resolved.

    The schedule stub already carries the placings string, so the common case
    costs no extra request. A racecard is fetched only for a race we hold
    valuations for and whose per-runner finishing positions we still lack —
    those positions are the only unambiguous way to settle a dead heat.
    """
    if no_db or not stubs:
        return 0
    from app.database.repository import Repository, session_factory

    Session = session_factory()
    captured = 0
    with Session() as session:
        repo = Repository(session)
        for stub in stubs:
            race_row = repo.race_by_source_id("sportsbet", stub.event_id)
            if race_row is None:
                continue  # never valued, so nothing to settle
            if race_row.result_placings and repo.finish_positions(race_row.race_id):
                continue  # already captured, positions and all
            if not race_row.result_placings:
                race_row.result_placings = stub.result
                race_row.status = "resulted"
                race_row.resulted_at = datetime.now(timezone.utc).replace(tzinfo=None)
                captured += 1
            if not repo.finish_positions(race_row.race_id):
                try:
                    race = sb.fetch_racecard(stub.event_id)
                except (SourceUnavailableError, SchemaMismatchError) as exc:
                    log.warning(
                        "result capture: racecard %s unavailable (%s); settling "
                        "from the placings string alone", stub.event_id, exc,
                    )
                else:
                    for runner in race.runners:
                        repo.upsert_runner(race_row, runner)
                    if race.result_placings:
                        race_row.result_placings = ",".join(
                            str(p) for p in race.result_placings
                        )
        session.commit()
    if captured:
        log.info("captured results for %d race(s)", captured)
    return captured


def scan_once(
    args, settings, calibration, sb: SportsbetClient, bf=None,
    stubs: list[RaceStub] | None = None, only_event_ids: set[str] | None = None,
):
    """One pass. Returns ``(valuations_by_race, skipped_messages, stubs)``.

    A *full* sweep fetches the schedule and every open racecard. A *quick*
    sweep (``stubs`` supplied, ``only_event_ids`` set) reuses the last
    schedule and refetches only the races inside the near-jump window —
    that is how the near-jump cadence stays inside the politeness budget
    instead of multiplying the whole day's request count by four.
    """
    for_date = args.date or racing_today()
    if stubs is None:
        _meetings, stubs, _raw = sb.fetch_schedule(for_date)
        capture_results(select_resulted_stubs(stubs, args), sb, args.no_db)
    if bf is not None:
        bf.refresh()
    selected = select_stubs(stubs, args)
    now = datetime.now(timezone.utc)
    if only_event_ids is not None:
        selected = [s for s in selected if s.event_id in only_event_ids]
    elif getattr(args, "day", False):
        pass  # the whole card, however far out
    else:
        horizon = settings.racecard_horizon_minutes * 60
        beyond = [s for s in selected
                  if s.start_time is not None
                  and (s.start_time - now).total_seconds() > horizon]
        if beyond:
            log.info("%d race(s) beyond the %d-minute horizon left for a later sweep",
                     len(beyond), settings.racecard_horizon_minutes)
        selected = [s for s in selected if s not in beyond]
    log.info("valuing %d open races%s", len(selected),
             " (near-jump refresh)" if only_event_ids is not None else "")
    by_race: list[list[PlaceValuation]] = []
    skipped: list[str] = []
    for stub in selected:
        try:
            race = sb.fetch_racecard(stub.event_id)
        except (SourceUnavailableError, SchemaMismatchError) as exc:
            skipped.append(f"{stub.meeting_name} R{stub.race_number}: {exc}")
            log.warning("racecard %s (%s R%s) skipped: %s",
                        stub.event_id, stub.meeting_name, stub.race_number, exc)
            continue
        race.venue = race.venue or stub.meeting_name
        race.race_number = race.race_number or stub.race_number
        race.start_time = race.start_time or stub.start_time
        if race.status != "open":
            skipped.append(f"{race.venue} R{race.race_number}: {race.status}")
            continue
        if race.country and race.country not in settings.allowed_countries:
            # "Aus/NZ" is one Sportsbet class; the racecard is where NZ
            # (and anything else) gets told apart. The calibration is AU-only.
            EXCLUDED_EVENT_IDS.add(stub.event_id)
            skipped.append(f"{race.venue} R{race.race_number}: country {race.country}, not valued")
            continue

        places = places_for_field(len(race.active_runners()))
        bf_win, bf_reliable, bf_delayed, bf_place = _betfair_inputs(
            bf, race, places, now
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
    return by_race, skipped, stubs


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
                # The Australian calendar date, not the UTC one: this is the
                # key the Betfair history joins on (LOCAL_MEETING_DATE), and
                # an evening meeting is on the next UTC day.
                meeting_date=(
                    race.start_time.astimezone(RACING_TZ).date()
                    if race.start_time else None
                ),
            )
        )
        race_row = repo.upsert_race(meeting, race)
        by_source_id = {}
        for runner in race.runners:
            row = repo.upsert_runner(race_row, runner)
            by_source_id[runner.source_id] = row
            observed = race.fetched_at or datetime.now(timezone.utc)
            secs = (
                (race.start_time - observed).total_seconds() if race.start_time else None
            )
            repo.record_price(race_row, row, runner, observed_at=observed,
                              seconds_to_jump=secs, raw_sha256=race.raw_sha256)
            # Any indicative (tote-derivative) quote is kept too, as its own
            # row with its own price code, so a later backtest of indicative
            # against final dividends has something to work with.
            for code, quote in runner.prices_by_code.items():
                if quote.price_type == "tote_indicative":
                    repo.record_price(
                        race_row, row, runner, observed_at=observed,
                        seconds_to_jump=secs, raw_sha256=race.raw_sha256,
                        price_code=code, price_type=quote.price_type,
                        win_price=quote.win_price, place_price=quote.place_price,
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


def _startup_notices(settings, bf: BetfairSession, log_path) -> None:
    """Say up front what this run can and cannot do."""
    console.print(f"[dim]drz-scanner model {MODEL_VERSION} · log {log_path or 'stderr only'}[/dim]")
    if not bf.configured:
        if settings.require_exchange_confirmation:
            console.print(
                "[bold yellow]No Betfair credentials.[/bold yellow] A BET needs a "
                "Betfair place market to agree with the model, so [bold]no row can "
                "reach BET on this run[/bold] — everything tops out at WATCH. "
                "Add BETFAIR_APP_KEY / BETFAIR_USERNAME / BETFAIR_PASSWORD to .env, "
                "or pass --no-exchange-confirmation to drop the requirement."
            )
        else:
            console.print(
                "[yellow]No Betfair credentials; exchange confirmation disabled by "
                "flag. BET rows will rest on the model alone.[/yellow]"
            )
    elif not settings.require_exchange_confirmation:
        console.print("[yellow]Exchange confirmation disabled by flag.[/yellow]")


def _local_hhmm(dt: datetime) -> str:
    return dt.astimezone(RACING_TZ).strftime("%H:%M")


def _day_caveat(retrieved: datetime) -> str:
    return (
        f"Morning card: every price here was taken at {_local_hhmm(retrieved)}. A fixed-odds "
        f"place bet locks the price you take, so a genuine edge at this price is real — but "
        f"(1) morning place prices are often shorter or longer than jump prices, (2) a "
        f"scratching after now changes the number of places paid in fields of exactly 8, "
        f"and (3) Betfair's morning markets are thin, so most rows will lack exchange "
        f"confirmation and top out at WATCH. This is a snapshot, not a live view."
    )


def _near_jump_ids(stubs: list[RaceStub], settings, now: datetime) -> set[str]:
    """Open races inside the near-jump window, by event id."""
    out = set()
    for s in stubs:
        if not s.is_open or s.start_time is None or s.event_id in EXCLUDED_EVENT_IDS:
            continue
        secs = (s.start_time - now).total_seconds()
        if -60 <= secs <= settings.near_jump_window_seconds:
            out.add(s.event_id)
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.day and args.loop:
        args.loop = False   # a day card is one pass by definition
    settings = get_settings()
    level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    log_path = setup_logging(level, log_dir=settings.log_dir)
    _apply_overrides(settings, args)
    calibration = load_calibration()
    for_date = args.date or racing_today()
    prune_archive(settings.raw_archive_dir, settings.raw_archive_keep_days)

    bf = BetfairSession(for_date, settings)
    _startup_notices(settings, bf, log_path)
    log.info(
        "drz-scanner model %s starting: date=%s loop=%s db=%s betfair=%s "
        "exchange_confirmation=%s caps(betfair/delayed/sportsbet)=%.0f/%.0f/%.0f "
        "drz_min=%.2f calibration=%s",
        MODEL_VERSION, for_date, args.loop, not args.no_db,
        "configured" if bf.configured else "absent",
        settings.require_exchange_confirmation,
        settings.max_win_price_betfair, settings.max_win_price_betfair_delayed,
        settings.max_win_price_sportsbet, settings.drz_min, calibration.version,
    )
    exit_code = 0
    stubs: list[RaceStub] | None = None
    last_full = 0.0
    opened = False
    try:
        with SportsbetClient() as sb:
            while True:
                started = time.monotonic()
                now = datetime.now(timezone.utc)
                quick_ids = (
                    _near_jump_ids(stubs, settings, now)
                    if stubs is not None and args.loop else set()
                )
                full_due = (started - last_full) >= settings.scan_interval_seconds
                do_quick = args.loop and quick_ids and not full_due
                try:
                    if do_quick:
                        by_race, skipped, _ = scan_once(
                            args, settings, calibration, sb, bf,
                            stubs=stubs, only_event_ids=quick_ids,
                        )
                    else:
                        by_race, skipped, stubs = scan_once(args, settings, calibration, sb, bf)
                        last_full = time.monotonic()
                except (SourceUnavailableError, SchemaMismatchError) as exc:
                    log.error("scan failed: %s", exc)
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
                    settled = _settled_bet_count(args.no_db)
                    retrieved = datetime.now(timezone.utc)
                    if do_quick and not by_race:
                        # A quick sweep with nothing to show is one line, not
                        # the banner and a table of nothing.
                        console.print(f"[dim]{_local_hhmm(retrieved)} near-jump refresh: "
                                      f"nothing to value[/dim]")
                    else:
                        render_scan(
                            by_race, calibration, settled, settings.proven_signal_threshold,
                            retrieved, show_all=args.show_all or args.day,
                            skipped=skipped, banner=not do_quick,
                        )
                    if not args.no_report:
                        try:
                            report = write_report(
                                settings.daycard_path if args.day else settings.report_path,
                                by_race=by_race,
                                calibration=calibration, settled_bets=settled,
                                proven_threshold=settings.proven_signal_threshold,
                                retrieved_at=retrieved, skipped=skipped,
                                refresh_seconds=0 if args.day else settings.report_refresh_seconds,
                                heading=("drz-scanner — the whole Australian day card"
                                         if args.day else "drz-scanner — today's Australian races"),
                                caveat=_day_caveat(retrieved) if args.day else None,
                            )
                            console.print(f"[dim]report: {report}[/dim]")
                            if args.open and not opened:
                                opened = True
                                import webbrowser

                                webbrowser.open(report.resolve().as_uri())
                        except OSError as exc:  # a report must never stop a scan
                            log.warning("report not written: %s", exc)
                if not args.loop:
                    if args.day:
                        console.print(
                            f"\n[bold]Day card written.[/bold] Prices are as of "
                            f"{_local_hhmm(datetime.now(timezone.utc))}; a fixed-odds bet locks "
                            f"the price you take, but the field (and so the place terms) can "
                            f"still change before the jump."
                        )
                    return exit_code

                # Cadence: while any race is inside the near-jump window,
                # quick sweeps of just those races every
                # NEAR_JUMP_INTERVAL_SECONDS; otherwise wait for the next full
                # sweep, which is due SCAN_INTERVAL_SECONDS after the last one
                # started, so a slow sweep does not push the schedule out.
                now = datetime.now(timezone.utc)
                near = bool(stubs) and bool(_near_jump_ids(stubs, settings, now))
                next_full_in = settings.scan_interval_seconds - (time.monotonic() - last_full)
                if near:
                    interval = min(settings.near_jump_interval_seconds, max(next_full_in, 0.0))
                    label = "near-jump refresh"
                else:
                    interval = next_full_in
                    label = "full sweep"
                elapsed = time.monotonic() - started
                wait = max(5.0, interval - elapsed)
                console.print(f"[dim]next {label} in {wait:.0f}s (Ctrl-C to stop)[/dim]\n")
                time.sleep(wait)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
        return 0
    finally:
        bf.close()


if __name__ == "__main__":
    sys.exit(main())
