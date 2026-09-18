"""Schedule selection and the CLI's argument handling."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.drz import build_parser, select_stubs
from app.sources.base import RaceStub


def stub(event_id, race_no, meeting, minutes, status="A", result=None):
    return RaceStub(
        event_id=event_id, race_number=race_no,
        start_time=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        name=f"R{race_no}", status_code=status, betting_status="PRICED",
        meeting_id="m1", meeting_name=meeting, class_name="Horses - Aus/NZ",
        result=result,
    )


STUBS = [
    stub("1", 3, "Flemington", 40),
    stub("2", 1, "Randwick", 5),
    stub("3", 2, "Randwick", 20),
    stub("4", 4, "Flemington", 60, status="R", result="1,2,3"),
    stub("5", 5, "Doomben", 10, status="S"),
]


def test_open_races_only_and_ordered_by_jump():
    got = select_stubs(STUBS, build_parser().parse_args([]))
    assert [s.event_id for s in got] == ["2", "3", "1"]


def test_meeting_filter_is_case_insensitive_substring():
    args = build_parser().parse_args(["--meeting", "rand"])
    assert [s.event_id for s in select_stubs(STUBS, args)] == ["2", "3"]


def test_race_filter():
    args = build_parser().parse_args(["--race", "2"])
    assert [s.event_id for s in select_stubs(STUBS, args)] == ["3"]


@pytest.mark.parametrize("status,result,expected", [
    ("A", None, True),
    ("R", None, False),
    ("S", None, False),
    ("A", "1,2,3", False),
])
def test_is_open(status, result, expected):
    assert stub("x", 1, "M", 10, status=status, result=result).is_open is expected


def test_cli_accepts_every_documented_flag():
    args = build_parser().parse_args([
        "--meeting", "Flemington", "--race", "3", "--min-drz", "1.15",
        "--max-win-odds", "7.5", "--show-all", "--no-db", "--loop",
        "--date", "2026-09-18", "--allow-uncalibrated",
    ])
    assert args.meeting == "Flemington"
    assert args.race == 3
    assert args.min_drz == 1.15
    assert args.max_win_odds == 7.5
    assert args.show_all and args.no_db and args.loop and args.allow_uncalibrated
    assert args.date == date(2026, 9, 18)


def test_overrides_reach_the_settings():
    from app.config import Settings
    from app.drz import _apply_overrides

    settings = Settings()
    _apply_overrides(settings, build_parser().parse_args(["--min-drz", "1.25"]))
    assert settings.drz_min == 1.25
    _apply_overrides(settings, build_parser().parse_args(["--max-win-odds", "6"]))
    assert settings.max_win_odds == 6
