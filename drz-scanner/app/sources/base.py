"""Source-agnostic domain objects passed between adapters and the engine.

Every field is populated from retrieved data or left ``None``; adapters must
never synthesise a value. Each object carries its source name and that
source's own identifiers so a stored record can be reconciled against the raw
archive it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


class SchemaMismatchError(Exception):
    """The source responded, but not in a shape we know how to parse.

    ``archive_path`` points at the saved raw payload for offline inspection.
    Raising this is always preferable to inventing a plausible-looking number.
    """

    def __init__(self, source: str, detail: str, archive_path: str | None = None):
        self.source = source
        self.detail = detail
        self.archive_path = archive_path
        msg = f"{source} schema mismatch: {detail}"
        if archive_path:
            msg += f" (raw payload archived at {archive_path})"
        super().__init__(msg)


@dataclass(frozen=True)
class PriceQuote:
    """One price offered under one Sportsbet price code.

    ``price_type`` is either ``"fixed"`` (price code ``L`` — the live, locked
    fixed-odds price a bet is actually struck at) or ``"tote_indicative"``
    (any other code that carries prices; see ``PRICE_CODE_NOTES``). An
    indicative price is not a price you can take, and this tool never treats
    one as such.
    """

    price_code: str
    price_type: str
    win_price: float | None
    place_price: float | None


@dataclass
class MeetingInfo:
    source: str
    source_id: str
    venue: str
    meeting_date: date | None
    jurisdiction: str | None = None
    race_type: str | None = None
    class_name: str | None = None


@dataclass
class RunnerInfo:
    source: str
    source_id: str
    horse_name: str
    saddlecloth: int | None = None
    jockey_name: str | None = None
    trainer_name: str | None = None
    barrier: int | None = None
    status: str = "active"          # active | scratched
    #: Finishing position from a resulted racecard, when the source gives one
    #: per runner. Two runners sharing a position is a dead heat — the only
    #: unambiguous way to detect one.
    finish_position: int | None = None
    win_price: float | None = None   # price code L only
    place_price: float | None = None  # price code L only
    price_type: str = "fixed"
    prices_by_code: dict[str, PriceQuote] = field(default_factory=dict)
    price_timestamp: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def tote_indicative(self) -> PriceQuote | None:
        for q in self.prices_by_code.values():
            if q.price_type == "tote_indicative" and (
                q.win_price is not None or q.place_price is not None
            ):
                return q
        return None


@dataclass
class RaceInfo:
    source: str
    source_id: str
    race_number: int | None
    start_time: datetime | None
    status: str                      # open | resulted | abandoned | unknown
    name: str | None = None
    venue: str | None = None
    meeting_source_id: str | None = None
    runners: list[RunnerInfo] = field(default_factory=list)
    result_placings: list[int] = field(default_factory=list)  # saddlecloths, in order
    result_raw: str | None = None
    fetched_at: datetime | None = None
    raw_sha256: str | None = None
    raw_archive_path: str | None = None

    def active_runners(self) -> list[RunnerInfo]:
        return [r for r in self.runners if r.is_active]


@dataclass
class RaceStub:
    """A race as it appears in the AllRacing schedule, before its racecard."""

    event_id: str
    race_number: int | None
    start_time: datetime | None
    name: str | None
    status_code: str | None
    betting_status: str | None
    meeting_id: str
    meeting_name: str
    class_name: str | None
    result: str | None = None

    @property
    def is_open(self) -> bool:
        """Open = still taking bets.

        ``statusCode`` "A" is active; anything else (R resulted, S suspended,
        C closed, X abandoned) is not an open race. A ``result`` string is a
        second, independent signal that the race is done.
        """
        if self.result:
            return False
        code = (self.status_code or "").strip().upper()
        if code and code != "A":
            return False
        status = (self.betting_status or "").strip().lower()
        if any(w in status for w in ("result", "settled", "abandon", "closed", "final")):
            return False
        return True
