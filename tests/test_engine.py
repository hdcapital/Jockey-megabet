"""Valuation engine tests over synthetic in-memory race data (test fixtures)."""

from datetime import date, datetime, timezone

import pytest

from app.config import Settings
from app.engine import price_race_sportsbet, value_offer
from app.matching.jockeys import find_rides
from app.models.devig import devig
from app.models.poisson_binomial import poisson_binomial
from app.sources.base import MegabetOffer, RaceInfo, RunnerInfo

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def runner(rid, horse, jockey, odds, status="active"):
    return RunnerInfo(
        source="sportsbet", source_id=rid, horse_name=horse, jockey_name=jockey,
        status=status, win_odds=odds, odds_timestamp=NOW,
    )


def make_races():
    r1 = RaceInfo(
        source="sportsbet", source_id="r1", race_number=1, start_time=NOW,
        status="open",
        runners=[
            runner("1", "Horse A", "Alpha Rider", 2.0),
            runner("2", "Horse B", "Other Jockey", 4.0),
            runner("3", "Horse C", "Third Jockey", 4.0),
        ],
    )
    r2 = RaceInfo(
        source="sportsbet", source_id="r2", race_number=2, start_time=NOW,
        status="open",
        runners=[
            runner("4", "Horse D", "A Rider", 3.0),  # initial form of Alpha Rider
            runner("5", "Horse E", "Other Jockey", 3.0),
            runner("6", "Horse F", "Third Jockey", 3.0),
        ],
    )
    return [r1, r2]


def offer(threshold=1, odds=2.5):
    return MegabetOffer(
        source="sportsbet", market_id="m1", selection_id="s1",
        meeting_name="Testville", meeting_source_id=None,
        meeting_date=date(2026, 8, 22), jockey_name="Alpha Rider",
        threshold=threshold, odds=odds, market_name="Alpha Rider to Ride 1+ Winners",
        fetched_at=NOW,
    )


def settings():
    return Settings(_env_file=None, archive_raw_responses=False)


class TestRideMatching:
    def test_finds_both_rides_including_initial_form(self):
        card = find_rides("Alpha Rider", make_races())
        assert card.match_status == "matched"
        assert [rm.runner.horse_name for rm in card.rides] == ["Horse A", "Horse D"]

    def test_scratched_ride_excluded_and_recorded(self):
        races = make_races()
        races[0].runners[0].status = "scratched"
        card = find_rides("Alpha Rider", races)
        assert [rm.runner.horse_name for rm in card.rides] == ["Horse D"]
        assert [rm.runner.horse_name for rm in card.scratched_rides] == ["Horse A"]


class TestValuation:
    def test_fair_value_matches_hand_computation(self):
        races = make_races()
        vals = value_offer(offer(threshold=1, odds=2.5), races, settings(), now=NOW)
        sb = next(v for v in vals if v.model == "sportsbet_novig")
        # Hand computation from the same synthetic inputs:
        p1 = devig([2.0, 4.0, 4.0]).fair_probabilities[0]  # exactly fair book -> 0.5
        p2 = devig([3.0, 3.0, 3.0]).fair_probabilities[0]  # -> 1/3
        expected_p = poisson_binomial([p1, p2]).prob_at_least(1)
        assert sb.fair_probability == pytest.approx(expected_p)
        assert sb.fair_odds == pytest.approx(1 / expected_p)
        assert sb.expected_return == pytest.approx(expected_p * 2.5 - 1)
        assert sb.quality == "MEDIUM"  # no Betfair data in this fixture

    def test_scratching_changes_valuation(self):
        races = make_races()
        before = value_offer(offer(), races, settings(), now=NOW)[0]
        races[0].runners[0].status = "scratched"
        after = value_offer(offer(), races, settings(), now=NOW)[0]
        # One ride left; devig over remaining active runners of race 1 no
        # longer includes the jockey, so P(>=1) is just ride 2's fair prob.
        assert after.fair_probability == pytest.approx(1 / 3)
        assert after.fair_probability < before.fair_probability

    def test_threshold_above_ride_count_gives_zero_probability(self):
        vals = value_offer(offer(threshold=3), make_races(), settings(), now=NOW)
        sb = next(v for v in vals if v.model == "sportsbet_novig")
        assert sb.fair_probability == 0.0
        assert sb.fair_odds is None  # impossible event has no finite fair odds

    def test_unmatched_jockey_yields_unavailable_low_quality(self):
        o = offer()
        o.jockey_name = "Nonexistent Jockey"
        vals = value_offer(o, make_races(), settings(), now=NOW)
        for v in vals:
            assert v.fair_probability is None
            assert v.quality == "LOW"

    def test_betfair_model_unavailable_without_betfair_data(self):
        vals = value_offer(offer(), make_races(), settings(), now=NOW)
        bf = next(v for v in vals if v.model == "betfair")
        assert bf.fair_probability is None
        assert bf.quality == "LOW"

    def test_consensus_falls_back_to_sportsbet(self):
        vals = value_offer(offer(), make_races(), settings(), now=NOW)
        cons = next(v for v in vals if v.model == "consensus")
        sb = next(v for v in vals if v.model == "sportsbet_novig")
        assert cons.fair_probability == pytest.approx(sb.fair_probability)
        assert all(r.consensus.fallback for r in cons.rides)


class TestRacePricing:
    def test_scratched_runner_excluded_from_devig(self):
        races = make_races()
        races[0].runners[1].status = "scratched"
        dv = price_race_sportsbet(races[0], "proportional")
        assert dv.raw_odds == (2.0, 4.0)
        assert sum(dv.fair_probabilities) == pytest.approx(1.0)

    def test_single_priced_runner_unpriceable(self):
        race = RaceInfo(
            source="sportsbet", source_id="rx", race_number=1, start_time=NOW,
            status="open", runners=[runner("1", "Only Horse", "Alpha Rider", 1.5)],
        )
        assert price_race_sportsbet(race, "proportional") is None
