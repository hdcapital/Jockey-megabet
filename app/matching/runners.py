"""Match Betfair exchange runners to Sportsbet racecard runners."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.matching.names import normalize_name, runner_names_match
from app.sources.base import RunnerInfo
from app.sources.betfair import BetfairRunnerQuote

log = logging.getLogger(__name__)


@dataclass
class RunnerMatch:
    sportsbet_runner: RunnerInfo
    betfair_quote: BetfairRunnerQuote | None
    status: str  # matched | unmatched | ambiguous
    detail: str = ""


def match_race_runners(
    sportsbet_runners: list[RunnerInfo],
    betfair_quotes: list[BetfairRunnerQuote],
) -> list[RunnerMatch]:
    """Pair runners by normalized horse name within one already-matched race.

    Betfair runner names usually carry a saddlecloth prefix ("4. Horse Name")
    which :func:`normalize_name` strips. Unresolved or duplicated names are
    recorded as unmatched/ambiguous — never guessed.
    """
    matches: list[RunnerMatch] = []
    by_name: dict[str, list[BetfairRunnerQuote]] = {}
    for q in betfair_quotes:
        by_name.setdefault(normalize_name(q.runner_name), []).append(q)

    for sr in sportsbet_runners:
        key = normalize_name(sr.horse_name)
        candidates = by_name.get(key, [])
        if not candidates:
            candidates = [
                q for q in betfair_quotes if runner_names_match(sr.horse_name, q.runner_name)
            ]
        if len(candidates) == 1:
            matches.append(RunnerMatch(sr, candidates[0], "matched"))
        elif not candidates:
            matches.append(
                RunnerMatch(sr, None, "unmatched", f"no Betfair runner named {key!r}")
            )
            log.info("runner unmatched on Betfair: %s", sr.horse_name)
        else:
            matches.append(
                RunnerMatch(
                    sr, None, "ambiguous", f"{len(candidates)} Betfair runners named {key!r}"
                )
            )
            log.warning("ambiguous Betfair runner match for %s", sr.horse_name)
    return matches
