from __future__ import annotations

from sentinel.rotation import (
    FALLBACK,
    GROWTH,
    Entrant,
    enters_above_tail,
    growth_tier,
    pick_entrant,
)


class TestGrowthTier:
    def test_at_the_floor_is_growth(self):
        assert growth_tier(0.15, 0.15) == GROWTH

    def test_below_the_floor_is_fallback(self):
        assert growth_tier(0.149, 0.15) == FALLBACK

    def test_unknown_growth_has_no_tier(self):
        assert growth_tier(None, 0.15) is None


class TestEntersAboveTail:
    SCORES = {"A": 90.0, "B": 50.0, "C": 30.0, "D": 20.0, "E": 10.0}

    def test_outgoing_name_is_removed_first(self):
        # E leaves; D, C, B sit below 60: three below, clears
        assert enters_above_tail(60.0, self.SCORES, "E", 3)
        # with nobody leaving, E counts too
        assert enters_above_tail(25.0, self.SCORES, None, 2)

    def test_needs_bottom_n_strictly_below(self):
        assert not enters_above_tail(25.0, self.SCORES, "E", 3)   # only D below

    def test_a_tie_is_not_above(self):
        assert not enters_above_tail(30.0, self.SCORES, "E", 2)   # C ties, D below


class TestPickEntrant:
    def test_growth_tier_first_even_when_a_slow_name_scores_higher(self):
        pick = pick_entrant([
            Entrant("SLOW", 70.0, FALLBACK, True),
            Entrant("GRO", 40.0, GROWTH, True),
        ])
        assert pick is not None and pick.ticker == "GRO"

    def test_fallback_when_no_growth_name_clears(self):
        pick = pick_entrant([
            Entrant("SLOW", 70.0, FALLBACK, True),
            Entrant("GRO", 40.0, GROWTH, False),
            Entrant("UNK", 80.0, None, True),
        ])
        assert pick is not None and pick.ticker == "UNK"     # unknown ranks with fallback

    def test_none_when_nothing_clears(self):
        assert pick_entrant([
            Entrant("GRO", 40.0, GROWTH, False),
            Entrant("NOSCORE", None, GROWTH, None),
        ]) is None

    def test_ties_break_on_ticker(self):
        pick = pick_entrant([
            Entrant("ZZZ", 50.0, GROWTH, True),
            Entrant("AAA", 50.0, GROWTH, True),
        ])
        assert pick is not None and pick.ticker == "AAA"
