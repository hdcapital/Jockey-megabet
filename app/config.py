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
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
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
    betfair_identity_url: str = "https://identitysso.betfair.com"
    betfair_identity_cert_url: str = "https://identitysso-cert.betfair.com"
    betfair_api_url: str = "https://api.betfair.com/exchange/betting/json-rpc/v1"
    # Below this total available-to-back volume (AUD) a Betfair-derived
    # probability is flagged low-confidence.
    betfair_min_liquidity: float = 500.0
    # If best-back/best-lay relative spread exceeds this, don't trust midpoint.
    betfair_max_relative_spread: float = 0.25

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
