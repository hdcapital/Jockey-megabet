"""Valuation engine: turn retrieved market data into Megabet fair values.

This module is pure computation over already-retrieved data structures, so
it is fully unit-testable with fixtures and never performs I/O itself.

Formulas (documented per README):
    raw_p_i        = 1 / decimal_odds_i
    overround      = sum(raw_p_i) over active runners
    fair_p_i       = de-vig(raw_p) via the configured method
    P(X >= k)      = Poisson-binomial survival over the jockey's ride probs
    fair_odds      = 1 / P(X >= k)
    expected_return= P(X >= k) * sportsbet_odds - 1
    edge_pct       = sportsbet_odds / fair_odds - 1   (== expected_return)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.matching.jockeys import JockeyRideCard, find_rides
from app.matching.meetings import find_betfair_market
from app.matching.runners import match_race_runners
from app.models.consensus import ConsensusResult, SourceProbability, consensus
from app.models.devig import DevigResult, devig
from app.models.poisson_binomial import (
    expected_return,
    fair_odds,
    poisson_binomial,
)
from app.sources.base import MegabetOffer, RaceInfo
from app.sources.betfair import BetfairMarket

log = logging.getLogger(__name__)

MODEL_VERSION = "1"


@dataclass
class RideProbability:
    race: RaceInfo
    horse_name: str
    sportsbet_odds: float | None
    sportsbet_fair_p: float | None
    devig: DevigResult | None
    betfair_p: float | None = None
    betfair_reliable: bool = False
    betfair_detail: str = ""
    consensus: ConsensusResult | None = None

    def probability_for(self, model: str) -> float | None:
        if model == "sportsbet_novig":
            return self.sportsbet_fair_p
        if model == "betfair":
            return self.betfair_p if self.betfair_reliable else None
        if model == "consensus":
            return self.consensus.probability if self.consensus else self.sportsbet_fair_p
        raise ValueError(f"unknown model {model!r}")


@dataclass
class MegabetValuation:
    offer: MegabetOffer
    ride_card: JockeyRideCard
    rides: list[RideProbability]
    model: str
    fair_probability: float | None
    fair_odds: float | None
    expected_return: float | None
    quality: str
    quality_detail: str
    computed_at: datetime
    # Companion fair odds from the other models, for display.
    alt_fair_odds: dict[str, float | None] = field(default_factory=dict)

    @property
    def edge_pct(self) -> float | None:
        return self.expected_return


def price_race_sportsbet(race: RaceInfo, method: str) -> DevigResult | None:
    """De-vig one race's active-runner win market. None if unpriceable."""
    active = [r for r in race.active_runners() if r.win_odds and r.win_odds > 1.0]
    if len(active) < 2:
        log.info(
            "race %s: only %d priced active runners — cannot de-vig",
            race.source_id, len(active),
        )
        return None
    return devig([r.win_odds for r in active], method=method)


def build_ride_probabilities(
    card: JockeyRideCard,
    venue: str,
    settings: Settings,
    betfair_markets: list[BetfairMarket] | None = None,
) -> list[RideProbability]:
    """Fair win probability for each of the jockey's active rides."""
    out: list[RideProbability] = []
    weights = {
        "betfair": settings.consensus_weight_betfair,
        "sportsbet": settings.consensus_weight_sportsbet,
    }
    for ride in card.rides:
        race, runner = ride.race, ride.runner
        dv = price_race_sportsbet(race, settings.devig_method)
        sb_fair = None
        if dv is not None and runner.win_odds and runner.win_odds > 1.0:
            active = [r for r in race.active_runners() if r.win_odds and r.win_odds > 1.0]
            idx = next(
                (i for i, r in enumerate(active) if r.source_id == runner.source_id), None
            )
            if idx is not None:
                sb_fair = dv.fair_probabilities[idx]

        rp = RideProbability(
            race=race,
            horse_name=runner.horse_name,
            sportsbet_odds=runner.win_odds,
            sportsbet_fair_p=sb_fair,
            devig=dv,
        )

        if betfair_markets:
            bf_market = find_betfair_market(venue, race, betfair_markets)
            if bf_market is not None:
                matches = match_race_runners(race.active_runners(), bf_market.runners)
                mine = next(
                    (mt for mt in matches
                     if mt.sportsbet_runner.source_id == runner.source_id
                     and mt.status == "matched"),
                    None,
                )
                if mine and mine.betfair_quote:
                    q = mine.betfair_quote
                    rp.betfair_p = q.probability
                    rp.betfair_reliable = q.reliable
                    rp.betfair_detail = q.detail

        rp.consensus = consensus(
            [
                SourceProbability("betfair", rp.betfair_p, rp.betfair_reliable,
                                  rp.betfair_detail),
                SourceProbability("sportsbet", rp.sportsbet_fair_p, True,
                                  "" if rp.sportsbet_fair_p is not None
                                  else "no de-viggable Sportsbet market"),
            ],
            weights,
        )
        out.append(rp)
    return out


def assess_quality(
    card: JockeyRideCard, rides: list[RideProbability], model: str,
    settings: Settings, now: datetime,
) -> tuple[str, str]:
    """HIGH / MEDIUM / LOW data-quality grade for a valuation."""
    problems: list[str] = []
    if card.match_status != "matched":
        problems.append(f"jockey match status: {card.match_status}")
    if not rides:
        problems.append("no active rides")
    unpriced = [r for r in rides if r.probability_for("sportsbet_novig") is None]
    if unpriced:
        problems.append(
            f"{len(unpriced)} ride(s) without a de-viggable Sportsbet price"
        )
    stale = [
        r for r in rides
        for ru in [next((x for x in r.race.runners if x.horse_name == r.horse_name), None)]
        if ru and ru.odds_timestamp
        and (now - ru.odds_timestamp).total_seconds() > settings.stale_price_seconds
    ]
    if stale:
        problems.append(f"{len(stale)} ride(s) with stale odds")
    abandoned = [r for r in rides if r.race.status == "abandoned"]
    if abandoned:
        problems.append(f"{len(abandoned)} race(s) abandoned")
    if problems:
        return "LOW", "; ".join(problems)

    betfair_ok = all(r.betfair_p is not None and r.betfair_reliable for r in rides)
    if model in ("betfair", "consensus") or betfair_ok:
        if betfair_ok:
            return "HIGH", "all rides matched; Betfair liquid on every ride"
        return "MEDIUM", "all rides matched with Sportsbet prices; Betfair missing or weak"
    return "MEDIUM", "all rides matched with Sportsbet prices; Betfair not used"


def value_offer(
    offer: MegabetOffer,
    races: list[RaceInfo],
    settings: Settings | None = None,
    betfair_markets: list[BetfairMarket] | None = None,
    models: tuple[str, ...] = ("sportsbet_novig", "betfair", "consensus"),
    now: datetime | None = None,
    ride_cache: dict | None = None,
) -> list[MegabetValuation]:
    """Value one Megabet offer under each requested probability model.

    Returns one valuation per model; models with no usable probabilities
    yield fair_probability None (marked unavailable) rather than a guess.
    ``ride_cache`` (optional, shared across a scan) avoids re-matching and
    re-logging the same jockey's rides for each of their thresholds.
    """
    from app.matching.names import normalize_name

    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    cache_key = (normalize_name(offer.meeting_name or ""),
                 normalize_name(offer.jockey_name))
    cached = ride_cache.get(cache_key) if ride_cache is not None else None
    if cached is not None:
        card, rides = cached
    else:
        card = find_rides(offer.jockey_name, races)
        rides = build_ride_probabilities(
            card, offer.meeting_name or "", settings, betfair_markets
        )
        if ride_cache is not None:
            ride_cache[cache_key] = (card, rides)

    valuations: list[MegabetValuation] = []
    per_model_fair_odds: dict[str, float | None] = {}
    for model in models:
        probs = [r.probability_for(model) for r in rides]
        usable = [p for p in probs if p is not None]
        if not rides or len(usable) < len(probs) or not usable:
            fair_p = fo = er = None
            missing = len(probs) - len(usable)
            detail = (
                f"{model}: unavailable — "
                + (f"{missing} of {len(probs)} rides missing a probability"
                   if rides else "no active rides matched")
            )
            quality, qdetail = "LOW", detail
        else:
            dist = poisson_binomial(usable)
            fair_p = dist.prob_at_least(offer.threshold)
            fo = fair_odds(fair_p)
            er = expected_return(fair_p, offer.odds) if fair_p is not None else None
            quality, qdetail = assess_quality(card, rides, model, settings, now)
        per_model_fair_odds[model] = fo
        valuations.append(
            MegabetValuation(
                offer=offer, ride_card=card, rides=rides, model=model,
                fair_probability=fair_p, fair_odds=fo, expected_return=er,
                quality=quality, quality_detail=qdetail, computed_at=now,
            )
        )
    for v in valuations:
        v.alt_fair_odds = dict(per_model_fair_odds)
    return valuations
