"""Match a Megabet jockey to their rides in a meeting's racecards."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.matching.names import jockey_names_match, normalize_name
from app.sources.base import RaceInfo, RunnerInfo

log = logging.getLogger(__name__)


@dataclass
class RideMatch:
    race: RaceInfo
    runner: RunnerInfo


@dataclass
class JockeyRideCard:
    source_jockey_name: str
    normalized_jockey_name: str
    rides: list[RideMatch] = field(default_factory=list)
    scratched_rides: list[RideMatch] = field(default_factory=list)
    ambiguous: bool = False
    match_status: str = "unmatched"  # matched | partial | ambiguous | unmatched
    notes: list[str] = field(default_factory=list)


def find_rides(jockey_name: str, races: list[RaceInfo]) -> JockeyRideCard:
    """Locate every ride for a jockey across a meeting's races.

    Never matches ambiguously: if two distinct runners in the *same race*
    match the jockey name (which should be impossible for a real jockey),
    the card is flagged ambiguous and downgraded rather than guessed.
    """
    card = JockeyRideCard(
        source_jockey_name=jockey_name,
        normalized_jockey_name=normalize_name(jockey_name),
    )
    for race in races:
        in_race = [
            r
            for r in race.runners
            if r.jockey_name and jockey_names_match(jockey_name, r.jockey_name)
        ]
        active = [r for r in in_race if r.status == "active"]
        scratched = [r for r in in_race if r.status != "active"]
        if len(active) > 1:
            card.ambiguous = True
            card.notes.append(
                f"race {race.race_number}: {len(active)} active runners matched "
                f"jockey {jockey_name!r} ({', '.join(r.horse_name for r in active)})"
            )
            log.warning("ambiguous jockey match: %s", card.notes[-1])
            continue
        for r in active:
            card.rides.append(RideMatch(race=race, runner=r))
        for r in scratched:
            card.scratched_rides.append(RideMatch(race=race, runner=r))
            log.info(
                "jockey %s: ride %s in race %s is scratched — excluded from model",
                jockey_name,
                r.horse_name,
                race.race_number,
            )
    if card.ambiguous:
        card.match_status = "ambiguous"
    elif card.rides:
        card.match_status = "matched"
    else:
        card.match_status = "unmatched"
        log.warning("no rides found for jockey %r", jockey_name)
    return card
