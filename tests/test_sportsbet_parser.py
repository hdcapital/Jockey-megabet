"""Sportsbet parser tests against clearly-labelled synthetic fixtures.

The fixtures mimic the researched payload *shape*; they contain no real
market data. Live-schema verification happens in the integration test.
"""

import json
from pathlib import Path

import pytest

from app.sources.sportsbet import SportsbetClient, parse_jockey_threshold

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class TestThresholdNameParsing:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Alpha Rider to Ride 2+ Winners", ("Alpha Rider", 2)),
            ("Jockey Megabet - Beta Hoop 3+ Wins", ("Beta Hoop", 3)),
            ("Gamma Pilot to Ride a Double", ("Gamma Pilot", 2)),
            ("Gamma Pilot To Ride A Treble", ("Gamma Pilot", 3)),
            ("Delta Steer to ride 4 or more winners", ("Delta Steer", 4)),
            ("Epsilon Whip 2+ wins", ("Epsilon Whip", 2)),
        ],
    )
    def test_parsed(self, name, expected):
        assert parse_jockey_threshold(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "Most Wins - Any Jockey",
            "Trainer Megabet - Some Trainer 2+ Winners As Trainer Only",
            "Winx to win Race 5",
            "",
        ],
    )
    def test_non_jockey_markets_rejected(self, name):
        assert parse_jockey_threshold(name) is None


class TestMegabetsPayloadParsing:
    def setup_method(self):
        self.client = SportsbetClient.__new__(SportsbetClient)  # no HTTP

    def test_offers_extracted(self):
        offers = SportsbetClient.parse_megabets(self.client, load("megabets_synthetic.json"))
        by_jockey = {o.jockey_name: o for o in offers}
        assert set(by_jockey) == {"Alpha Rider", "Beta Hoop", "Gamma Pilot", "Delta Steer"}
        assert by_jockey["Alpha Rider"].threshold == 2
        assert by_jockey["Alpha Rider"].odds == 3.5
        assert by_jockey["Alpha Rider"].meeting_name == "Testville"
        assert by_jockey["Beta Hoop"].threshold == 3
        assert by_jockey["Gamma Pilot"].threshold == 2  # "double"
        assert by_jockey["Delta Steer"].threshold == 4
        assert by_jockey["Delta Steer"].odds == 26.0

    def test_trainer_and_aggregate_markets_skipped(self):
        offers = SportsbetClient.parse_megabets(self.client, load("megabets_synthetic.json"))
        assert all("Trainer" not in o.jockey_name for o in offers)
        assert all("Any Jockey" not in o.jockey_name for o in offers)

    def test_empty_payload_yields_no_offers(self):
        assert SportsbetClient.parse_megabets(self.client, {}) == []
        assert SportsbetClient.parse_megabets(self.client, {"megabets": []}) == []


class TestListingDiscovery:
    """Discovery over the live-verified listing shape (synthetic fixtures)."""

    def test_jockey_extras_stubs_identified(self):
        listing = load("megabets_listing_synthetic.json")["_payload"]
        stubs = SportsbetClient.jockey_extras_events(listing)
        assert [s["id"] for s in stubs] == [111001]

    def test_stub_meeting_name(self):
        listing = load("megabets_listing_synthetic.json")["_payload"]
        stub = SportsbetClient.jockey_extras_events(listing)[0]
        assert SportsbetClient._stub_meeting_name(stub) == "Testville"

    def test_discovery_fetches_event_racecards(self):
        from datetime import datetime, timezone

        from app.http import FetchResult

        listing = load("megabets_listing_synthetic.json")["_payload"]
        extras_card = load("jockey_extras_racecard_synthetic.json")
        fetched: list[str] = []

        class FakeHttp:
            def get_json(self, url):
                fetched.append(url)
                import json as _json

                body = extras_card if "/Events/" in url else listing
                raw = _json.dumps(body).encode()
                return FetchResult(
                    url=url, status_code=200,
                    fetched_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
                    body=raw, sha256="f" * 64,
                )

        client = SportsbetClient.__new__(SportsbetClient)
        client.client = FakeHttp()
        offers = client.discover_jockey_megabets()

        # Only the Jockey Extras event's racecard is fetched (not trainer/multis).
        assert sum("/Events/111001/" in u for u in fetched) == 1
        assert not any("/Events/111002/" in u for u in fetched)
        by_key = {(o.jockey_name, o.threshold): o for o in offers}
        # Live shape: jockey = market name, thresholds = selection names.
        assert set(by_key) == {
            ("Alpha Rider", 1), ("Alpha Rider", 2), ("Alpha Rider", 3),
            ("Beta Hoop", 2),
        }
        assert by_key[("Alpha Rider", 1)].odds == 1.75
        assert by_key[("Alpha Rider", 2)].odds == 6.5
        assert by_key[("Alpha Rider", 3)].odds == 51.0
        assert by_key[("Beta Hoop", 2)].odds == 2.7
        # Meeting context comes from the listing stub.
        assert all(o.meeting_name == "Testville" for o in offers)
        assert all(o.meeting_source_id == "111001" for o in offers)


class TestSelectionThresholdParsing:
    """Live-verified selection names ('To Ride Two or More Winners')."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("To Ride One or More Winners", 1),
            ("To Ride Two or More Winners", 2),
            ("To Ride Three or More Winners", 3),
            ("To Ride Four or More Winners", 4),
            ("To Ride Five or More Winners", 5),
            ("To Ride 2+ Winners", 2),
            ("To Ride a Winner", 1),
            ("To Ride a Double", 2),
            ("to ride two or more winners", 2),
        ],
    )
    def test_parsed(self, name, expected):
        from app.sources.sportsbet import parse_selection_threshold

        assert parse_selection_threshold(name) == expected

    @pytest.mark.parametrize("name", ["Yes", "Winx", "Most Winners", "To Win The Race"])
    def test_non_threshold_selections_rejected(self, name):
        from app.sources.sportsbet import parse_selection_threshold

        assert parse_selection_threshold(name) is None


class TestLiveShapeRacecardParsing:
    """Ordinary racecard in the live-verified shape (synthetic fixture)."""

    def setup_method(self):
        self.client = SportsbetClient.__new__(SportsbetClient)
        self.card = SportsbetClient.parse_racecard(
            self.client, load("racecard_live_shape_synthetic.json"), event_id="820001"
        )

    def test_venue_and_race_metadata(self):
        assert self.card.meeting.venue == "Testville"
        race = self.card.races[0]
        assert race.race_number == 2
        assert race.status == "open"

    def test_runners_jockeys_and_live_prices(self):
        by_name = {r.horse_name: r for r in self.card.races[0].runners}
        assert set(by_name) == {
            "Fast Fixture", "Second Sample", "Scratchy Example", "Fourth Fake"
        }
        ff = by_name["Fast Fixture"]
        assert ff.jockey_name == "Alpha Rider"
        assert ff.saddlecloth == 1
        # Live 'L' price is used, never the stale MDP/TMD morning prices.
        assert ff.win_odds == 2.3
        assert by_name["Second Sample"].win_odds == 4.6

    def test_scratched_runner_detected_and_not_priced(self):
        by_name = {r.horse_name: r for r in self.card.races[0].runners}
        scr = by_name["Scratchy Example"]
        # statusCode "S" with the live price withdrawn -> scratched, and the
        # stale MDP price must NOT leak through as a live price.
        assert scr.status == "scratched"
        assert scr.win_odds is None


class TestRacecardParsing:
    def setup_method(self):
        self.client = SportsbetClient.__new__(SportsbetClient)

    def test_runners_and_jockeys_extracted(self):
        card = SportsbetClient.parse_racecard(
            self.client, load("racecard_synthetic.json"), event_id="810001"
        )
        assert card.meeting.venue == "Testville"
        race = card.races[0]
        assert race.race_number == 1
        assert race.status == "open"
        assert len(race.runners) == 5
        by_name = {r.horse_name: r for r in race.runners}
        assert by_name["Fast Fixture"].jockey_name == "Alpha Rider"
        assert by_name["Fast Fixture"].win_odds == 2.6
        assert by_name["Fast Fixture"].saddlecloth == 1

    def test_scratched_runner_flagged(self):
        card = SportsbetClient.parse_racecard(
            self.client, load("racecard_synthetic.json"), event_id="810001"
        )
        race = card.races[0]
        scratched = [r for r in race.runners if r.status == "scratched"]
        assert [r.horse_name for r in scratched] == ["Scratchy Example"]
        assert len(race.active_runners()) == 4
