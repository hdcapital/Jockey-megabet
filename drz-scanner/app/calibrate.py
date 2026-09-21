"""``python -m app.calibrate`` — refit the model parameters from real results.

Two fits live here.

**lam / tau** (always): maximum likelihood of the observed placings under the
discounted-Harville model, using Betfair BSP as the win probabilities. The
likelihood is of (winner, *unordered* set of the other placed runners),
because the published history records who placed, not in what order::

    3 places: L = q_j * [ a_i/(A-a_j) * b_k/(B-b_j-b_i)
                        + a_k/(A-a_j) * b_i/(B-b_j-b_k) ]
    2 places: L = q_j * a_i/(A-a_j)

Trained on everything before the last 12 months, tested on the last 12.

**beta** (when possible): the conditional-logit exponent for *our own* stored
Sportsbet win prices, joined to these results on date + track + race number +
TAB number. Nobody sells historical Sportsbet prices, so this fit can only
ever use what this scanner has captured live — which is why every scan stores
``runner_prices`` whether or not it valued anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize, minimize_scalar

from app.calibration import Calibration, CalibrationError, load_calibration, write_calibration
from app.config import get_settings
from app.history import HistorySchemaError, HistoryUnavailableError, RaceArrays, load_history
from app.logging_setup import setup_logging
from app.place_model import place_probabilities_batch

log = logging.getLogger("app.calibrate")

#: Win-probability buckets for the residual correction and the report.
WIN_PROB_BUCKETS = ((0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
                    (0.20, 0.30), (0.30, 0.50), (0.50, 1.01))
#: Place-probability buckets for the published calibration table.
PLACE_PROB_BUCKETS = tuple(
    (lo / 100, hi / 100)
    for lo, hi in ((0, 5), (5, 10), (10, 20), (20, 30), (30, 40), (40, 50),
                   (50, 60), (60, 70), (70, 80), (80, 90), (90, 100))
)


@dataclass
class Split:
    train: np.ndarray
    test: np.ndarray


def time_split(arrays: RaceArrays, holdout_days: int = 365) -> Split:
    cutoff = arrays.dates.max() - np.timedelta64(holdout_days, "D")
    test = arrays.dates > cutoff
    return Split(train=~test, test=test)


def _subset(a: RaceArrays, sel: np.ndarray) -> RaceArrays:
    return RaceArrays(
        q=a.q[sel], mask=a.mask[sel], won=a.won[sel], placed=a.placed[sel],
        place_bsp=a.place_bsp[sel], win_bsp=a.win_bsp[sel],
        best_back=a.best_back[sel], best_lay=a.best_lay[sel], names=a.names[sel],
        tab_numbers=a.tab_numbers[sel], n_runners=a.n_runners[sel],
        places=a.places[sel], dates=a.dates[sel],
        keys=a.keys[sel].reset_index(drop=True),
    )


def _placing_indices(a: RaceArrays) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(winner, other placed #1, other placed #2 or -1) per race."""
    winner = a.won.argmax(axis=1)
    others = a.placed & ~a.won
    first = others.argmax(axis=1)
    # second other: the next True after ``first``
    second = np.full(a.n_races, -1, dtype=int)
    rows, cols = np.nonzero(others)
    for r in range(a.n_races):
        c = cols[rows == r]
        if c.size > 1:
            second[r] = c[1]
    return winner, first, second


def negative_log_likelihood(params: tuple[float, float], a: RaceArrays,
                            idx: tuple[np.ndarray, np.ndarray, np.ndarray]) -> float:
    lam, tau = params
    if not (0.01 < lam <= 3.0 and 0.01 < tau <= 3.0):
        return 1e12
    q, mask = a.q, a.mask
    alpha = np.where(mask, np.power(q, lam), 0.0)
    beta = np.where(mask, np.power(q, tau), 0.0)
    A = alpha.sum(axis=1)
    B = beta.sum(axis=1)
    j, i, k = idx
    r = np.arange(a.n_races)
    three = a.places == 3
    kk = np.where(k >= 0, k, 0)
    qj, aj, bj = q[r, j], alpha[r, j], beta[r, j]
    ai, bi = alpha[r, i], beta[r, i]
    ak = np.where(three, alpha[r, kk], 0.0)
    bk = np.where(three, beta[r, kk], 0.0)
    base = qj / (A - aj)
    l2 = base * ai
    l3 = base * (
        ai * bk / (B - bj - bi) + ak * bi / (B - bj - bk)
    )
    likelihood = np.where(three, l3, l2)
    return float(-np.log(np.maximum(likelihood, 1e-300)).sum())


def fit_lam_tau(a: RaceArrays, x0: tuple[float, float] = (0.7, 0.7)) -> tuple[float, float, float]:
    idx = _placing_indices(a)
    res = minimize(
        negative_log_likelihood, np.array(x0), args=(a, idx),
        method="Nelder-Mead",
        options={"xatol": 1e-4, "fatol": 1e-4, "maxiter": 2000},
    )
    lam, tau = float(res.x[0]), float(res.x[1])
    log.info("fit: lam=%.4f tau=%.4f nll=%.2f (%d races)", lam, tau, res.fun, a.n_races)
    return lam, tau, float(res.fun)


def fit_win_exponent(a: RaceArrays) -> float:
    """Conditional-logit exponent on BSP-implied win probabilities.

    A value of 1 means the exchange's implied probabilities are already
    calibrated and need no favourite-longshot correction.
    """
    def nll(b: float) -> float:
        p = np.where(a.mask, np.power(a.q, b), 0.0)
        p = p / p.sum(axis=1, keepdims=True)
        return float(-np.log(np.maximum(p[a.won], 1e-300)).sum())

    # Bounded rather than bracketed: a bracket that happens not to straddle
    # the minimum makes minimize_scalar raise, which would abort the whole
    # calibration over a detail of the starting guess.
    res = minimize_scalar(nll, bounds=(0.2, 3.0), method="bounded",
                          options={"xatol": 1e-5})
    return float(res.x)


def binary_log_loss(p: np.ndarray, y: np.ndarray, mask: np.ndarray) -> float:
    p = np.clip(p[mask], 1e-12, 1 - 1e-12)
    yy = y[mask].astype(float)
    return float(-(yy * np.log(p) + (1 - yy) * np.log(1 - p)).mean())


#: Races per chunk in :func:`evaluate`. The batch engine builds three
#: (chunk, N, N) float64 tensors, so a full 45,000-race pass at N=24 would
#: peak near a gigabyte — fine on a workstation, not fine on the laptop this
#: is meant to run on. Chunking costs nothing measurable and bounds it.
EVAL_CHUNK = 4000


def evaluate(a: RaceArrays, lam: float, tau: float,
             correction: list[dict[str, float]] | None = None) -> np.ndarray:
    p = np.empty_like(a.q)
    for lo in range(0, a.n_races, EVAL_CHUNK):
        hi = min(lo + EVAL_CHUNK, a.n_races)
        p[lo:hi] = place_probabilities_batch(
            a.q[lo:hi], a.mask[lo:hi], a.places[lo:hi], lam, tau
        )
    if correction:
        for bucket in correction:
            sel = (a.q >= bucket["lo"]) & (a.q < bucket["hi"]) & a.mask
            p = np.where(sel, np.clip(p + bucket["delta"], 1e-6, 1 - 1e-6), p)
    return p


def fit_bucket_correction(train: RaceArrays, lam: float, tau: float) -> list[dict[str, float]]:
    """Additive P(place) correction by win-probability bucket, from train only."""
    p = evaluate(train, lam, tau)
    out: list[dict[str, float]] = []
    for lo, hi in WIN_PROB_BUCKETS:
        sel = train.mask & (train.q >= lo) & (train.q < hi)
        if sel.sum() < 200:
            continue
        delta = float(train.placed[sel].mean() - p[sel].mean())
        out.append({"lo": float(lo), "hi": float(hi), "delta": round(delta, 6)})
    return out


def calibration_table(a: RaceArrays, p: np.ndarray) -> list[dict[str, float]]:
    rows = []
    for lo, hi in PLACE_PROB_BUCKETS:
        sel = a.mask & (p >= lo) & (p < hi)
        if sel.sum() < 50:
            continue
        rows.append({
            "bucket_lo": round(lo, 2),
            "bucket_hi": round(hi, 2),
            "n": int(sel.sum()),
            "predicted": round(float(p[sel].mean()), 4),
            "actual": round(float(a.placed[sel].mean()), 4),
        })
    return rows


def place_bsp_log_loss(a: RaceArrays, p: np.ndarray) -> float | None:
    """Log-loss of place-BSP-implied probabilities, normalised to the terms."""
    valid = a.mask & np.isfinite(a.place_bsp) & (a.place_bsp > 1)
    if valid.sum() < 1000:
        return None
    implied = np.zeros_like(a.place_bsp)
    implied[valid] = 1.0 / a.place_bsp[valid]
    total = implied.sum(axis=1)
    scale = np.divide(a.places, total, out=np.zeros_like(total, dtype=float), where=total > 0)
    return binary_log_loss(np.clip(implied * scale[:, None], 1e-12, 1 - 1e-12), a.placed, valid)


# ---------------------------------------------------------------------------
# Win-price band calibration
# ---------------------------------------------------------------------------

#: Win-price bands, as (exclusive low, inclusive high) decimal odds.
WIN_PRICE_BANDS = (
    (1.0, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 9.0), (9.0, 15.0),
    (15.0, 21.0), (21.0, 31.0), (31.0, 51.0), (51.0, 101.0), (101.0, 1e9),
)

#: A band needs this many runners before its ratio is trusted at all.
MIN_BAND_RUNNERS = 300

#: The cap is drawn where the bands stop holding. A band "holds" at 0.97 or
#: better: the model may be a little cold there, but it is not selling
#: probability it does not have.
BAND_HOLDS_AT = 0.97

#: The cap search starts here — below it the model is knowingly 2-3% hot, and
#: that is a shrink the band correction handles rather than a reason to cap.
CAP_SEARCH_FROM = 3.0

#: Runner-level relative spread allowed in the live-style table.
LIVE_MAX_RELATIVE_SPREAD = 0.10


def band_table(
    a: RaceArrays,
    lam: float,
    tau: float,
    q: np.ndarray,
    price: np.ndarray,
    evaluate_mask: np.ndarray,
) -> list[dict[str, float]]:
    """Actual place rate divided by modelled place probability, per band.

    ``q`` are the normalised win probabilities to model from, ``price`` the
    decimal win price each runner is banded by, and ``evaluate_mask`` picks
    which runners count towards the ratio (the whole book is always used to
    normalise, because a partial book cannot be normalised at all).
    """
    p = np.empty_like(q)
    for lo in range(0, a.n_races, EVAL_CHUNK):
        hi = min(lo + EVAL_CHUNK, a.n_races)
        p[lo:hi] = place_probabilities_batch(
            q[lo:hi], a.mask[lo:hi], a.places[lo:hi], lam, tau
        )
    rows: list[dict[str, float]] = []
    for lo, hi in WIN_PRICE_BANDS:
        sel = evaluate_mask & (price > lo) & (price <= hi)
        n = int(sel.sum())
        if n < MIN_BAND_RUNNERS:
            continue
        modelled = float(p[sel].mean())
        actual = float(a.placed[sel].mean())
        rows.append({
            "lo": lo,
            "hi": hi,
            "n": n,
            "actual": round(actual, 4),
            "modelled": round(modelled, 4),
            "ratio": round(actual / modelled, 4) if modelled > 0 else 0.0,
        })
    return rows


def bsp_bands(a: RaceArrays, lam: float, tau: float) -> list[dict[str, float]]:
    """Band table from Betfair starting prices — the full sample."""
    raw = np.where(a.mask & np.isfinite(a.win_bsp) & (a.win_bsp > 1), 1.0 / a.win_bsp, 0.0)
    total = raw.sum(axis=1, keepdims=True)
    q = np.divide(raw, total, out=np.zeros_like(raw), where=total > 0)
    price = np.where(a.mask, np.nan_to_num(a.win_bsp), 0.0)
    return band_table(a, lam, tau, q, price, a.mask)


def live_style_bands(a: RaceArrays, lam: float, tau: float) -> list[dict[str, float]]:
    """Band table from the prices actually showing at the scheduled off.

    The probability used is the midpoint *in probability space* of best back
    and best lay — the same quantity the scanner derives live. Normalisation
    uses every runner that has both sides, because a book missing a runner
    cannot be normalised; the ratio is then measured only over runners whose
    own relative spread is within ``LIVE_MAX_RELATIVE_SPREAD``, which is the
    reliability gate the scanner itself applies per runner.
    """
    have = (
        a.mask
        & np.isfinite(a.best_back) & np.isfinite(a.best_lay)
        & (a.best_back > 1) & (a.best_lay > 1)
    )
    complete = (have | ~a.mask).all(axis=1)
    if complete.sum() < 1000:
        log.warning("live-style band table: only %d complete books", complete.sum())
        return []
    sub = _subset(a, complete)
    have = have[complete]
    mid = np.where(have, 0.5 * (1.0 / a.best_back[complete] + 1.0 / a.best_lay[complete]), 0.0)
    total = mid.sum(axis=1, keepdims=True)
    q = np.divide(mid, total, out=np.zeros_like(mid), where=total > 0)
    price = np.divide(1.0, mid, out=np.zeros_like(mid), where=mid > 0)
    spread = np.divide(
        a.best_lay[complete] - a.best_back[complete],
        a.best_back[complete],
        out=np.full_like(mid, np.inf),
        where=have,
    )
    return band_table(sub, lam, tau, q, price, have & (spread <= LIVE_MAX_RELATIVE_SPREAD))


def recommended_cap(bands: list[dict[str, float]]) -> float | None:
    """Top edge of the highest band that holds, with every band below it.

    Starting at ``CAP_SEARCH_FROM``, walk up while each band's ratio is at
    least ``BAND_HOLDS_AT``; the answer is the top edge of the last one that
    did. ``None`` when even the first band fails.
    """
    cap: float | None = None
    for band in bands:
        if band["lo"] < CAP_SEARCH_FROM:
            continue
        if band["ratio"] < BAND_HOLDS_AT:
            break
        cap = band["hi"]
    return cap


# ---------------------------------------------------------------------------
# beta fit: our own stored Sportsbet prices joined to these results
# ---------------------------------------------------------------------------

@dataclass
class BetaFit:
    beta: float | None
    n_races: int
    n_unmatched_races: int
    n_name_mismatches: int
    detail: str


def _norm_track(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _norm_name(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def fit_beta_from_stored_prices(a: RaceArrays, db_url: str | None = None) -> BetaFit:
    """Join stored last-pre-jump Sportsbet win prices to results and fit B.

    The join key is date + track + race number + TAB (saddlecloth) number.
    Selection names are compared as a *check*: a mismatch is counted and the
    runner dropped. Nothing is ever guessed onto a runner that did not match.
    """
    import pandas as pd
    from sqlalchemy import select

    from app.database import models as m
    from app.database.repository import session_factory

    Session = session_factory(db_url)
    with Session() as session:
        rows = session.execute(
            select(
                m.RunnerPrice.observed_at, m.RunnerPrice.win_price,
                m.RunnerPrice.seconds_to_jump, m.RunnerPrice.status,
                m.Runner.saddlecloth, m.Runner.horse_name,
                m.Race.race_number, m.Race.start_time,
                m.Meeting.venue, m.Meeting.meeting_date,
            )
            .join(m.Runner, m.Runner.runner_id == m.RunnerPrice.runner_id)
            .join(m.Race, m.Race.race_id == m.RunnerPrice.race_id)
            .join(m.Meeting, m.Meeting.meeting_id == m.Race.meeting_id)
            .where(m.RunnerPrice.status == "active", m.RunnerPrice.win_price.isnot(None))
        ).all()

    if not rows:
        return BetaFit(None, 0, 0, 0, "no stored Sportsbet prices yet")

    df = pd.DataFrame(rows, columns=[
        "observed_at", "win_price", "seconds_to_jump", "status", "saddlecloth",
        "horse_name", "race_number", "start_time", "venue", "meeting_date",
    ])
    df = df[df["saddlecloth"].notna() & df["meeting_date"].notna()]
    if df.empty:
        return BetaFit(None, 0, 0, 0, "stored prices carry no saddlecloth/meeting date")
    # Last observation before the jump for each runner.
    df = df.sort_values("observed_at").groupby(
        ["meeting_date", "venue", "race_number", "saddlecloth"], as_index=False
    ).last()
    df["track_key"] = df["venue"].map(_norm_track)
    df["date_key"] = pd.to_datetime(df["meeting_date"]).dt.strftime("%Y-%m-%d")

    hist = a.keys.copy()
    hist["race_index"] = np.arange(a.n_races)
    hist["track_key"] = hist["TRACK"].map(_norm_track)
    hist["date_key"] = pd.to_datetime(hist["meeting_date"]).dt.strftime("%Y-%m-%d")

    merged = df.merge(
        hist, left_on=["date_key", "track_key", "race_number"],
        right_on=["date_key", "track_key", "RACE_NO"], how="left",
    )
    unmatched = int(merged["race_index"].isna().sum())
    merged = merged[merged["race_index"].notna()]
    if merged.empty:
        return BetaFit(None, 0, unmatched, 0,
                       "no stored race matched a history race on date+track+race number")

    # Join by TAB number, and CHECK the runner name. The key (date + track +
    # race number + TAB) is strong but not unique-by-construction: two venues
    # can normalise to the same track key and a meeting can be renumbered. A
    # name that does not match means the pairing is wrong, so the runner is
    # dropped and counted — never quietly accepted.
    name_mismatch = 0
    per_race: dict[int, list[tuple[int, float]]] = {}
    for _, r in merged.iterrows():
        ri = int(r["race_index"])
        tab = int(r["saddlecloth"])
        hist_names = {
            int(t): _norm_name(n)
            for t, n in zip(a.tab_numbers[ri], a.names[ri])
            if int(t) > 0
        }
        expected = hist_names.get(tab)
        if expected is None:
            name_mismatch += 1
            continue
        if _norm_name(r["horse_name"]) != expected:
            name_mismatch += 1
            continue
        per_race.setdefault(ri, []).append((tab, float(r["win_price"])))

    # Assemble padded arrays of (stored price, did-win) using TAB order, which
    # is the order ``prepare`` sorted the history rows into.
    races, prices, wins = [], [], []
    for ri, entries in per_race.items():
        n = int(a.n_runners[ri])
        if len(entries) != n:
            continue  # a partially captured race cannot be normalised
        entries.sort()
        races.append(ri)
        prices.append([p for _, p in entries])
        wins.append(a.won[ri][:n])
    if len(races) == 0:
        return BetaFit(None, 0, unmatched, name_mismatch,
                       "no fully-captured race matched a history race")

    max_n = max(len(p) for p in prices)
    P = np.zeros((len(races), max_n))
    W = np.zeros((len(races), max_n), dtype=bool)
    M = np.zeros((len(races), max_n), dtype=bool)
    for r, (pr, wn) in enumerate(zip(prices, wins)):
        P[r, : len(pr)] = 1.0 / np.array(pr)
        W[r, : len(wn)] = wn
        M[r, : len(pr)] = True

    def nll(b: float) -> float:
        p = np.where(M, np.power(P, b), 0.0)
        p = p / p.sum(axis=1, keepdims=True)
        return float(-np.log(np.maximum(p[W], 1e-300)).sum())

    res = minimize_scalar(nll, bounds=(0.2, 3.0), method="bounded",
                          options={"xatol": 1e-5})
    return BetaFit(
        beta=float(res.x),
        n_races=len(races),
        n_unmatched_races=unmatched,
        n_name_mismatches=name_mismatch,
        detail=(
            f"conditional-logit fit on {len(races)} joined races"
            + (f"; {name_mismatch} runner(s) dropped on a name mismatch"
               if name_mismatch else "; every joined runner's name matched")
        ),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.calibrate",
        description="Refit lam/tau (and beta, if stored prices allow) from real results.",
    )
    p.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1),
                   help="first meeting date to use (default 2024-01-01)")
    p.add_argument("--end", type=date.fromisoformat, default=date.today(),
                   help="last meeting date to use (default today)")
    p.add_argument("--holdout-days", type=int, default=365,
                   help="size of the out-of-sample window (default 365)")
    p.add_argument("--out", type=Path, default=None, help="calibration.json path")
    p.add_argument("--force", action="store_true",
                   help="write even if out-of-sample log-loss got worse")
    p.add_argument("--no-download", action="store_true",
                   help="use only history files already cached under data/history/")
    p.add_argument("--skip-beta", action="store_true", help="do not attempt the beta fit")
    p.add_argument("--dry-run", action="store_true", help="report, do not write")
    p.add_argument("--lam", type=float, default=None,
                   help="pin lam instead of fitting it (the metrics are still "
                        "measured at the pinned value)")
    p.add_argument("--tau", type=float, default=None, help="pin tau; see --lam")
    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    settings = get_settings()

    try:
        arrays = load_history(args.start, args.end, download_missing=not args.no_download)
    except (HistoryUnavailableError, HistorySchemaError) as exc:
        log.error("calibration aborted: %s", exc)
        print(f"\nCalibration could not run: {exc}\n"
              f"The shipped data/calibration.json is unchanged.", file=sys.stderr)
        return 2

    split = time_split(arrays, args.holdout_days)
    train, test = _subset(arrays, split.train), _subset(arrays, split.test)
    log.info("races: %d total, %d train, %d test", arrays.n_races, train.n_races, test.n_races)
    if train.n_races < 1000 or test.n_races < 500:
        log.error("not enough races to fit (train=%d test=%d)", train.n_races, test.n_races)
        return 2

    fitted_lam, fitted_tau, _ = fit_lam_tau(train)
    lam = args.lam if args.lam is not None else fitted_lam
    tau = args.tau if args.tau is not None else fitted_tau
    pinned = (args.lam is not None) or (args.tau is not None)
    if pinned:
        log.info("using pinned lam=%.4f tau=%.4f (free fit gave %.4f/%.4f)",
                 lam, tau, fitted_lam, fitted_tau)
    win_exponent = fit_win_exponent(arrays)

    p_model = evaluate(test, lam, tau)
    p_harville = evaluate(test, 1.0, 1.0)
    ll_model = binary_log_loss(p_model, test.placed, test.mask)
    ll_harville = binary_log_loss(p_harville, test.placed, test.mask)

    correction = fit_bucket_correction(train, lam, tau)
    p_corrected = evaluate(test, lam, tau, correction)
    ll_corrected = binary_log_loss(p_corrected, test.placed, test.mask)
    adopt = ll_corrected < ll_model
    if not adopt:
        log.info("bucket correction rejected: %.6f vs %.6f out of sample",
                 ll_corrected, ll_model)
        correction = []
    else:
        log.info("bucket correction adopted: %.6f vs %.6f out of sample",
                 ll_corrected, ll_model)

    ll_place_bsp = place_bsp_log_loss(test, p_model)
    table = calibration_table(test, p_corrected if adopt else p_model)

    # --- win-price band calibration (training window only) ----------------
    bsp_band_rows = bsp_bands(train, lam, tau)
    live_band_rows = live_style_bands(train, lam, tau)
    cap = recommended_cap(live_band_rows) if live_band_rows else None

    cal = Calibration(
        lam=round(lam, 4),
        tau=round(tau, 4),
        version=f"lamtau-{datetime.now(timezone.utc):%Y%m%d}-n{arrays.n_races}",
        fitted_on=datetime.now(timezone.utc).date().isoformat(),
        n_races=int(arrays.n_races),
        date_range=(str(arrays.dates.min()), str(arrays.dates.max())),
        out_of_sample={
            "n_races": int(test.n_races),
            "logloss_model": round(ll_corrected if adopt else ll_model, 6),
            "logloss_model_uncorrected": round(ll_model, 6),
            "logloss_plain_harville": round(ll_harville, 6),
            "logloss_place_bsp_implied": (
                round(ll_place_bsp, 6) if ll_place_bsp is not None else None
            ),
            "bsp_win_exponent": round(win_exponent, 4),
            "bucket_correction_adopted": bool(adopt),
            "free_fit_lam": round(fitted_lam, 4),
            "free_fit_tau": round(fitted_tau, 4),
        },
        calibration_table=table,
        win_prob_bucket_correction=correction,
        band_ratio={"betfair": bsp_band_rows} if bsp_band_rows else {},
        recommended_max_win_price=(
            {"betfair": cap} if cap is not None else {}
        ),
        provenance=(
            "Fitted by maximum likelihood on Betfair Australia historical BSP "
            "(betfair-datascientists ANZ_Thoroughbreds), thoroughbreds only, "
            "New Zealand excluded, every runner priced, exactly one winner, "
            "2 or 3 placed runners including the winner, 5+ runners. Trained "
            f"on meetings before the final {args.holdout_days} days, tested on "
            "the final window. Reproduce with `python -m app.calibrate`."
            + (
                f" Shipped lam/tau are pinned to {lam}/{tau}; the unconstrained "
                f"fit on this data gave {fitted_lam:.4f}/{fitted_tau:.4f}. The "
                f"likelihood surface is flat between them, so the rounder "
                f"numbers are shipped for legibility."
                if pinned else ""
            )
        ),
    )

    existing = load_calibration(args.out)
    print()
    print(f"  races used            {arrays.n_races:,} "
          f"({arrays.dates.min()} .. {arrays.dates.max()})")
    print(f"  lam / tau             {lam:.4f} / {tau:.4f}"
          + (f"   (pinned; free fit {fitted_lam:.4f}/{fitted_tau:.4f})" if pinned else ""))
    print(f"  BSP win exponent      {win_exponent:.4f}   (1.0 = no FLB correction needed)")
    print(f"  out-of-sample races   {test.n_races:,}")
    print(f"  log-loss  model       {ll_model:.4f}")
    if adopt:
        print(f"  log-loss  + bucket    {ll_corrected:.4f}   (adopted)")
    else:
        print(f"  log-loss  + bucket    {ll_corrected:.4f}   (rejected, no improvement)")
    print(f"  log-loss  Harville    {ll_harville:.4f}")
    if ll_place_bsp is not None:
        print(f"  log-loss  place BSP   {ll_place_bsp:.4f}   (the exchange's own opinion)")
    print(f"  current file          lam {existing.lam:.4f} tau {existing.tau:.4f} "
          f"({existing.version})")

    if bsp_band_rows:
        print()
        print("  win-price bands (training window; actual place rate / model)")
        print(f"    {'band':>11} {'n':>7} {'BSP':>6} {'live':>6}")
        live_by_band = {(b["lo"], b["hi"]): b for b in live_band_rows}
        for b in bsp_band_rows:
            name = (f"({b['lo']:g},{b['hi']:g}]" if b["hi"] < 1e9
                    else f"({b['lo']:g}+]")
            live = live_by_band.get((b["lo"], b["hi"]))
            live_cell = f"{live['ratio']:.2f}" if live else "-"
            print(f"    {name:>11} {b['n']:7,} {b['ratio']:6.2f} {live_cell:>6}")
        shrinking = [b for b in bsp_band_rows if b["ratio"] < 1.0]
        print(f"    {len(shrinking)} band(s) shrink p_place; bands above 1.0 are "
              f"applied as a no-op (never scaled up)")
    if cap is not None:
        configured = settings.max_win_price_betfair
        print()
        print(f"  recommended max win price (betfair)  {cap:.0f}"
              f"   configured {configured:.0f}")
        if cap < configured:
            print(f"  WARNING: the bands stop holding at {cap:.0f}, below the "
                  f"configured {configured:.0f}. Lower MAX_WIN_PRICE_BETFAIR, or "
                  f"accept that rows between {cap:.0f} and {configured:.0f} rest "
                  f"on probabilities this data does not validate.")
            log.warning("recommended cap %.0f is below configured %.0f", cap, configured)
        print("  (advisory only — the configured value is never changed for you)")

    if not args.skip_beta:
        try:
            bf = fit_beta_from_stored_prices(arrays)
            if bf.beta is not None:
                cal.beta = round(bf.beta, 4)
                cal.beta_n_races = bf.n_races
                cal.beta_fitted_on = datetime.now(timezone.utc).date().isoformat()
                usable = bf.n_races >= settings.beta_min_races
                print(f"  sportsbet beta        {bf.beta:.4f} on {bf.n_races} joined races "
                      f"({'in use' if usable else f'need {settings.beta_min_races}'})")
                if bf.n_unmatched_races:
                    print(f"    unmatched races     {bf.n_unmatched_races} (recorded, never guessed)")
                if bf.n_name_mismatches:
                    print(f"    name mismatches     {bf.n_name_mismatches} runner(s) dropped")
            else:
                print(f"  sportsbet beta        not fitted — {bf.detail}")
        except Exception as exc:  # a beta failure must not lose the lam/tau fit
            log.warning("beta fit skipped: %s", exc)
            print(f"  sportsbet beta        not fitted — {exc}")

    if args.dry_run:
        print("\n  --dry-run: nothing written\n")
        return 0
    try:
        path = write_calibration(cal, args.out, force=args.force)
    except CalibrationError as exc:
        print(f"\n  NOT WRITTEN: {exc}\n", file=sys.stderr)
        return 3
    print(f"\n  written to {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
