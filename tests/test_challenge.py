"""Jockey Challenge (most wins) model tests — synthetic probability fixtures."""

import itertools
from datetime import date, datetime, timezone

import pytest

from app.config import Settings
from app.engine import value_challenges
from app.models.challenge import simulate_most_wins
from app.sources.base import ChallengeOffer, RaceInfo, RunnerInfo

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def exact_most_wins(race_probs):
    """Independent brute-force enumeration of dead-heat-adjusted win shares."""
    competitors = sorted({c for rp in race_probs for c in rp})
    shares = {c: 0.0 for c in competitors}
    other = 0.0
    outcomes_per_race = []
    for rp in race_probs:
        outs = [(c, p) for c, p in rp.items()]
        outs.append(("__none__", 1.0 - sum(rp.values())))
        outcomes_per_race.append(outs)
    for combo in itertools.product(*outcomes_per_race):
        prob = 1.0
        counts = {}
        none_races = 0
        for name, p in combo:
            prob *= p
            if name == "__none__":
                none_races += 1
            else:
                counts[name] = counts.get(name, 0) + 1
        best = max(counts.values(), default=0)
        if best == 0:
            if none_races:
                other += prob
            continue
        leaders = [c for c, n in counts.items() if n == best]
        for c in leaders:
            shares[c] += prob / len(leaders)
    return shares, other


class TestSimulation:
    RACE_PROBS = [
        {"alpha rider": 0.5, "beta hoop": 0.3},
        {"alpha rider": 0.4, "beta hoop": 0.4},
        {"beta hoop": 0.6},
    ]

    def test_matches_exact_enumeration(self):
        exact, exact_other = exact_most_wins(self.RACE_PROBS)
        result = simulate_most_wins(self.RACE_PROBS, n_sims=200_000, seed=7)
        for c, p in exact.items():
            assert result.probabilities[c] == pytest.approx(p, abs=0.005)
        assert result.other_probability == pytest.approx(exact_other, abs=0.005)

    def test_seed_reproducible(self):
        a = simulate_most_wins(self.RACE_PROBS, n_sims=20_000, seed=42)
        b = simulate_most_wins(self.RACE_PROBS, n_sims=20_000, seed=42)
        assert a.probabilities == b.probabilities

    def test_probability_mass_conserved(self):
        r = simulate_most_wins(self.RACE_PROBS, n_sims=50_000, seed=3)
        total = (sum(r.probabilities.values()) + r.other_probability
                 + r.no_winner_probability)
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_dominant_jockey(self):
        r = simulate_most_wins([{"a": 0.9}, {"a": 0.9}], n_sims=50_000, seed=1)
        assert r.probabilities["a"] > 0.95

    def test_rejects_overfull_race(self):
        with pytest.raises(ValueError):
            simulate_most_wins([{"a": 0.7, "b": 0.5}])


def runner(rid, horse, jockey, odds):
    return RunnerInfo(source="sportsbet", source_id=rid, horse_name=horse,
                      jockey_name=jockey, status="active", win_odds=odds,
                      odds_timestamp=NOW)


def offer(competitor, odds):
    return ChallengeOffer(
        source="sportsbet", market_id="c1", selection_id=competitor,
        meeting_name="Testville", meeting_source_id="111099",
        meeting_date=date(2026, 8, 22), competitor=competitor, odds=odds,
        market_name="Jockey Challenge - Testville", fetched_at=NOW,
    )


class TestValueChallenges:
    def make_world(self):
        races = [
            RaceInfo(source="sportsbet", source_id=f"r{i}", race_number=i,
                     start_time=NOW, status="open",
                     runners=[runner(f"{i}a", f"Horse A{i}", "Alpha Rider", 2.0),
                              runner(f"{i}b", f"Horse B{i}", "Beta Hoop", 4.0),
                              runner(f"{i}c", f"Horse C{i}", "Gamma Pilot", 4.0)])
            for i in (1, 2, 3)
        ]
        return {"testville": races}

    def settings(self):
        return Settings(_env_file=None, archive_raw_responses=False)

    def test_probabilities_and_ev(self):
        offers = [offer("Alpha Rider", 2.0), offer("Beta Hoop", 5.0),
                  offer("Any Other Jockey", 6.0)]
        vals = value_challenges(offers, self.make_world(), self.settings(), now=NOW)
        by_comp = {v.offer.competitor: v for v in vals}
        alpha = by_comp["Alpha Rider"]
        assert alpha.quality == "MEDIUM"
        assert alpha.n_races == 3
        # Exact check: fair book per race -> alpha .5, beta .25, gamma .25.
        exact, other = exact_most_wins(
            [{"alpha rider": 0.5, "beta hoop": 0.25, "gamma pilot": 0.25}] * 3
        )
        assert alpha.fair_probability == pytest.approx(exact["alpha rider"], abs=0.01)
        assert alpha.expected_return == pytest.approx(
            alpha.fair_probability * 2.0 - 1
        )
        # "Any Other" aggregates the unlisted Gamma Pilot.
        assert by_comp["Any Other Jockey"].fair_probability == pytest.approx(
            exact["gamma pilot"] + other, abs=0.01
        )
        # Probabilities sum to ~1 across the whole book.
        total = sum(v.fair_probability for v in vals)
        assert total == pytest.approx(1.0, abs=0.01)

    def test_unmatched_competitor_low_quality(self):
        vals = value_challenges([offer("Nonexistent Rider", 3.0)],
                                self.make_world(), self.settings(), now=NOW)
        assert vals[0].fair_probability is None
        assert vals[0].quality == "LOW"

    def test_no_races_reported_low(self):
        vals = value_challenges([offer("Alpha Rider", 2.0)], {}, self.settings(),
                                now=NOW)
        assert vals[0].fair_probability is None
        assert "no priceable races" in vals[0].quality_detail

    def test_settlement_assumption_always_recorded(self):
        vals = value_challenges([offer("Alpha Rider", 2.0)], self.make_world(),
                                self.settings(), now=NOW)
        assert "dead-heats divided" in vals[0].quality_detail
