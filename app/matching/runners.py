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


def _name_candidates(
    sr: RunnerInfo,
    by_name: dict[str, list[BetfairRunnerQuote]],
    betfair_quotes: list[BetfairRunnerQuote],
) -> list[BetfairRunnerQuote]:
    key = normalize_name(sr.horse_name)
    candidates = by_name.get(key, [])
    if not candidates:
        candidates = [
            q for q in betfair_quotes if runner_names_match(sr.horse_name, q.runner_name)
        ]
    return candidates


def match_race_runners(
    sportsbet_runners: list[RunnerInfo],
    betfair_quotes: list[BetfairRunnerQuote],
    race_label: str = "",
) -> list[RunnerMatch]:
    """Pair runners within one already-matched race.

    Betfair names Australian runners "4. Horse Name". The saddlecloth is the
    key both feeds preserve exactly, so a runner is matched first by cloth
    number (with the name checked, never overridden), then by normalized
    name. Unresolved or duplicated names are recorded as unmatched or
    ambiguous, never guessed. One INFO line summarises the race; the
    per-runner detail is DEBUG so a normal log shows the shape of the
    problem rather than hundreds of lines.
    """
    matches: list[RunnerMatch] = []
    by_name: dict[str, list[BetfairRunnerQuote]] = {}
    by_cloth: dict[int, list[BetfairRunnerQuote]] = {}
    for q in betfair_quotes:
        by_name.setdefault(normalize_name(q.runner_name), []).append(q)
        if q.cloth_number is not None:
            by_cloth.setdefault(q.cloth_number, []).append(q)

    unmatched: list[str] = []
    for sr in sportsbet_runners:
        key = normalize_name(sr.horse_name)
        candidates: list[BetfairRunnerQuote] = []
        if sr.saddlecloth is not None and len(by_cloth.get(sr.saddlecloth, [])) == 1:
            cand = by_cloth[sr.saddlecloth][0]
            if runner_names_match(sr.horse_name, cand.runner_name):
                candidates = [cand]
            else:
                log.debug(
                    "cloth %s is %r on Sportsbet but %r on Betfair; falling back to name",
                    sr.saddlecloth, sr.horse_name, cand.runner_name,
                )
        if not candidates:
            candidates = _name_candidates(sr, by_name, betfair_quotes)
        if len(candidates) == 1:
            matches.append(RunnerMatch(sr, candidates[0], "matched"))
        elif not candidates:
            matches.append(
                RunnerMatch(sr, None, "unmatched", f"no Betfair runner named {key!r}")
            )
            unmatched.append(sr.horse_name)
            log.debug("runner unmatched on Betfair: %s", sr.horse_name)
        else:
            matches.append(
                RunnerMatch(
                    sr, None, "ambiguous", f"{len(candidates)} Betfair runners named {key!r}"
                )
            )
            log.warning("ambiguous Betfair runner match for %s", sr.horse_name)

    if unmatched:
        matched = len(sportsbet_runners) - len(unmatched)
        sample = ", ".join(q.runner_name for q in betfair_quotes[:4]) or "(no runners)"
        level = log.warning if matched * 2 < len(sportsbet_runners) else log.info
        level(
            "betfair runners %s: matched %d/%d; unmatched: %s; Betfair lists: %s%s",
            race_label or "(race)", matched, len(sportsbet_runners),
            ", ".join(unmatched), sample, " ..." if len(betfair_quotes) > 4 else "",
        )
    return matches
