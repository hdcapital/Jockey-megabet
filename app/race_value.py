"""Race value scanner: Sportsbet fixed win odds against Betfair as the truth.

Run:  python -m app.race_value            # loop every VALUE_INTERVAL_SECONDS
      python -m app.race_value --once     # one pass
      python -m app.race_value --all      # include rows that fail the gates

Every minute it finds the thoroughbred races jumping in the next
``VALUE_WINDOW_MINUTES`` (20) on Sportsbet that also have a Betfair WIN
market, and for every runner compares Sportsbet's fixed win price with the
probability the exchange gives it. Rows are sorted by expected value.

Making the two sources comparable
---------------------------------
* **Betfair is converted to a probability, not used as a price.** For each
  runner the probability is the midpoint of best back and best lay taken in
  probability space, ``(1/back + 1/lay) / 2`` (the odds midpoint overstates
  the longer side of a wide spread). The exchange book of these midpoints
  is then normalised to exactly 100%, so what is left is the market's view
  of the race and none of the spread.
* **The field is the same field.** A runner Sportsbet has scratched but the
  exchange still lists is taken out of the Betfair book before normalising
  (this is what Betfair's reduction factor does to matched bets once it
  removes the runner). A runner the exchange has removed but Sportsbet still
  prices has no truth to compare with and is reported, not valued.
* **Sportsbet is used as the price you would actually be paid.** Its odds
  are *not* de-vigged: the bookmaker's margin is part of the bet. Its
  overround is shown so a padded race is visible.
* **Commission is not part of the fair probability.** It only matters for
  the "lock" column: backing at Sportsbet and laying the same runner on
  Betfair at the best lay, after ``BETFAIR_COMMISSION`` on the lay winnings.
* **In-play and suspended markets are never compared**, and the two
  snapshots' time gap is checked, because prices in the last minutes move
  faster than a minute-long loop.

Columns: ``EV = p_fair * sportsbet_odds - 1`` (per $1 staked if Betfair is
right); ``EV@lay = sportsbet_odds / best_lay - 1`` (the same with the least
favourable exchange price, a conservative floor); ``lock`` is the profit per
$1 backed of an immediate back/lay hedge; ``kelly`` is the full-Kelly stake
fraction at the fair probability (display only; nothing here bets).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, get_settings
from app.http import ArchivingClient, SourceUnavailableError
from app.logging_setup import setup_logging
from app.matching.meetings import find_betfair_market
from app.matching.runners import match_race_runners
from app.sources.base import RaceInfo, SchemaMismatchError
from app.sources.betfair import (
    BetfairClient,
    BetfairLoginError,
    BetfairMarket,
    BetfairNotConfiguredError,
    BetfairRunnerQuote,
)
from app.sources.sportsbet import SportsbetClient, _first, _to_dt

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    SYDNEY = ZoneInfo("Australia/Sydney")
except Exception:  # no tz database (Windows without tzdata): AEST is close enough
    SYDNEY = timezone(timedelta(hours=10))

#: Flags that make a row unreliable (hidden unless --all).
BLOCKING_FLAGS = ("SPREAD", "THIN", "BOOK", "ONE-SIDED", "DELAYED", "SUSPECT")


# ---------------------------------------------------------------------------
# Race selection
# ---------------------------------------------------------------------------

@dataclass
class UpcomingRace:
    venue: str
    event_id: str
    race_number: int | None
    start_time: datetime


def listing_dates(now: datetime) -> list[date]:
    """Sportsbet AllRacing dates to fetch: today in Sydney, and yesterday too
    before 6am (a late WA or NZ card is listed under the previous day)."""
    local = now.astimezone(SYDNEY)
    dates = [local.date()]
    if local.hour < 6:
        dates.insert(0, local.date() - timedelta(days=1))
    return dates


def upcoming_races(
    meetings: list[dict[str, Any]],
    now: datetime,
    window: timedelta,
    grace: timedelta,
) -> list[UpcomingRace]:
    """Races in Sportsbet meeting nodes starting within [now-grace, now+window]
    that have not resulted or been abandoned, soonest first."""
    out: list[UpcomingRace] = []
    seen: set[str] = set()
    for meeting in meetings:
        venue = _first(meeting, "name", "venueName", "meetingName")
        if not isinstance(venue, str):
            continue
        for ev in _first(meeting, "events", "races") or []:
            if not isinstance(ev, dict):
                continue
            event_id = _first(ev, "id", "eventId")
            start = _to_dt(_first(ev, "startTime", "advertisedStartTime", "displayStartTime"))
            if event_id is None or start is None or str(event_id) in seen:
                continue
            code = str(ev.get("statusCode") or "").strip().upper()
            betting = str(ev.get("bettingStatus") or "").lower()
            if code == "R" or any(w in betting for w in ("result", "abandon", "closed")):
                continue
            if not (now - grace <= start <= now + window):
                continue
            seen.add(str(event_id))
            number = _first(ev, "raceNumber", "eventNumber", "number")
            out.append(UpcomingRace(
                venue=venue.strip(),
                event_id=str(event_id),
                race_number=int(number) if isinstance(number, (int, float)) else None,
                start_time=start,
            ))
    out.sort(key=lambda r: r.start_time)
    return out


# ---------------------------------------------------------------------------
# Betfair truth and the comparison maths
# ---------------------------------------------------------------------------

def midpoint_probability(back: float | None, lay: float | None) -> float | None:
    """Exchange probability of one runner: midpoint in probability space.

    With one side missing the price that is there is used (and the row is
    flagged ONE-SIDED by the caller); with neither, None.
    """
    pb = 1.0 / back if back and back > 1.0 else None
    pl = 1.0 / lay if lay and lay > 1.0 else None
    if pb is not None and pl is not None:
        return (pb + pl) / 2.0
    return pb if pb is not None else pl


def exchange_fair_probabilities(
    quotes: list[BetfairRunnerQuote], exclude: set[int] | None = None
) -> tuple[dict[int, float], float]:
    """{selection id: normalised probability} and the raw midpoint book.

    ``exclude`` holds selection ids taken out of the field (scratched on
    Sportsbet, still listed on the exchange) before normalising.
    """
    exclude = exclude or set()
    raw: dict[int, float] = {}
    for q in quotes:
        if q.selection_id in exclude:
            continue
        p = midpoint_probability(q.best_back, q.best_lay)
        if p is not None:
            raw[q.selection_id] = p
    book = sum(raw.values())
    if book <= 0:
        return {}, 0.0
    return {sid: p / book for sid, p in raw.items()}, book


def lay_lock_profit(back_odds: float, lay_odds: float, commission: float) -> float:
    """Profit per $1 backed at ``back_odds`` when laid at ``lay_odds``.

    Lay stake ``back_odds / (lay_odds - commission)`` equalises both
    outcomes; the result is ``back_odds*(1-c)/(lay_odds-c) - 1``.
    """
    return back_odds * (1.0 - commission) / (lay_odds - commission) - 1.0


@dataclass
class ValueRow:
    venue: str
    race_number: int | None
    start_time: datetime | None
    horse: str
    saddlecloth: int | None
    sb_odds: float
    bf_back: float | None
    bf_lay: float | None
    bf_lay_size: float | None
    p_fair: float
    ev: float
    ev_at_lay: float | None
    lock: float | None
    kelly: float
    sb_overround: float
    bf_book: float
    bf_matched: float | None
    snapshot_gap: float | None
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # race-level, non-blocking

    @property
    def fair_odds(self) -> float:
        return 1.0 / self.p_fair

    @property
    def reliable(self) -> bool:
        return not any(f.split()[0] in BLOCKING_FLAGS for f in self.flags)

    @property
    def spread(self) -> float | None:
        if self.bf_back and self.bf_lay:
            return (self.bf_lay - self.bf_back) / self.bf_back
        return None


def compare_race(
    venue: str,
    race: RaceInfo,
    market: BetfairMarket,
    settings: Any,
    sb_fetched_at: datetime | None = None,
) -> tuple[list[ValueRow], list[str]]:
    """Value every Sportsbet runner in one race against the exchange.

    Returns the rows and race-level notes (why a race or runner was skipped).
    """
    label = f"{venue} R{race.race_number}"
    notes: list[str] = []
    if race.status != "open":
        return [], [f"{label}: Sportsbet race is {race.status}"]
    if (race.win_market_status or "").upper() == "S":
        return [], [f"{label}: Sportsbet win market suspended"]
    if market.inplay:
        return [], [f"{label}: Betfair market is in-play"]
    if market.book_status != "OPEN":
        return [], [f"{label}: Betfair market is {market.book_status or 'without a book'}"]

    matches = match_race_runners(race.runners, market.runners, label, market.removed_names)
    exclude: set[int] = set()
    for m in matches:
        if m.sportsbet_runner.status == "scratched" and m.betfair_quote is not None:
            exclude.add(m.betfair_quote.selection_id)
            notes.append(
                f"{label}: {m.sportsbet_runner.horse_name} scratched on Sportsbet, "
                "still on Betfair — removed from the exchange book"
            )
    stale = [m.sportsbet_runner.horse_name for m in matches
             if m.status == "removed_on_betfair" and m.sportsbet_runner.status == "active"]
    if stale:
        notes.append(f"{label}: Sportsbet still prices {', '.join(stale)} (scratched on Betfair)")

    fair, book = exchange_fair_probabilities(market.runners, exclude)
    priced = [r for r in race.active_runners() if r.win_odds and r.win_odds > 1.0]
    overround = sum(1.0 / r.win_odds for r in priced)
    gap = None
    if sb_fetched_at and market.fetched_at:
        gap = abs((market.fetched_at - sb_fetched_at).total_seconds())

    # Market-level flags apply to every row of the race.
    race_flags: list[str] = []
    if market.delayed:
        race_flags.append("DELAYED key")
    elif (market.total_matched or 0.0) < settings.value_min_matched:
        race_flags.append(f"THIN ${market.total_matched or 0:,.0f}")
    if book and abs(book - 1.0) > settings.value_max_book_deviation:
        race_flags.append(f"BOOK {book:.0%}")
    if gap is not None and gap > settings.value_max_snapshot_gap_seconds:
        race_flags.append(f"GAP {gap:.0f}s")
    if exclude:
        race_flags.append("RENORM")

    rows: list[ValueRow] = []
    for m in matches:
        sr, q = m.sportsbet_runner, m.betfair_quote
        if sr.status != "active" or not sr.win_odds or sr.win_odds <= 1.0:
            continue
        if q is None or q.selection_id not in fair:
            continue
        p = fair[q.selection_id]
        odds = sr.win_odds
        ev = p * odds - 1.0
        flags = list(race_flags)
        if q.best_back and q.best_lay:
            spread = (q.best_lay - q.best_back) / q.best_back
            if spread > settings.value_max_relative_spread + 1e-9:
                flags.append(f"SPREAD {spread:.0%}")
        else:
            flags.append("ONE-SIDED")
        if ev > settings.value_suspect_ev:
            flags.append("SUSPECT")
        rows.append(ValueRow(
            venue=venue,
            race_number=race.race_number,
            start_time=race.start_time or market.market_start,
            horse=sr.horse_name,
            saddlecloth=sr.saddlecloth,
            sb_odds=odds,
            bf_back=q.best_back,
            bf_lay=q.best_lay,
            bf_lay_size=q.lay_volume,
            p_fair=p,
            ev=ev,
            ev_at_lay=(odds / q.best_lay - 1.0) if q.best_lay else None,
            lock=(lay_lock_profit(odds, q.best_lay, settings.betfair_commission)
                  if q.best_lay else None),
            kelly=max(0.0, ev / (odds - 1.0)),
            sb_overround=overround,
            bf_book=book,
            bf_matched=market.total_matched,
            snapshot_gap=gap,
            flags=flags,
        ))
    return rows, notes


# ---------------------------------------------------------------------------
# One scan and the loop
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    at: datetime
    rows: list[ValueRow] = field(default_factory=list)
    races_in_window: int = 0
    races_compared: int = 0
    no_betfair_market: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    betfair_summary: str = ""
    error: str | None = None


class ValueScanner:
    """Holds both clients across iterations so Betfair logs in once, not
    once a minute (repeated logins earn TEMPORARY_BAN_TOO_MANY_REQUESTS)."""

    LOGIN_BACKOFF = timedelta(minutes=10)

    def __init__(self, settings: Any = None, archive: bool = False,
                 sportsbet: SportsbetClient | None = None,
                 betfair: BetfairClient | None = None):
        self.settings = settings or get_settings()
        self.sb = sportsbet or SportsbetClient(
            ArchivingClient("sportsbet", archive=archive, settings=self.settings)
        )
        self.bf = betfair or BetfairClient(
            client=ArchivingClient("betfair", archive=archive, settings=self.settings),
            settings=self.settings,
        )
        self._login_retry_at: datetime | None = None

    def close(self) -> None:
        self.sb.close()
        self.bf.close()

    def scan_once(self, now: datetime | None = None) -> ScanResult:
        s = self.settings
        now = now or datetime.now(timezone.utc)
        res = ScanResult(at=now)
        window = timedelta(minutes=s.value_window_minutes)
        grace = timedelta(minutes=s.value_grace_minutes)

        if self._login_retry_at and now < self._login_retry_at:
            res.error = (f"Betfair login refused earlier; next attempt at "
                         f"{self._login_retry_at.astimezone(SYDNEY):%H:%M}")
            return res

        # 1. Betfair catalogue for the window (one light call). Start times
        #    on the two sides can differ by minutes, so it is padded.
        countries = tuple(c.strip().upper() for c in s.value_betfair_countries.split(",") if c.strip())
        try:
            catalogue = self.bf.list_win_markets(
                now - grace - timedelta(minutes=30), now + window + timedelta(minutes=30),
                countries=countries,
            )
        except BetfairLoginError as exc:
            self._login_retry_at = now + self.LOGIN_BACKOFF
            res.error = f"Betfair login refused: {exc.code} — {exc.hint}"
            return res
        except SourceUnavailableError as exc:
            res.error = f"Betfair unavailable: {exc}"
            return res

        # 2. Sportsbet races in the window.
        meetings: list[dict[str, Any]] = []
        for d in listing_dates(now):
            try:
                found, _ = self.sb.fetch_meetings(d)
                meetings.extend(found)
            except SchemaMismatchError as exc:
                log.warning("sportsbet listing %s: %s", d, exc)
            except SourceUnavailableError as exc:
                res.error = f"Sportsbet unavailable: {exc}"
                return res
        upcoming = upcoming_races(meetings, now, window, grace)
        res.races_in_window = len(upcoming)

        # 3. Pair each with its exchange market before fetching anything else,
        #    so racecards are only fetched for races Betfair can price.
        pairs: list[tuple[UpcomingRace, BetfairMarket]] = []
        for up in upcoming:
            stub = RaceInfo(source="sportsbet", source_id=up.event_id,
                            race_number=up.race_number, start_time=up.start_time,
                            status="open")
            market = find_betfair_market(up.venue, stub, catalogue)
            if market is None:
                res.no_betfair_market.append(f"{up.venue} R{up.race_number}")
            else:
                pairs.append((up, market))

        # 4. Sportsbet racecards, then the Betfair books last so the two
        #    snapshots are as close together as the request throttle allows.
        cards: list[tuple[UpcomingRace, BetfairMarket, RaceInfo, datetime | None]] = []
        for up, market in pairs:
            try:
                card = self.sb.fetch_racecard(up.event_id)
            except (SourceUnavailableError, SchemaMismatchError) as exc:
                res.notes.append(f"{up.venue} R{up.race_number}: racecard failed: {exc}")
                continue
            cards.append((up, market, card.races[0], card.fetched_at))
        if cards:
            try:
                self.bf.fetch_market_books([m for _, m, _, _ in cards])
            except SourceUnavailableError as exc:
                res.error = f"Betfair books unavailable: {exc}"
                return res
        res.betfair_summary = self.bf.status.summary()

        for up, market, race, fetched in cards:
            if race.start_time is None:
                race.start_time = up.start_time
            rows, notes = compare_race(up.venue, race, market, s, fetched)
            res.notes.extend(notes)
            if rows:
                res.races_compared += 1
            res.rows.extend(rows)
        res.rows.sort(key=lambda r: r.ev, reverse=True)
        return res


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "scanned_at", "venue", "race", "start_time", "saddlecloth", "horse",
    "sb_odds", "bf_back", "bf_lay", "bf_lay_size", "p_fair", "fair_odds", "ev",
    "ev_at_lay", "lock", "kelly", "sb_overround", "bf_book", "bf_matched",
    "snapshot_gap_s", "reliable", "flags",
]


def append_csv(result: ScanResult, directory: Path) -> Path | None:
    """Append every compared runner (not only the value ones) to a daily CSV,
    so the scanner's calls can later be checked against results and the
    closing price."""
    if not result.rows:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.at.astimezone(SYDNEY):%Y-%m-%d}.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(CSV_FIELDS)
        for r in result.rows:
            w.writerow([
                result.at.isoformat(timespec="seconds"), r.venue, r.race_number,
                r.start_time.isoformat() if r.start_time else "", r.saddlecloth, r.horse,
                r.sb_odds, r.bf_back, r.bf_lay, r.bf_lay_size, round(r.p_fair, 5),
                round(r.fair_odds, 3), round(r.ev, 5),
                round(r.ev_at_lay, 5) if r.ev_at_lay is not None else "",
                round(r.lock, 5) if r.lock is not None else "",
                round(r.kelly, 5), round(r.sb_overround, 4), round(r.bf_book, 4),
                r.bf_matched, round(r.snapshot_gap, 1) if r.snapshot_gap is not None else "",
                r.reliable, "; ".join(r.flags),
            ])
    return path


def render(result: ScanResult, show_all: bool = False, top: int = 30,
           min_ev: float | None = None) -> None:
    from rich import box
    from rich.console import Console
    from rich.table import Table

    console = Console()
    local = result.at.astimezone(SYDNEY)
    console.rule(f"Sportsbet vs Betfair — {local:%a %d %b %H:%M:%S} Sydney")
    if result.error:
        console.print(f"[red]{result.error}[/red]\n[red]Nothing shown: no comparison "
                      "can be made without both sources.[/red]")
        return
    if result.betfair_summary:
        console.print(f"[dim]{result.betfair_summary}[/dim]")
    console.print(
        f"{result.races_in_window} races in the window; {result.races_compared} compared"
        + (f"; no Betfair market: {', '.join(result.no_betfair_market)}"
           if result.no_betfair_market else "")
    )
    for n in result.notes:
        console.print(f"[yellow]· {n}[/yellow]")

    rows = result.rows if show_all else [r for r in result.rows if r.reliable]
    if min_ev is not None:
        rows = [r for r in rows if r.ev >= min_ev]
    hidden = len(result.rows) - len([r for r in result.rows if r.reliable])
    if not rows:
        console.print("No runners to show" + (f" ({hidden} hidden by the reliability "
                      "gates; --all shows them)" if hidden and not show_all else "") + ".")
        return

    t = Table(box=box.SIMPLE_HEAD, padding=(0, 1), header_style="bold")
    for col, just in [("Jump", "right"), ("Race", "left"), ("Runner", "left"),
                      ("SB", "right"), ("BF back/lay", "right"), ("Fair", "right"),
                      ("EV", "right"), ("EV@lay", "right"), ("Lock", "right"),
                      ("Kelly", "right"), ("SB ovr", "right"), ("BF $", "right"),
                      ("Flags", "left")]:
        t.add_column(col, justify=just, no_wrap=col != "Flags")
    for r in rows[:top]:
        mins = ((r.start_time - result.at).total_seconds() / 60.0) if r.start_time else None
        colour = "green" if r.ev > 0 and r.reliable else ("yellow" if r.ev > 0 else "dim")
        t.add_row(
            f"{round(mins):d}m" if mins is not None else "—",
            f"{r.venue} R{r.race_number}",
            f"{r.saddlecloth or ''}. {r.horse}" if r.saddlecloth else r.horse,
            f"{r.sb_odds:.2f}",
            f"{r.bf_back or 0:.2f}/{r.bf_lay or 0:.2f}",
            f"{r.fair_odds:.2f}",
            f"[{colour}]{r.ev:+.1%}[/{colour}]",
            f"{r.ev_at_lay:+.1%}" if r.ev_at_lay is not None else "—",
            f"{r.lock:+.1%}" if r.lock is not None else "—",
            f"{r.kelly:.1%}" if r.kelly else "",
            f"{r.sb_overround:.0%}",
            f"{r.bf_matched:,.0f}" if r.bf_matched else "—",
            ", ".join(r.flags),
        )
    console.print(t)
    if hidden and not show_all:
        console.print(f"[dim]{hidden} rows hidden by the reliability gates (--all).[/dim]")
    console.print("[dim]EV is model-implied: it is right only if the exchange is. "
                  "Nothing here places a bet.[/dim]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.race_value",
        description="Sportsbet fixed win odds vs Betfair, races in the next N minutes",
    )
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--window", type=int, help="minutes ahead (default VALUE_WINDOW_MINUTES)")
    parser.add_argument("--interval", type=int, help="seconds between passes")
    parser.add_argument("--all", action="store_true", help="include rows failing the gates")
    parser.add_argument("--min-ev", type=float, default=None, help="e.g. 0.02")
    parser.add_argument("--top", type=int, default=30, help="rows to show")
    parser.add_argument("--no-log", action="store_true", help="don't append the daily CSV")
    parser.add_argument("--archive", action="store_true",
                        help="archive raw responses (off by default: ~20 files a minute)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(logging.DEBUG if args.verbose else logging.WARNING)
    settings = get_settings()
    if args.window:
        settings.value_window_minutes = args.window
    interval = args.interval or settings.value_interval_seconds
    try:
        scanner = ValueScanner(settings, archive=args.archive)
    except BetfairNotConfiguredError as exc:
        print(f"Betfair is the source of truth for this scanner and is not configured: {exc}")
        return 2
    log_dir = PROJECT_ROOT / "data" / "value_log"
    try:
        while True:
            started = time.monotonic()
            try:
                result = scanner.scan_once()
            except Exception as exc:  # a loop must survive one bad pass
                log.exception("scan failed")
                result = ScanResult(at=datetime.now(timezone.utc), error=f"scan failed: {exc}")
            render(result, show_all=args.all, top=args.top, min_ev=args.min_ev)
            if not args.no_log:
                append_csv(result, log_dir)
            if args.once:
                return 0 if result.error is None else 2
            time.sleep(max(1.0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0
    finally:
        scanner.close()


if __name__ == "__main__":
    sys.exit(main())
