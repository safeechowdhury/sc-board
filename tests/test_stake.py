import unittest

from props import stake
from props.leagues import nfl, nhl


def market(stat, value, *lines):
    return {"stat": {"name": stat, "value": value},
            "lines": [{"line": line, "over": over, "under": under, "suspended": False}
                      for line, over, under in lines]}


def player(name, *markets):
    return {"name": name, "markets": list(markets)}


def fixture(*players, start="Wed, 14 Oct 2026 23:30:00 GMT"):
    return {"status": "active",
            "data": {"__typename": "SportFixtureDataMatch", "startTime": start,
                     "competitors": [{"name": "Denver Broncos"}, {"name": "Seattle Seahawks"}]},
            "swishGame": {"status": "PreGame", "players": list(players)}}


class ParseTest(unittest.TestCase):
    def payload(self, *players):
        return {"fixtures": [fixture(*players)]}

    def test_a_market_with_both_sides_prices_each_line(self):
        props, skipped = stake.parse({"fixtures": [
            fixture(player("AJ Barner", market("receptions", 0, (2.5, 1.80, 1.87), (3.5, 1.55, 2.3))))]}, nfl)
        self.assertEqual(props, [
            {"player": "AJ Barner", "stat": "receptions", "line": 2.5, "over": 1.8, "under": 1.87},
            {"player": "AJ Barner", "stat": "receptions", "line": 3.5, "over": 1.55, "under": 2.3},
        ])
        self.assertFalse(skipped)

    def test_the_stats_map_by_their_names(self):
        props, _ = stake.parse(self.payload(
            player("Bo Nix", market("passing yards", 0, (225.5, 1.9, 1.9))),
            player("Javonte Williams", market("rushing yards", 0, (45.5, 1.85, 1.95))),
            player("Courtland Sutton", market("anytime touchdown", 0, (0.5, 3.2, 1.33))),
        ), nfl)
        self.assertEqual(sorted({p["stat"] for p in props}), ["pass_yds", "rush_rec_tds", "rush_yds"])

    def test_a_one_sided_line_is_left_out(self):
        props, _ = stake.parse(self.payload(
            player("Sam Darnold", market("passing touchdowns", 0, (1.5, 2.4, None))),
        ), nfl)
        self.assertEqual(props, [])

    def test_parts_of_a_game_are_dropped_and_unmapped_stats_are_listed(self):
        props, skipped = stake.parse(self.payload(
            player("Marvin Mims", market("1st quarter receiving yards", 0, (25.5, 3.1, 1.36))),
            player("Jonah Elliss", market("sacks", 0, (0.5, 3.4, 1.3))),
            player("Scott Wedgewood", market("shutouts", 0, (0.5, 9.0, 1.01))),
        ), nhl)
        self.assertEqual(props, [])   # a quarter's line is dropped quietly
        self.assertEqual(dict(skipped), {"shutouts": 1})

    def test_nhl_stats_map_to_their_rows(self):
        props, _ = stake.parse(self.payload(
            player("Cale Makar", market("shots", 0, (2.5, 1.85, 1.95))),
            player("Nathan MacKinnon", market("points", 0, (1.5, 1.7, 2.1))),
            player("Scott Wedgewood", market("saves", 0, (25.5, 1.9, 1.9))),
        ), nhl)
        self.assertEqual(sorted({p["stat"] for p in props}), ["points", "saves", "shots"])


if __name__ == "__main__":
    unittest.main()
