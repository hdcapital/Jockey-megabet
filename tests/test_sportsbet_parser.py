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
        by_jockey = {o.jockey_name: o for o in offers}
        assert set(by_jockey) == {"Alpha Rider", "Beta Hoop"}
        assert by_jockey["Alpha Rider"].threshold == 2
        assert by_jockey["Alpha Rider"].odds == 3.4
        assert by_jockey["Beta Hoop"].threshold == 1  # "to ride a winner"
        assert by_jockey["Beta Hoop"].odds == 1.65
        # Meeting context comes from the listing stub.
        assert all(o.meeting_name == "Testville" for o in offers)
        assert all(o.meeting_source_id == "111001" for o in offers)


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
