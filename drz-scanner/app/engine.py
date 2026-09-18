"""Valuation: from one parsed racecard to scored, tiered rows.

The engine never fetches anything. It takes a parsed race, a calibration and
(optionally) exchange quotes, and returns one :class:`PlaceValuation` per
active runner under every win model that could price the race. Keeping it
free of I/O is what lets the end-to-end test run without a network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import MODEL_VERSION
from app.calibration import Calibration
from app.config import Settings
from app.place_model import (
    PLACE_TERMS_FRAGILE_FIELD,
    place_probabilities,
    places_for_field,
)
from app.scoring import (
    QUALITY_HIGH,
    TIER_SUSPECT,
    TierInputs,
    apply_race_stake_cap,
    decide_tier,
    drz_score,
    fair_place_odds,
    quality_for,
    suggest_stake,
)
from app.sources.base import RaceInfo
from app.winprob import (
    WIN_MODEL_BETFAIR,
    WinProbabilitySet,
    best_model,
    betfair_win_probabilities,
    max_win_price_for,
    sportsbet_win_probabilities,
)

log = logging.getLogger(__name__)


#: Which band table a model's probabilities may be corrected by. The table is
#: measured per probability source; applying the Betfair-measured bands to
#: Sportsbet-derived probabilities would be borrowing evidence that was never
#: collected. ``None`` means "no band correction for this model yet".
BAND_SOURCE = {
    "betfair": "betfair",
    "sportsbet_beta": None,
    "sportsbet_power": None,
}


def band_source_for(win_model: str) -> str | None:
    return BAND_SOURCE.get(win_model)


class NoPlaceMarketError(Exception):
    """The field is too small for a place market — a normal, reported state."""


class RaceAlreadyJumpedError(NoPlaceMarketError):
    """The advertised start has passed. Sportsbet's status flags can lag the
    jump by a minute or two, and a price from a race that is being run is
    not a price you can take."""


#: Grace after the advertised start before a still-"open" race is skipped.
#: Australian races routinely jump a minute or two late.
JUMPED_GRACE_SECONDS = 120


@dataclass
class PlaceValuation:
    observed_at: datetime
    race_source_id: str
    runner_source_id: str
    venue: str | None
    race_number: int | None
    start_time: datetime | None
    horse_name: str
    saddlecloth: int | None
    jockey_name: str | None

    active_runner_count: int
    places: int
    terms_fragile: bool

    win_price: float | None
    place_price: float | None
    price_type: str
    price_code: str
    price_age_seconds: float | None
    seconds_to_jump: float | None

    win_model: str
    win_probs_by_model: dict[str, float]
    p_place_by_model: dict[str, float]
    drz_by_model: dict[str, float]
    p_place: float | None
    #: P(place) before the win-price band correction, and the factor applied.
    #: Both are stored so a row can be recomputed either way later.
    p_place_raw: float | None
    band_shrink: float
    drz: float | None
    ev: float | None
    fair_place_odds: float | None

    p_place_betfair: float | None
    drz_exchange_place: float | None
    betfair_delayed: bool
    #: The cap that applied to this row's win model, and whether it bound.
    max_win_price: float
    beyond_price_cap: bool

    lam: float
    tau: float
    calibration_version: str
    model_version: str

    tier: str
    tier_reasons: list[str] = field(default_factory=list)
    quality: str = QUALITY_HIGH
    quality_notes: list[str] = field(default_factory=list)
    suggested_stake: float | None = None
    raw_sha256: str | None = None
    raw_archive_path: str | None = None
    tote_indicative_win: float | None = None
    tote_indicative_place: float | None = None
    tote_indicative_code: str | None = None


def value_race(
    race: RaceInfo,
    calibration: Calibration,
    settings: Settings,
    now: datetime | None = None,
    betfair_win_probs: list[float | None] | None = None,
    betfair_reliable: list[bool] | None = None,
    betfair_delayed: bool = False,
    betfair_place_probs: dict[str, float] | None = None,
    allow_uncalibrated: bool = False,
) -> list[PlaceValuation]:
    """Score every active runner in one race.

    ``betfair_win_probs``/``betfair_reliable`` are per *active runner*, in the
    same order as ``race.active_runners()``; ``betfair_place_probs`` maps a
    runner's source id to the exchange's own place probability.
    """
    now = now or datetime.now(timezone.utc)
    active = race.active_runners()
    n = len(active)
    places = places_for_field(n)
    if places == 0:
        raise NoPlaceMarketError(
            f"{race.venue} R{race.race_number}: {n} active runners — "
            f"below the 5 needed for a place market"
        )

    priced = [r for r in active if r.win_price and r.win_price > 1.0]
    if len(priced) != n:
        # A field where some runner has no live win price is a field in
        # motion (a scratching landing, or a market suspended). We cannot
        # normalise a partial book without inventing the missing share.
        missing = [r.horse_name for r in active if r not in priced]
        raise NoPlaceMarketError(
            f"{race.venue} R{race.race_number}: {len(missing)} of {n} active "
            f"runners have no live win price ({', '.join(missing[:4])}) — "
            f"market in motion, not valued this scan"
        )

    win_prices = [r.win_price for r in active]
    models: dict[str, WinProbabilitySet] = {}

    sb = sportsbet_win_probabilities(win_prices, calibration, settings.beta_min_races)
    models[sb.win_model] = sb

    if betfair_win_probs is not None and betfair_reliable is not None:
        bf = betfair_win_probabilities(
            betfair_win_probs, betfair_reliable, betfair_delayed
        )
        if bf is not None:
            models[bf.win_model] = bf

    terms_fragile = n == PLACE_TERMS_FRAGILE_FIELD
    seconds_to_jump = (
        (race.start_time - now).total_seconds() if race.start_time else None
    )
    if seconds_to_jump is not None and seconds_to_jump < -JUMPED_GRACE_SECONDS:
        raise RaceAlreadyJumpedError(
            f"{race.venue} R{race.race_number}: advertised start was "
            f"{-seconds_to_jump:.0f}s ago and the race still shows as open — "
            f"not valued"
        )

    # P(place) per model, then Dr Z per model.
    #
    # Two corrections sit on top of the place model, in this order:
    #   1. the additive win-probability bucket correction (fitted by
    #      likelihood, adopted only because it improved out-of-sample
    #      log-loss), then
    #   2. the multiplicative win-price band correction, clamped to <= 1 so
    #      it can only ever shrink a probability.
    # Neither is renormalised: renormalising would put back exactly the level
    # shift each was adopted for.
    p_place_by_model: dict[str, list[float]] = {}
    p_place_raw_by_model: dict[str, list[float]] = {}
    shrink_by_model: dict[str, list[float]] = {}
    for name, mset in models.items():
        pp = place_probabilities(mset.probabilities, places, calibration.lam, calibration.tau)
        # The model's own output must sum to the number of dividends before
        # either correction touches it.
        pp.check_sum(tol=1e-6)
        corrected = [
            calibration.correct_place_probability(q, p)
            for q, p in zip(mset.probabilities, pp.p_place)
        ]
        p_place_raw_by_model[name] = corrected
        # The band table was measured on one probability source at a time, so
        # it is only applied to the model it was measured on.
        source = band_source_for(name)
        shrink = [
            calibration.shrink_factor(source, r.win_price) if source else 1.0
            for r in active
        ]
        shrink_by_model[name] = shrink
        p_place_by_model[name] = [c * f for c, f in zip(corrected, shrink)]

    chosen = best_model(models) or sb.win_model

    out: list[PlaceValuation] = []
    for i, runner in enumerate(active):
        place_price = runner.place_price
        age = (
            (now - runner.price_timestamp).total_seconds()
            if runner.price_timestamp
            else None
        )
        drz_by_model: dict[str, float] = {}
        drz_without_betfair: dict[str, float] = {}
        pp_by_model: dict[str, float] = {}
        wp_by_model: dict[str, float] = {}
        for name in models:
            p = p_place_by_model[name][i]
            wp_by_model[name] = models[name].probabilities[i]
            pp_by_model[name] = p
            if place_price:
                drz_by_model[name] = drz_score(p, place_price)
                if name != WIN_MODEL_BETFAIR:
                    drz_without_betfair[name] = drz_by_model[name]

        p_place = pp_by_model.get(chosen)
        drz = drz_by_model.get(chosen)
        p_place_raw = p_place_raw_by_model[chosen][i] if chosen in models else None
        shrink = shrink_by_model[chosen][i] if chosen in models else 1.0

        # The exchange's own place market, valued the same way. This is the
        # only check on the model that does not come from the model.
        p_bf_place = (betfair_place_probs or {}).get(runner.source_id)
        drz_exchange_place = (
            drz_score(p_bf_place, place_price)
            if p_bf_place is not None and place_price
            else None
        )
        cap, cap_reason = max_win_price_for(
            chosen, betfair_delayed and WIN_MODEL_BETFAIR in models, settings
        )
        beyond_cap = bool(runner.win_price and runner.win_price > cap)
        # "A delayed Betfair price ALONE upgrading this row" means: Betfair
        # is delayed, and without it nothing else here clears the BET bar.
        # Testing the number of models instead would never fire, because the
        # Sportsbet model is always present.
        delayed_only = (
            WIN_MODEL_BETFAIR in models
            and models[WIN_MODEL_BETFAIR].betfair_delayed
            and bool(drz_by_model)
            and max(drz_by_model.values()) >= settings.drz_min
            and not any(d >= settings.drz_min for d in drz_without_betfair.values())
        )
        quality, notes = quality_for(
            age,
            settings.max_price_age_seconds,
            len(models),
            betfair_delayed and WIN_MODEL_BETFAIR in models,
            terms_fragile,
        )
        tier_res = decide_tier(
            TierInputs(
                drz_by_model=drz_by_model,
                win_price=runner.win_price or 0.0,
                price_age_seconds=age,
                quality=quality,
                uncalibrated=models[chosen].uncalibrated,
                allow_uncalibrated=allow_uncalibrated,
                win_model=chosen,
                max_win_price=cap,
                max_win_price_reason=cap_reason,
                drz_exchange_place=drz_exchange_place,
                require_exchange_confirmation=settings.require_exchange_confirmation,
                price_type=runner.price_type,
                betfair_delayed_only=delayed_only,
                seconds_to_jump=seconds_to_jump,
                drz_min=settings.drz_min,
                drz_watch_min=settings.drz_watch_min,
                drz_suspect=settings.drz_suspect,
                max_price_age_seconds=settings.max_price_age_seconds,
                delayed_no_bet_window_seconds=settings.betfair_delayed_no_bet_window_seconds,
            )
        )
        tote = runner.tote_indicative()
        out.append(
            PlaceValuation(
                observed_at=now,
                race_source_id=race.source_id,
                runner_source_id=runner.source_id,
                venue=race.venue,
                race_number=race.race_number,
                start_time=race.start_time,
                horse_name=runner.horse_name,
                saddlecloth=runner.saddlecloth,
                jockey_name=runner.jockey_name,
                active_runner_count=n,
                places=places,
                terms_fragile=terms_fragile,
                win_price=runner.win_price,
                place_price=place_price,
                price_type=runner.price_type,
                price_code="L",
                price_age_seconds=age,
                seconds_to_jump=seconds_to_jump,
                win_model=chosen,
                win_probs_by_model=wp_by_model,
                p_place_by_model=pp_by_model,
                drz_by_model=drz_by_model,
                p_place=p_place,
                p_place_raw=p_place_raw,
                band_shrink=shrink,
                drz=drz,
                ev=(drz - 1.0) if drz is not None else None,
                fair_place_odds=fair_place_odds(p_place) if p_place else None,
                p_place_betfair=p_bf_place,
                drz_exchange_place=drz_exchange_place,
                betfair_delayed=betfair_delayed and WIN_MODEL_BETFAIR in models,
                max_win_price=cap,
                beyond_price_cap=beyond_cap,
                lam=calibration.lam,
                tau=calibration.tau,
                calibration_version=calibration.version,
                model_version=MODEL_VERSION,
                tier=tier_res.tier,
                tier_reasons=tier_res.reasons,
                quality=quality,
                quality_notes=notes,
                raw_sha256=race.raw_sha256,
                raw_archive_path=race.raw_archive_path,
                tote_indicative_win=tote.win_price if tote else None,
                tote_indicative_place=tote.place_price if tote else None,
                tote_indicative_code=tote.price_code if tote else None,
            )
        )

    _apply_stakes(out, settings)
    for v in out:
        if v.tier == TIER_SUSPECT:
            log.warning(
                "SUSPECT row: %s R%s %s drz=%s win=%s place=%s — raw payload %s",
                v.venue, v.race_number, v.horse_name,
                {k: round(x, 3) for k, x in v.drz_by_model.items()},
                v.win_price, v.place_price, v.raw_archive_path,
            )
    return out


def staking_drz(v: PlaceValuation) -> float | None:
    """The Dr Z a stake is sized from: the most conservative estimate we hold.

    A BET requires every model to clear the threshold, so the stake should
    be sized off the *worst* of them — and off the exchange's own place
    market where one confirmed the row, since that has been the better
    forecaster. Sizing off the most optimistic model would stake hardest
    exactly where the models disagree most.
    """
    candidates = list(v.drz_by_model.values())
    if v.drz_exchange_place is not None:
        candidates.append(v.drz_exchange_place)
    return min(candidates) if candidates else None


def _apply_stakes(valuations: list[PlaceValuation], settings: Settings) -> None:
    """Quarter-Kelly stakes for BET rows only, then the per-race cap.

    An indicative tote price never gets a stake: there is no backtest of
    indicative-versus-final dividends, so any number here would be a guess.
    """
    betting = [
        v for v in valuations
        if v.tier == "BET" and v.drz and v.place_price and v.price_type == "fixed"
        and staking_drz(v)
    ]
    if not betting:
        return
    stakes = [
        suggest_stake(
            staking_drz(v), v.place_price, settings.bankroll,
            settings.kelly_fraction, settings.stake_cap_per_bet_pct,
        )
        for v in betting
    ]
    capped = apply_race_stake_cap(
        stakes, settings.bankroll, settings.stake_cap_per_race_pct
    )
    for v, s in zip(betting, capped):
        v.suggested_stake = s
