"""Choosing win probabilities for a race, in a fixed priority order.

This module exists because of the favourite-longshot bias. A bookmaker's
place price is derived from a win price, and the win price of a longshot
carries far more margin than that of a favourite. Feed raw implied
probabilities — or proportionally de-vigged ones, which is the same thing
scaled — into a place model and it will hand back big, confident, imaginary
edges on outsiders.

Priority, recorded per valuation as ``win_model``:

``betfair``
    Betfair win-market midpoints under the spread/liquidity gates, then
    normalised. Measured on 44,856 Australian races, the conditional-logit
    exponent on BSP-implied probabilities is 1.01 — indistinguishable from 1,
    so no bias correction is applied to exchange prices.

``sportsbet_beta``
    ``p_i = raw_i**B / sum_j raw_j**B`` with a single global ``B`` fitted by
    conditional-logit maximum likelihood on *our own* stored last-pre-jump
    Sportsbet win prices joined to results. Only used once the fit rests on
    at least ``BETA_MIN_RACES`` joined races.

``sportsbet_power``
    Power de-vig: solve ``k`` per race so ``sum(raw_i**k) == 1``. The default
    until the beta fit exists. Rows priced this way are labelled
    UNCALIBRATED, because ``k`` is chosen to balance the book rather than to
    match observed outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.calibration import Calibration
from app.devig import beta_probabilities, power_devig

WIN_MODEL_BETFAIR = "betfair"
WIN_MODEL_BETA = "sportsbet_beta"
WIN_MODEL_POWER = "sportsbet_power"

#: Models whose probabilities have never been checked against settled results.
UNCALIBRATED_MODELS = frozenset({WIN_MODEL_POWER})

#: Preference order, best first.
MODEL_PRIORITY = (WIN_MODEL_BETFAIR, WIN_MODEL_BETA, WIN_MODEL_POWER)


@dataclass(frozen=True)
class WinProbabilitySet:
    """Win probabilities for one race's active runners under one model."""

    win_model: str
    probabilities: tuple[float, ...]
    exponent: float | None = None
    overround: float | None = None
    detail: str = ""
    betfair_delayed: bool = False
    #: Runner-index -> reason, for runners the model could not price.
    gaps: dict[int, str] = field(default_factory=dict)

    @property
    def uncalibrated(self) -> bool:
        return self.win_model in UNCALIBRATED_MODELS


def sportsbet_win_probabilities(
    win_prices, calibration: Calibration, beta_min_races: int
) -> WinProbabilitySet:
    """Best available Sportsbet-priced model: fitted beta if we have it."""
    if calibration.beta_usable(beta_min_races):
        res = beta_probabilities(win_prices, calibration.beta)  # type: ignore[arg-type]
        return WinProbabilitySet(
            win_model=WIN_MODEL_BETA,
            probabilities=res.probabilities,
            exponent=res.exponent,
            overround=res.overround,
            detail=(
                f"conditional-logit beta={calibration.beta:.4f} fitted on "
                f"{calibration.beta_n_races} stored races ({calibration.beta_fitted_on})"
            ),
        )
    res = power_devig(win_prices)
    why = (
        "no fitted beta yet"
        if calibration.beta is None
        else f"beta fit rests on only {calibration.beta_n_races} races "
             f"(need {beta_min_races})"
    )
    return WinProbabilitySet(
        win_model=WIN_MODEL_POWER,
        probabilities=res.probabilities,
        exponent=res.exponent,
        overround=res.overround,
        detail=f"power de-vig k={res.exponent:.4f}; {why}",
    )


def betfair_win_probabilities(
    probabilities: list[float | None],
    reliable: list[bool],
    delayed: bool,
) -> WinProbabilitySet | None:
    """Normalised exchange probabilities, or ``None`` if unusable.

    Every active runner must carry a probability that passed its gate: a
    partially-priced exchange market cannot be normalised without inventing
    the missing runners' share, and this tool does not invent.
    """
    gaps = {
        i: "no reliable exchange price"
        for i, (p, ok) in enumerate(zip(probabilities, reliable))
        if p is None or not ok
    }
    if gaps or not probabilities:
        return None
    total = sum(p for p in probabilities if p is not None)
    if total <= 0:
        return None
    return WinProbabilitySet(
        win_model=WIN_MODEL_BETFAIR,
        probabilities=tuple((p or 0.0) / total for p in probabilities),
        overround=total,
        detail=(
            "Betfair win-market midpoints, normalised"
            + (" (DELAYED app key: spread-only gate)" if delayed else "")
        ),
        betfair_delayed=delayed,
    )


def max_win_price_for(
    win_model: str, betfair_delayed: bool, settings
) -> tuple[float, str]:
    """The win-price cap that applies to a model, and why.

    Betfair-derived probabilities stay calibrated far further out than
    Sportsbet-derived ones, so a single global cutoff would either throw away
    sound exchange-priced runners or admit unsound bookmaker-priced ones.
    """
    if win_model == WIN_MODEL_BETFAIR:
        if betfair_delayed:
            return (
                settings.max_win_price_betfair_delayed,
                "delayed exchange prices carry no matched volume",
            )
        return (
            settings.max_win_price_betfair,
            "exchange win probabilities are calibrated to about $51",
        )
    return (
        settings.max_win_price_sportsbet,
        "bookmaker win probabilities carry the longshot overround",
    )


def best_model(models: dict[str, WinProbabilitySet]) -> str | None:
    """The highest-priority model present in ``models``."""
    for name in MODEL_PRIORITY:
        if name in models:
            return name
    return None
