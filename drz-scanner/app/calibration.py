"""Loading, validating and writing ``data/calibration.json``.

The file is the single source of truth for every fitted parameter the
scanner uses:

* ``lam`` / ``tau`` — the discounted-Harville exponents.
* ``win_prob_bucket_correction`` — optional additive correction to P(place)
  by win-probability bucket, adopted only when it improved out-of-sample
  log-loss at fit time.
* ``beta`` — the conditional-logit exponent for Sportsbet win prices, present
  only once enough of our own stored prices have been joined to results.

Nothing falls back to a hard-coded number silently: if the file is missing or
unreadable the scanner says so and uses the shipped defaults, and every stored
valuation records which calibration version produced it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

# Shipped fallbacks, used only when data/calibration.json cannot be read.
# These are the values in the shipped file; duplicating them here means a
# corrupt file degrades to "documented defaults" rather than to a crash.
DEFAULT_LAM = 0.71
DEFAULT_TAU = 0.70


@dataclass
class Calibration:
    lam: float = DEFAULT_LAM
    tau: float = DEFAULT_TAU
    version: str = "shipped-default"
    fitted_on: str | None = None
    n_races: int | None = None
    date_range: tuple[str, str] | None = None
    out_of_sample: dict[str, Any] = field(default_factory=dict)
    calibration_table: list[dict[str, Any]] = field(default_factory=list)
    win_prob_bucket_correction: list[dict[str, float]] = field(default_factory=list)
    beta: float | None = None
    beta_n_races: int | None = None
    beta_fitted_on: str | None = None
    provenance: str = ""
    source_path: Path | None = None
    loaded_from_file: bool = False

    # -- derived ---------------------------------------------------------
    @property
    def age_days(self) -> float | None:
        if not self.fitted_on:
            return None
        try:
            d = date.fromisoformat(self.fitted_on[:10])
        except ValueError:
            return None
        return (datetime.now(timezone.utc).date() - d).days

    def beta_usable(self, min_races: int) -> bool:
        return self.beta is not None and (self.beta_n_races or 0) >= min_races

    def correct_place_probability(self, win_prob: float, p_place: float) -> float:
        """Apply the fitted win-probability-bucket correction, if any.

        Note that this is an *additive, per-runner* adjustment, so a race's
        corrected probabilities no longer sum exactly to its number of place
        dividends — typically a percent or two short, because the correction
        is negative for the strongest runners. That is deliberate. The
        correction is adopted only when it improves out-of-sample log-loss,
        and renormalising it away would remove the level shift that produced
        the improvement. The engine checks the *uncorrected* probabilities
        sum exactly before applying this.
        """
        for bucket in self.win_prob_bucket_correction:
            if bucket["lo"] <= win_prob < bucket["hi"]:
                return min(max(p_place + bucket["delta"], 1e-6), 1.0 - 1e-6)
        return p_place

    def to_dict(self) -> dict[str, Any]:
        d = {
            "version": self.version,
            "lam": self.lam,
            "tau": self.tau,
            "fitted_on": self.fitted_on,
            "n_races": self.n_races,
            "date_range": list(self.date_range) if self.date_range else None,
            "out_of_sample": self.out_of_sample,
            "calibration_table": self.calibration_table,
            "win_prob_bucket_correction": self.win_prob_bucket_correction,
            "provenance": self.provenance,
        }
        if self.beta is not None:
            d["beta"] = self.beta
            d["beta_n_races"] = self.beta_n_races
            d["beta_fitted_on"] = self.beta_fitted_on
        return d


class CalibrationError(Exception):
    """The calibration file exists but is not usable."""


def load_calibration(path: Path | None = None) -> Calibration:
    path = Path(path or get_settings().calibration_path)
    if not path.exists():
        log.warning(
            "calibration file %s missing — using shipped defaults "
            "(lam=%.2f tau=%.2f); run `python -m app.calibrate`",
            path, DEFAULT_LAM, DEFAULT_TAU,
        )
        return Calibration(source_path=path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("calibration file %s unreadable (%s) — using shipped defaults",
                  path, exc)
        return Calibration(source_path=path)

    for key in ("lam", "tau"):
        if not isinstance(raw.get(key), (int, float)):
            raise CalibrationError(
                f"{path}: missing or non-numeric {key!r}; refusing to guess"
            )
    lam, tau = float(raw["lam"]), float(raw["tau"])
    if not (0.0 < lam <= 2.0 and 0.0 < tau <= 2.0):
        raise CalibrationError(
            f"{path}: lam={lam} tau={tau} outside the plausible range (0, 2]"
        )
    dr = raw.get("date_range")
    return Calibration(
        lam=lam,
        tau=tau,
        version=str(raw.get("version", "unversioned")),
        fitted_on=raw.get("fitted_on"),
        n_races=raw.get("n_races"),
        date_range=(dr[0], dr[1]) if isinstance(dr, list) and len(dr) == 2 else None,
        out_of_sample=raw.get("out_of_sample") or {},
        calibration_table=raw.get("calibration_table") or [],
        win_prob_bucket_correction=raw.get("win_prob_bucket_correction") or [],
        beta=raw.get("beta"),
        beta_n_races=raw.get("beta_n_races"),
        beta_fitted_on=raw.get("beta_fitted_on"),
        provenance=raw.get("provenance", ""),
        source_path=path,
        loaded_from_file=True,
    )


def write_calibration(
    cal: Calibration, path: Path | None = None, force: bool = False
) -> Path:
    """Write the calibration, refusing to regress out-of-sample log-loss.

    A refit that predicts the held-out year *worse* than the file already on
    disk is not an improvement, however tidy its parameters look, so it is
    rejected unless ``force`` is set.
    """
    path = Path(path or get_settings().calibration_path)
    new_ll = (cal.out_of_sample or {}).get("logloss_model")
    if path.exists() and new_ll is not None and not force:
        try:
            old = json.loads(path.read_text())
            old_ll = (old.get("out_of_sample") or {}).get("logloss_model")
        except (OSError, json.JSONDecodeError):
            old_ll = None
        if isinstance(old_ll, (int, float)) and new_ll > old_ll + 1e-9:
            raise CalibrationError(
                f"refusing to overwrite {path}: new out-of-sample log-loss "
                f"{new_ll:.6f} is worse than the existing {old_ll:.6f}. "
                f"Pass --force to override."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cal.to_dict(), indent=2) + "\n")
    log.info("calibration written to %s", path)
    return path
