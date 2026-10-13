"""Signal tracking, results report verdicts, setup wizard and place parsing."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from app.config import Settings
from app.setup_wizard import update_env
from app.sources.betfair import BetfairMarket
from app.sources.sportsbet import SportsbetClient
from app.value.compare import ValueRow
from app.value.report import stats, verdict
from app.value.tracker import Tracker, outcome
from tests.test_betfair_end_to_end import NOW, settings

FIXTURES = Path(__file__).parent / "fixtures"


def row(ev=0.05, flags=None, sid=1, kind="WIN"):
    return ValueRow(
        kind=kind, venue="Randwick", race_number=1, start_time=NOW + timedelta(minutes=10),
        horse="Alpha", saddlecloth=1, sb_odds=3.0, bf_back=2.6, bf_lay=2.62,
        bf_lay_fill=2.62, p_fair=(1 + ev) / 3.0, ev=ev, ev_at_lay=None, lock=None,
        kelly=0.0, sb_overround=1.1, bf_book=1.0, bf_matched=1e5, snapshot_gap=1.0,
        market_id="1.1", selection_id=sid, flags=flags or [],
    )


def tracker(tmp_path, **over):
    return Tracker(settings(**over), tmp_path / "s.json", tmp_path / "r.csv")


def test_streaks_confirm_on_the_second_scan_and_reset_on_a_miss(tmp_path):
    t = tracker(tmp_path)
    r = row()
    t.observe([r], [], NOW)
    assert r.streak == 1 and t.signals[r.key].confirmed_at is None
    r = row()
    t.observe([r], [], NOW + timedelta(minutes=1))
    assert r.streak == 2 and t.signals[r.key].confirmed_odds == 3.0
    r = row(ev=0.0)  # below VALUE_MIN_EV
    t.observe([r], [], NOW + timedelta(minutes=2))
    assert r.streak == 0
    r = row()
    t.observe([r], [], NOW + timedelta(minutes=3))
    assert r.streak == 1  # a gap restarts the count; the signal stays confirmed
    assert t.signals[r.key].confirmed_at is not None


def test_unreliable_rows_never_become_signals(tmp_path):
    t = tracker(tmp_path)
    t.observe([row(ev=0.2, flags=["SPREAD 6 ticks"])], [], NOW)
    assert t.signals == {}


def test_streaks_expire_after_a_long_break(tmp_path):
    t = tracker(tmp_path)
    t.observe([row()], [], NOW)
    t2 = tracker(tmp_path)  # restart, much later
    r = row()
    t2.observe([r], [], NOW + timedelta(minutes=30))
    assert r.streak == 1


def test_confirmation_count_is_configurable(tmp_path):
    t = tracker(tmp_path, value_confirm_scans=1)
    r = row()
    t.observe([r], [], NOW)
    assert t.signals[r.key].confirmed_at is not None


def test_outcomes():
    assert outcome("WIN", "WINNER") == "won"
    assert outcome("WIN", "LOSER") == "lost"
    assert outcome("PLACE", "WINNER") == "placed"
    assert outcome("PLACE", "LOSER") == "unplaced"
    assert outcome("WIN", "REMOVED_VACANT") == "void"
    assert outcome("WIN", "ACTIVE") is None


def test_void_and_timeout_settlement(tmp_path):
    t = tracker(tmp_path)
    t.observe([row(sid=1), row(sid=2)], [], NOW)
    tm = t.markets["1.1"]
    tm.off_at = (NOW + timedelta(minutes=10)).isoformat()
    m = BetfairMarket("1.1", "", "Randwick", NOW, 1, book_status="CLOSED",
                      runner_status={1: "REMOVED", 2: "LOSER"})
    settled = {s.selection_id: s.result for s in t.apply_results([m], NOW + timedelta(minutes=15))}
    assert settled == {1: "void", 2: "lost"}
    rows = (tmp_path / "r.csv").read_text().splitlines()
    assert len(rows) == 3 and ",void,0.0" in rows[1] + rows[2]

    t.observe([row(sid=3)], [], NOW + timedelta(hours=1))
    t.markets["1.1"].off_at = NOW.isoformat()
    m = BetfairMarket("1.1", "", "Randwick", NOW, 1, book_status="OPEN")
    late = t.apply_results([m], NOW + timedelta(hours=9))
    assert [s.result for s in late] == ["unknown"]


def test_state_file_is_plain_json(tmp_path):
    t = tracker(tmp_path)
    t.observe([row()], [], NOW)
    data = json.loads((tmp_path / "s.json").read_text())
    assert set(data) == {"streaks", "streaks_at", "signals", "markets"}


# -- report verdicts ----------------------------------------------------------

def results(n, clv, won_every=3, odds=3.0):
    return [{"kind": "WIN", "result": "won" if i % won_every == 0 else "lost",
             "pnl": str(odds - 1 if i % won_every == 0 else -1.0), "clv": str(clv),
             "bet_ev": "0.05", "bet_p": "0.35", "vs_bsp": "", "confirmed": "True",
             "close_seconds_before_start": "20"} for i in range(n)]


def test_report_says_too_early_with_few_bets():
    assert verdict(stats("x", results(10, 0.04)))[0].startswith("Too early to tell")


def test_report_good_sign_and_warning():
    good = verdict(stats("x", results(120, 0.04)))
    assert good[0].startswith("Good sign") and "100%" in good[0]
    bad = verdict(stats("x", results(120, -0.03)))
    assert bad[0].startswith("Warning")


# -- setup wizard ---------------------------------------------------------------

def test_update_env_keeps_comments_and_round_trips_awkward_passwords(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# my notes\n#BETFAIR_APP_KEY=example\nBETFAIR_USERNAME='old'\nOTHER=1\n")
    pw = "p@ss'w$rd #1\\x"
    update_env(env, {"BETFAIR_USERNAME": "punter", "BETFAIR_PASSWORD": pw,
                     "BETFAIR_APP_KEY": "abc123"})
    text = env.read_text()
    assert "# my notes" in text and "#BETFAIR_APP_KEY=example" in text and "OTHER=1" in text
    assert text.count("BETFAIR_USERNAME") == 1
    s = Settings(_env_file=str(env))
    assert (s.betfair_username, s.betfair_password, s.betfair_app_key) == ("punter", pw, "abc123")


# -- Sportsbet place prices ---------------------------------------------------------

def test_sportsbet_place_price_and_terms_are_parsed():
    payload = json.loads((FIXTURES / "racecard_live_shape_synthetic.json").read_text())
    card = SportsbetClient.__new__(SportsbetClient).parse_racecard(payload, "x")
    race = card.races[0]
    assert race.places == 3
    by = {r.horse_name: r for r in race.runners}
    assert by["Fast Fixture"].place_odds == 1.3
    assert by["Scratchy Example"].place_odds is None  # scratched: never priced


# -- alert sound ------------------------------------------------------------------

def test_tracker_reports_what_is_new_in_each_scan(tmp_path):
    t = tracker(tmp_path)
    t.observe([row()], [], NOW)
    assert t.new_signals == ["WIN:1.1:1"] and t.new_confirmations == []
    t.observe([row()], [], NOW + timedelta(minutes=1))
    assert t.new_signals == [] and t.new_confirmations == ["WIN:1.1:1"]
    t.observe([row()], [], NOW + timedelta(minutes=2))
    assert t.new_confirmations == []  # chimes once per bet, not every scan


def test_chime_is_a_short_valid_wav(tmp_path):
    import wave
    from app.value.sound import synthesise

    with wave.open(str(synthesise(tmp_path / "c.wav"))) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert 0.3 < w.getnframes() / w.getframerate() < 1.0


def test_chime_falls_back_to_the_terminal_bell(tmp_path, monkeypatch, capsys):
    from app.value import sound

    monkeypatch.setattr(sound.sys, "platform", "linux")
    monkeypatch.setattr(sound.shutil, "which", lambda _: None)
    sound.Chime(tmp_path).play()
    assert capsys.readouterr().out == "\a"
    assert (tmp_path / "chime.wav").exists()
