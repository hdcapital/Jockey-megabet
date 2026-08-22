"""End-to-end pipeline test: value -> persist -> render -> backtest settle.

Uses synthetic in-memory racing data (test fixtures only) and a temporary
SQLite database. Verifies that a scan's observations round-trip through the
database and can be settled by the backtester once results exist.
"""

import argparse
from datetime import date, datetime, timezone

import pytest

from app.config import get_settings
from app.engine import value_offer
from app.sources.base import MegabetOffer, RaceInfo, RunnerInfo

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
MEETING_DATE = date(2026, 8, 22)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("ARCHIVE_RAW_RESPONSES", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def runner(rid, horse, jockey, odds, status="active"):
    return RunnerInfo(source="sportsbet", source_id=rid, horse_name=horse,
                      jockey_name=jockey, status=status, win_odds=odds,
                      odds_timestamp=NOW)


def make_world():
    races = [
        RaceInfo(source="sportsbet", source_id="r1", race_number=1,
                 start_time=NOW, status="open",
                 runners=[runner("1", "Horse A", "Alpha Rider", 2.0),
                          runner("2", "Horse B", "Beta Hoop", 4.0),
                          runner("3", "Horse C", "Other", 4.0)]),
        RaceInfo(source="sportsbet", source_id="r2", race_number=2,
                 start_time=NOW, status="open",
                 runners=[runner("4", "Horse D", "Alpha Rider", 3.0),
                          runner("5", "Horse E", "Beta Hoop", 3.0),
                          runner("6", "Horse F", "Other", 3.0)]),
    ]
    offer = MegabetOffer(
        source="sportsbet", market_id="m1", selection_id="s1",
        meeting_name="Testville", meeting_source_id=None,
        meeting_date=MEETING_DATE, jockey_name="Alpha Rider", threshold=1,
        odds=2.2, market_name="Alpha Rider to Ride 1+ Winners", fetched_at=NOW,
    )
    return offer, races


def test_scan_persist_render_and_settle(temp_db, capsys, monkeypatch):
    from rich.console import Console

    from app.reporting import tables
    from app.scan import persist_scan
    from app.reporting.tables import render_valuations

    monkeypatch.setattr(tables, "console", Console(width=250))

    offer, races = make_world()
    valuations = value_offer(offer, races, get_settings(), now=NOW)
    races_by_meeting = {"testville": races}
    persist_scan([offer], races_by_meeting, valuations, NOW)

    render_valuations(valuations, NOW)
    out = capsys.readouterr().out
    assert "Alpha Rider" in out
    assert "1+ wins" in out
    assert "not guaranteed profit" in out.replace("\n", " ")

    # Verify stored observation counts.
    from sqlalchemy import func, select

    from app.database import models as m
    from app.database.repository import session_factory

    Session = session_factory()
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(m.ModelValuation)) == 3
        assert s.scalar(select(func.count()).select_from(m.MegabetPrice)) == 1
        assert s.scalar(select(func.count()).select_from(m.RunnerPrice)) == 6
        assert s.scalar(select(func.count()).select_from(m.Race)) == 2

    # Now the races result: Alpha Rider wins race 1, loses race 2 -> 1 win,
    # so the 1+ megabet is settled as won.
    for race in races:
        race.status = "resulted"
    races[0].winner_names = ["Horse A"]
    races[1].winner_names = ["Horse E"]
    valuations2 = value_offer(offer, races, get_settings(), now=NOW)
    persist_scan([offer], races_by_meeting, valuations2, NOW)

    from app.backtest import settle_observations

    with Session() as s:
        settled, unsettleable = settle_observations(s, "sportsbet_novig")
    assert len(settled) == 1
    assert settled[0].actual_wins == 1
    assert settled[0].won is True


def test_jockey_change_sets_possible_void_flag(temp_db):
    from sqlalchemy import select

    from app.database import models as m
    from app.database.repository import session_factory
    from app.scan import persist_scan

    offer, races = make_world()
    races_by_meeting = {"testville": races}
    persist_scan([offer], races_by_meeting,
                 value_offer(offer, races, get_settings(), now=NOW), NOW)

    # Late rider replacement: Alpha Rider loses the Horse D ride.
    races[1].runners[0].jockey_name = "Replacement Rider"
    persist_scan([offer], races_by_meeting,
                 value_offer(offer, races, get_settings(), now=NOW), NOW)

    Session = session_factory()
    with Session() as s:
        mb = s.scalar(select(m.MegabetMarket))
        assert mb.possible_void_on_jockey_change is True
        runner = s.scalar(select(m.Runner).where(m.Runner.horse_name == "Horse D"))
        assert runner.jockey == "Replacement Rider"


def test_backtest_cli_runs_empty(temp_db, capsys):
    from app.database.repository import init_db
    init_db()
    from app.backtest import main
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing to backtest" in out
