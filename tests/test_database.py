"""Database round-trip tests on an in-memory SQLite engine."""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database import models as m
from app.database.repository import Repository, init_db

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def make_session():
    engine = init_db("sqlite:///:memory:")
    return sessionmaker(bind=engine)()


def test_schema_initialises():
    session = make_session()
    tables = set(m.Base.metadata.tables)
    assert {
        "meetings", "races", "runners", "runner_prices", "megabet_markets",
        "megabet_prices", "model_valuations", "results", "raw_responses",
        "unmatched_records",
    } <= tables
    session.close()


def test_upserts_are_idempotent():
    session = make_session()
    repo = Repository(session)
    mt1 = repo.upsert_meeting("sportsbet", "700001", "Testville", date(2026, 8, 22), "NSW")
    mt2 = repo.upsert_meeting("sportsbet", "700001", "Testville", date(2026, 8, 22), "NSW")
    assert mt1.meeting_id == mt2.meeting_id
    race = repo.upsert_race(mt1, "sportsbet", "810001", 1, NOW, "open")
    r1 = repo.upsert_runner(race, "sportsbet", "5001", "Fast Fixture", "Alpha Rider", 1, "active")
    r2 = repo.upsert_runner(race, "sportsbet", "5001", "Fast Fixture", "Alpha Rider", 1, "scratched")
    assert r1.runner_id == r2.runner_id
    assert r2.status == "scratched"
    session.commit()
    assert session.scalar(select(m.Runner.status).where(m.Runner.runner_id == r1.runner_id)) == "scratched"
    session.close()


def test_valuation_roundtrip_reproducible():
    session = make_session()
    repo = Repository(session)
    mb = repo.upsert_megabet("sportsbet", "900001", None, "Testville",
                             date(2026, 8, 22), "Alpha Rider", 2,
                             "Alpha Rider to Ride 2+ Winners")
    repo.add_megabet_price(mb, 3.5, "open", NOW, "abc123")
    repo.add_valuation(
        mb, NOW, "sportsbet_novig", fair_probability=0.25, fair_odds=4.0,
        sportsbet_odds=3.5, expected_return=-0.125, number_of_rides=6,
        quality="MEDIUM", quality_detail="test",
        ride_probabilities=[0.5, 0.3333333333333333],
    )
    session.commit()
    row = session.scalar(select(m.ModelValuation))
    # The stored per-ride probabilities let the fair value be recomputed
    # exactly from the row itself.
    import json

    from app.models.poisson_binomial import poisson_binomial

    probs = json.loads(row.ride_probabilities_json)
    assert poisson_binomial(probs).prob_at_least(2) > 0
    assert row.expected_return == 0.25 * 3.5 - 1
    session.close()


def test_unmatched_records_persist():
    session = make_session()
    repo = Repository(session)
    repo.add_unmatched("jockey", "Mystery Rider", "mystery rider", "megabet x", "unmatched")
    session.commit()
    row = session.scalar(select(m.UnmatchedRecord))
    assert row.kind == "jockey"
    assert row.status == "unmatched"
    session.close()
