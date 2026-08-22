"""Live integration test against the real Sportsbet service.

This test performs real network requests. Behaviour by environment:

* Network reachable, Jockey Megabets on offer  -> full pipeline assertion.
* Network reachable, no Jockey Megabets now    -> reported and passes
  (that is a legitimate real-world state, not a failure).
* Network blocked (e.g. egress policy denies www.sportsbet.com.au)
  -> skipped with the real error, never faked.

Run explicitly with:  pytest -m live
"""

import logging

import pytest

from app.http import SourceUnavailableError
from app.sources.base import SchemaMismatchError
from app.sources.sportsbet import SportsbetClient

log = logging.getLogger(__name__)

pytestmark = pytest.mark.live


@pytest.mark.live
def test_live_sportsbet_pipeline():
    with SportsbetClient() as sb:
        try:
            offers = sb.discover_jockey_megabets()
        except SourceUnavailableError as exc:
            pytest.skip(f"Sportsbet unreachable from this environment: {exc}")
        except SchemaMismatchError as exc:
            pytest.fail(
                "Sportsbet responded but the Megabets schema did not parse — "
                f"adapt app/sources/sportsbet.py using the archived payload: {exc}"
            )

        if not offers:
            log.info("live check: Sportsbet reachable, zero Jockey Megabets currently offered")
            return  # legitimate state; nothing to price

        offer = offers[0]
        assert offer.jockey_name
        assert offer.threshold >= 1
        assert offer.odds > 1.0

        # Pull today's meetings and derive probabilities for one racecard.
        from datetime import datetime, timezone

        meetings, _ = sb.fetch_meetings(datetime.now(timezone.utc).date())
        assert meetings, "Sportsbet returned no meetings for today"

        from app.sources.sportsbet import _first

        for meeting in meetings:
            races = _first(meeting, "races", "events") or []
            for rn in races:
                event_id = _first(rn, "id", "eventId") if isinstance(rn, dict) else None
                if event_id is None:
                    continue
                card = sb.fetch_racecard(str(event_id))
                priced = [
                    r for race in card.races for r in race.active_runners()
                    if r.win_odds and r.win_odds > 1.0
                ]
                if len(priced) >= 2:
                    from app.models.devig import devig

                    dv = devig([r.win_odds for r in priced])
                    assert abs(sum(dv.fair_probabilities) - 1.0) < 1e-9
                    return
        pytest.fail("no racecard with >=2 priced active runners found today")
