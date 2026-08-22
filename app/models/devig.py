"""Pluggable de-vig (margin removal) methods for a single win market.

Input: the decimal win odds of every *active* runner in one race.
Output: fair win probabilities that sum to 1, plus the market overround.

Implemented methods
-------------------
proportional
    fair_p_i = raw_p_i / sum(raw_p). The classic multiplicative method.

power
    Solves for k such that sum(raw_p_i ** k) = 1, then fair_p_i = raw_p_i**k.
    Pushes proportionally more margin onto longshots (a crude favourite-
    longshot-bias correction).

shin
    Shin (1992/1993) insider-trading model. Solves for z (the implied
    proportion of insider money) such that the Shin-adjusted probabilities
    sum to 1.

None of these is claimed to be objectively superior; the method is selected
by configuration (``DEVIG_METHOD``) and recorded with every stored
valuation so results are attributable and comparable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

REGISTRY: dict[str, "DevigMethod"] = {}


@dataclass(frozen=True)
class DevigResult:
    method: str
    raw_odds: tuple[float, ...]
    raw_probabilities: tuple[float, ...]
    overround: float  # sum of raw implied probabilities (e.g. 1.18)
    fair_probabilities: tuple[float, ...]


DevigMethod = Callable[[tuple[float, ...]], tuple[float, ...]]


def register(name: str) -> Callable[[DevigMethod], DevigMethod]:
    def deco(fn: DevigMethod) -> DevigMethod:
        REGISTRY[name] = fn
        return fn

    return deco


def _raw_probabilities(odds: tuple[float, ...]) -> tuple[float, ...]:
    for o in odds:
        if o <= 1.0:
            raise ValueError(f"invalid decimal odds (must exceed 1.0): {o}")
    return tuple(1.0 / o for o in odds)


@register("proportional")
def proportional(raw: tuple[float, ...]) -> tuple[float, ...]:
    total = sum(raw)
    return tuple(p / total for p in raw)


@register("power")
def power(raw: tuple[float, ...]) -> tuple[float, ...]:
    # Find k with sum(p_i ** k) = 1 by bisection. k > 1 when overround > 1.
    def total(k: float) -> float:
        return sum(p**k for p in raw)

    lo, hi = 0.5, 1.0
    # Expand bounds until they bracket the root of total(k) - 1.
    while total(hi) > 1.0:
        hi *= 2.0
        if hi > 64:
            break
    while total(lo) < 1.0:
        lo /= 2.0
        if lo < 1e-6:
            break
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if total(mid) > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    fair = tuple(p**k for p in raw)
    s = sum(fair)
    return tuple(p / s for p in fair)


@register("shin")
def shin(raw: tuple[float, ...]) -> tuple[float, ...]:
    # Shin's estimator: pi_i(z) = (sqrt(z^2 + 4(1-z) p_i^2 / B) - z) / (2(1-z))
    # where B = sum(p_i); solve sum(pi_i(z)) = 1 for z in [0, 1) by bisection.
    b = sum(raw)
    if b <= 1.0:  # no margin to remove; already (sub)fair
        return proportional(raw)

    def implied(z: float) -> list[float]:
        return [
            (math.sqrt(z * z + 4.0 * (1.0 - z) * (p * p) / b) - z) / (2.0 * (1.0 - z))
            for p in raw
        ]

    lo, hi = 0.0, 0.999999
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if sum(implied(mid)) > 1.0:
            lo = mid
        else:
            hi = mid
    fair = implied((lo + hi) / 2.0)
    s = sum(fair)
    return tuple(p / s for p in fair)


def devig(odds: list[float] | tuple[float, ...], method: str = "proportional") -> DevigResult:
    """Remove bookmaker margin from a set of win odds for one race."""
    if not odds:
        raise ValueError("cannot de-vig an empty market")
    if method not in REGISTRY:
        raise ValueError(f"unknown de-vig method {method!r}; available: {sorted(REGISTRY)}")
    odds_t = tuple(float(o) for o in odds)
    raw = _raw_probabilities(odds_t)
    fair = REGISTRY[method](raw)
    return DevigResult(
        method=method,
        raw_odds=odds_t,
        raw_probabilities=raw,
        overround=sum(raw),
        fair_probabilities=fair,
    )
