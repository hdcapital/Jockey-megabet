"""Persistence helpers: idempotent upserts + timestamped observations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database import models as m
from app.http import FetchResult

log = logging.getLogger(__name__)


def get_engine(url: str | None = None):
    url = url or get_settings().database_url
    if url.startswith("sqlite"):
        # Ensure the parent directory for a file-backed SQLite DB exists.
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url)


def init_db(url: str | None = None):
    engine = get_engine(url)
    m.Base.metadata.create_all(engine)
    _migrate(engine)
    return engine


def _migrate(engine) -> None:
    """Minimal additive migrations for databases created by older versions."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("megabet_markets")}
    if "market_type" not in cols:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE megabet_markets "
                "ADD COLUMN market_type VARCHAR(16) DEFAULT 'jockey'"
            ))
        log.info("db migration: added megabet_markets.market_type")


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=init_db(url), expire_on_commit=False)


class Repository:
    def __init__(self, session: Session):
        self.s = session
        # (horse_name, old_jockey, new_jockey) observed this scan — used to
        # flag Megabets that Sportsbet may void under its settlement rules.
        self.jockey_changes: list[tuple[str, str, str]] = []

    # -- reference data -------------------------------------------------
    def upsert_meeting(self, source: str, source_id: str, venue: str,
                       meeting_date, jurisdiction: str | None) -> m.Meeting:
        row = self.s.scalar(
            select(m.Meeting).where(
                m.Meeting.source == source, m.Meeting.source_id == source_id
            )
        )
        if row is None:
            row = m.Meeting(source=source, source_id=source_id, venue=venue,
                            meeting_date=meeting_date, jurisdiction=jurisdiction)
            self.s.add(row)
            self.s.flush()
        else:
            row.venue, row.meeting_date = venue, meeting_date or row.meeting_date
        return row

    def upsert_race(self, meeting: m.Meeting, source: str, source_id: str,
                    race_number, start_time, status: str) -> m.Race:
        row = self.s.scalar(
            select(m.Race).where(m.Race.source == source, m.Race.source_id == source_id)
        )
        if row is None:
            row = m.Race(meeting_id=meeting.meeting_id, source=source,
                         source_id=source_id, race_number=race_number,
                         start_time=start_time, status=status)
            self.s.add(row)
            self.s.flush()
        else:
            if row.status != status:
                log.info("race %s status %s -> %s", source_id, row.status, status)
            row.status, row.start_time = status, start_time or row.start_time
        return row

    def upsert_runner(self, race: m.Race, source: str, source_id: str,
                      horse_name: str, jockey, saddlecloth, status: str) -> m.Runner:
        row = self.s.scalar(
            select(m.Runner).where(
                m.Runner.race_id == race.race_id,
                m.Runner.source == source,
                m.Runner.source_id == source_id,
            )
        )
        if row is None:
            row = m.Runner(race_id=race.race_id, source=source, source_id=source_id,
                           horse_name=horse_name, jockey=jockey,
                           saddlecloth=saddlecloth, status=status)
            self.s.add(row)
            self.s.flush()
        else:
            if row.status != status:
                log.info("runner %s (%s) status %s -> %s",
                         horse_name, source_id, row.status, status)
            if jockey and row.jockey and row.jockey != jockey:
                log.warning("jockey change on %s: %r -> %r", horse_name, row.jockey, jockey)
                self.jockey_changes.append((horse_name, row.jockey, jockey))
            row.status = status
            row.jockey = jockey or row.jockey
        return row

    def upsert_megabet(self, source: str, source_market_id: str, meeting_row,
                       meeting_name, meeting_date, jockey: str, threshold: int,
                       market_name: str, market_type: str = "jockey") -> m.MegabetMarket:
        row = self.s.scalar(
            select(m.MegabetMarket).where(
                m.MegabetMarket.source == source,
                m.MegabetMarket.source_market_id == source_market_id,
                m.MegabetMarket.jockey == jockey,
                m.MegabetMarket.threshold == threshold,
            )
        )
        if row is None:
            row = m.MegabetMarket(
                source=source, source_market_id=source_market_id,
                meeting_id=meeting_row.meeting_id if meeting_row else None,
                meeting_name=meeting_name, meeting_date=meeting_date,
                jockey=jockey, threshold=threshold, source_market_name=market_name,
                market_type=market_type,
            )
            self.s.add(row)
            self.s.flush()
        return row

    # -- observations ----------------------------------------------------
    def add_megabet_price(self, megabet: m.MegabetMarket, odds: float,
                          status: str | None, ts: datetime, sha: str | None) -> None:
        self.s.add(m.MegabetPrice(timestamp=ts, megabet_id=megabet.megabet_id,
                                  sportsbet_odds=odds, market_status=status,
                                  raw_sha256=sha))

    def add_runner_price(self, runner: m.Runner, ts: datetime, bookmaker: str,
                         **fields) -> None:
        self.s.add(m.RunnerPrice(timestamp=ts, runner_id=runner.runner_id,
                                 bookmaker=bookmaker, **fields))

    def add_valuation(self, megabet: m.MegabetMarket, ts: datetime, model: str,
                      fair_probability, fair_odds, sportsbet_odds,
                      expected_return, number_of_rides: int, quality: str,
                      quality_detail: str | None,
                      ride_probabilities: list[float] | None) -> None:
        self.s.add(m.ModelValuation(
            timestamp=ts, megabet_id=megabet.megabet_id, probability_model=model,
            fair_probability=fair_probability, fair_odds=fair_odds,
            sportsbet_odds=sportsbet_odds, expected_return=expected_return,
            number_of_rides=number_of_rides, quality=quality,
            quality_detail=quality_detail,
            ride_probabilities_json=json.dumps(ride_probabilities)
            if ride_probabilities is not None else None,
        ))

    def add_result(self, race: m.Race, winning_runner, winning_jockey) -> None:
        existing = self.s.scalar(select(m.Result).where(m.Result.race_id == race.race_id))
        if existing is None:
            self.s.add(m.Result(race_id=race.race_id, winning_runner=winning_runner,
                                winning_jockey=winning_jockey,
                                settled_at=datetime.now(timezone.utc)))
        else:
            existing.winning_runner = winning_runner or existing.winning_runner
            existing.winning_jockey = winning_jockey or existing.winning_jockey

    def add_raw_response(self, source: str, fetch: FetchResult) -> None:
        self.s.add(m.RawResponse(
            source=source, endpoint=fetch.url, fetched_at=fetch.fetched_at,
            http_status=fetch.status_code, sha256=fetch.sha256,
            archive_path=str(fetch.archive_path) if fetch.archive_path else None,
        ))

    def add_unmatched(self, kind: str, source_name: str, normalized: str | None,
                      context: str | None, status: str) -> None:
        self.s.add(m.UnmatchedRecord(
            timestamp=datetime.now(timezone.utc), kind=kind, source_name=source_name,
            normalized_name=normalized, context=context, status=status,
        ))
