"""One full scan, plus the cheap between-scan polls for closing prices and
results. Both clients are held for the whole run so Betfair logs in once."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import PROJECT_ROOT, get_settings
from app.http import ArchivingClient, SourceUnavailableError
from app.matching.meetings import find_betfair_market
from app.sources.base import RaceInfo, SchemaMismatchError
from app.sources.betfair import BetfairClient, BetfairLoginError, BetfairMarket
from app.sources.sportsbet import SportsbetClient, _first, _to_dt
from app.value.compare import ValueRow, compare_race
from app.value.tracker import Signal, Tracker

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    SYDNEY = ZoneInfo("Australia/Sydney")
except Exception:  # no tz database (Windows without tzdata): AEST is close enough
    SYDNEY = timezone(timedelta(hours=10))


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


def place_market_for(win: BetfairMarket, markets: list[BetfairMarket]) -> BetfairMarket | None:
    """The PLACE market of the same race: same Betfair event, same start."""
    for m in markets:
        if m.market_type != "PLACE" or m.market_start != win.market_start:
            continue
        if win.event_id and m.event_id == win.event_id:
            return m
        if not win.event_id and m.venue == win.venue:
            return m
    return None


@dataclass
class ScanResult:
    at: datetime
    rows: list[ValueRow] = field(default_factory=list)
    races_in_window: int = 0
    races_compared: int = 0
    no_betfair_market: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    betfair_summary: str = ""
    delayed: bool = False
    error: str | None = None


class ValueScanner:
    LOGIN_BACKOFF = timedelta(minutes=10)

    def __init__(self, settings: Any = None, archive: bool = False,
                 sportsbet: SportsbetClient | None = None,
                 betfair: BetfairClient | None = None,
                 tracker: Tracker | None = None):
        self.settings = settings or get_settings()
        self.sb = sportsbet or SportsbetClient(
            ArchivingClient("sportsbet", archive=archive, settings=self.settings)
        )
        self.bf = betfair or BetfairClient(
            client=ArchivingClient("betfair", archive=archive, settings=self.settings),
            settings=self.settings,
        )
        data = PROJECT_ROOT / "data"
        self.tracker = tracker or Tracker(
            self.settings, data / "value_state.json", data / "value_results.csv"
        )
        self._login_retry_at: datetime | None = None
        self._last_close_poll: datetime | None = None

    def close(self) -> None:
        self.sb.close()
        self.bf.close()

    def _betfair_blocked(self, now: datetime) -> str | None:
        if self._login_retry_at and now < self._login_retry_at:
            at = f"{self._login_retry_at.astimezone(SYDNEY):%I:%M %p}".lstrip("0")
            return f"Betfair refused the login earlier; trying again at {at}"
        return None

    def scan_once(self, now: datetime | None = None) -> ScanResult:
        s = self.settings
        now = now or datetime.now(timezone.utc)
        res = ScanResult(at=now)
        window = timedelta(minutes=s.value_window_minutes)
        grace = timedelta(minutes=s.value_grace_minutes)

        blocked = self._betfair_blocked(now)
        if blocked:
            res.error = blocked
            return res

        # 1. Betfair catalogue for the window (one light call). Start times
        #    on the two sides can differ by minutes, so it is padded.
        countries = tuple(c.strip().upper() for c in s.value_betfair_countries.split(",")
                          if c.strip())
        types = ("WIN", "PLACE") if s.value_include_places else ("WIN",)
        try:
            catalogue = self.bf.list_markets(
                now - grace - timedelta(minutes=30), now + window + timedelta(minutes=30),
                countries=countries, market_types=types,
            )
        except BetfairLoginError as exc:
            self._login_retry_at = now + self.LOGIN_BACKOFF
            res.error = f"Betfair refused the login: {exc.code} — {exc.hint}"
            return res
        except SourceUnavailableError as exc:
            res.error = f"Can't reach Betfair: {exc}"
            return res
        win_markets = [m for m in catalogue if m.market_type == "WIN"]

        # 2. Sportsbet races in the window.
        meetings: list[dict[str, Any]] = []
        for d in listing_dates(now):
            try:
                found, _ = self.sb.fetch_meetings(d)
                meetings.extend(found)
            except SchemaMismatchError as exc:
                log.warning("sportsbet listing %s: %s", d, exc)
            except SourceUnavailableError as exc:
                res.error = f"Can't reach Sportsbet: {exc}"
                return res
        upcoming = upcoming_races(meetings, now, window, grace)
        res.races_in_window = len(upcoming)

        # 3. Pair each with its exchange markets before fetching anything
        #    else, so racecards are only fetched for races Betfair prices.
        pairs: list[tuple[UpcomingRace, BetfairMarket, BetfairMarket | None]] = []
        for up in upcoming:
            stub = RaceInfo(source="sportsbet", source_id=up.event_id,
                            race_number=up.race_number, start_time=up.start_time,
                            status="open")
            market = find_betfair_market(up.venue, stub, win_markets)
            if market is None:
                res.no_betfair_market.append(f"{up.venue} R{up.race_number}")
            else:
                pairs.append((up, market, place_market_for(market, catalogue)))

        # 4. Sportsbet racecards, then all Betfair books in one go, last, so
        #    the two snapshots are as close together as possible.
        cards = []
        for up, market, place in pairs:
            try:
                card = self.sb.fetch_racecard(up.event_id)
            except (SourceUnavailableError, SchemaMismatchError) as exc:
                res.notes.append(f"{up.venue} R{up.race_number}: Sportsbet racecard failed ({exc})")
                continue
            cards.append((up, market, place, card.races[0], card.fetched_at))
        books = [m for _, w, p, _, _ in cards for m in (w, p) if m is not None]
        if books:
            try:
                self.bf.fetch_market_books(books)
            except SourceUnavailableError as exc:
                res.error = f"Can't get Betfair prices: {exc}"
                return res
        res.betfair_summary = self.bf.status.summary()
        res.delayed = self.bf.status.delayed

        for up, market, place, race, fetched in cards:
            if race.start_time is None:
                race.start_time = up.start_time
            rows, notes = compare_race(up.venue, race, market, s, fetched, place_market=place)
            res.notes.extend(notes)
            if rows:
                res.races_compared += 1
            res.rows.extend(rows)
        res.rows.sort(key=lambda r: r.ev, reverse=True)
        self.tracker.observe(res.rows, books, now)
        return res

    def between_scans(self, now: datetime | None = None) -> list[Signal]:
        """Closing-price polls for tracked races about to jump, and result
        checks for those that have; returns signals settled by this call."""
        now = now or datetime.now(timezone.utc)
        if self._betfair_blocked(now):
            return []
        settled: list[Signal] = []
        try:
            poll_every = timedelta(seconds=self.settings.value_close_poll_seconds)
            if self._last_close_poll is None or now - self._last_close_poll >= poll_every:
                near = self.tracker.markets_near_jump(now, timedelta(seconds=90))
                if near:
                    self._last_close_poll = now
                    markets = [tm.as_market() for tm in near]
                    self.bf.fetch_market_books(markets)
                    self.tracker.update_books(markets, now)
            waiting = self.tracker.markets_awaiting_result(now)
            if waiting:
                markets = [tm.as_market() for tm in waiting]
                self.bf.fetch_results(markets)
                settled = self.tracker.apply_results(markets, now)
        except SourceUnavailableError as exc:
            log.warning("between-scan Betfair poll failed: %s", exc)
        return settled
