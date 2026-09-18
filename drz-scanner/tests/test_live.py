"""Live Sportsbet checks. Marked ``live``; skipped wherever the site is
unreachable (which includes this build's environment — see BUILD_STATUS.md).

Run with::

    python -m pytest -m live

These are the tests that would close the remaining schema questions: whether
the live ``priceCode=L`` entry really carries ``placePrice``, and what the
other price codes are.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.http import SourceUnavailableError
from app.sources.base import SchemaMismatchError
from app.sources.sportsbet import (
    LIVE_PRICE_CODE,
    PLACE_PRICE_KEYS,
    SportsbetClient,
)

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    with SportsbetClient() as c:
        yield c


@pytest.fixture(scope="module")
def schedule(client):
    try:
        return client.fetch_schedule(datetime.now(timezone.utc).date())
    except SourceUnavailableError as exc:
        pytest.skip(f"sportsbet unreachable: {exc}")


def test_schedule_has_thoroughbred_meetings(schedule):
    meetings, stubs, _ = schedule
    assert meetings, "no thoroughbred meetings returned"
    assert all(m.class_name and m.class_name.lower().startswith("horse") for m in meetings)
    assert stubs


def test_an_open_racecard_carries_a_live_place_price(client, schedule):
    """The open question from BUILD_STATUS.md, answered by a real payload."""
    _meetings, stubs, _ = schedule
    open_stubs = [s for s in stubs if s.is_open][:5]
    if not open_stubs:
        pytest.skip("no open races right now")

    errors = []
    for stub in open_stubs:
        try:
            race = client.fetch_racecard(stub.event_id)
        except (SourceUnavailableError, SchemaMismatchError) as exc:
            errors.append(str(exc))
            continue
        active = race.active_runners()
        assert active, f"{stub.meeting_name} R{stub.race_number}: no active runners"
        assert all(r.win_price for r in active)
        assert any(r.place_price for r in active), (
            f"no active runner carries a place price under code "
            f"{LIVE_PRICE_CODE}; tried {PLACE_PRICE_KEYS}"
        )
        return
    pytest.fail("no racecard parsed:\n" + "\n".join(errors))


def test_report_every_price_code_seen(client, schedule):
    """Not an assertion — a record of what the live payload actually carries.

    Run this and paste the output into BUILD_STATUS.md to settle what MDP,
    TMD and any other code mean.
    """
    _meetings, stubs, _ = schedule
    open_stubs = [s for s in stubs if s.is_open][:3]
    if not open_stubs:
        pytest.skip("no open races right now")
    seen: dict[str, dict] = {}
    for stub in open_stubs:
        try:
            race = client.fetch_racecard(stub.event_id)
        except (SourceUnavailableError, SchemaMismatchError):
            continue
        for runner in race.runners:
            for code, quote in runner.prices_by_code.items():
                entry = seen.setdefault(code, {"n": 0, "with_win": 0, "with_place": 0})
                entry["n"] += 1
                entry["with_win"] += quote.win_price is not None
                entry["with_place"] += quote.place_price is not None
    print("\nprice codes observed live:")
    for code, stats in sorted(seen.items()):
        print(f"  {code:6s} n={stats['n']:4d} "
              f"win={stats['with_win']:4d} place={stats['with_place']:4d}")
    assert seen, "no price codes seen at all"
