"""Exchange pricing: the Betfair tick ladder, fill prices at a stake, and
fair probabilities from a market's depth.

Everything here is pure arithmetic on retrieved prices, so it is tested by
hand in ``tests/test_value_pricing.py``.
"""

from __future__ import annotations

import math

from app.sources.betfair import BetfairRunnerQuote

#: Betfair's price ladder: (upper bound of the band, increment inside it).
TICK_BANDS: tuple[tuple[float, float], ...] = (
    (2.0, 0.01), (3.0, 0.02), (4.0, 0.05), (6.0, 0.1), (10.0, 0.2),
    (20.0, 0.5), (30.0, 1.0), (50.0, 2.0), (100.0, 5.0), (1000.0, 10.0),
)


def tick_index(price: float) -> float:
    """Number of ladder steps from 1.01 up to ``price``."""
    idx, lo = 0.0, 1.01
    for hi, step in TICK_BANDS:
        if price <= hi:
            return round(idx + (price - lo) / step, 6)
        idx += (hi - lo) / step
        lo = hi
    return round(idx + (price - 1000.0) / 10.0, 6)


def spread_ticks(back: float, lay: float) -> int:
    """Ladder steps between best back and best lay (1 = as tight as it gets)."""
    return int(round(tick_index(lay) - tick_index(back)))


def _ladder(quote_ladder: list[tuple[float, float]], best: float | None,
            size: float | None) -> list[tuple[float, float]]:
    if quote_ladder:
        return quote_ladder
    if best is not None and best > 1.0:
        return [(best, size if size is not None else math.inf)]
    return []


def fill_price(ladder: list[tuple[float, float]], stake: float) -> tuple[float | None, float]:
    """Average odds for ``stake`` taken from the ladder, and how much filled.

    Sizes on Betfair's back and lay ladders are both in backer's stake, so
    the average is stake-weighted odds. When the ladder holds less than the
    stake, the average of what is there is returned with the smaller fill.
    """
    left, filled, weighted = stake, 0.0, 0.0
    for price, size in ladder:
        if price <= 1.0 or size <= 0:
            continue
        take = min(left, size)
        weighted += price * take
        filled += take
        left -= take
        if left <= 1e-9:
            break
    if filled <= 0:
        return None, 0.0
    return weighted / filled, filled


def depth_probability(quote: BetfairRunnerQuote, stake: float) -> tuple[float | None, bool]:
    """(probability, shallow) for one runner from its ladder at ``stake``.

    The probability is the midpoint, in probability space, of the average
    back and lay odds for that stake, so a token amount at the top of the
    book cannot move it on its own. ``shallow`` is True when either side
    holds less than the stake (or is missing).
    """
    back = _ladder(quote.back_ladder, quote.best_back, quote.back_volume)
    lay = _ladder(quote.lay_ladder, quote.best_lay, quote.lay_volume)
    vb, fb = fill_price(back, stake)
    vl, fl = fill_price(lay, stake)
    if vb is not None and vl is not None:
        return (1.0 / vb + 1.0 / vl) / 2.0, (fb < stake - 1e-9 or fl < stake - 1e-9)
    if vb is not None:
        return 1.0 / vb, True
    if vl is not None:
        return 1.0 / vl, True
    return None, True


def fair_probabilities(
    quotes: list[BetfairRunnerQuote],
    stake: float,
    places: int = 1,
    exclude: set[int] | None = None,
) -> tuple[dict[int, float], float, set[int]]:
    """Normalised exchange probabilities for one market.

    Returns ``({selection id: p}, raw book, {shallow selection ids})``. The
    book of depth midpoints is scaled to ``places`` (1 for a win market, 2 or
    3 for a place market) so the spread is gone; a place probability is
    capped below 1. ``exclude`` takes runners out of the field first.
    """
    exclude = exclude or set()
    raw: dict[int, float] = {}
    shallow: set[int] = set()
    for q in quotes:
        if q.selection_id in exclude:
            continue
        p, thin = depth_probability(q, stake)
        if p is None:
            continue
        raw[q.selection_id] = p
        if thin:
            shallow.add(q.selection_id)
    book = sum(raw.values())
    if book <= 0:
        return {}, 0.0, shallow
    scale = places / book
    return {sid: min(p * scale, 0.995) for sid, p in raw.items()}, book, shallow


def lay_lock_profit(back_odds: float, lay_odds: float, commission: float) -> float:
    """Profit per $1 backed at ``back_odds`` when laid at ``lay_odds``.

    Lay stake ``back_odds / (lay_odds - commission)`` equalises both
    outcomes; the result is ``back_odds*(1-c)/(lay_odds-c) - 1``.
    """
    return back_odds * (1.0 - commission) / (lay_odds - commission) - 1.0


def kelly_fraction(p: float, odds: float) -> float:
    """Full-Kelly fraction of bankroll for a bet at ``odds`` with win chance
    ``p``; 0 when there is no edge."""
    if odds <= 1.0:
        return 0.0
    return max(0.0, (p * odds - 1.0) / (odds - 1.0))


def places_for_field(n_active: int) -> int:
    """Standard Australian place terms: 3 dividends for 8+ runners, 2 for
    5-7, no place betting below 5."""
    if n_active >= 8:
        return 3
    if n_active >= 5:
        return 2
    return 0
