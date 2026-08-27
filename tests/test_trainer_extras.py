"""Trainer Extras tests: discovery, matching and valuation (synthetic fixtures)."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.engine import value_offer
from app.matching.trainers import find_trainer_entries
from app.models.devig import devig
from app.models.poisson_binomial import poisson_binomial
from app.sources.base import MegabetOffer, RaceInfo, RunnerInfo
from app.sources.sportsbet import SportsbetClient, parse_selection_threshold

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIXTURES / name).read_text())


def settings():
    return Settings(_env_file=None, archive_raw_responses=False)


def runner(rid, horse, jockey, trainer, odds, status="active"):
    return RunnerInfo(source="sportsbet", source_id=rid, horse_name=horse,
                      jockey_name=jockey, trainer_name=trainer, status=status,
                      win_odds=odds, odds_timestamp=NOW)


def make_races():
    # Race 1: trainer has TWO runners (2.0 and 4.0 in an exactly fair book).
    r1 = RaceInfo(source="sportsbet", source_id="r1", race_number=1,
                  start_time=NOW, status="open",
                  runners=[runner("1", "Horse A", "J One", "Tessa Trainer", 2.0),
                           runner("2", "Horse B", "J Two", "Tessa Trainer", 4.0),
                           runner("3", "Horse C", "J Three", "Other Trainer", 4.0)])
    # Race 2: one runner, one scratched runner for the trainer.
    r2 = RaceInfo(source="sportsbet", source_id="r2", race_number=2,
                  start_time=NOW, status="open",
                  runners=[runner("4", "Horse D", "J One", "Tania Trainer", 3.0),
                           runner("5", "Horse E", "J Two", "Tessa Trainer", 3.0,
                                  status="scratched"),
                           runner("6", "Horse F", "J Three", "Other Trainer", 3.0),
                           runner("7", "Horse G", "J Four", "Third Trainer", 3.0)])
    return [r1, r2]


class TestSelectionVerbs:
    @pytest.mark.parametrize("name,expected", [
        ("To Train One or More Winners", 1),
        ("To Train Two or More Winners", 2),
        ("To Have Two or More Winners", 2),
        ("To Saddle Three or More Winners", 3),
        ("To Train a Winning Double", 2),
    ])
    def test_trainer_verbs(self, name, expected):
        assert parse_selection_threshold(name) == expected


class TestTrainerDiscovery:
    def test_trainer_event_stub_identified(self):
        listing = load("megabets_listing_synthetic.json")["_payload"]
        stubs = SportsbetClient.extras_events(listing, "trainer")
        assert [s["id"] for s in stubs] == [111002]

    def test_trainer_offers_parsed(self):
        client = SportsbetClient.__new__(SportsbetClient)
        offers = SportsbetClient.parse_megabets(
            client, load("trainer_extras_racecard_synthetic.json"),
            market_type="trainer",
        )
        by_key = {(o.jockey_name, o.threshold): o for o in offers}
        assert set(by_key) == {("Tessa Trainer", 1), ("Tessa Trainer", 2),
                               ("Sam Saddler", 2)}
        assert by_key[("Tessa Trainer", 2)].odds == 8.0
        assert all(o.market_type == "trainer" for o in offers)


class TestTrainerMatching:
    def test_multiple_runners_in_one_race_not_ambiguous(self):
        card = find_trainer_entries("Tessa Trainer", make_races())
        assert card.match_status == "matched"
        assert len(card.rides) == 1  # only race 1 has ACTIVE runners
        assert sorted(r.horse_name for r in card.rides[0].runners) == \
            ["Horse A", "Horse B"]

    def test_initial_form_matches(self):
        # "T Trainer" abbreviates both "Tessa Trainer" (r1) and
        # "Tania Trainer" (r2), so the initial form finds both races —
        # while the two full names never match each other.
        card = find_trainer_entries("T Trainer", make_races())
        assert len(card.rides) == 2
        tessa = find_trainer_entries("Tessa Trainer", make_races())
        assert [e.race.source_id for e in tessa.rides] == ["r1"]

    def test_scratched_only_race_recorded_separately(self):
        races = make_races()
        races[0].runners[0].status = "scratched"
        races[0].runners[1].status = "scratched"
        card = find_trainer_entries("Tessa Trainer", races)
        assert card.rides == []
        # Race 1 (both runners scratched) plus race 2 (Horse E, scratched
        # in the base fixture) are both recorded as scratched-only races.
        assert len(card.scratched_rides) == 2


class TestTrainerValuation:
    def offer(self, threshold, odds):
        return MegabetOffer(
            source="sportsbet", market_id="m1", selection_id="s1",
            meeting_name="Testville", meeting_source_id=None,
            meeting_date=date(2026, 8, 22), jockey_name="Tessa Trainer",
            threshold=threshold, odds=odds,
            market_name="Tessa Trainer - To Train 1+ Winners",
            fetched_at=NOW, market_type="trainer",
        )

    def test_race_probability_is_sum_of_runner_probabilities(self):
        vals = value_offer(self.offer(1, 2.0), make_races(), settings(), now=NOW)
        sb = next(v for v in vals if v.model == "sportsbet_novig")
        # Race 1 fair probs: exactly fair book [2.0, 4.0, 4.0] -> .5 + .25.
        p1 = sum(devig([2.0, 4.0, 4.0]).fair_probabilities[:2])
        expected = poisson_binomial([p1]).prob_at_least(1)
        assert sb.fair_probability == pytest.approx(expected)
        assert sb.fair_probability == pytest.approx(0.75)
        assert sb.expected_return == pytest.approx(0.75 * 2.0 - 1)
        assert sb.quality == "MEDIUM"

    def test_scratched_runner_excluded_from_sum(self):
        races = make_races()
        # Un-scratch Horse E: trainer now has entries in both races.
        races[1].runners[1].status = "active"
        before = value_offer(self.offer(2, 10.0), races, settings(), now=NOW)[0]
        races[1].runners[1].status = "scratched"
        after = value_offer(self.offer(2, 10.0), races, settings(), now=NOW)[0]
        # With the race-2 runner scratched the trainer can no longer win
        # twice (only one active race entry) -> P(2+) becomes 0.
        assert before.fair_probability > 0
        assert after.fair_probability == 0.0

    def test_betfair_model_unavailable_for_trainers(self):
        vals = value_offer(self.offer(1, 2.0), make_races(), settings(), now=NOW)
        bf = next(v for v in vals if v.model == "betfair")
        assert bf.fair_probability is None
        cons = next(v for v in vals if v.model == "consensus")
        sb = next(v for v in vals if v.model == "sportsbet_novig")
        assert cons.fair_probability == pytest.approx(sb.fair_probability)
