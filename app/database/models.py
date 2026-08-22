"""SQLAlchemy ORM models.

SQLite by default; only portable column types are used so the same schema
runs on PostgreSQL by changing ``DATABASE_URL``. Timestamps are stored as
timezone-aware UTC datetimes.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_meeting_source"),)

    meeting_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    venue: Mapped[str] = mapped_column(String(128))
    meeting_date: Mapped[date | None] = mapped_column(Date)
    jurisdiction: Mapped[str | None] = mapped_column(String(64))

    races: Mapped[list["Race"]] = relationship(back_populates="meeting")


class Race(Base):
    __tablename__ = "races"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_race_source"),)

    race_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.meeting_id"))
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    race_number: Mapped[int | None] = mapped_column(Integer)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="open")

    meeting: Mapped[Meeting] = relationship(back_populates="races")
    runners: Mapped[list["Runner"]] = relationship(back_populates="race")


class Runner(Base):
    __tablename__ = "runners"
    __table_args__ = (
        UniqueConstraint("race_id", "source", "source_id", name="uq_runner_source"),
    )

    runner_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.race_id"))
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    horse_name: Mapped[str] = mapped_column(String(128))
    jockey: Mapped[str | None] = mapped_column(String(128))
    saddlecloth: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="active")

    race: Mapped[Race] = relationship(back_populates="runners")


class RunnerPrice(Base):
    __tablename__ = "runner_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    runner_id: Mapped[int] = mapped_column(ForeignKey("runners.runner_id"))
    bookmaker: Mapped[str] = mapped_column(String(32))
    decimal_odds: Mapped[float | None] = mapped_column(Float)
    raw_probability: Mapped[float | None] = mapped_column(Float)
    fair_probability: Mapped[float | None] = mapped_column(Float)
    devig_method: Mapped[str | None] = mapped_column(String(32))
    race_overround: Mapped[float | None] = mapped_column(Float)
    back_price: Mapped[float | None] = mapped_column(Float)
    lay_price: Mapped[float | None] = mapped_column(Float)
    back_volume: Mapped[float | None] = mapped_column(Float)
    lay_volume: Mapped[float | None] = mapped_column(Float)
    total_matched: Mapped[float | None] = mapped_column(Float)
    raw_sha256: Mapped[str | None] = mapped_column(String(64))


class MegabetMarket(Base):
    __tablename__ = "megabet_markets"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_market_id", "jockey", "threshold", name="uq_megabet"
        ),
    )

    megabet_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    source_market_id: Mapped[str] = mapped_column(String(64))
    meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.meeting_id"))
    meeting_name: Mapped[str | None] = mapped_column(String(128))
    meeting_date: Mapped[date | None] = mapped_column(Date)
    jockey: Mapped[str] = mapped_column(String(128))
    threshold: Mapped[int] = mapped_column(Integer)
    source_market_name: Mapped[str] = mapped_column(String(256))
    possible_void_on_jockey_change: Mapped[bool] = mapped_column(Boolean, default=False)


class MegabetPrice(Base):
    __tablename__ = "megabet_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    megabet_id: Mapped[int] = mapped_column(ForeignKey("megabet_markets.megabet_id"))
    sportsbet_odds: Mapped[float] = mapped_column(Float)
    market_status: Mapped[str | None] = mapped_column(String(32))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))


class ModelValuation(Base):
    __tablename__ = "model_valuations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    megabet_id: Mapped[int] = mapped_column(ForeignKey("megabet_markets.megabet_id"))
    probability_model: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(16), default="1")
    fair_probability: Mapped[float | None] = mapped_column(Float)
    fair_odds: Mapped[float | None] = mapped_column(Float)
    sportsbet_odds: Mapped[float | None] = mapped_column(Float)
    expected_return: Mapped[float | None] = mapped_column(Float)
    number_of_rides: Mapped[int] = mapped_column(Integer)
    quality: Mapped[str] = mapped_column(String(16), default="LOW")
    quality_detail: Mapped[str | None] = mapped_column(Text)
    # JSON-encoded list of the per-ride fair probabilities used, so the
    # Poisson-binomial number is reproducible from the stored row alone.
    ride_probabilities_json: Mapped[str | None] = mapped_column(Text)


class Result(Base):
    __tablename__ = "results"
    __table_args__ = (UniqueConstraint("race_id", name="uq_result_race"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.race_id"))
    winning_runner: Mapped[str | None] = mapped_column(String(128))
    winning_jockey: Mapped[str | None] = mapped_column(String(128))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawResponse(Base):
    __tablename__ = "raw_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    http_status: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    archive_path: Mapped[str | None] = mapped_column(Text)


class UnmatchedRecord(Base):
    __tablename__ = "unmatched_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(32))  # jockey | runner | meeting
    source_name: Mapped[str] = mapped_column(String(256))
    normalized_name: Mapped[str | None] = mapped_column(String(256))
    context: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))  # unmatched | ambiguous
