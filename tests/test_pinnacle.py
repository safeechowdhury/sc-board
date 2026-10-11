import datetime as dt
import unittest

from props import books, pinnacle
from props.leagues import nfl, nhl

from .helpers import pinnacle_payload


class ParseTest(unittest.TestCase):
    def test_prop_becomes_a_line_with_decimal_prices(self):
        props, skipped = pinnacle.parse(pinnacle_payload([("Jordan Addison", "Receptions", 4.5, 110, -138)]), nfl)
        self.assertEqual(props, [{"player": "Jordan Addison", "stat": "receptions", "line": 4.5,
                                  "over": 2.1, "under": 1.725}])
        self.assertFalse(skipped)

    def test_stat_names_map_to_the_leagues_columns(self):
        props, _ = pinnacle.parse(pinnacle_payload([
            ("Jacoby Brissett", "Touchdown Passes", 1.5, 151, -193),
            ("Jacoby Brissett", "Interceptions", 0.5, -128, 102),
            ("Kyle Monangai", "Touchdowns", 0.5, 139, -176),
        ]), nfl)
        self.assertEqual([p["stat"] for p in props], ["pass_tds", "pass_int", "rush_rec_tds"])

    def test_props_for_a_game_already_under_way_are_left_out(self):
        payload = pinnacle_payload([("Ryan Flournoy", "Receiving Yards", 24.5, -115, -115), ("Puka Nacua", "Receptions", 6.5, -120, -110)])
        payload["matchups"][0]["startTime"] = "2026-10-04T17:00:00Z"   # 1 PM kickoff: Pinnacle leaves the prop up, unmarked
        payload["matchups"][1]["startTime"] = "2026-10-04T20:25:00Z"
        props, _ = pinnacle.parse(payload, nfl, now=dt.datetime(2026, 10, 4, 18, 36, tzinfo=dt.timezone.utc))
        self.assertEqual([p["player"] for p in props], ["Puka Nacua"])

    def test_unmapped_stats_are_counted_not_guessed(self):
        props, skipped = pinnacle.parse(pinnacle_payload([("Connor McDavid", "Faceoffs Won", 10.5, -110, -110)]), nhl)
        self.assertEqual(props, [])
        self.assertEqual(dict(skipped), {"Faceoffs Won": 1})

    def test_needs_both_sides_at_the_same_line(self):
        payload = pinnacle_payload([("Kaapo Kakko", "Goals", 0.5, 310, -474), ("Claude Giroux", "Goals", 0.5, 290, -440)])
        payload["markets"][0]["prices"].pop()                   # under missing
        payload["markets"][1]["prices"][1]["points"] = 1.5      # sides on different lines
        self.assertEqual(pinnacle.parse(payload, nhl)[0], [])

    def test_other_matchups_and_periods_are_ignored(self):
        payload = pinnacle_payload([("Kaapo Kakko", "Goals", 0.5, 310, -474)])
        payload["matchups"].append({"id": 999, "units": "Goals", "special": {"category": "Game Props"}})
        payload["markets"][0]["period"] = 1
        self.assertEqual(pinnacle.parse(payload, nhl)[0], [])


class PricesTest(unittest.TestCase):
    PROP = {"player": "Kenneth Walker", "stat": "receptions", "line": 2.5, "over": 1.9, "under": 1.95}

    def test_keyed_by_player_id_and_stat_with_names_normalized(self):
        rows = [{"id": "00-0038134", "name": "Kenneth Walker III", "stat": "receptions", "team": "KC", "line": 2.5},
                {"id": "00-0038134", "name": "Kenneth Walker III", "stat": "rush_yds", "team": "KC", "line": 2.5}]
        self.assertEqual(books.prices([self.PROP], rows), ({"00-0038134|receptions": [2.5, 1.9, 1.95]}, 0))

    def test_player_without_an_id_is_keyed_by_name(self):
        rows = [{"id": None, "name": "Kenneth Walker", "stat": "receptions", "team": "KC", "line": 2.5}]
        self.assertEqual(books.prices([self.PROP], rows)[0], {"Kenneth Walker|receptions": [2.5, 1.9, 1.95]})

    def test_a_name_two_players_share_is_left_out(self):
        rows = [{"id": "a", "name": "Kenneth Walker", "stat": "receptions", "team": "KC", "line": 2.5},
                {"id": "b", "name": "Kenneth Walker", "stat": "receptions", "team": "SEA", "line": 2.5}]
        self.assertEqual(books.prices([self.PROP], rows), ({}, 1))

    def test_a_team_named_by_the_book_tells_them_apart(self):
        rows = [{"id": "a", "name": "Will Smith", "stat": "hits", "team": "LAD", "line": 0.5},
                {"id": "b", "name": "Will Smith", "stat": "hits", "team": "TEX", "line": 0.5}]
        prop = {"player": "Will Smith", "team": "LAD", "stat": "hits", "line": 0.5, "over": 1.6, "under": 2.3}
        self.assertEqual(books.prices([prop], rows), ({"a|hits": [0.5, 1.6, 2.3]}, 0))


    def test_a_row_gets_the_price_at_its_line_before_any_other(self):
        rows = [{"id": "z", "name": "Zay Flowers", "stat": "receptions", "team": "BAL", "line": 4.5}]
        ladder = [{"player": "Zay Flowers", "stat": "receptions", "line": n - 0.5, "over": o, "under": None}
                  for n, o in ((3, 1.4), (4, 1.9), (5, 2.7), (6, 4.0))]
        main = {"player": "Zay Flowers", "stat": "receptions", "line": 3.5, "over": 1.95, "under": 1.85}
        self.assertEqual(books.prices([main, *ladder], rows)[0], {"z|receptions": [4.5, 2.7, None]})
        exact = {**main, "line": 4.5, "over": 2.6, "under": 1.5}
        self.assertEqual(books.prices([*ladder, exact], rows)[0], {"z|receptions": [4.5, 2.6, 1.5]})   # two-way wins a tie
        self.assertEqual(books.prices([main, ladder[0]], rows)[0], {"z|receptions": [3.5, 1.95, 1.85]})
        self.assertEqual(books.prices(ladder[:2] + ladder[3:], rows)[0], {"z|receptions": [3.5, 1.9, None]})   # nearest rung


class DecimalTest(unittest.TestCase):
    def test_american_to_decimal(self):
        self.assertEqual([pinnacle.decimal(a) for a in (100, -100, 150, -200, -112)], [2.0, 2.0, 2.5, 1.5, 1.893])
