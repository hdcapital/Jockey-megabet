"""SQLAlchemy models. SQLite by default; the column types are portable.

Two tables carry the record that matters:

``place_valuations``
    One row per runner per scan, with every input and every model's output,
    so any displayed number can be recomputed from stored data alone.
``runner_prices``
    One row per runner per scan, whether or not it was valued. This is the
    raw material the Sportsbet beta fit needs later; without it, the fit can
    never be done, because nobody sells historical Sportsbet prices.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (UniqueConstraint("source", "source_id"),)

    meeting_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    venue: Mapped[str] = mapped_column(String(128))
    meeting_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    class_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    races: Mapped[list["Race"]] = relationship(back_populates="meeting")


class Race(Base):
    __tablename__ = "races"
    __table_args__ = (UniqueConstraint("source", "source_id"),)

    race_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.meeting_id"))
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    race_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="open")
    name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Full placings by saddlecloth, exactly as the source wrote them ("1,16,18").
    result_placings: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resulted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    meeting: Mapped[Meeting] = relationship(back_populates="races")


class Runner(Base):
    __tablename__ = "runners"
    __table_args__ = (UniqueConstraint("source", "source_id"),)

    runner_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.race_id"))
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    horse_name: Mapped[str] = mapped_column(String(128))
    saddlecloth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jockey_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trainer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    barrier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Finishing position, written once the race resolves. Two runners on the
    #: same position is the only unambiguous dead-heat signal.
    finish_position: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RunnerPrice(Base):
    """Every price seen for every runner on every scan — never backfilled."""

    __tablename__ = "runner_prices"
    __table_args__ = (
        Index("ix_runner_prices_runner_ts", "runner_id", "observed_at"),
        Index("ix_runner_prices_race_ts", "race_id", "observed_at"),
    )

    price_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.race_id"))
    runner_id: Mapped[int] = mapped_column(ForeignKey("runners.runner_id"))
    source: Mapped[str] = mapped_column(String(32), default="sportsbet")
    price_code: Mapped[str] = mapped_column(String(8), default="L")
    price_type: Mapped[str] = mapped_column(String(24), default="fixed")
    win_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    place_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    seconds_to_jump: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlaceValuation(Base):
    """One scored runner at one instant, with every input kept."""

    __tablename__ = "place_valuations"
    __table_args__ = (
        Index("ix_place_valuations_race_ts", "race_id", "observed_at"),
        Index("ix_place_valuations_tier", "tier"),
    )

    valuation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.race_id"))
    runner_id: Mapped[int] = mapped_column(ForeignKey("runners.runner_id"))
    race_source_id: Mapped[str] = mapped_column(String(64))
    runner_source_id: Mapped[str] = mapped_column(String(64))

    active_runner_count: Mapped[int] = mapped_column(Integer)
    places: Mapped[int] = mapped_column(Integer)
    terms_fragile: Mapped[bool] = mapped_column(Boolean, default=False)

    win_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    place_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_type: Mapped[str] = mapped_column(String(24), default="fixed")
    price_code: Mapped[str] = mapped_column(String(8), default="L")
    price_age_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    win_model: Mapped[str] = mapped_column(String(32))
    #: Every model's win probability for this runner, as JSON.
    win_probs_json: Mapped[str] = mapped_column(Text)
    #: Every model's P(place), as JSON.
    p_place_json: Mapped[str] = mapped_column(Text)
    #: Every model's Dr Z score, as JSON.
    drz_json: Mapped[str] = mapped_column(Text)
    p_place: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: P(place) before the win-price band correction, and the factor applied.
    p_place_raw: Mapped[float | None] = mapped_column(Float, nullable=True)
    band_shrink: Mapped[float | None] = mapped_column(Float, nullable=True)
    drz: Mapped[float | None] = mapped_column(Float, nullable=True)
    ev: Mapped[float | None] = mapped_column(Float, nullable=True)
    fair_place_odds: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Betfair's own place-market opinion, when a matching market existed.
    p_place_betfair: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Dr Z from the exchange place market — the independent confirmation.
    drz_exchange_place: Mapped[float | None] = mapped_column(Float, nullable=True)
    betfair_delayed: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The cap that applied to this row's win model, and whether it bound.
    max_win_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    beyond_price_cap: Mapped[bool] = mapped_column(Boolean, default=False)

    lam: Mapped[float] = mapped_column(Float)
    tau: Mapped[float] = mapped_column(Float)
    calibration_version: Mapped[str] = mapped_column(String(64))
    #: Bumped whenever the valuation pipeline changes shape, so a backtest can
    #: separate rows produced by different versions of the model.
    model_version: Mapped[str] = mapped_column(String(32), default="")

    tier: Mapped[str] = mapped_column(String(16))
    tier_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality: Mapped[str] = mapped_column(String(16), default="HIGH")
    suggested_stake: Mapped[float | None] = mapped_column(Float, nullable=True)
    seconds_to_jump: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_archive_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Settlement, written by the backtester only from stored placings.
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    placed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    settled_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    dead_heat_divisor: Mapped[float | None] = mapped_column(Float, nullable=True)
    deduction_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: "finish_positions" (dead heats resolved) or "placings_string" (they
    #: could not be), so a report can say which evidence it rests on.
    settlement_basis: Mapped[str | None] = mapped_column(String(24), nullable=True)


class RawResponse(Base):
    __tablename__ = "raw_responses"

    raw_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    http_status: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    archive_path: Mapped[str | None] = mapped_column(Text, nullable=True)
