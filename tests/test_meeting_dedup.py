"""Two Sportsbet meeting nodes with one venue name must not be merged."""

import logging
from datetime import date

from app.sources.sportsbet import SportsbetClient


class _Res:
    archive_path = None

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _client(payload):
    c = SportsbetClient.__new__(SportsbetClient)
    c.client = type("C", (), {"get_json": lambda self, url, params=None: _Res(payload)})()
    return c


def test_section_type_classifies_meetings_without_their_own_class():
    payload = {"dates": [{"sections": [
        {"raceType": "Horses", "meetings": [
            {"id": 1, "name": "Newcastle", "events": [{"id": 11, "raceNumber": 1}]}]},
        {"raceType": "Greyhounds", "meetings": [
            {"id": 2, "name": "NEWCASTLE", "events": [{"id": 22, "raceNumber": 1}]}]},
    ]}]}
    meetings, _ = _client(payload).fetch_meetings(date(2026, 10, 3))
    assert [m["name"] for m in meetings] == ["Newcastle"]


def test_same_named_second_meeting_is_skipped_with_a_warning(caplog):
    import app.scan as scan
    from app.sources.base import MeetingInfo, MegabetOffer, RacecardInfo, RaceInfo
    from datetime import datetime, timezone

    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    c = SportsbetClient.__new__(SportsbetClient)
    c.fetch_meetings = lambda d: ([
        {"name": "Newcastle", "className": "Horses - Aus/NZ", "events": [{"id": "a"}]},
        {"name": "NEWCASTLE", "events": [{"id": "b"}, {"id": "c"}], "_class_unknown": True},
    ], None)
    c.fetch_racecard = lambda eid: RacecardInfo(
        meeting=MeetingInfo("sportsbet", eid, "Newcastle", now.date()), fetched_at=now,
        races=[RaceInfo(source="sportsbet", source_id=eid, race_number=1,
                        start_time=now, status="open")],
    )
    offer = MegabetOffer(source="sportsbet", market_id="m", selection_id="s",
                         meeting_name="Newcastle", meeting_source_id=None,
                         meeting_date=now.date(), jockey_name="J", threshold=1,
                         odds=2.0, market_name="x", fetched_at=now)
    with caplog.at_level(logging.WARNING, logger="app.scan"):
        races = scan.gather_meeting_races(c, [offer], now.date())
    assert [r.source_id for r in races["newcastle"]] == ["a"]
    assert any("'NEWCASTLE' (unknown class, 2 races) skipped" in r.getMessage()
               for r in caplog.records)
