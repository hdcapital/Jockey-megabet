"""Source-agnostic domain objects passed between adapters and the engine.

Every field is populated from retrieved data or left ``None``; adapters must
never synthesise values. Each object carries its source name and the source's
own identifiers so stored records can be reconciled against raw archives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


class SchemaMismatchError(Exception):
    """The source responded, but not in a shape we know how to parse.

    ``archive_path`` points at the saved raw payload for offline inspection.
    """

    def __init__(self, source: str, detail: str, archive_path: str | None = None):
        self.source = source
        self.detail = detail
        self.archive_path = archive_path
        msg = f"{source} schema mismatch: {detail}"
        if archive_path:
            msg += f" (raw payload archived at {archive_path})"
        super().__init__(msg)


@dataclass
class MeetingInfo:
    source: str
    source_id: str
    venue: str
    meeting_date: date | None
    jurisdiction: str | None = None
    race_type: str | None = None  # e.g. Thoroughbred / Harness / Greyhound


@dataclass
class RunnerInfo:
    source: str
    source_id: str
    horse_name: str
    saddlecloth: int | None = None
    jockey_name: str | None = None
    status: str = "active"  # active | scratched | unknown
    win_odds: float | None = None
    odds_timestamp: datetime | None = None


@dataclass
class RaceInfo:
    source: str
    source_id: str
    race_number: int | None
    start_time: datetime | None
    status: str  # open | closed | resulted | abandoned | unknown
    name: str | None = None
    runners: list[RunnerInfo] = field(default_factory=list)
    winner_names: list[str] = field(default_factory=list)  # populated once resulted

    def active_runners(self) -> list[RunnerInfo]:
        return [r for r in self.runners if r.status == "active"]


@dataclass
class RacecardInfo:
    meeting: MeetingInfo
    races: list[RaceInfo] = field(default_factory=list)
    fetched_at: datetime | None = None
    raw_sha256: str | None = None


@dataclass
class MegabetOffer:
    """One Jockey Megabet selection: a jockey/threshold at a price."""

    source: str
    market_id: str
    selection_id: str
    meeting_name: str | None
    meeting_source_id: str | None  # Sportsbet event id of the meeting, if given
    meeting_date: date | None
    jockey_name: str
    threshold: int  # k in "k+ wins"
    odds: float
    market_name: str
    fetched_at: datetime
    market_status: str = "open"
    raw_sha256: str | None = None
