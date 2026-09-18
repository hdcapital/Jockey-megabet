"""value -> persist -> render -> settle, including a dead heat.

No network: the racecard comes from a fixture, the database is a temp file.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.backtest import (
    BASIS_PLACINGS,
    BASIS_POSITIONS,
    run_report,
    settle_all,
    settle_runner,
)
from app.calibration import Calibration
from app.database import models as m
from app.database.repository import Repository, session_factory
from app.engine import NoPlaceMarketError, value_race
from app.reporting.tables import render_scan
from app.scoring import (
    REASON_NO_EXCHANGE,
    REASON_UNCALIBRATED,
    TIER_BET,
    TIER_SUSPECT,
)
from app.sources.base import MeetingInfo
from app.sources.sportsbet import parse_racecard

NOW = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)


def _race(fixture, name, start_offset_s=900):
    race = parse_racecard(fixture(name), event_id="900101", fetched_at=NOW)
    race.start_time = NOW + timedelta(seconds=start_offset_s)
    race.raw_sha256 = "deadbeef" * 8
    race.raw_archive_path = "/tmp/archived.json"
    return race


def test_nine_runner_race_values_every_active_runner(fixture, settings, calibration):
    race = _race(fixture, "racecard_open_9_runners.json")
    vals = value_race(race, calibration, settings, now=NOW)

    assert len(vals) == 9                      # the scratching is excluded
    assert all(v.places == 3 for v in vals)
    assert all(v.active_runner_count == 9 for v in vals)
    assert not any(v.terms_fragile for v in vals)
    # P(place) across the field must sum to the number of dividends.
    assert sum(v.p_place for v in vals) == pytest.approx(3.0, abs=0.05)
    # Every row carries its provenance.
    assert all(v.calibration_version == calibration.version for v in vals)
    assert all(v.lam == calibration.lam and v.tau == calibration.tau for v in vals)
    assert all(v.win_model == "sportsbet_power" for v in vals)
    assert all(v.drz == pytest.approx(v.p_place * v.place_price) for v in vals)
    assert all(v.ev == pytest.approx(v.drz - 1.0) for v in vals)


def test_a_realistically_margined_book_produces_nothing(fixture, settings, calibration):
    """The expected result on a normal race, and the reason to trust the tool.

    The fixture's place prices carry an ordinary Sportsbet margin. Every Dr Z
    score must land below 1, and the *longshots* must score worst — which is
    exactly what power de-vig buys: under proportional de-vig the tail would
    show fat, fictitious edges.
    """
    race = _race(fixture, "racecard_open_9_runners.json")
    vals = value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True)
    assert all(v.drz < 1.0 for v in vals)
    assert all(v.tier == "—" for v in vals)
    by_saddle = {v.saddlecloth: v for v in vals}
    assert by_saddle[9].drz < by_saddle[1].drz, (
        "the longshot must not out-score the favourite on a uniformly margined book"
    )


def test_uncalibrated_rows_cannot_reach_bet(fixture, settings, calibration):
    """A genuinely generous place price still cannot be BET by default."""
    race = _race(fixture, "racecard_open_9_runners.json")
    race.runners[2].place_price = 2.55        # ~drz 1.16, inside the suspect cap

    vals = value_race(race, calibration, settings, now=NOW)
    target = next(v for v in vals if v.saddlecloth == 3)
    assert target.drz > settings.drz_min
    assert target.tier == "WATCH"
    assert any(REASON_UNCALIBRATED in r for r in target.tier_reasons)
    assert target.suggested_stake is None

    # Lifting the uncalibrated gate is not enough on its own: with no
    # exchange place market to confirm it, the row is still held at WATCH.
    allowed = value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True)
    target = next(v for v in allowed if v.saddlecloth == 3)
    assert target.tier == "WATCH"
    assert any(REASON_NO_EXCHANGE in r for r in target.tier_reasons)

    # With a confirming exchange place price it reaches BET.
    confirmed = value_race(
        race, calibration, settings, now=NOW, allow_uncalibrated=True,
        betfair_place_probs={race.runners[2].source_id: 0.50},   # 0.50*2.55 = 1.275
    )
    target = next(v for v in confirmed if v.saddlecloth == 3)
    assert target.drz_exchange_place == pytest.approx(0.50 * 2.55)
    assert target.tier == TIER_BET
    assert target.suggested_stake and target.suggested_stake > 0


def test_six_runner_race_gets_two_places(fixture, settings, calibration):
    race = _race(fixture, "racecard_open_6_runners.json")
    vals = value_race(race, calibration, settings, now=NOW)
    assert all(v.places == 2 for v in vals)
    assert sum(v.p_place for v in vals) == pytest.approx(2.0, abs=0.05)


def test_eight_runner_race_is_flagged_terms_fragile(fixture, settings, calibration):
    race = _race(fixture, "racecard_open_8_runners.json")
    vals = value_race(race, calibration, settings, now=NOW)
    assert all(v.places == 3 for v in vals)
    assert all(v.terms_fragile for v in vals)
    assert any("one scratching" in n for v in vals for n in v.quality_notes)


def test_small_field_has_no_place_market(fixture, settings, calibration):
    race = _race(fixture, "racecard_open_6_runners.json")
    for r in race.runners[4:]:
        r.status = "scratched"
    with pytest.raises(NoPlaceMarketError):
        value_race(race, calibration, settings, now=NOW)


def test_suspect_row_is_produced_and_never_bet(fixture, settings, calibration):
    race = _race(fixture, "racecard_open_9_runners.json")
    # A stale place price left far too big after a market move.
    race.runners[0].place_price = 4.50
    vals = value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True)
    top = next(v for v in vals if v.horse_name == "Ten Bagger")
    assert top.drz > settings.drz_suspect
    assert top.tier == TIER_SUSPECT
    assert top.suggested_stake is None


def test_betfair_second_opinion_is_recorded(fixture, settings, calibration):
    race = _race(fixture, "racecard_open_9_runners.json")
    active = race.active_runners()
    # A plausible exchange book, reliable for every runner.
    raw = [1 / (r.win_price * 0.95) for r in active]
    total = sum(raw)
    probs = [p / total for p in raw]
    vals = value_race(
        race, calibration, settings, now=NOW,
        betfair_win_probs=probs,
        betfair_reliable=[True] * len(active),
        betfair_place_probs={active[0].source_id: 0.61},
        allow_uncalibrated=True,
    )
    assert all("betfair" in v.drz_by_model for v in vals)
    assert all("sportsbet_power" in v.drz_by_model for v in vals)
    assert all(v.win_model == "betfair" for v in vals)
    top = next(v for v in vals if v.runner_source_id == active[0].source_id)
    assert top.p_place_betfair == 0.61


def test_partial_betfair_book_is_refused(fixture, settings, calibration):
    race = _race(fixture, "racecard_open_9_runners.json")
    n = len(race.active_runners())
    vals = value_race(
        race, calibration, settings, now=NOW,
        betfair_win_probs=[0.2] * (n - 1) + [None],
        betfair_reliable=[True] * (n - 1) + [False],
        allow_uncalibrated=True,
    )
    assert all("betfair" not in v.drz_by_model for v in vals)


def test_persist_render_and_settle_with_a_dead_heat(
    fixture, settings, calibration, tmp_path, capsys, monkeypatch
):
    """The realistic flow: value an open race, then settle it once it resolves.

    The result arrives the way the scanner actually gets it — by re-reading
    the racecard after the race has run, which carries each runner's
    finishing position. Two of them share third.
    """
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    race = _race(fixture, "racecard_open_9_runners.json")
    vals = value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True)

    Session = session_factory(settings.database_url)
    with Session() as session:
        repo = Repository(session)
        meeting = repo.upsert_meeting(
            MeetingInfo(source="sportsbet", source_id="5701",
                        venue="Fixtureville", meeting_date=NOW.date())
        )
        race_row = repo.upsert_race(meeting, race)
        rows = {}
        for runner in race.runners:
            rows[runner.source_id] = repo.upsert_runner(race_row, runner)
            repo.record_price(race_row, rows[runner.source_id], runner,
                              observed_at=NOW, seconds_to_jump=900.0,
                              raw_sha256=race.raw_sha256)
        for v in vals:
            repo.record_valuation(v, race_row, rows[v.runner_source_id])
        session.commit()

        stored = session.query(m.PlaceValuation).all()
        assert len(stored) == 9
        one = stored[0]
        assert json.loads(one.win_probs_json)
        assert json.loads(one.p_place_json)
        assert json.loads(one.drz_json)
        assert one.calibration_version == calibration.version
        assert session.query(m.RunnerPrice).count() == 10  # scratching included
        assert settle_all(session) == 0, "nothing to settle before the race runs"

        # --- the race resolves -------------------------------------------
        resulted = parse_racecard(
            fixture("racecard_resulted_dead_heat.json"),
            event_id="900101", fetched_at=NOW,
        )
        assert resulted.status == "resulted"
        repo.upsert_race(meeting, resulted)
        for runner in resulted.runners:
            repo.upsert_runner(race_row, runner)
        session.commit()

        positions = repo.finish_positions(race_row.race_id)
        assert positions[3] == 3 and positions[9] == 3, "the dead heat is recorded"
        assert session.get(m.Race, race_row.race_id).result_placings == "2,5,3,9"

        assert settle_all(session) == 9

        for v in session.query(m.PlaceValuation).all():
            runner = session.get(m.Runner, v.runner_id)
            assert v.settled
            assert v.settlement_basis == BASIS_POSITIONS
            assert v.deduction_status == "unknown"
            assert v.placed == (runner.saddlecloth in (2, 5, 3, 9))
            expected_div = 2.0 if runner.saddlecloth in (3, 9) else 1.0
            assert v.dead_heat_divisor == pytest.approx(expected_div)

        def row(saddle):
            return next(v for v in session.query(m.PlaceValuation).all()
                        if session.get(m.Runner, v.runner_id).saddlecloth == saddle)

        assert row(2).settled_return == pytest.approx(row(2).place_price)
        assert row(3).settled_return == pytest.approx(row(3).place_price / 2)
        assert row(1).settled_return == 0.0

        run_report(session)

    # Render into a wide recording console so column ellipsis cannot make the
    # assertions below pass or fail for cosmetic reasons.
    from rich.console import Console

    import app.reporting.tables as tables

    recorder = Console(width=220, record=True, force_terminal=False)
    monkeypatch.setattr(tables, "console", recorder)
    tables.render_scan([vals], calibration, settled_bets=9, proven_threshold=300,
                       retrieved_at=NOW, show_all=True)
    out = recorder.export_text()
    assert "UNPROVEN" in out
    assert "Fixtureville" in out
    assert "Ten Bagger" in out
    assert "9 runners, 3 places" in out


# Per-runner finishing positions: the unambiguous path. Two runners sharing
# a position dead-heated for it, and only they carry a divisor.
DEAD_HEAT_THIRD = {2: 1, 5: 2, 3: 3, 9: 3, 1: 5, 6: 7}
DEAD_HEAT_SECOND = {1: 1, 4: 2, 7: 2, 2: 4}
CLEAN_FINISH = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}


@pytest.mark.parametrize("positions,places,saddle,expect_placed,expect_div", [
    (CLEAN_FINISH, 3, 3, True, 1.0),
    (CLEAN_FINISH, 3, 4, False, 1.0),
    (CLEAN_FINISH, 2, 2, True, 1.0),
    (CLEAN_FINISH, 2, 3, False, 1.0),
    # Dead heat for third over three dividends: 2 and 5 paid in full, 3 and 9
    # share the third dividend, everyone else gets nothing and no divisor.
    (DEAD_HEAT_THIRD, 3, 2, True, 1.0),
    (DEAD_HEAT_THIRD, 3, 5, True, 1.0),
    (DEAD_HEAT_THIRD, 3, 3, True, 2.0),
    (DEAD_HEAT_THIRD, 3, 9, True, 2.0),
    (DEAD_HEAT_THIRD, 3, 1, False, 1.0),
    (DEAD_HEAT_THIRD, 3, 6, False, 1.0),
    # Dead heat for second over two dividends.
    (DEAD_HEAT_SECOND, 2, 1, True, 1.0),
    (DEAD_HEAT_SECOND, 2, 4, True, 2.0),
    (DEAD_HEAT_SECOND, 2, 7, True, 2.0),
    (DEAD_HEAT_SECOND, 2, 2, False, 1.0),
])
def test_settle_from_finish_positions(positions, places, saddle,
                                      expect_placed, expect_div):
    s = settle_runner(saddle, [], places, place_price=3.0, positions=positions)
    assert s.basis == BASIS_POSITIONS
    assert s.placed is expect_placed
    assert s.dead_heat_divisor == pytest.approx(expect_div)
    assert s.gross_return == pytest.approx(
        3.0 / expect_div if expect_placed else 0.0
    )


def test_a_beaten_runner_never_carries_a_dead_heat_divisor():
    """Otherwise every loser in the race is reported as a dead-heat settlement."""
    beaten = settle_runner(6, [], 3, 3.0, positions=DEAD_HEAT_THIRD)
    assert not beaten.placed
    assert beaten.dead_heat_divisor == 1.0


@pytest.mark.parametrize("placings,places,saddle,expect_placed", [
    ([1, 2, 3], 3, 3, True),
    ([1, 2, 3], 3, 4, False),
    ([1, 2], 2, 2, True),
    ([1, 2], 2, 3, False),
])
def test_settle_from_placings_when_positions_are_absent(placings, places,
                                                        saddle, expect_placed):
    s = settle_runner(saddle, placings, places, place_price=3.0)
    assert s.basis == BASIS_PLACINGS
    assert s.placed is expect_placed
    assert s.dead_heat_divisor == 1.0


def test_a_full_finishing_order_is_never_mistaken_for_a_dead_heat():
    """The bug this contract exists to prevent.

    The placings string is a finishing order, so a 3-place race can arrive
    with every runner listed. Reading the surplus as a dead heat would settle
    the runner that finished LAST as a placed runner paid a fractional
    dividend. Only the first three may be paid, and no dead heat may be
    inferred from length alone.
    """
    full_order = [2, 5, 3, 9, 1, 7, 4, 8, 6]
    paid = [s for s in full_order
            if settle_runner(s, full_order, 3, 3.40).placed]
    assert paid == [2, 5, 3]
    last = settle_runner(6, full_order, 3, 3.40)
    assert not last.placed and last.gross_return == 0.0
    for saddle in (2, 5, 3):
        assert settle_runner(saddle, full_order, 3, 3.40).gross_return == 3.40


def test_positions_take_precedence_over_the_placings_string():
    s = settle_runner(9, [2, 5, 3], 3, 3.0, positions=DEAD_HEAT_THIRD)
    assert s.basis == BASIS_POSITIONS
    assert s.placed and s.dead_heat_divisor == 2.0


def test_settle_runner_refuses_without_evidence():
    assert settle_runner(None, [1, 2, 3], 3, 2.0) is None
    assert settle_runner(1, [], 3, 2.0) is None
    assert settle_runner(1, [1, 2, 3], 3, None) is None
    # A runner absent from the position map falls back to the placings string.
    assert settle_runner(99, [1, 2, 3], 3, 2.0, positions={1: 1}).basis == BASIS_PLACINGS
    # ...and with neither source, nothing is settled.
    assert settle_runner(99, [], 3, 2.0, positions={1: 1}) is None


def test_a_runner_scratched_after_valuation_settles_as_a_void(
    fixture, settings, calibration, monkeypatch
):
    """Sportsbet refunds a fixed-odds bet on a scratching. Settling it as a
    loss would put a phantom -100% into ROI for a bet that never happened."""
    from app.backtest import BASIS_VOID

    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    race = _race(fixture, "racecard_open_9_runners.json")
    vals = value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True)

    Session = session_factory(settings.database_url)
    with Session() as session:
        repo = Repository(session)
        meeting = repo.upsert_meeting(MeetingInfo(
            source="sportsbet", source_id="5701", venue="Fixtureville",
            meeting_date=NOW.date()))
        race_row = repo.upsert_race(meeting, race)
        rows = {r.source_id: repo.upsert_runner(race_row, r) for r in race.runners}
        for v in vals:
            repo.record_valuation(v, race_row, rows[v.runner_source_id])
        session.commit()

        # The race resolves; runner 4 was scratched after we valued it.
        resulted = parse_racecard(fixture("racecard_resulted_dead_heat.json"),
                                  event_id="900101", fetched_at=NOW)
        for runner in resulted.runners:
            if runner.saddlecloth == 4:
                runner.status = "scratched"
                runner.finish_position = None
        repo.upsert_race(meeting, resulted)
        for runner in resulted.runners:
            repo.upsert_runner(race_row, runner)
        session.commit()
        assert settle_all(session) == 9

        def row(saddle):
            return next(v for v in session.query(m.PlaceValuation).all()
                        if session.get(m.Runner, v.runner_id).saddlecloth == saddle)

        void = row(4)
        assert void.void and void.placed is None
        assert void.settled_return == pytest.approx(1.0), "stake refunded"
        assert void.settlement_basis == BASIS_VOID
        assert not row(2).void and row(2).placed
        assert not row(1).void and row(1).placed is False

        # Strike rate ignores voids; ROI counts them at their stake.
        from app.backtest import _roi_row

        n, roi, strike = _roi_row([void, row(2), row(1)])
        assert n == 3
        assert strike == pytest.approx(0.5)
        assert roi == pytest.approx((1.0 + row(2).place_price + 0.0) / 3 - 1.0)
