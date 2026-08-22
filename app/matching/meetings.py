"""Match Betfair markets to Sportsbet races (meeting + race number + time)."""

from __future__ import annotations

import logging
from datetime import timedelta

from app.matching.names import venue_names_match
from app.sources.base import RaceInfo
from app.sources.betfair import BetfairMarket

log = logging.getLogger(__name__)

# A race matched purely on venue+race-number must also start within this
# window of the Sportsbet advertised time to be accepted.
START_TIME_TOLERANCE = timedelta(minutes=40)


def find_betfair_market(
    venue: str,
    race: RaceInfo,
    markets: list[BetfairMarket],
) -> BetfairMarket | None:
    """Find the Betfair WIN market for a Sportsbet race, or None.

    Keys: venue name (normalized), race number, and advertised start time as
    a tie-breaker/validator. Ambiguity returns None (logged), not a guess.
    """
    candidates = [
        m
        for m in markets
        if m.venue
        and venue_names_match(venue, m.venue)
        and (m.race_number is None or race.race_number is None or m.race_number == race.race_number)
    ]
    if race.start_time is not None:
        timed = [
            m
            for m in candidates
            if m.market_start is not None
            and abs(m.market_start - race.start_time) <= START_TIME_TOLERANCE
        ]
        if timed:
            candidates = timed
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Prefer exact race-number match if that uniquely resolves it.
        exact = [m for m in candidates if m.race_number == race.race_number]
        if len(exact) == 1:
            return exact[0]
        log.warning(
            "ambiguous Betfair market for %s R%s (%d candidates) — skipping",
            venue,
            race.race_number,
            len(candidates),
        )
    return None
