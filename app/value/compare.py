"""Compare one race's Sportsbet fixed prices with its Betfair markets.

Win: Sportsbet win price vs the Betfair WIN market. Place: Sportsbet's
fixed place price vs the Betfair "To Be Placed" market, only when both pay
the same number of places. Betfair is the source of truth; see the module
docstring of :mod:`app.race_value` for every adjustment made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.matching.runners import match_race_runners
from app.sources.base import RaceInfo
from app.sources.betfair import BetfairMarket
from app.value.pricing import (
    fair_probabilities,
    fill_price,
    kelly_fraction,
    lay_lock_profit,
    places_for_field,
    spread_ticks,
)

#: Flags that make a row unreliable (hidden unless --all, never a signal).
BLOCKING_FLAGS = ("SPREAD", "THIN", "BOOK", "ONE-SIDED", "SHALLOW", "DELAYED", "SUSPECT")

#: What each flag means, for the on-screen key.
FLAG_HELP = {
    "SPREAD": "Betfair back/lay too far apart",
    "THIN": "too little money matched on Betfair",
    "SHALLOW": "not enough Betfair money at the top to fill the test stake",
    "ONE-SIDED": "Betfair has only a back or only a lay price",
    "BOOK": "Betfair's prices don't add up to a sensible market",
    "DELAYED": "your Betfair key gives delayed prices",
    "SUSPECT": "edge too big to be real (stale price or scratching)",
    "GAP": "the two price snapshots were far apart in time",
    "RENORM": "a Sportsbet scratching was taken out of Betfair's market",
    "8 RUNNERS": "one more scratching cuts the place terms to 2 places",
}


@dataclass
class ValueRow:
    kind: str  # WIN | PLACE
    venue: str
    race_number: int | None
    start_time: datetime | None
    horse: str
    saddlecloth: int | None
    sb_odds: float
    bf_back: float | None
    bf_lay: float | None
    bf_lay_fill: float | None  # average lay odds for the test stake
    p_fair: float
    ev: float
    ev_at_lay: float | None
    lock: float | None
    kelly: float
    sb_overround: float
    bf_book: float
    bf_matched: float | None
    snapshot_gap: float | None
    market_id: str = ""
    selection_id: int = 0
    places: int = 1
    spread_ticks: int | None = None
    flags: list[str] = field(default_factory=list)
    streak: int = 0  # consecutive scans this row has shown value (tracker)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.market_id}:{self.selection_id}"

    @property
    def fair_odds(self) -> float:
        return 1.0 / self.p_fair

    @property
    def reliable(self) -> bool:
        return not any(f.split()[0] in BLOCKING_FLAGS for f in self.flags)


def _skip_reason(label: str, market: BetfairMarket) -> str | None:
    if market.inplay:
        return f"{label}: Betfair market is in-play"
    if market.book_status != "OPEN":
        return f"{label}: Betfair market is {market.book_status or 'without a book'}"
    return None


def _compare_market(
    kind: str,
    venue: str,
    race: RaceInfo,
    market: BetfairMarket,
    places: int,
    settings: Any,
    sb_fetched_at: datetime | None,
    notes: list[str],
) -> list[ValueRow]:
    label = f"{venue} R{race.race_number}{' place' if kind == 'PLACE' else ''}"
    reason = _skip_reason(label, market)
    if reason:
        notes.append(reason)
        return []

    matches = match_race_runners(race.runners, market.runners, label, market.removed_names)
    exclude: set[int] = set()
    for m in matches:
        if m.sportsbet_runner.status == "scratched" and m.betfair_quote is not None:
            exclude.add(m.betfair_quote.selection_id)
            if kind == "WIN":
                notes.append(f"{label}: {m.sportsbet_runner.horse_name} scratched on "
                             "Sportsbet, still on Betfair — taken out of Betfair's market")
    if kind == "WIN":
        stale = [m.sportsbet_runner.horse_name for m in matches
                 if m.status == "removed_on_betfair" and m.sportsbet_runner.status == "active"]
        if stale:
            notes.append(f"{label}: Sportsbet still prices {', '.join(stale)} "
                         "(scratched on Betfair)")

    stake = settings.value_depth_stake
    fair, book, shallow = fair_probabilities(market.runners, stake, places, exclude)

    def sb_price(r):
        return r.win_odds if kind == "WIN" else r.place_odds

    priced = [r for r in race.active_runners() if sb_price(r) and sb_price(r) > 1.0]
    overround = sum(1.0 / sb_price(r) for r in priced) / places
    gap = None
    if sb_fetched_at and market.fetched_at:
        gap = abs((market.fetched_at - sb_fetched_at).total_seconds())

    min_matched = (settings.value_min_matched if kind == "WIN"
                   else settings.value_min_matched_place)
    race_flags: list[str] = []
    if market.delayed:
        race_flags.append("DELAYED key")
    elif (market.total_matched or 0.0) < min_matched:
        race_flags.append(f"THIN ${market.total_matched or 0:,.0f}")
    if book and abs(book / places - 1.0) > settings.value_max_book_deviation:
        race_flags.append(f"BOOK {book / places:.0%}")
    if gap is not None and gap > settings.value_max_snapshot_gap_seconds:
        race_flags.append(f"GAP {gap:.0f}s")
    if exclude:
        race_flags.append("RENORM")
    if kind == "PLACE" and len(race.active_runners()) == 8:
        race_flags.append("8 RUNNERS")

    rows: list[ValueRow] = []
    for m in matches:
        sr, q = m.sportsbet_runner, m.betfair_quote
        odds = sb_price(sr)
        if sr.status != "active" or not odds or odds <= 1.0:
            continue
        if q is None or q.selection_id not in fair:
            continue
        p = fair[q.selection_id]
        ev = p * odds - 1.0
        flags = list(race_flags)
        ticks = None
        if q.best_back and q.best_lay:
            ticks = spread_ticks(q.best_back, q.best_lay)
            if ticks > settings.value_max_spread_ticks:
                flags.append(f"SPREAD {ticks} ticks")
        else:
            flags.append("ONE-SIDED")
        if q.selection_id in shallow and "ONE-SIDED" not in flags:
            flags.append("SHALLOW")
        if ev > settings.value_suspect_ev:
            flags.append("SUSPECT")
        lay_ladder = q.lay_ladder or ([(q.best_lay, q.lay_volume or 1e9)] if q.best_lay else [])
        lay_fill, _ = fill_price(lay_ladder, stake)
        rows.append(ValueRow(
            kind=kind,
            venue=venue,
            race_number=race.race_number,
            start_time=race.start_time or market.market_start,
            horse=sr.horse_name,
            saddlecloth=sr.saddlecloth,
            sb_odds=odds,
            bf_back=q.best_back,
            bf_lay=q.best_lay,
            bf_lay_fill=lay_fill,
            p_fair=p,
            ev=ev,
            ev_at_lay=(odds / lay_fill - 1.0) if lay_fill else None,
            lock=(lay_lock_profit(odds, lay_fill, settings.betfair_commission)
                  if lay_fill else None),
            kelly=kelly_fraction(p, odds),
            sb_overround=overround,
            bf_book=book,
            bf_matched=market.total_matched,
            snapshot_gap=gap,
            market_id=market.market_id,
            selection_id=q.selection_id,
            places=places,
            spread_ticks=ticks,
            flags=flags,
        ))
    return rows


def compare_race(
    venue: str,
    race: RaceInfo,
    market: BetfairMarket,
    settings: Any,
    sb_fetched_at: datetime | None = None,
    place_market: BetfairMarket | None = None,
) -> tuple[list[ValueRow], list[str]]:
    """Value every Sportsbet runner in one race against the exchange, win
    and (when ``place_market`` is given and the terms agree) place.

    Returns the rows and race-level notes (why a race or market was skipped).
    """
    label = f"{venue} R{race.race_number}"
    notes: list[str] = []
    if race.status != "open":
        return [], [f"{label}: Sportsbet race is {race.status}"]
    if (race.win_market_status or "").upper() == "S":
        return [], [f"{label}: Sportsbet market suspended"]

    rows = _compare_market("WIN", venue, race, market, 1, settings, sb_fetched_at, notes)
    if place_market is not None and getattr(settings, "value_include_places", True):
        sb_places = race.places or places_for_field(len(race.active_runners()))
        bf_places = place_market.number_of_winners
        if not any(r.place_odds for r in race.active_runners()):
            pass  # Sportsbet isn't offering a fixed place price on this race
        elif not sb_places or bf_places != sb_places:
            notes.append(f"{label}: place terms differ (Sportsbet {sb_places or 'none'}, "
                         f"Betfair {bf_places or 'unknown'}) — places not compared")
        else:
            rows += _compare_market("PLACE", venue, race, place_market, sb_places,
                                    settings, sb_fetched_at, notes)
    return rows, notes
