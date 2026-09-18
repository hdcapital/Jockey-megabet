"""Dr Z place-value scoring, tiering and (display-only) stake sizing.

The Dr Z score is the plainest possible statement of value::

    drz = p_place * place_price        (expected return per $1 staked)
    ev  = drz - 1

A row is only interesting when the model's place probability and the
bookmaker's place price disagree. Sportsbet's fixed place prices carry a
large margin, so on most days nothing clears the bar — that is the tool
working, not the tool failing.

Tiers
-----
BET
    ``drz >= DRZ_MIN`` under **every** model that could price the race, win
    price within Ziemba's filter, a fresh price, HIGH quality, and a win
    model that has been checked against results.
WATCH
    In the ``[DRZ_WATCH_MIN, DRZ_MIN)`` band, or clearing the BET bar under
    only one of several models.
SUSPECT
    ``drz > DRZ_SUSPECT``. Never BET. A Dr Z score above 1.3 on a market as
    heavily margined as fixed-odds place betting is almost always a stale
    price, a scratching in flight, or a parse error — so it is logged and its
    payload archived rather than shown as the find of the day.

No part of this places a bet. The stake column is arithmetic on a bankroll
you type into a config file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

TIER_BET = "BET"
TIER_WATCH = "WATCH"
TIER_SUSPECT = "SUSPECT"
TIER_NONE = "—"

QUALITY_HIGH = "HIGH"
QUALITY_MEDIUM = "MEDIUM"
QUALITY_LOW = "LOW"


@dataclass
class TierInputs:
    """Everything the tier decision looks at, so it can be logged verbatim."""

    drz_by_model: dict[str, float]
    win_price: float
    price_age_seconds: float | None
    quality: str
    uncalibrated: bool
    allow_uncalibrated: bool
    price_type: str = "fixed"
    betfair_delayed_only: bool = False
    seconds_to_jump: float | None = None
    drz_min: float = 1.10
    drz_watch_min: float = 1.03
    drz_suspect: float = 1.30
    max_win_odds: float = 9.0
    max_price_age_seconds: int = 60
    delayed_no_bet_window_seconds: int = 300


@dataclass
class TierResult:
    tier: str
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.tier


def decide_tier(inp: TierInputs) -> TierResult:
    """Tier plus the reasons, in the order they were applied."""
    reasons: list[str] = []
    if not inp.drz_by_model:
        return TierResult(TIER_NONE, ["no model could price this runner"])

    values = list(inp.drz_by_model.values())
    best = max(values)
    worst = min(values)

    # SUSPECT dominates everything: a score this high is evidence of a data
    # problem, and a data problem must never be presented as a bet.
    if best > inp.drz_suspect:
        return TierResult(
            TIER_SUSPECT,
            [f"drz {best:.3f} exceeds suspect cap {inp.drz_suspect:.2f} — "
             f"likely stale price, scratching in flight, or parse error"],
        )

    # An indicative tote price is not a price you can take.
    if inp.price_type != "fixed":
        return TierResult(
            TIER_WATCH if best >= inp.drz_watch_min else TIER_NONE,
            [f"price type {inp.price_type!r} is indicative, not a takeable fixed price"],
        )

    n_clearing = sum(1 for v in values if v >= inp.drz_min)
    all_clear = worst >= inp.drz_min

    blockers: list[str] = []
    if not all_clear:
        blockers.append(
            f"drz clears {inp.drz_min:.2f} under {n_clearing}/{len(values)} models"
        )
    if inp.win_price > inp.max_win_odds:
        blockers.append(
            f"win price {inp.win_price:.2f} above the {inp.max_win_odds:.1f} filter"
        )
    if inp.price_age_seconds is None:
        blockers.append("price age unknown")
    elif inp.price_age_seconds >= inp.max_price_age_seconds:
        blockers.append(
            f"price is {inp.price_age_seconds:.0f}s old "
            f"(limit {inp.max_price_age_seconds}s)"
        )
    if inp.quality != QUALITY_HIGH:
        blockers.append(f"quality {inp.quality}")
    if inp.uncalibrated and not inp.allow_uncalibrated:
        blockers.append("win model UNCALIBRATED (use --allow-uncalibrated to override)")
    if (
        inp.betfair_delayed_only
        and inp.seconds_to_jump is not None
        and inp.seconds_to_jump <= inp.delayed_no_bet_window_seconds
    ):
        blockers.append(
            f"only a DELAYED Betfair price supports this inside "
            f"{inp.delayed_no_bet_window_seconds}s of the jump"
        )

    if not blockers:
        return TierResult(TIER_BET, [f"drz {worst:.3f}-{best:.3f} across all models"])

    reasons = blockers
    if best >= inp.drz_watch_min:
        return TierResult(TIER_WATCH, reasons)
    return TierResult(TIER_NONE, reasons)


def drz_score(p_place: float, place_price: float) -> float:
    """Expected return per $1 staked on the place."""
    return p_place * place_price


def fair_place_odds(p_place: float) -> float | None:
    return 1.0 / p_place if p_place > 0 else None


def kelly_fraction(drz: float, place_price: float) -> float:
    """Full-Kelly fraction ``f = (drz - 1) / (price - 1)``, floored at 0."""
    if place_price <= 1.0:
        return 0.0
    return max(0.0, (drz - 1.0) / (place_price - 1.0))


def suggest_stake(
    drz: float,
    place_price: float,
    bankroll: float,
    kelly_multiplier: float,
    cap_per_bet_pct: float,
) -> float:
    """Quarter-Kelly stake, capped at a percentage of bankroll. Display only."""
    f = kelly_fraction(drz, place_price) * kelly_multiplier
    return math.floor(min(f, cap_per_bet_pct) * bankroll * 100) / 100


def apply_race_stake_cap(
    stakes: list[float], bankroll: float, cap_per_race_pct: float
) -> list[float]:
    """Scale a race's stakes down proportionally to respect the per-race cap.

    Stakes are floored to the cent rather than rounded, so three $6.67 legs
    cannot add up to a cent more than the cap they were scaled to fit.
    """
    total = sum(stakes)
    cap = cap_per_race_pct * bankroll
    if total <= cap or total <= 0:
        return [math.floor(s * 100) / 100 for s in stakes]
    scale = cap / total
    return [math.floor(s * scale * 100) / 100 for s in stakes]


def quality_for(
    price_age_seconds: float | None,
    max_age: int,
    n_models: int,
    betfair_delayed: bool,
    terms_fragile: bool,
) -> tuple[str, list[str]]:
    """HIGH / MEDIUM / LOW plus the reasons, from what we know about the row."""
    notes: list[str] = []
    quality = QUALITY_HIGH
    if price_age_seconds is None:
        return QUALITY_LOW, ["price age unknown"]
    if price_age_seconds >= max_age * 4:
        quality = QUALITY_LOW
        notes.append(f"price {price_age_seconds:.0f}s old")
    elif price_age_seconds >= max_age:
        quality = QUALITY_MEDIUM
        notes.append(f"price {price_age_seconds:.0f}s old")
    if n_models < 1:
        return QUALITY_LOW, notes + ["no win model available"]
    if betfair_delayed:
        quality = QUALITY_MEDIUM if quality == QUALITY_HIGH else quality
        notes.append("Betfair prices are delayed")
    if terms_fragile:
        notes.append("exactly 8 active runners: one scratching drops this to 2 places")
    return quality, notes


def suspect_payload(row: Any) -> dict[str, Any]:
    """Compact record of a SUSPECT row, for the log and the archive."""
    return {
        "race": getattr(row, "race_source_id", None),
        "runner": getattr(row, "horse_name", None),
        "win_price": getattr(row, "win_price", None),
        "place_price": getattr(row, "place_price", None),
        "drz_by_model": getattr(row, "drz_by_model", None),
        "raw_archive_path": getattr(row, "raw_archive_path", None),
    }
