"""The browser report: every race, every runner, nothing invented, nothing unescaped."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine import value_race
from app.reporting.html_report import render_html, write_report
from app.sources.sportsbet import parse_racecard

NOW = datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)


def _races(fixture, settings, calibration):
    out = []
    for name, mins in (("racecard_open_9_runners.json", 15), ("racecard_open_6_runners.json", 40)):
        race = parse_racecard(fixture(name), event_id=name, fetched_at=NOW)
        race.start_time = NOW + timedelta(minutes=mins)
        race.runners[2].place_price = 2.55
        race.runners[2].horse_name = 'Tricky <b>"Name"</b> & Co'
        out.append(value_race(race, calibration, settings, now=NOW, allow_uncalibrated=True,
                              betfair_place_probs={race.runners[2].source_id: 0.5}))
    return out


def test_report_lists_every_race_and_runner_and_escapes_html(fixture, settings, calibration):
    races = _races(fixture, settings, calibration)
    page = render_html(races, calibration, settled_bets=0, proven_threshold=300,
                       retrieved_at=NOW, skipped=["Nowhere R1: country New Zealand, not valued"])
    assert page.count('<section class="race') == 2
    import html as _html

    for race in races:
        for v in race:
            assert _html.escape(v.horse_name) in page, v.horse_name
    assert '<b>"Name"</b>' not in page, "runner names must be escaped"
    assert "&lt;b&gt;" in page
    assert "UNPROVEN" in page
    assert 'http-equiv="refresh"' in page
    assert "Not valued this sweep" in page and "New Zealand" in page
    # The 9-runner race has a confirmed BET and is ordered first (earlier jump).
    assert page.index("Fixtureville R4") < page.index("Fixtureville R5")
    assert 'class="BET"' in page
    assert "BF place" in page


def test_report_writes_atomically(tmp_path, fixture, settings, calibration):
    target = tmp_path / "sub" / "latest.html"
    path = write_report(target, by_race=_races(fixture, settings, calibration),
                        calibration=calibration, settled_bets=0, proven_threshold=300,
                        retrieved_at=NOW, skipped=[])
    assert path == target and path.exists()
    assert not (tmp_path / "sub" / "latest.html.tmp").exists()
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_empty_sweep_still_renders(calibration):
    page = render_html([], calibration, settled_bets=0, proven_threshold=300, retrieved_at=NOW)
    assert "No open Australian race" in page
