"""Valuing Sportsbet's "bonus back if 2nd or 3rd" promotion.

The offer: back a horse in a nominated race with a fixed-odds win bet. If it
wins you are paid as normal. If it finishes 2nd or 3rd the stake is lost but
Sportsbet credits a **bonus bet** equal to the stake (up to a cap). A bonus
bet is not cash: its stake is not returned when it wins, so $1 of bonus is
worth some ``r < 1`` of cash, the *conversion rate*.

Per $1 staked on runner ``i`` at Sportsbet price ``O``::

    win        (p1)            +(O - 1)
    2nd / 3rd  (p23)           -1 + r
    otherwise  (1 - p1 - p23)  -1

    EV = p1 * O + p23 * r - 1

So the promotion adds ``p23 * r`` to a bet whose normal value is
``p1 * O - 1`` (negative by the bookmaker's margin on that runner). The best
horse is the one where the 2nd/3rd chance most outweighs the margin. That is
rarely the favourite (short price, small p23 relative to its price) and never
a roughie (huge margin, tiny p23): it is usually somewhere in the $3-$8 range.

Three numbers per runner are assumption-free given the probabilities, and
they are what the ranking stands on:

``ev_no_promo``  ``p1 * O - 1`` — what the bet is worth without the offer.
``p23``          P(2nd) + P(3rd), from the same discounted-Harville model
                 the place scanner uses (fitted on 44,856 AU races).
``breakeven_r``  ``(1 - p1 * O) / p23`` — the lowest bonus conversion rate at
                 which the bet is break-even. A runner with breakeven 0.35 is
                 +EV for anyone who can turn a bonus bet into 35c; one at 0.90
                 needs near-perfect conversion. Ranking by this needs no view
                 on ``r`` at all.

Hedging on Betfair
------------------
With Betfair lay prices the bet can be partly or fully hedged. With
commission ``c``, Betfair win lay price ``Lw`` and place lay price ``Lp``
(a "To Be Placed" market paying **3** places, so it covers 1st-3rd):

*Lay the win* (classic matched-betting qualifier): lay ``O / (Lw - c)`` per
$1. Every outcome then returns the same qualifying loss
``(1 - c) * O / (Lw - c) - 1``, plus ``r`` if the horse runs 2nd or 3rd.

*Full lock* (lay win **and** place): with lay stakes per $1 of

    win lay    (O - r) / (Lw - c)
    place lay  r / (Lp - c)

all three outcomes pay the same, so the profit is locked whatever happens::

    lock = (1 - c) * ((O - r) / (Lw - c) + r / (Lp - c)) - 1

A positive ``lock`` is an arbitrage given ``r``; it still depends on actually
converting the bonus bet at ``r``, so ``r`` should be set to what you really
achieve.

Nothing here places a bet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from app.calibration import Calibration
from app.place_model import place_probabilities

__all__ = [
    "BonusBackTerms",
    "FinishProbabilities",
    "RunnerInputs",
    "BonusBackValuation",
    "RacePick",
    "finish_probabilities",
    "promo_p23",
    "bonus_bet_conversion",
    "bonus_bet_unhedged_value",
    "lay_win_stake",
    "lock_stakes",
    "kelly_fraction_promo",
    "value_runner",
    "pick_best",
    "OBJECTIVES",
    "ExchangeRunner",
    "PromoRace",
    "PromoRaceError",
    "value_promo_race",
]

#: How a race's single promo bet is chosen.
#:   ev     — highest expected profit of a straight back bet (risk-neutral)
#:   lock   — highest guaranteed profit with a full Betfair hedge
#:   growth — highest expected log-bankroll growth at the Kelly stake
OBJECTIVES = ("ev", "lock", "growth")


@dataclass(frozen=True)
class BonusBackTerms:
    """The offer, and what you can do with what it pays."""

    #: Cash value of $1 of bonus bet. Matched-betting conversion on Betfair
    #: typically achieves 0.70-0.80; an unhedged bonus bet on a fair-ish
    #: longshot is worth its EV, which is lower. Set this to what you get.
    bonus_value: float = 0.70
    #: Largest stake the bonus is paid on (Sportsbet caps the refund).
    max_stake: float = 50.0
    #: Betfair commission on net market winnings.
    commission: float = 0.08
    #: Smallest per-$1 EV worth recommending.
    min_ev: float = 0.02

    def __post_init__(self) -> None:
        if not 0.0 <= self.bonus_value < 1.0:
            raise ValueError("bonus_value must be in [0, 1)")
        if not 0.0 <= self.commission < 1.0:
            raise ValueError("commission must be in [0, 1)")
        if self.max_stake <= 0:
            raise ValueError("max_stake must be positive")


@dataclass(frozen=True)
class FinishProbabilities:
    p_first: tuple[float, ...]
    p_second: tuple[float, ...]
    p_third: tuple[float, ...]

    @property
    def p_second_or_third(self) -> tuple[float, ...]:
        return tuple(a + b for a, b in zip(self.p_second, self.p_third))


def finish_probabilities(win_probs, lam: float, tau: float) -> FinishProbabilities:
    """P(1st), P(2nd), P(3rd) per runner under discounted Harville.

    Uses the same exponents as the place model. The promotion pays on 2nd
    and 3rd regardless of field size, so this is computed for every field of
    three or more, not only fields with a three-dividend place market.
    """
    q = np.asarray(list(win_probs), dtype=float)
    if q.ndim != 1 or q.size < 3:
        raise ValueError("need at least three runners for 2nd and 3rd to exist")
    if np.any(q <= 0):
        raise ValueError("every runner needs a positive win probability")
    q = q / q.sum()
    if q.size == 3:
        # Exactly three runners: 3rd is whatever is left once 1st and 2nd
        # are decided. place_probabilities needs more runners than places.
        a = np.power(q, lam)
        A = a.sum()
        second = np.array([
            sum(q[j] * a[i] / (A - a[j]) for j in range(3) if j != i)
            for i in range(3)
        ])
        third = 1.0 - q - second
        return FinishProbabilities(tuple(q), tuple(second), tuple(np.clip(third, 0, 1)))
    pp = place_probabilities(q, 3, lam, tau)
    return FinishProbabilities(pp.p_first, pp.p_second, pp.p_third)


def promo_p23(
    win_probs,
    calibration: Calibration,
    band_source: str | None = None,
    win_prices=None,
) -> tuple[FinishProbabilities, tuple[float, ...]]:
    """Finish probabilities plus a *calibrated* P(2nd or 3rd) per runner.

    P(2nd or 3rd) is P(top 3) minus P(win). The place scanner's two
    corrections to P(top 3) — the additive win-probability bucket correction
    and the shrink-only win-price band correction — are applied to P(top 3)
    first, so a favourite's 2nd/3rd chance carries the same measured
    discount its place chance does. Never negative.
    """
    fp = finish_probabilities(win_probs, calibration.lam, calibration.tau)
    out = []
    prices = list(win_prices) if win_prices is not None else [None] * len(fp.p_first)
    for q, p2, p3, price in zip(fp.p_first, fp.p_second, fp.p_third, prices):
        top3 = calibration.correct_place_probability(q, q + p2 + p3)
        if band_source:
            top3 *= calibration.shrink_factor(band_source, price)
        out.append(max(0.0, top3 - q))
    return fp, tuple(out)


# --- bonus-bet conversion ---------------------------------------------------

def bonus_bet_conversion(back_price: float, lay_price: float, commission: float) -> float:
    """Cash locked per $1 of bonus bet by backing at ``back_price`` and laying.

    A bonus bet returns winnings only, so with a lay stake
    ``(back - 1) / (lay - c)`` every outcome pays
    ``(1 - c) * (back - 1) / (lay - c)``. Longer prices with a tight back/lay
    gap convert best.
    """
    if back_price <= 1.0 or lay_price <= 1.0:
        raise ValueError("prices must exceed 1.0")
    return (1.0 - commission) * (back_price - 1.0) / (lay_price - commission)


def bonus_bet_unhedged_value(win_probs, win_prices) -> float:
    """Best expected cash per $1 of bonus bet in one race, unhedged.

    ``max_j p_j * (O_j - 1)``: the stake is never returned, so the payout on
    a win is ``O - 1``.
    """
    return max(p * (o - 1.0) for p, o in zip(win_probs, win_prices))


# --- hedges -----------------------------------------------------------------

def lay_win_stake(back_price: float, lay_price: float, commission: float) -> float:
    """Betfair win lay stake per $1 backed that balances win against loss."""
    return back_price / (lay_price - commission)


def lock_stakes(
    back_price: float, win_lay: float, place_lay: float, r: float, commission: float
) -> tuple[float, float, float]:
    """``(win_lay_stake, place_lay_stake, locked_profit)`` per $1 backed.

    The place lay must be on a 3-winner "To Be Placed" market so that it
    covers exactly 1st-3rd. Derivation: equate the three outcomes

        win:     (O-1) - Sw(Lw-1) - Sp(Lp-1)
        2nd/3rd: -1 + r + Sw(1-c) - Sp(Lp-1)
        4th+:    -1 + Sw(1-c) + Sp(1-c)

    2nd/3rd = 4th+ gives ``Sp = r / (Lp - c)``; win = 4th+ gives
    ``Sw = (O - r) / (Lw - c)``.
    """
    sw = (back_price - r) / (win_lay - commission)
    sp = r / (place_lay - commission)
    profit = (1.0 - commission) * (sw + sp) - 1.0
    return sw, sp, profit


# --- staking ----------------------------------------------------------------

def kelly_fraction_promo(p1: float, p23: float, odds: float, r: float) -> float:
    """Full-Kelly bankroll fraction for a straight back bet under the promo.

    Maximises ``p1 ln(1+f(O-1)) + p23 ln(1-f(1-r)) + p0 ln(1-f)``. The
    derivative is strictly decreasing, so bisection on ``[0, 1)`` finds the
    unique optimum; 0 when the bet is not +EV.
    """
    p0 = max(0.0, 1.0 - p1 - p23)
    b = odds - 1.0
    loss23 = 1.0 - r

    def grad(f: float) -> float:
        return p1 * b / (1 + f * b) - p23 * loss23 / (1 - f * loss23) - p0 / (1 - f)

    if grad(0.0) <= 0:
        return 0.0
    lo, hi = 0.0, 1.0 - 1e-9
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if grad(mid) > 0:
            lo = mid
        else:
            hi = mid
    return lo


# --- valuation --------------------------------------------------------------

@dataclass
class RunnerInputs:
    """Everything known about one runner. ``None`` means not available."""

    horse_name: str
    saddlecloth: int | None
    win_price: float                       # Sportsbet fixed win price
    p_win: float                           # chosen model
    p_second: float
    p_third: float
    p23: float                             # calibrated P(2nd or 3rd)
    win_model: str = ""
    #: Same runner's (p_win, p23) under every other model available; the
    #: conservative EV is the worst of them.
    alt_models: dict[str, tuple[float, float]] = field(default_factory=dict)
    bf_win_lay: float | None = None
    bf_win_lay_size: float | None = None
    bf_place_lay: float | None = None
    bf_place_lay_size: float | None = None
    #: numberOfWinners of the matched Betfair place market.
    bf_place_winners: int | None = None
    max_win_price: float = math.inf
    jockey_name: str | None = None


@dataclass
class BonusBackValuation:
    inputs: RunnerInputs
    ev_no_promo: float
    promo_value: float
    ev_back: float
    #: Worst ev_back across every model that priced the race.
    ev_back_conservative: float
    breakeven_r: float | None
    sd_back: float
    kelly_fraction: float
    stake: float
    expected_profit: float
    lay_win_stake: float | None = None
    lay_win_qualifying: float | None = None
    lay_win_ev: float | None = None
    lock_win_lay_stake: float | None = None
    lock_place_lay_stake: float | None = None
    lock_profit: float | None = None
    #: Expected log-bankroll growth at ``kelly_fraction``, conservative model.
    log_growth: float = 0.0
    eligible: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def horse_name(self) -> str:
        return self.inputs.horse_name


def _log_growth(f: float, p1: float, p23: float, odds: float, r: float) -> float:
    if f <= 0:
        return 0.0
    p0 = max(0.0, 1.0 - p1 - p23)
    return (
        p1 * math.log1p(f * (odds - 1.0))
        + p23 * math.log1p(-f * (1.0 - r))
        + p0 * math.log1p(-f)
    )


def _ev(p1: float, p23: float, odds: float, r: float) -> float:
    return p1 * odds + p23 * r - 1.0


def value_runner(
    x: RunnerInputs, terms: BonusBackTerms, bankroll: float, kelly_multiplier: float
) -> BonusBackValuation:
    r, c = terms.bonus_value, terms.commission
    o = x.win_price
    ev_no = x.p_win * o - 1.0
    promo = x.p23 * r
    ev = ev_no + promo
    evs = [ev] + [_ev(p1, p23, o, r) for p1, p23 in x.alt_models.values()]
    breakeven = (1.0 - x.p_win * o) / x.p23 if x.p23 > 0 else None

    p0 = max(0.0, 1.0 - x.p_win - x.p23)
    second = x.p_win * (o - 1) ** 2 + x.p23 * (r - 1) ** 2 + p0
    sd = math.sqrt(max(0.0, second - (ev) ** 2))

    notes: list[str] = []
    eligible = True
    if o > x.max_win_price:
        eligible = False
        notes.append(f"win price {o:.2f} beyond the {x.max_win_price:.0f} cap for {x.win_model}")
    # Kelly on the conservative probabilities: stake hardest only where every
    # model agrees.
    worst = min(
        [(x.p_win, x.p23)] + list(x.alt_models.values()),
        key=lambda m: _ev(m[0], m[1], o, r),
    )
    f = kelly_fraction_promo(worst[0], worst[1], o, r) * kelly_multiplier
    growth = _log_growth(f, worst[0], worst[1], o, r)
    stake = math.floor(min(f * bankroll, terms.max_stake) * 100) / 100 if eligible else 0.0

    v = BonusBackValuation(
        inputs=x,
        ev_no_promo=ev_no,
        promo_value=promo,
        ev_back=ev,
        ev_back_conservative=min(evs),
        breakeven_r=breakeven,
        sd_back=sd,
        kelly_fraction=f,
        stake=stake,
        expected_profit=stake * min(evs),
        log_growth=growth,
        eligible=eligible,
        notes=notes,
    )

    if x.bf_win_lay and x.bf_win_lay > 1.0:
        ls = lay_win_stake(o, x.bf_win_lay, c)
        qual = (1.0 - c) * ls - 1.0
        v.lay_win_stake = ls
        v.lay_win_qualifying = qual
        win_pl = (o - 1.0) - ls * (x.bf_win_lay - 1.0)
        lose_pl = -1.0 + ls * (1.0 - c)
        v.lay_win_ev = x.p_win * win_pl + x.p23 * (lose_pl + r) + p0 * lose_pl
        if x.bf_win_lay_size is not None and ls * terms.max_stake > x.bf_win_lay_size:
            notes.append(
                f"win lay needs ${ls * terms.max_stake:.0f} at {x.bf_win_lay:.2f}, "
                f"only ${x.bf_win_lay_size:.0f} offered"
            )
    if (
        x.bf_win_lay and x.bf_win_lay > 1.0
        and x.bf_place_lay and x.bf_place_lay > 1.0
    ):
        if x.bf_place_winners == 3:
            sw, sp, lock = lock_stakes(o, x.bf_win_lay, x.bf_place_lay, r, c)
            v.lock_win_lay_stake, v.lock_place_lay_stake, v.lock_profit = sw, sp, lock
            if x.bf_place_lay_size is not None and sp * terms.max_stake > x.bf_place_lay_size:
                notes.append(
                    f"place lay needs ${sp * terms.max_stake:.0f} at "
                    f"{x.bf_place_lay:.2f}, only ${x.bf_place_lay_size:.0f} offered"
                )
        else:
            notes.append(
                f"Betfair place market pays {x.bf_place_winners} places, not 3 — "
                f"no exact lock"
            )
    return v


@dataclass
class RacePick:
    objective: str
    best: BonusBackValuation | None
    ranked: list[BonusBackValuation]
    reason: str


def _score(v: BonusBackValuation, objective: str) -> float | None:
    if objective == "ev":
        return v.ev_back_conservative
    if objective == "lock":
        return v.lock_profit
    if objective == "growth":
        return v.log_growth
    raise ValueError(f"unknown objective {objective!r}; choose from {OBJECTIVES}")


def pick_best(
    valuations: list[BonusBackValuation], objective: str, terms: BonusBackTerms
) -> RacePick:
    """The single runner to put the promo bet on, or none.

    Ineligible rows (beyond the model's win-price cap) are ranked but never
    picked. A pick must clear ``terms.min_ev`` on the chosen objective.
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown objective {objective!r}; choose from {OBJECTIVES}")
    scored = [(v, _score(v, objective)) for v in valuations]
    ranked = [v for v, s in sorted(
        scored, key=lambda t: (t[1] is None, -(t[1] if t[1] is not None else 0.0))
    )]
    candidates = [(v, s) for v, s in scored if v.eligible and s is not None]
    if not candidates:
        why = (
            "no runner has both Betfair win and 3-place lay prices"
            if objective == "lock" else "no eligible runner"
        )
        return RacePick(objective, None, ranked, why)
    best, s = max(candidates, key=lambda t: t[1])
    # Growth ranks by log growth but the minimum is still an EV per $1, so
    # the bar means the same thing whichever objective chose the runner.
    s_show = best.ev_back_conservative if objective == "growth" else s
    if s_show < terms.min_ev:
        return RacePick(
            objective, None, ranked,
            f"best is {best.horse_name} at {s_show:+.3f} per $1, below the "
            f"{terms.min_ev:+.3f} minimum",
        )
    return RacePick(
        objective, best, ranked,
        f"{best.horse_name}: {s_show:+.3f} per $1 ({objective})",
    )


# --- one race, end to end (no I/O) -------------------------------------------

@dataclass
class ExchangeRunner:
    """Betfair data for one Sportsbet runner, already matched by saddlecloth."""

    win_probability: float | None = None
    win_reliable: bool = False
    win_lay: float | None = None
    win_lay_size: float | None = None
    place_lay: float | None = None
    place_lay_size: float | None = None


@dataclass
class PromoRace:
    venue: str | None
    race_number: int | None
    start_time: object
    n_runners: int
    win_model: str
    sportsbet_overround: float | None
    valuations: list[BonusBackValuation]
    pick: RacePick
    betfair_delayed: bool = False
    place_market_winners: int | None = None


class PromoRaceError(Exception):
    """A race the promotion cannot be valued on (reported, not raised to the user)."""


def value_promo_race(
    race,
    calibration: Calibration,
    settings,
    terms: BonusBackTerms,
    objective: str = "ev",
    exchange: list[ExchangeRunner] | None = None,
    betfair_delayed: bool = False,
    place_market_winners: int | None = None,
) -> PromoRace:
    """Value every active runner of ``race`` under the promotion and pick one.

    ``exchange`` is per active runner, in ``race.active_runners()`` order.
    The win model priority is the place scanner's: Betfair midpoints where
    every runner has a reliable one, otherwise the Sportsbet de-vig. Every
    model available is kept so the conservative EV can be taken.
    """
    from app.engine import band_source_for
    from app.winprob import (
        best_model,
        betfair_win_probabilities,
        max_win_price_for,
        sportsbet_win_probabilities,
    )

    active = race.active_runners()
    n = len(active)
    if n < 3:
        raise PromoRaceError(f"{race.venue} R{race.race_number}: only {n} active runners")
    if any(not r.win_price or r.win_price <= 1.0 for r in active):
        raise PromoRaceError(
            f"{race.venue} R{race.race_number}: a runner has no live win price — "
            f"market in motion"
        )
    prices = [r.win_price for r in active]
    models = {}
    sb = sportsbet_win_probabilities(prices, calibration, settings.beta_min_races)
    models[sb.win_model] = sb
    if exchange is not None:
        bf = betfair_win_probabilities(
            [e.win_probability for e in exchange],
            [e.win_reliable for e in exchange],
            betfair_delayed,
        )
        if bf is not None:
            models[bf.win_model] = bf
    chosen = best_model(models) or sb.win_model

    finish = {}
    for name, mset in models.items():
        fp, p23 = promo_p23(mset.probabilities, calibration, band_source_for(name), prices)
        finish[name] = (fp, p23)

    cap, _ = max_win_price_for(chosen, betfair_delayed and chosen == "betfair", settings)
    vals: list[BonusBackValuation] = []
    for i, runner in enumerate(active):
        fp, p23 = finish[chosen]
        ex = exchange[i] if exchange is not None else ExchangeRunner()
        inputs = RunnerInputs(
            horse_name=runner.horse_name,
            saddlecloth=runner.saddlecloth,
            jockey_name=runner.jockey_name,
            win_price=runner.win_price,
            p_win=fp.p_first[i],
            p_second=fp.p_second[i],
            p_third=fp.p_third[i],
            p23=p23[i],
            win_model=chosen,
            alt_models={
                name: (finish[name][0].p_first[i], finish[name][1][i])
                for name in models if name != chosen
            },
            bf_win_lay=ex.win_lay,
            bf_win_lay_size=ex.win_lay_size,
            bf_place_lay=ex.place_lay,
            bf_place_lay_size=ex.place_lay_size,
            bf_place_winners=place_market_winners,
            max_win_price=cap,
        )
        v = value_runner(inputs, terms, settings.bankroll, settings.kelly_fraction)
        if models[chosen].uncalibrated:
            v.notes.append(f"{chosen} probabilities are UNCALIBRATED")
        vals.append(v)
    return PromoRace(
        venue=race.venue,
        race_number=race.race_number,
        start_time=race.start_time,
        n_runners=n,
        win_model=chosen,
        sportsbet_overround=sb.overround,
        valuations=vals,
        pick=pick_best(vals, objective, terms),
        betfair_delayed=betfair_delayed and chosen == "betfair",
        place_market_winners=place_market_winners,
    )
