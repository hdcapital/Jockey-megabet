"""Follow every value signal from the first sighting to the result.

* **Confirmation**: a row must show value (reliable, EV at or above
  ``VALUE_MIN_EV``) on ``VALUE_CONFIRM_SCANS`` scans in a row before it is
  a bet worth a look. One-scan gaps are usually a price Sportsbet hasn't
  moved yet or a scratching in flight.
* **Closing price**: the market of every signal is followed until Betfair
  turns it in-play; the last fair probability seen before that is the
  close. Near the jump the scanner polls those markets every
  ``VALUE_CLOSE_POLL_SECONDS``.
* **Result**: once the market settles, Betfair's own runner status
  (WINNER / LOSER / REMOVED) and Betfair Starting Price are recorded and
  the signal is appended to ``data/value_results.csv``.

State lives in ``data/value_state.json`` so closing the window loses
nothing: pending signals are settled on the next run.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.sources.betfair import BetfairMarket
from app.value.compare import ValueRow
from app.value.pricing import fair_probabilities

log = logging.getLogger(__name__)

RESULT_FIELDS = [
    "kind", "venue", "race", "start_time", "saddlecloth", "horse",
    "first_seen", "first_odds", "first_ev", "confirmed", "confirmed_at",
    "bet_odds", "bet_ev", "bet_p", "close_at", "close_seconds_before_start",
    "close_p", "close_fair_odds", "clv", "bsp", "vs_bsp", "result", "pnl",
]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@dataclass
class Signal:
    key: str
    kind: str
    venue: str
    race_number: int | None
    start_time: str | None
    horse: str
    saddlecloth: int | None
    market_id: str
    selection_id: int
    places: int
    first_seen: str
    first_odds: float
    first_ev: float
    first_p: float
    confirmed_at: str | None = None
    confirmed_odds: float | None = None
    confirmed_ev: float | None = None
    confirmed_p: float | None = None
    close_p: float | None = None
    close_at: str | None = None
    bsp: float | None = None
    result: str | None = None

    @property
    def bet_odds(self) -> float:
        return self.confirmed_odds if self.confirmed_odds else self.first_odds

    @property
    def bet_p(self) -> float:
        return self.confirmed_p if self.confirmed_p else self.first_p


@dataclass
class TrackedMarket:
    market_id: str
    kind: str
    venue: str
    start_time: str | None
    places: int
    snapshot: dict[str, float] = field(default_factory=dict)  # selection id -> p
    snapshot_at: str | None = None
    off_at: str | None = None
    last_result_poll: str | None = None

    def as_market(self) -> BetfairMarket:
        """A bare market object the Betfair client can fetch books for."""
        return BetfairMarket(
            market_id=self.market_id, market_name="", venue=self.venue,
            market_start=_dt(self.start_time),
            market_type="PLACE" if self.kind == "PLACE" else "WIN",
            number_of_winners=self.places,
        )


def outcome(kind: str, status: str) -> str | None:
    """Our result word for Betfair's runner status in a CLOSED market."""
    s = status.upper()
    if s.startswith("REMOVED"):
        return "void"
    if s in ("WINNER", "PLACED"):
        return "won" if kind == "WIN" else "placed"
    if s == "LOSER":
        return "lost" if kind == "WIN" else "unplaced"
    return None


class Tracker:
    #: Give up on a market's result this long after it jumped.
    RESULT_TIMEOUT = timedelta(hours=8)
    RESULT_POLL = timedelta(seconds=90)
    #: Streaks from a scan older than this are stale (the program was shut).
    STREAK_EXPIRY = timedelta(minutes=5)

    def __init__(self, settings: Any, state_path: Path, results_path: Path):
        self.settings = settings
        self.state_path = state_path
        self.results_path = results_path
        self.streaks: dict[str, int] = {}
        self.streaks_at: datetime | None = None
        self.signals: dict[str, Signal] = {}
        self.markets: dict[str, TrackedMarket] = {}
        self.settled_today: list[dict[str, Any]] = []
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("value state %s unreadable (%s); starting fresh", self.state_path, exc)
            return
        self.streaks = {k: int(v) for k, v in raw.get("streaks", {}).items()}
        self.streaks_at = _dt(raw.get("streaks_at"))
        self.signals = {k: Signal(**v) for k, v in raw.get("signals", {}).items()}
        self.markets = {k: TrackedMarket(**v) for k, v in raw.get("markets", {}).items()}

    def save(self) -> None:
        data = {
            "streaks": self.streaks,
            "streaks_at": _iso(self.streaks_at),
            "signals": {k: asdict(v) for k, v in self.signals.items()},
            "markets": {k: asdict(v) for k, v in self.markets.items()},
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("could not save value state: %s", exc)

    # -- scans -------------------------------------------------------------
    def qualifies(self, row: ValueRow) -> bool:
        return row.reliable and row.ev >= self.settings.value_min_ev

    def observe(self, rows: list[ValueRow], markets: list[BetfairMarket], now: datetime) -> None:
        """Update streaks and signals from one full scan, then snapshot the
        markets the scan looked at (the latest pre-jump view of each)."""
        if self.streaks_at is None or now - self.streaks_at > self.STREAK_EXPIRY:
            self.streaks = {}
        need = self.settings.value_confirm_scans
        new: dict[str, int] = {}
        for r in rows:
            if not self.qualifies(r):
                r.streak = 0
                continue
            r.streak = self.streaks.get(r.key, 0) + 1
            new[r.key] = r.streak
            sig = self.signals.get(r.key)
            if sig is None:
                sig = self.signals[r.key] = Signal(
                    key=r.key, kind=r.kind, venue=r.venue, race_number=r.race_number,
                    start_time=_iso(r.start_time), horse=r.horse, saddlecloth=r.saddlecloth,
                    market_id=r.market_id, selection_id=r.selection_id, places=r.places,
                    first_seen=_iso(now), first_odds=r.sb_odds, first_ev=r.ev, first_p=r.p_fair,
                )
            if r.streak >= need and sig.confirmed_at is None:
                sig.confirmed_at = _iso(now)
                sig.confirmed_odds, sig.confirmed_ev, sig.confirmed_p = r.sb_odds, r.ev, r.p_fair
            if r.market_id not in self.markets:
                self.markets[r.market_id] = TrackedMarket(
                    market_id=r.market_id, kind=r.kind, venue=r.venue,
                    start_time=_iso(r.start_time), places=r.places,
                )
        self.streaks, self.streaks_at = new, now
        self.update_books(markets, now)

    def update_books(self, markets: list[BetfairMarket], now: datetime) -> None:
        """Record each tracked market's fair probabilities while it is open,
        and fix the closing snapshot the first time it is seen in-play."""
        for m in markets:
            tm = self.markets.get(m.market_id)
            if tm is None or tm.off_at:
                continue
            start = _dt(tm.start_time)
            jumped = m.inplay or (
                m.book_status in ("SUSPENDED", "CLOSED")
                and start is not None and now >= start - timedelta(minutes=1)
            )
            if jumped:
                tm.off_at = _iso(now)
                for sig in self._signals_for(tm.market_id):
                    sig.close_p = tm.snapshot.get(str(sig.selection_id))
                    sig.close_at = tm.snapshot_at
                continue
            if m.book_status != "OPEN" or not m.runners:
                continue
            fair, _, _ = fair_probabilities(m.runners, self.settings.value_depth_stake, tm.places)
            if fair:
                tm.snapshot = {str(k): v for k, v in fair.items()}
                tm.snapshot_at = _iso(now)
        self.save()

    def _signals_for(self, market_id: str) -> list[Signal]:
        return [s for s in self.signals.values() if s.market_id == market_id]

    # -- between scans -------------------------------------------------------
    def markets_near_jump(self, now: datetime, lead: timedelta) -> list[TrackedMarket]:
        """Tracked markets not yet off that start within ``lead`` (or are late)."""
        out = []
        for tm in self.markets.values():
            start = _dt(tm.start_time)
            if tm.off_at or start is None:
                continue
            if start - now <= lead and now - start < timedelta(minutes=30):
                out.append(tm)
            elif now - start >= timedelta(minutes=30):
                tm.off_at = _iso(now)  # never seen in-play: close from last snapshot
                for sig in self._signals_for(tm.market_id):
                    sig.close_p = tm.snapshot.get(str(sig.selection_id))
                    sig.close_at = tm.snapshot_at
        return out

    def markets_awaiting_result(self, now: datetime) -> list[TrackedMarket]:
        out = []
        for tm in list(self.markets.values()):
            if not tm.off_at:
                continue
            last = _dt(tm.last_result_poll)
            if last is None or now - last >= self.RESULT_POLL:
                out.append(tm)
        return out

    def apply_results(self, markets: list[BetfairMarket], now: datetime) -> list[Signal]:
        """Settle the signals of every CLOSED market; return those settled."""
        settled: list[Signal] = []
        for m in markets:
            tm = self.markets.get(m.market_id)
            if tm is None:
                continue
            tm.last_result_poll = _iso(now)
            closed = m.book_status == "CLOSED"
            timed_out = now - (_dt(tm.off_at) or now) > self.RESULT_TIMEOUT
            if not (closed or timed_out):
                continue
            for sig in self._signals_for(m.market_id):
                status = m.runner_status.get(sig.selection_id, "")
                sig.result = (outcome(sig.kind, status) if closed else None) or "unknown"
                sig.bsp = m.actual_sp.get(sig.selection_id)
                settled.append(sig)
                del self.signals[sig.key]
            del self.markets[m.market_id]
        if settled:
            self._append_results(settled)
        self.save()
        return settled

    # -- output ----------------------------------------------------------------
    @staticmethod
    def result_row(sig: Signal) -> dict[str, Any]:
        odds, p = sig.bet_odds, sig.bet_p
        start, close_at = _dt(sig.start_time), _dt(sig.close_at)
        pnl = {"won": odds - 1, "placed": odds - 1, "lost": -1.0,
               "unplaced": -1.0, "void": 0.0}.get(sig.result or "")
        return {
            "kind": sig.kind, "venue": sig.venue, "race": sig.race_number,
            "start_time": sig.start_time, "saddlecloth": sig.saddlecloth, "horse": sig.horse,
            "first_seen": sig.first_seen, "first_odds": sig.first_odds,
            "first_ev": round(sig.first_ev, 5), "confirmed": sig.confirmed_at is not None,
            "confirmed_at": sig.confirmed_at, "bet_odds": odds,
            "bet_ev": round(p * odds - 1, 5), "bet_p": round(p, 5),
            "close_at": sig.close_at,
            "close_seconds_before_start": (round((start - close_at).total_seconds())
                                           if start and close_at else None),
            "close_p": round(sig.close_p, 5) if sig.close_p else None,
            "close_fair_odds": round(1 / sig.close_p, 3) if sig.close_p else None,
            "clv": round(sig.close_p * odds - 1, 5) if sig.close_p else None,
            "bsp": sig.bsp,
            "vs_bsp": round(odds / sig.bsp - 1, 5) if sig.bsp else None,
            "result": sig.result, "pnl": pnl,
        }

    def _append_results(self, signals: list[Signal]) -> None:
        try:
            self.results_path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.results_path.exists()
            with open(self.results_path, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=RESULT_FIELDS)
                if new:
                    w.writeheader()
                for sig in signals:
                    row = self.result_row(sig)
                    w.writerow(row)
                    self.settled_today.append(row)
        except OSError as exc:
            log.error("could not write %s: %s", self.results_path, exc)

    def pending_count(self) -> int:
        return len(self.signals)
