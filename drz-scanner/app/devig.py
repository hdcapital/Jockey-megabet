"""Turning bookmaker win prices into win probabilities.

The only method this tool uses on Sportsbet prices is **power** de-vig (or a
fitted **beta** exponent, which is the same functional form with the exponent
estimated from settled results instead of solved per race).

Proportional de-vig is deliberately *not* offered for place valuation. A
bookmaker's overround is not spread evenly across a field: the margin on a
40/1 shot is far larger than on a 2/1 favourite. Dividing every implied
probability by the same constant therefore leaves the longshot margin sitting
inside the probabilities, and a place model fed those probabilities will
report large, entirely fictitious edges on outsiders. Power de-vig moves
probability from longshots to favourites and removes that artefact.

It is included here only as a reference point for tests that demonstrate the
difference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = ["DevigResult", "power_devig", "proportional_devig", "beta_probabilities"]


@dataclass(frozen=True)
class DevigResult:
    method: str
    raw_odds: tuple[float, ...]
    raw_probabilities: tuple[float, ...]
    overround: float
    probabilities: tuple[float, ...]
    exponent: float | None = None


def _raw(odds: tuple[float, ...]) -> tuple[float, ...]:
    for o in odds:
        if not math.isfinite(o) or o <= 1.0:
            raise ValueError(f"invalid decimal odds (must exceed 1.0): {o!r}")
    return tuple(1.0 / o for o in odds)


def proportional_devig(odds) -> DevigResult:
    """fair_i = raw_i / sum(raw). Reference only — never used for valuation."""
    odds_t = tuple(float(o) for o in odds)
    raw = _raw(odds_t)
    total = sum(raw)
    return DevigResult(
        method="proportional",
        raw_odds=odds_t,
        raw_probabilities=raw,
        overround=total,
        probabilities=tuple(p / total for p in raw),
    )


def power_devig(odds, tol: float = 1e-12, max_iter: int = 200) -> DevigResult:
    """Solve for k with ``sum(raw_i ** k) == 1``; fair_i = raw_i ** k.

    k > 1 whenever the book is over-round, which shrinks small probabilities
    proportionally harder than large ones.
    """
    odds_t = tuple(float(o) for o in odds)
    if not odds_t:
        raise ValueError("cannot de-vig an empty market")
    raw = np.array(_raw(odds_t))

    def total(k: float) -> float:
        return float(np.power(raw, k).sum())

    lo, hi = 0.5, 1.0
    while total(hi) > 1.0:
        hi *= 2.0
        if hi > 256.0:
            break
    while total(lo) < 1.0:
        lo /= 2.0
        if lo < 1e-9:
            break
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if total(mid) > 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    k = 0.5 * (lo + hi)
    fair = np.power(raw, k)
    fair = fair / fair.sum()
    return DevigResult(
        method="power",
        raw_odds=odds_t,
        raw_probabilities=tuple(float(p) for p in raw),
        overround=float(raw.sum()),
        probabilities=tuple(float(p) for p in fair),
        exponent=k,
    )


def beta_probabilities(odds, beta: float) -> DevigResult:
    """p_i = raw_i**beta / sum_j raw_j**beta with a *globally fitted* beta.

    Unlike :func:`power_devig`, beta is not solved per race to make the
    probabilities sum to 1 — it is estimated once by conditional-logit maximum
    likelihood against settled results, then applied everywhere. Normalising
    afterwards is what makes the probabilities a distribution.
    """
    odds_t = tuple(float(o) for o in odds)
    raw = np.array(_raw(odds_t))
    fair = np.power(raw, beta)
    fair = fair / fair.sum()
    return DevigResult(
        method="beta",
        raw_odds=odds_t,
        raw_probabilities=tuple(float(p) for p in raw),
        overround=float(raw.sum()),
        probabilities=tuple(float(p) for p in fair),
        exponent=float(beta),
    )
