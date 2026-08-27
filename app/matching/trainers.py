"""Match a Trainer Extras market to the trainer's runners in each race.

Unlike a jockey, a trainer may legitimately saddle SEVERAL runners in one
race — that is not ambiguity. Within a race the trainer's win probability
is the sum of their runners' fair win probabilities (exact, because race
winners are mutually exclusive); across races the win count follows the
same Poisson-binomial distribution as a jockey's rides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.matching.names import jockey_names_match, normalize_name
from app.sources.base import RaceInfo, RunnerInfo

log = logging.getLogger(__name__)


@dataclass
class TrainerRaceEntry:
    race: RaceInfo
    runners: list[RunnerInfo]  # the trainer's ACTIVE runners in this race
    scratched: list[RunnerInfo] = field(default_factory=list)


@dataclass
class TrainerCard:
    """Duck-type compatible with JockeyRideCard where the pipeline needs it
    (``rides``/``match_status``/``normalized_jockey_name`` etc.)."""

    source_jockey_name: str  # the trainer's name as the market spells it
    normalized_jockey_name: str
    rides: list[TrainerRaceEntry] = field(default_factory=list)  # per-RACE entries
    scratched_rides: list[TrainerRaceEntry] = field(default_factory=list)
    ambiguous: bool = False
    match_status: str = "unmatched"
    notes: list[str] = field(default_factory=list)


def find_trainer_entries(trainer_name: str, races: list[RaceInfo]) -> TrainerCard:
    """Locate every race in which the trainer has at least one runner."""
    card = TrainerCard(
        source_jockey_name=trainer_name,
        normalized_jockey_name=normalize_name(trainer_name),
    )
    for race in races:
        mine = [
            r for r in race.runners
            if r.trainer_name and jockey_names_match(trainer_name, r.trainer_name)
        ]
        active = [r for r in mine if r.status == "active"]
        scratched = [r for r in mine if r.status != "active"]
        if active:
            card.rides.append(TrainerRaceEntry(race=race, runners=active,
                                               scratched=scratched))
        elif scratched:
            card.scratched_rides.append(
                TrainerRaceEntry(race=race, runners=[], scratched=scratched)
            )
            log.info(
                "trainer %s: all runners scratched in race %s (%s)",
                trainer_name, race.race_number,
                ", ".join(r.horse_name for r in scratched),
            )
    card.match_status = "matched" if card.rides else "unmatched"
    if card.match_status == "unmatched":
        log.warning("no runners found for trainer %r", trainer_name)
    return card
