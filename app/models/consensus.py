"""Configurable consensus win probability across sources.

Default behaviour (documented, not claimed optimal):

* When both a Betfair-derived probability and a Sportsbet no-vig probability
  are available and the Betfair estimate is marked reliable, blend them with
  the configured weights (defaults: 0.7 Betfair / 0.3 Sportsbet, renormalised
  if they don't sum to 1).
* When Betfair is unavailable or unreliable, fall back to the Sportsbet
  no-vig probability alone and record ``fallback=True`` so downstream
  consumers/reporting can show the basis of the number.

The design is source-generic: any future bookmaker adapter simply supplies
a ``SourceProbability`` and a weight in the configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceProbability:
    source: str
    probability: float | None
    reliable: bool = True
    detail: str = ""


@dataclass(frozen=True)
class ConsensusResult:
    probability: float | None
    used_sources: tuple[str, ...]
    weights: dict[str, float]
    fallback: bool  # True when preferred blend degraded to fewer sources
    detail: str = ""


def consensus(
    inputs: list[SourceProbability],
    weights: dict[str, float],
) -> ConsensusResult:
    """Blend per-source probabilities with configured weights.

    Sources with ``probability is None`` or ``reliable is False`` are
    excluded; remaining weights are renormalised. Returns probability None
    only when no usable source exists.
    """
    usable = [
        s
        for s in inputs
        if s.probability is not None and s.reliable and weights.get(s.source, 0.0) > 0.0
    ]
    excluded = [s for s in inputs if s not in usable]
    if not usable:
        return ConsensusResult(
            probability=None,
            used_sources=(),
            weights={},
            fallback=True,
            detail="no usable probability source",
        )
    total_w = sum(weights[s.source] for s in usable)
    blended = sum(s.probability * weights[s.source] for s in usable) / total_w  # type: ignore[operator]
    fallback = bool(excluded)
    if fallback:
        log.info(
            "consensus fell back to %s (excluded: %s)",
            "+".join(s.source for s in usable),
            ", ".join(f"{s.source}: {s.detail or 'unavailable'}" for s in excluded),
        )
    return ConsensusResult(
        probability=blended,
        used_sources=tuple(s.source for s in usable),
        weights={s.source: weights[s.source] / total_w for s in usable},
        fallback=fallback,
        detail="; ".join(f"{s.source} excluded: {s.detail}" for s in excluded),
    )
