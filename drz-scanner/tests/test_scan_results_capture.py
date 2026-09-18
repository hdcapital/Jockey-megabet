"""A scan must capture the results of races it valued earlier.

Without this the settlement pipeline is a closed loop with nothing entering
it: `select_stubs` keeps only open races, so a resulted race's outcome is
never fetched, `Race.result_placings` stays empty forever, the backtester
settles nothing, and the UNPROVEN banner can never come down.

These tests drive `scan_once` against the shipped fixtures through a fake
client, so the whole path runs with no network.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.database import models as m
from app.database.repository import Repository, session_factory
from app.drz import (
    build_parser,
    capture_results,
    racing_today,
    scan_once,
    select_resulted_stubs,
    select_stubs,
)
from app.sources.base import SchemaMismatchError
from app.sources.sportsbet import parse_all_racing, parse_racecard

NOW = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)


class FakeSportsbet:
    """Serves the shipped fixtures and counts what was asked for."""

    def __init__(self, load, schedule="all_racing_schedule.json"):
        self.load = load
        self.schedule_name = schedule
        self.racecards = {
            "900101": "racecard_open_9_runners.json",
            "900104": "racecard_resulted_dead_heat.json",
        }
        self.fetched: list[str] = []

    def fetch_schedule(self, for_date):
        meetings, stubs = parse_all_racing(self.load(self.schedule_name), for_date)
        # The fixture pins startTime to a fixed epoch; the loop's near-jump
        # window is measured against the real clock, so re-date open races
        # the way a live schedule would carry them.
        for s in stubs:
            if s.is_open:
                s.start_time = datetime.now(timezone.utc) + timedelta(minutes=15)
        return meetings, stubs, None

    def fetch_racecard(self, event_id):
        self.fetched.append(event_id)
        name = self.racecards.get(event_id)
        if name is None:
            raise SchemaMismatchError("sportsbet", f"no fixture for {event_id}")
        # Dated against the real clock: scan_once measures time-to-jump from
        # now, and a race whose advertised start is hours past is skipped as
        # already run (which is exactly what a fixture dated at a fixed NOW
        # in the past would look like).
        fetched = datetime.now(timezone.utc)
        race = parse_racecard(self.load(name), event_id=event_id, fetched_at=fetched)
        if race.status == "open":
            race.start_time = fetched + timedelta(minutes=15)
        return race


@pytest.fixture
def scan_env(settings, monkeypatch, calibration):
    # Every module that resolves settings holds its own reference, bound at
    # import time, so each one has to be redirected at the temp database.
    for target in ("app.config.get_settings",
                   "app.drz.get_settings",
                   "app.database.repository.get_settings",
                   "app.http.get_settings"):
        monkeypatch.setattr(target, lambda: settings)
    return settings, calibration


def test_resulted_races_are_selected_for_capture_not_for_valuation(fixture):
    _meetings, stubs = parse_all_racing(
        fixture("all_racing_schedule.json"), date(2026, 9, 18)
    )
    args = build_parser().parse_args([])
    assert [s.race_number for s in select_stubs(stubs, args)] == [4]
    assert [s.race_number for s in select_resulted_stubs(stubs, args)] == [7]


def test_a_scan_captures_the_result_of_a_race_it_valued(fixture, scan_env):
    """The regression test for the closed loop: scan, then scan again."""
    settings, calibration = scan_env
    args = build_parser().parse_args(["--allow-uncalibrated"])
    sb = FakeSportsbet(fixture)

    # Scan one: the open race (900101) is valued and stored. The resulted
    # race (900104) is not one we hold, so nothing is captured for it.
    by_race, _skipped, stubs = scan_once(args, settings, calibration, sb)
    assert len(by_race) == 1
    assert stubs, "a full sweep hands back the schedule for quick sweeps"

    Session = session_factory(settings.database_url)
    with Session() as session:
        race = Repository(session).race_by_source_id("sportsbet", "900101")
        assert race is not None
        assert race.result_placings is None
        assert session.query(m.PlaceValuation).count() == 9

    # Now that race resolves. The schedule stub carries its placings.
    sb.racecards["900101"] = "racecard_resulted_dead_heat.json"
    resolved_schedule = fixture("all_racing_schedule.json")
    events = resolved_schedule["dates"][0]["sections"][0]["meetings"][0]["events"]
    events[0].update(statusCode="R", bettingStatus="RESULTED", result="2,5,3,9")

    class ResolvedSportsbet(FakeSportsbet):
        def fetch_schedule(self, for_date):
            meetings, stubs = parse_all_racing(resolved_schedule, for_date)
            return meetings, stubs, None

    sb2 = ResolvedSportsbet(fixture)
    sb2.racecards["900101"] = "racecard_resulted_dead_heat.json"
    by_race, _skipped, _ = scan_once(args, settings, calibration, sb2)
    assert by_race == [], "a resulted race must not be valued"

    with Session() as session:
        repo = Repository(session)
        race = repo.race_by_source_id("sportsbet", "900101")
        assert race.result_placings == "2,5,3,9", "the result was captured"
        assert race.status == "resulted"
        positions = repo.finish_positions(race.race_id)
        assert positions[3] == 3 and positions[9] == 3, "dead heat recorded"

        from app.backtest import settle_all

        assert settle_all(session) == 9, "the stored signals now settle"
        assert all(v.settled for v in session.query(m.PlaceValuation).all())


def test_capture_ignores_races_we_never_valued(fixture, scan_env):
    """A result for a race we hold no signal on is not worth a request."""
    settings, _calibration = scan_env
    _meetings, stubs = parse_all_racing(
        fixture("all_racing_schedule.json"), date(2026, 9, 18)
    )
    sb = FakeSportsbet(fixture)
    session_factory(settings.database_url)   # create the schema
    captured = capture_results(select_resulted_stubs(stubs, build_parser().parse_args([])),
                               sb, no_db=False)
    assert captured == 0
    assert sb.fetched == [], "no racecard fetched for a race we never valued"


def test_capture_is_skipped_entirely_with_no_db(fixture, scan_env):
    settings, _calibration = scan_env
    _meetings, stubs = parse_all_racing(
        fixture("all_racing_schedule.json"), date(2026, 9, 18)
    )
    sb = FakeSportsbet(fixture)
    assert capture_results(stubs, sb, no_db=True) == 0
    assert sb.fetched == []


def test_the_scanner_uses_the_australian_date_not_the_utc_one():
    """AEST/AEDT is UTC+10/+11, so for the first hours of an Australian day
    the UTC date is still yesterday — and the schedule endpoint is keyed on
    the Australian date."""
    from zoneinfo import ZoneInfo

    utc_now = datetime.now(timezone.utc)
    expected = utc_now.astimezone(ZoneInfo("Australia/Sydney")).date()
    assert racing_today() == expected
    # And the two genuinely differ during the Australian morning.
    morning = datetime(2026, 9, 18, 23, 30, tzinfo=timezone.utc)
    assert morning.date() != morning.astimezone(ZoneInfo("Australia/Sydney")).date()


def test_a_quick_sweep_reuses_the_schedule_and_refetches_only_near_jump_races(
    fixture, scan_env
):
    """The near-jump cadence must not multiply the day's request count."""
    settings, calibration = scan_env
    args = build_parser().parse_args(["--allow-uncalibrated", "--no-db"])

    class CountingSportsbet(FakeSportsbet):
        def __init__(self, load):
            super().__init__(load)
            self.schedule_fetches = 0

        def fetch_schedule(self, for_date):
            self.schedule_fetches += 1
            return super().fetch_schedule(for_date)

    sb = CountingSportsbet(fixture)
    by_race, _skipped, stubs = scan_once(args, settings, calibration, sb)
    assert sb.schedule_fetches == 1
    assert len(by_race) == 1
    first_cards = list(sb.fetched)

    # Quick sweep: the one open race is inside the window (15 min ahead is
    # outside the default 10-minute window, so widen it for the test).
    settings.near_jump_window_seconds = 20 * 60
    from app.drz import _near_jump_ids

    ids = _near_jump_ids(stubs, settings, datetime.now(timezone.utc))
    assert ids == {"900101"}
    by_race2, _skipped2, _ = scan_once(
        args, settings, calibration, sb, stubs=stubs, only_event_ids=ids
    )
    assert sb.schedule_fetches == 1, "a quick sweep must not refetch the schedule"
    assert sb.fetched == first_cards + ["900101"], "only the near-jump race is refetched"
    assert len(by_race2) == 1

    # And an empty near-jump set values nothing at all.
    by_race3, _s, _ = scan_once(
        args, settings, calibration, sb, stubs=stubs, only_event_ids=set()
    )
    assert by_race3 == []
    assert sb.schedule_fetches == 1
