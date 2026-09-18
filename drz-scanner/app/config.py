"""Application configuration.

Every tunable comes from an environment variable (optionally via a local
``.env`` file, which must never be committed). See ``.env.example``.

Nothing here is a betting recommendation: the thresholds are *display*
thresholds that decide which rows get highlighted, and each one is recorded
with the valuation it produced so a stored row can always be explained.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Database -----------------------------------------------------------
    database_url: str = Field(default=f"sqlite:///{DATA_DIR / 'drz.db'}")

    # --- HTTP behaviour -----------------------------------------------------
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 2.0
    # Minimum spacing between successive requests to the same host. The
    # scanner's politeness budget is enforced here, not by the caller.
    http_min_request_interval_seconds: float = 0.75
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # --- Raw response archive ----------------------------------------------
    archive_raw_responses: bool = True
    raw_archive_dir: Path = DATA_DIR / "raw"

    # --- Sportsbet ----------------------------------------------------------
    sportsbet_base_url: str = "https://www.sportsbet.com.au"

    # --- Betfair ------------------------------------------------------------
    betfair_app_key: str | None = None
    betfair_username: str | None = None
    betfair_password: str | None = None
    betfair_cert_file: str | None = None
    betfair_key_file: str | None = None
    betfair_identity_url: str = "https://identitysso.betfair.com"
    betfair_identity_cert_url: str = "https://identitysso-cert.betfair.com"
    betfair_api_url: str = "https://api.betfair.com/exchange/betting/json-rpc/v1"
    betfair_min_liquidity: float = 500.0
    betfair_max_relative_spread: float = 0.25
    # A DELAYED application key returns no matched volume. When that is
    # detected the liquidity gate is impossible to apply, so the adapter
    # falls back to a spread-only gate and tags the row betfair_delayed.
    betfair_delayed_max_relative_spread: float = 0.10
    # Inside this many seconds to the jump a delayed-only Betfair price may
    # not, on its own, promote a row to BET.
    betfair_delayed_no_bet_window_seconds: int = 300

    # --- Scoring ------------------------------------------------------------
    drz_min: float = 1.10          # BET threshold on p_place * place_price
    drz_watch_min: float = 1.03    # WATCH threshold
    drz_suspect: float = 1.30      # above this: never BET, archive and log
    max_price_age_seconds: int = 60

    # --- Maximum win price, per win model ----------------------------------
    # The cap answers one question: out to what price are the win
    # probabilities trustworthy? So it depends on where they came from.
    #
    # Measured on 44,856 AU thoroughbred races (Jan 2024 - Aug 2026), actual
    # place rate divided by modelled place probability, by win-price band:
    #
    #   (1,2] 0.97  (2,3] 0.98  (3,5] 0.99  (5,9] 1.00  (9,15] 1.03
    #   (15,21] 1.02  (21,31] 1.00  (31,51] 1.00  (51,101] 0.99  (101+] 0.84
    #
    # Under Betfair prices the model is calibrated from about $3 to $51, runs
    # 2-3% hot below $3 and collapses above $101. The old 9.0 cutoff was only
    # ever justified for Sportsbet-derived probabilities, where the longshot
    # overround — not the place model — is what breaks down.
    #
    # None of this says an edge exists out there. It says only that the
    # probabilities are worth acting on in that range.
    max_win_price_betfair: float = 51.0
    max_win_price_sportsbet: float = 9.0
    # A delayed exchange key gives no matched volume, so the price is less
    # trustworthy the further out you go; the cap is pulled in accordingly.
    max_win_price_betfair_delayed: float = 21.0

    # A BET must be confirmed by the exchange's own "To Be Placed" market.
    # Measured over 436k runners, model-only signals priced against exchange
    # place prices less a 10-15% margin returned 0.80-0.88 per $1 staked.
    require_exchange_confirmation: bool = True
    bankroll: float = 1000.0
    kelly_fraction: float = 0.25   # quarter-Kelly
    stake_cap_per_bet_pct: float = 0.01
    stake_cap_per_race_pct: float = 0.02
    # Below this many settled BET signals the UNPROVEN banner stays up.
    proven_signal_threshold: int = 300

    # --- Win-probability model ---------------------------------------------
    # Number of joined races required before the fitted-beta model is used.
    beta_min_races: int = 1500

    # --- Scanner ------------------------------------------------------------
    scan_interval_seconds: int = 180        # full schedule sweep
    near_jump_interval_seconds: int = 40    # races inside the near-jump window
    near_jump_window_seconds: int = 600     # "within 10 minutes of the jump"

    # --- Calibration --------------------------------------------------------
    calibration_path: Path = DATA_DIR / "calibration.json"
    calibration_max_age_days: int = 35
    history_dir: Path = DATA_DIR / "history"
    history_base_url: str = "https://betfair-datascientists.github.io/data/assets"
    history_mirror_url: str = (
        "https://raw.githubusercontent.com/betfair-datascientists/"
        "betfair-datascientists.github.io/master/docs/data/assets"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
