"""De-vig framework tests. All odds vectors are SYNTHETIC TEST FIXTURES."""

import pytest

from app.models.devig import REGISTRY, devig


ODDS = [2.6, 4.8, 6.0, 8.5, 12.0, 21.0, 34.0]  # synthetic race, ~112% book


@pytest.mark.parametrize("method", sorted(REGISTRY))
class TestAllMethods:
    def test_fair_probabilities_sum_to_one(self, method):
        res = devig(ODDS, method=method)
        assert sum(res.fair_probabilities) == pytest.approx(1.0, abs=1e-9)

    def test_ordering_preserved(self, method):
        res = devig(ODDS, method=method)
        # Shorter odds -> higher fair probability, order must be preserved.
        pairs = sorted(zip(ODDS, res.fair_probabilities))
        probs = [p for _, p in pairs]
        assert probs == sorted(probs, reverse=True)

    def test_overround_recorded(self, method):
        res = devig(ODDS, method=method)
        assert res.overround == pytest.approx(sum(1 / o for o in ODDS))
        assert res.overround > 1.0

    def test_raw_inputs_stored(self, method):
        res = devig(ODDS, method=method)
        assert res.raw_odds == tuple(ODDS)
        assert res.raw_probabilities == tuple(1 / o for o in ODDS)
        assert res.method == method


class TestProportional:
    def test_exact_values(self):
        res = devig([2.0, 4.0, 4.0], method="proportional")
        # raw = [.5, .25, .25], sum = 1.0 -> unchanged
        assert res.fair_probabilities == pytest.approx((0.5, 0.25, 0.25))

    def test_margin_removed_proportionally(self):
        res = devig([1.8, 3.6, 3.6], method="proportional")
        raw = [1 / 1.8, 1 / 3.6, 1 / 3.6]
        total = sum(raw)
        assert res.fair_probabilities == pytest.approx(tuple(p / total for p in raw))


class TestPowerAndShin:
    def test_power_pushes_margin_to_longshots(self):
        prop = devig(ODDS, method="power").fair_probabilities
        base = devig(ODDS, method="proportional").fair_probabilities
        # Power method should give the favourite a higher probability than
        # proportional and the extreme longshot a lower one.
        assert prop[0] > base[0]
        assert prop[-1] < base[-1]

    def test_shin_same_direction(self):
        shin = devig(ODDS, method="shin").fair_probabilities
        base = devig(ODDS, method="proportional").fair_probabilities
        assert shin[0] > base[0]
        assert shin[-1] < base[-1]

    def test_shin_no_margin_falls_back(self):
        res = devig([2.0, 4.0, 4.0], method="shin")  # exactly fair book
        assert sum(res.fair_probabilities) == pytest.approx(1.0)


class TestValidation:
    def test_rejects_odds_at_or_below_one(self):
        with pytest.raises(ValueError):
            devig([1.0, 3.0])
        with pytest.raises(ValueError):
            devig([0.9, 3.0])

    def test_rejects_empty_market(self):
        with pytest.raises(ValueError):
            devig([])

    def test_rejects_unknown_method(self):
        with pytest.raises(ValueError):
            devig([2.0, 3.0], method="magic")

    def test_scratched_runner_excluded_upstream(self):
        # Scratched runners are removed before devig; a 2-runner market
        # after scratches still de-vigs cleanly.
        res = devig([1.5, 3.2], method="proportional")
        assert sum(res.fair_probabilities) == pytest.approx(1.0)
