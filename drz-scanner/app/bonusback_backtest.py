"""``python -m app.bonusback_backtest`` — test the bonus-back model on real results.

Two questions, answered on the Betfair ANZ thoroughbred history (the same
files ``app.calibrate`` fits on), restricted to races where three runners
placed, so "2nd or 3rd" is observable as *placed and did not win*:

1. **Is P(2nd or 3rd) calibrated?** Predicted against actual by win-price
   band and by probability bucket, and log-loss against plain Harville.
   This part uses nothing but BSP and results: it is measurement.

2. **Which selection rule realises the most from the promotion?** Here an
   assumption is unavoidable, because nobody publishes historical Sportsbet
   prices. Sportsbet's win price is *simulated* from the BSP probabilities
   with a power margin (``implied_i = q_i ** k``, ``k`` solved so the book
   sums to the chosen overround), which loads the margin onto longshots the
   way corporate bookmakers do. Outcomes are real. The answer is therefore
   "given a book of this shape, this rule picks the horse that pays", and it
   is reported for several overrounds and bonus conversion rates so you can
   read off the one that matches what you see. Once the live scanner has
   stored enough real Sportsbet prices, ``--stored-prices`` will be the
   honest version of this test; until then this is the best available.

Only the last ``--holdout-days`` (default 365) are used, which is after the
calibration's fit window, so every number here is out-of-sample.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date

import numpy as np

from app.calibration import load_calibration
from app.history import HistorySchemaError, HistoryUnavailableError, RaceArrays, load_history
from app.logging_setup import setup_logging
from app.place_model import place_probabilities_batch

log = logging.getLogger("app.bonusback_backtest")

CHUNK = 4000
PRICE_BANDS = ((1, 2), (2, 3), (3, 5), (5, 9), (9, 15), (15, 21), (21, 51), (51, 1001))
P23_BUCKETS = ((0, .05), (.05, .1), (.1, .15), (.15, .2), (.2, .25), (.25, .3),
               (.3, .35), (.35, .4), (.4, 1.0))
RULES = ("model_ev", "lowest_breakeven", "favourite", "second_favourite",
         "max_p23", "random")


@dataclass
class Promo:
    q: np.ndarray        # (R, N) BSP win probabilities
    mask: np.ndarray
    won: np.ndarray
    y23: np.ndarray      # placed and did not win
    p23: np.ndarray      # calibrated model
    p23_harville: np.ndarray
    win_bsp: np.ndarray


def three_place_races(a: RaceArrays) -> RaceArrays:
    sel = a.places == 3
    return RaceArrays(**{
        k: (v[sel] if isinstance(v, np.ndarray) else v.loc[sel].reset_index(drop=True))
        for k, v in a.__dict__.items()
    })


def model_p23(a: RaceArrays, calibration, apply_corrections: bool = True,
              lam: float | None = None, tau: float | None = None) -> np.ndarray:
    """Vectorised version of :func:`app.bonusback.promo_p23` for a batch."""
    lam = calibration.lam if lam is None else lam
    tau = calibration.tau if tau is None else tau
    top3 = np.empty_like(a.q)
    three = np.full(a.n_races, 3)
    for lo in range(0, a.n_races, CHUNK):
        hi = min(lo + CHUNK, a.n_races)
        top3[lo:hi] = place_probabilities_batch(
            a.q[lo:hi], a.mask[lo:hi], three[lo:hi], lam, tau
        )
    if apply_corrections:
        for b in calibration.win_prob_bucket_correction:
            sel = (a.q >= b["lo"]) & (a.q < b["hi"]) & a.mask
            top3 = np.where(sel, np.clip(top3 + b["delta"], 1e-6, 1 - 1e-6), top3)
        shrink = np.ones_like(a.q)
        for band in calibration.band_ratio.get("betfair", []):
            sel = (a.win_bsp > band["lo"]) & (a.win_bsp <= band["hi"]) & a.mask
            shrink = np.where(sel, min(1.0, float(band["ratio"])), shrink)
        top3 = top3 * shrink
    return np.where(a.mask, np.maximum(top3 - a.q, 0.0), 0.0)


def build(a: RaceArrays, calibration) -> Promo:
    a = three_place_races(a)
    return Promo(
        q=a.q, mask=a.mask, won=a.won,
        y23=a.placed & ~a.won & a.mask,
        p23=model_p23(a, calibration),
        p23_harville=model_p23(a, calibration, apply_corrections=False, lam=1.0, tau=1.0),
        win_bsp=a.win_bsp,
    )


def log_loss(p: np.ndarray, y: np.ndarray, mask: np.ndarray) -> float:
    p = np.clip(p[mask], 1e-9, 1 - 1e-9)
    yy = y[mask].astype(float)
    return float(-(yy * np.log(p) + (1 - yy) * np.log(1 - p)).mean())


def calibration_rows(pr: Promo, by: str) -> list[dict]:
    rows = []
    if by == "price":
        key, buckets = pr.win_bsp, PRICE_BANDS
        inside = lambda lo, hi: (key > lo) & (key <= hi)  # noqa: E731
    else:
        key, buckets = pr.p23, P23_BUCKETS
        inside = lambda lo, hi: (key >= lo) & (key < hi)  # noqa: E731
    for lo, hi in buckets:
        sel = inside(lo, hi) & pr.mask
        n = int(sel.sum())
        if n == 0:
            continue
        pred = float(pr.p23[sel].mean())
        harv = float(pr.p23_harville[sel].mean())
        act = float(pr.y23[sel].mean())
        se = float(np.sqrt(act * (1 - act) / n))
        rows.append(dict(lo=lo, hi=hi, n=n, predicted=pred, harville=harv,
                         actual=act, se=se))
    return rows


def simulated_prices(q: np.ndarray, mask: np.ndarray, overround: float,
                     shape: str = "power") -> np.ndarray:
    """Bookmaker win prices summing to ``overround``.

    ``power``: ``1 / q**k`` with ``sum q**k = R`` — margin loaded onto
    longshots, the usual corporate-bookmaker shape. ``flat``: ``1 / (q * R)``
    — the same margin on every runner, a sensitivity check.
    """
    if shape == "flat":
        return np.where(mask, 1.0 / np.where(mask, q * overround, 1.0), np.nan)
    lo = np.full(q.shape[0], 0.3)
    hi = np.full(q.shape[0], 1.0)
    qq = np.where(mask, q, 1.0)
    for _ in range(60):
        k = 0.5 * (lo + hi)
        tot = np.where(mask, qq ** k[:, None], 0.0).sum(axis=1)
        # smaller k -> larger total
        too_big = tot > overround
        lo = np.where(too_big, k, lo)
        hi = np.where(too_big, hi, k)
    k = 0.5 * (lo + hi)
    return np.where(mask, 1.0 / qq ** k[:, None], np.nan)


def choose(rule: str, pr: Promo, odds: np.ndarray, r: float, rng) -> np.ndarray:
    """Column index of the runner each race's rule backs."""
    neg = -np.inf
    if rule == "model_ev":
        score = pr.q * odds + pr.p23 * r - 1
    elif rule == "lowest_breakeven":
        score = -(1 - pr.q * odds) / np.maximum(pr.p23, 1e-9)
    elif rule == "favourite":
        score = pr.q.copy()
    elif rule == "second_favourite":
        order = np.argsort(np.where(pr.mask, -pr.q, np.inf), axis=1)
        return order[:, 1]
    elif rule == "max_p23":
        score = pr.p23.copy()
    elif rule == "random":
        n = pr.mask.sum(axis=1)
        return (rng.random(len(n)) * n).astype(int)
    else:
        raise ValueError(rule)
    return np.argmax(np.where(pr.mask, score, neg), axis=1)


def realise(pr: Promo, odds: np.ndarray, idx: np.ndarray, r: float) -> dict:
    rows = np.arange(len(idx))
    o = odds[rows, idx]
    won = pr.won[rows, idx]
    y23 = pr.y23[rows, idx]
    pnl = np.where(won, o - 1, -1.0) + np.where(y23, r, 0.0)
    pred = pr.q[rows, idx] * o + pr.p23[rows, idx] * r - 1
    return dict(
        n=len(idx), roi=float(pnl.mean()), se=float(pnl.std(ddof=1) / np.sqrt(len(idx))),
        predicted=float(pred.mean()), win_rate=float(won.mean()),
        rate23=float(y23.mean()), avg_odds=float(np.median(o)),
    )


def strategy_table(pr: Promo, overrounds, rates, seed: int = 7,
                   shape: str = "power") -> list[dict]:
    out = []
    for R in overrounds:
        odds = simulated_prices(pr.q, pr.mask, R, shape)
        for r in rates:
            for rule in RULES:
                rng = np.random.default_rng(seed)
                res = realise(pr, odds, choose(rule, pr, odds, r, rng), r)
                out.append(dict(overround=R, r=r, rule=rule, **res))
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.bonusback_backtest", description=__doc__.split("\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 31))
    p.add_argument("--holdout-days", type=int, default=365)
    p.add_argument("--overround", type=float, nargs="+", default=[1.12, 1.16, 1.20])
    p.add_argument("--bonus-value", type=float, nargs="+", default=[0.6, 0.7, 0.8])
    p.add_argument("--margin-shape", choices=("power", "flat"), default="power",
                   help="how the simulated Sportsbet margin is spread over the field")
    p.add_argument("--no-download", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.INFO)
    cal = load_calibration()
    try:
        a = load_history(args.start, args.end, download_missing=not args.no_download)
    except (HistoryUnavailableError, HistorySchemaError) as exc:
        print(f"history unavailable: {exc}")
        return 2
    cutoff = a.dates.max() - np.timedelta64(args.holdout_days, "D")
    sel = a.dates > cutoff
    a = RaceArrays(**{
        k: (v[sel] if isinstance(v, np.ndarray) else v.loc[sel].reset_index(drop=True))
        for k, v in a.__dict__.items()
    })
    pr = build(a, cal)
    print(f"\nOut-of-sample races with 3 placings: {pr.q.shape[0]:,} "
          f"(runners {int(pr.mask.sum()):,}), after {str(cutoff)}")
    print(f"log-loss P(2nd or 3rd): model {log_loss(pr.p23, pr.y23, pr.mask):.5f}  "
          f"plain Harville {log_loss(pr.p23_harville, pr.y23, pr.mask):.5f}")

    for by in ("price", "p23"):
        print(f"\nP(2nd or 3rd) calibration by {'BSP win price' if by == 'price' else 'model p23'}:")
        print(f"{'bucket':>14} {'n':>7} {'model':>7} {'harv':>7} {'actual':>7} {'±se':>6} {'act/mod':>8}")
        for row in calibration_rows(pr, by):
            print(f"{row['lo']:>6}-{row['hi']:<7} {row['n']:>7} {row['predicted']:>7.3f} "
                  f"{row['harville']:>7.3f} {row['actual']:>7.3f} {row['se']:>6.3f} "
                  f"{row['actual'] / row['predicted']:>8.3f}")

    print(f"\nSelection rules, one promo bet per race, simulated Sportsbet prices "
          f"({args.margin_shape} margin), REAL outcomes. roi = realised profit per $1 "
          f"incl. bonus at r.")
    print(f"{'R':>5} {'r':>4} {'rule':>18} {'roi':>8} {'±se':>6} {'pred':>7} "
          f"{'win%':>6} {'2/3%':>6} {'med$':>6}")
    for row in strategy_table(pr, args.overround, args.bonus_value, shape=args.margin_shape):
        print(f"{row['overround']:>5.2f} {row['r']:>4.2f} {row['rule']:>18} "
              f"{row['roi']:>+8.4f} {row['se']:>6.4f} {row['predicted']:>+7.4f} "
              f"{row['win_rate']:>6.1%} {row['rate23']:>6.1%} {row['avg_odds']:>6.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
