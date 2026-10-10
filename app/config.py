"""Application configuration.

All tunables come from environment variables (optionally via a local `.env`
file, which must never be committed). See `.env.example` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # The .env file is looked up next to the project, not in the current
    # working directory, so the scanner finds its credentials however it is
    # launched (double-clicked .bat, systemd unit, another shell directory).
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- Database -----------------------------------------------------------
    # SQLite by default; any SQLAlchemy URL works (e.g. postgresql+psycopg://...)
    database_url: str = Field(
        default=f"sqlite:///{PROJECT_ROOT / 'data' / 'megabet.db'}"
    )

    # --- HTTP behaviour -----------------------------------------------------
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 2.0
    # Minimum spacing between successive requests to the same host.
    http_min_request_interval_seconds: float = 0.75
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # --- Raw response archive ----------------------------------------------
    archive_raw_responses: bool = True
    raw_archive_dir: Path = PROJECT_ROOT / "data" / "raw"

    # --- Sportsbet ----------------------------------------------------------
    sportsbet_base_url: str = "https://www.sportsbet.com.au"

    # --- Betfair ------------------------------------------------------------
    betfair_app_key: str | None = None
    betfair_username: str | None = None
    betfair_password: str | None = None
    # Optional certificate (non-interactive) login
    betfair_cert_file: str | None = None
    betfair_key_file: str | None = None
    # Betfair runs a separate identity host for Australian and New Zealand
    # accounts (identitysso.betfair.com.au). The configured host is tried
    # first; when it rejects the username/password the other one is tried
    # and the log says which host accepted the login.
    betfair_identity_url: str = "https://identitysso.betfair.com"
    betfair_identity_cert_url: str = "https://identitysso-cert.betfair.com"
    betfair_identity_url_au: str = "https://identitysso.betfair.com.au"
    betfair_identity_cert_url_au: str = "https://identitysso-cert.betfair.com.au"
    betfair_api_url: str = "https://api.betfair.com/exchange/betting/json-rpc/v1"
    # Below this total available-to-back volume (AUD) a Betfair-derived
    # probability is flagged low-confidence.
    betfair_min_liquidity: float = 500.0
    # If best-back/best-lay relative spread exceeds this, don't trust midpoint.
    betfair_max_relative_spread: float = 0.25
    # Delayed application keys (the free tier) report zero matched volume on
    # every book, so the liquidity gate above can never pass. "auto" detects
    # that signature per session; "true"/"false" force it.
    betfair_key_delayed: str = "auto"
    # With a delayed key reliability rests on the spread alone, so it is
    # tighter than the liquid-market spread gate.
    betfair_delayed_max_relative_spread: float = 0.10

    # --- Consensus model ----------------------------------------------------
    # Weights used when both sources are available. Documented default:
    # lean on the exchange when it is liquid, but keep bookmaker information.
    # These are configurable and NOT claimed to be optimal.
    consensus_weight_betfair: float = 0.7
    consensus_weight_sportsbet: float = 0.3

    # --- De-vig -------------------------------------------------------------
    # One of: proportional | power | shin
    devig_method: str = "proportional"

    # --- Scanner ------------------------------------------------------------
    scan_interval_seconds: int = 180
    # Prices older than this are considered stale for valuation quality.
    stale_price_seconds: int = 600
    min_edge_pct: float = 0.0

    # --- Race value scanner (python -m app.race_value) -----------------------
    # Sportsbet fixed odds vs Betfair as the source of truth, races jumping
    # within the next VALUE_WINDOW_MINUTES, refreshed every
    # VALUE_INTERVAL_SECONDS.
    value_interval_seconds: int = 60
    value_window_minutes: int = 20
    # A race whose advertised start passed this long ago is still shown while
    # it has not jumped (late starts are common); in-play markets never are.
    value_grace_minutes: int = 2
    # Betfair countries to load (Sportsbet's Aus/NZ meetings).
    value_betfair_countries: str = "AU,NZ"
    # Compare Sportsbet's fixed place price with Betfair's place market too.
    value_include_places: bool = True
    # Betfair commission on net winnings, used only for the lock-in column
    # (never for the fair probability). Betfair Australia's base rate depends
    # on the race's state; set yours here.
    betfair_commission: float = 0.08
    # Betfair's price for a runner is the average back and lay odds for this
    # stake (AUD) across the visible price levels.
    value_depth_stake: float = 100.0
    # Trust gates: back/lay at most this many Betfair price steps apart, and
    # at least this much matched on the market (win / place).
    value_max_spread_ticks: int = 3
    value_min_matched: float = 2000.0
    value_min_matched_place: float = 500.0
    # A market whose midpoints add up this far from 100% is thin or forming.
    value_max_book_deviation: float = 0.05
    # An edge above this is almost always stale data or a scratching.
    value_suspect_ev: float = 0.30
    # Sportsbet and Betfair snapshots further apart than this are flagged.
    value_max_snapshot_gap_seconds: float = 45.0
    # A bet worth a look: edge of at least VALUE_MIN_EV on
    # VALUE_CONFIRM_SCANS scans in a row.
    value_min_ev: float = 0.02
    value_confirm_scans: int = 2
    # Closing prices: races with a signal are polled this often near the jump.
    value_close_poll_seconds: int = 15
    # Stake suggestion: a fraction of full Kelly, of this bankroll (AUD).
    # 0 shows the stake as a percentage of your bank instead.
    value_bankroll: float = 0.0
    value_kelly_fraction: float = 0.25


@lru_cache
def get_settings() -> Settings:
    return Settings()
