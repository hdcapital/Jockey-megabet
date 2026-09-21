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


def test_a_cli_override_may_lower_every_cap():
    from app.config import Settings
    from app.drz import _apply_overrides

    settings = Settings()
    _apply_overrides(settings, build_parser().parse_args(["--max-win-odds", "6"]))
    assert settings.max_win_price_betfair == 6
    assert settings.max_win_price_sportsbet == 6
    assert settings.max_win_price_betfair_delayed == 6


def test_a_cli_override_may_not_raise_a_cap_without_i_know():
    """The caps come from a measurement, so widening one is not a flag away."""
    from app.config import Settings
    from app.drz import _apply_overrides

    settings = Settings()
    _apply_overrides(settings, build_parser().parse_args(["--max-win-odds", "200"]))
    # Betfair's 51 and the delayed 21 are left alone; Sportsbet's 9 too.
    assert settings.max_win_price_betfair == 51.0
    assert settings.max_win_price_betfair_delayed == 21.0
    assert settings.max_win_price_sportsbet == 9.0


def test_i_know_allows_raising_a_cap():
    from app.config import Settings
    from app.drz import _apply_overrides

    settings = Settings()
    _apply_overrides(
        settings, build_parser().parse_args(["--max-win-odds", "200", "--i-know"])
    )
    assert settings.max_win_price_betfair == 200
    assert settings.max_win_price_sportsbet == 200


def test_a_mixed_override_lowers_what_it_can_without_i_know():
    """21 is below Betfair's 51 but above Sportsbet's 9: lower one, keep the other."""
    from app.config import Settings
    from app.drz import _apply_overrides

    settings = Settings()
    _apply_overrides(settings, build_parser().parse_args(["--max-win-odds", "21"]))
    assert settings.max_win_price_betfair == 21
    assert settings.max_win_price_betfair_delayed == 21
    assert settings.max_win_price_sportsbet == 9.0, "9 must not be raised to 21"


def test_exchange_confirmation_can_be_disabled_explicitly():
    from app.config import Settings
    from app.drz import _apply_overrides

    settings = Settings()
    assert settings.require_exchange_confirmation
    _apply_overrides(
        settings, build_parser().parse_args(["--no-exchange-confirmation"])
    )
    assert not settings.require_exchange_confirmation
