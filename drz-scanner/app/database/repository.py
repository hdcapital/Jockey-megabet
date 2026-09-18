"""Persistence: idempotent upserts for reference data, append-only observations.

Prices and valuations are *never* updated in place. A scan writes what it saw
at the instant it saw it; the backtester later annotates those rows with a
settlement. That is what makes closing-line value computable at all — and it
is why the backtester refuses to invent a price for a race it did not watch.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database import models as m
from app.http import FetchResult
from app.sources.base import MeetingInfo, RaceInfo, RunnerInfo

log = logging.getLogger(__name__)


def get_engine(url: str | None = None):
    url = url or get_settings().database_url
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url)


def init_db(url: str | None = None):
    engine = get_engine(url)
    m.Base.metadata.create_all(engine)
    return engine


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=init_db(url), expire_on_commit=False)


class Repository:
    def __init__(self, session: Session):
        self.s = session

    # -- reference data -------------------------------------------------
    def upsert_meeting(self, info: MeetingInfo) -> m.Meeting:
        row = self.s.scalar(
            select(m.Meeting).where(
                m.Meeting.source == info.source, m.Meeting.source_id == info.source_id
            )
        )
        if row is None:
            row = m.Meeting(
                source=info.source,
                source_id=info.source_id,
                venue=info.venue,
                meeting_date=info.meeting_date,
                jurisdiction=info.jurisdiction,
                class_name=info.class_name,
            )
            self.s.add(row)
            self.s.flush()
        else:
            row.venue = info.venue or row.venue
            row.meeting_date = info.meeting_date or row.meeting_date
        return row

    def upsert_race(self, meeting: m.Meeting, race: RaceInfo) -> m.Race:
        row = self.s.scalar(
            select(m.Race).where(
                m.Race.source == race.source, m.Race.source_id == race.source_id
            )
        )
        placings = (
            ",".join(str(p) for p in race.result_placings)
            if race.result_placings
            else None
        )
        if row is None:
            row = m.Race(
                meeting_id=meeting.meeting_id,
                source=race.source,
                source_id=race.source_id,
                race_number=race.race_number,
                start_time=_naive(race.start_time),
                status=race.status,
                name=race.name,
                result_placings=placings,
                resulted_at=_naive(race.fetched_at) if placings else None,
            )
            self.s.add(row)
            self.s.flush()
        else:
            row.status = race.status or row.status
            row.start_time = _naive(race.start_time) or row.start_time
            if placings and not row.result_placings:
                row.result_placings = placings
                row.resulted_at = _naive(race.fetched_at)
        return row

    def upsert_runner(self, race: m.Race, info: RunnerInfo) -> m.Runner:
        row = self.s.scalar(
            select(m.Runner).where(
                m.Runner.source == info.source, m.Runner.source_id == info.source_id
            )
        )
        if row is None:
            row = m.Runner(
                race_id=race.race_id,
                source=info.source,
                source_id=info.source_id,
                horse_name=info.horse_name,
                saddlecloth=info.saddlecloth,
                jockey_name=info.jockey_name,
                trainer_name=info.trainer_name,
                barrier=info.barrier,
                finish_position=info.finish_position,
            )
            self.s.add(row)
            self.s.flush()
        else:
            row.jockey_name = info.jockey_name or row.jockey_name
            row.trainer_name = info.trainer_name or row.trainer_name
        if info.finish_position is not None:
            row.finish_position = info.finish_position
        return row

    # -- observations ----------------------------------------------------
    def record_price(
        self,
        race: m.Race,
        runner: m.Runner,
        info: RunnerInfo,
        observed_at: datetime,
        seconds_to_jump: float | None,
        raw_sha256: str | None,
    ) -> m.RunnerPrice:
        row = m.RunnerPrice(
            observed_at=_naive(observed_at),
            race_id=race.race_id,
            runner_id=runner.runner_id,
            source=info.source,
            price_code="L",
            price_type=info.price_type,
            win_price=info.win_price,
            place_price=info.place_price,
            status=info.status,
            seconds_to_jump=seconds_to_jump,
            raw_sha256=raw_sha256,
        )
        self.s.add(row)
        return row

    def record_valuation(self, valuation, race: m.Race, runner: m.Runner) -> m.PlaceValuation:
        v = valuation
        row = m.PlaceValuation(
            observed_at=_naive(v.observed_at),
            race_id=race.race_id,
            runner_id=runner.runner_id,
            race_source_id=v.race_source_id,
            runner_source_id=v.runner_source_id,
            active_runner_count=v.active_runner_count,
            places=v.places,
            terms_fragile=v.terms_fragile,
            win_price=v.win_price,
            place_price=v.place_price,
            price_type=v.price_type,
            price_code=v.price_code,
            price_age_seconds=v.price_age_seconds,
            win_model=v.win_model,
            win_probs_json=json.dumps(v.win_probs_by_model),
            p_place_json=json.dumps(v.p_place_by_model),
            drz_json=json.dumps(v.drz_by_model),
            p_place=v.p_place,
            p_place_raw=v.p_place_raw,
            band_shrink=v.band_shrink,
            drz=v.drz,
            ev=v.ev,
            fair_place_odds=v.fair_place_odds,
            p_place_betfair=v.p_place_betfair,
            drz_exchange_place=v.drz_exchange_place,
            betfair_delayed=v.betfair_delayed,
            max_win_price=v.max_win_price,
            beyond_price_cap=v.beyond_price_cap,
            lam=v.lam,
            tau=v.tau,
            calibration_version=v.calibration_version,
            model_version=v.model_version,
            tier=v.tier,
            tier_reasons="; ".join(v.tier_reasons) if v.tier_reasons else None,
            quality=v.quality,
            suggested_stake=v.suggested_stake,
            seconds_to_jump=v.seconds_to_jump,
            raw_sha256=v.raw_sha256,
            raw_archive_path=v.raw_archive_path,
        )
        self.s.add(row)
        return row

    def record_raw(self, result: FetchResult, source: str) -> m.RawResponse:
        row = m.RawResponse(
            source=source,
            url=result.url,
            fetched_at=_naive(result.fetched_at),
            http_status=result.status_code,
            sha256=result.sha256,
            archive_path=str(result.archive_path) if result.archive_path else None,
        )
        self.s.add(row)
        return row

    # -- reads -----------------------------------------------------------
    def unsettled_valuations(self) -> list[m.PlaceValuation]:
        return list(
            self.s.scalars(
                select(m.PlaceValuation).where(m.PlaceValuation.settled.is_(False))
            )
        )

    def finish_positions(self, race_id: int) -> dict[int, int]:
        """Saddlecloth -> finishing position for one race, where recorded."""
        rows = self.s.scalars(
            select(m.Runner).where(m.Runner.race_id == race_id)
        )
        return {
            r.saddlecloth: r.finish_position
            for r in rows
            if r.saddlecloth is not None and r.finish_position is not None
        }

    def race_by_source_id(self, source: str, source_id: str) -> m.Race | None:
        return self.s.scalar(
            select(m.Race).where(m.Race.source == source, m.Race.source_id == source_id)
        )

    def race_by_id(self, race_id: int) -> m.Race | None:
        return self.s.get(m.Race, race_id)

    def runner_by_id(self, runner_id: int) -> m.Runner | None:
        return self.s.get(m.Runner, runner_id)

    def settled_bet_count(self) -> int:
        from sqlalchemy import func

        return int(
            self.s.scalar(
                select(func.count())
                .select_from(m.PlaceValuation)
                .where(
                    m.PlaceValuation.tier == "BET",
                    m.PlaceValuation.settled.is_(True),
                )
            )
            or 0
        )


def _naive(dt: datetime | None) -> datetime | None:
    """SQLite has no timezone type; store UTC-naive and read it back as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
