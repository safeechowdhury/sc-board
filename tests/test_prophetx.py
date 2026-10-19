import unittest

from props import prophetx
from props.leagues import nba, nhl


def offer(name, odds):
    return {"name": name, "odds": odds, "stake": 50}


def total(player_market, *lines, game=None):
    """A player's total as fetch() keeps it: each line's best offer per side."""
    return {"type": "player_total_points", "name": player_market, "lines": list(lines), **({"game": game} if game else {})}


class SidesTest(unittest.TestCase):
    def test_the_best_offer_on_each_side_by_its_own_name(self):
        line = {"selections": [[offer("over 24.5", 104), offer("over 24.5", -110)], [offer("under 24.5", -117)]],
                "outcomes": [{"name": "over 24.5", "line": 24.5}, {"name": "under 24.5", "line": 24.5}]}
        self.assertEqual(prophetx._sides(line), {"line": 24.5, "over": 104, "under": -117})

    def test_a_yes_no_lists_its_no_first(self):
        market = {"selections": [[offer("NO", -400)], [offer("YES", 260), offer("YES", 255)]],
                  "outcomes": [{"id": 2, "name": "NO"}, {"id": 1, "name": "YES"}]}
        self.assertEqual(prophetx._sides(market), {"line": None, "over": 260, "under": -400})

    def test_a_side_with_no_offers_is_missing(self):
        line = {"selections": [None, [offer("under 9.5", -150)]], "outcomes": [{"line": 9.5}]}
        self.assertEqual(prophetx._sides(line), {"line": 9.5, "under": -150})


class ParseTest(unittest.TestCase):
    def test_two_way_lines_become_props(self):
        props, skipped = prophetx.parse({"markets": [
            total("Cade Cunningham Total Points", {"line": 24.5, "over": 104, "under": -117},
                  {"line": 14.5, "over": -2200}),   # one side only: left out
        ]}, nba)
        self.assertEqual(props, [{"player": "Cade Cunningham", "stat": "points", "line": 24.5,
                                  "over": 2.04, "under": 1.855}])
        self.assertEqual(skipped, {})

    def test_a_wide_line_is_left_out(self):
        props, _ = prophetx.parse({"markets": [
            total("Jalen Duren Total Points", {"line": 12.5, "over": -200, "under": -200})]}, nba)
        self.assertEqual(props, [])

    def test_a_yes_no_is_the_over_at_half(self):
        props, _ = prophetx.parse({"markets": [
            {"type": "player_to_score_a_goal", "name": "Trevor Zegras To Score A Goal",
             "lines": [{"line": None, "over": 260, "under": -330}]}]}, nhl)
        self.assertEqual([(p["player"], p["stat"], p["line"]) for p in props], [("Trevor Zegras", "goals", 0.5)])

    def test_unmapped_types_are_counted(self):
        _, skipped = prophetx.parse({"markets": [
            {"type": "player_to_score_first_basket_including_free_throws",
             "name": "Cade Cunningham To Score First Basket (Including Free Throws)", "lines": []}]}, nba)
        self.assertEqual(skipped, {"player_to_score_first_basket_including_free_throws": 1})


if __name__ == "__main__":
    unittest.main()
