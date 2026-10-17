import unittest
import unittest.mock

from props import betano
from props.leagues import nfl, nhl


def market(title, columns, rows):
    return {"tableLayout": {"title": title, "columnTitles": [{"title": c} for c in columns],
                            "rows": [{"title": player, "groupSelections": groups} for player, groups in rows]}}


def over_under(line, over, under):
    return [{"handicap": line, "selections": [{"name": f"Over {line}", "price": over},
                                              {"name": f"Under {line}", "price": under}]}]


def rungs(*counts_and_prices):
    return [{"handicap": count, "selections": [{"name": f"{player} {count}+", "price": price}]}
            for count, price in counts_and_prices]


class ParseTest(unittest.TestCase):
    def payload(self, *markets):
        return {"markets": markets}

    def test_an_over_under_table_prices_every_player_at_its_line(self):
        table = market("Shots on Goal", ["Over", "Under"], [
            ("Timo Meier", over_under(2.5, 1.63, 2.15)),
            ("Thomas Chabot", over_under(1.5, 1.75, 2.0)),
        ])
        props, skipped = betano.parse(self.payload(table), nhl)
        self.assertEqual(props, [
            {"player": "Timo Meier", "stat": "shots", "line": 2.5, "over": 1.63, "under": 2.15},
            {"player": "Thomas Chabot", "stat": "shots", "line": 1.5, "over": 1.75, "under": 2.0},
        ])
        self.assertFalse(skipped)

    def test_the_titles_variants_are_one_stat(self):
        tables = [market("Points O/U", ["Over", "Under"], [("Jack Hughes", over_under(0.5, 1.8, 2.0))]),
                  market("Total Blocked Shots", ["Over", "Under"], [("Brenden Dillon", over_under(0.5, 2.1, 1.7))])]
        props, _ = betano.parse(self.payload(*tables), nhl)
        self.assertEqual(sorted({p["stat"] for p in props}), ["blocked_shots", "points"])

    def test_a_scorer_table_prices_the_anytime_and_first_columns(self):
        table = {"tableLayout": {"title": "Goal scorer",
                                 "columnTitles": [{"title": "Anytime"}, {"title": "First"}, {"title": "Last"}],
                                 "rows": [{"title": "Jack Hughes", "groupSelections": [
                                     {"handicap": 0, "selections": [
                                         {"name": "Jack Hughes", "price": 2.5},
                                         {"name": "Jack Hughes", "price": 10.25},
                                         {"name": "Jack Hughes", "price": 10.25}]}]}]}}
        props, _ = betano.parse(self.payload(table), nhl)
        self.assertEqual(props, [
            {"player": "Jack Hughes", "stat": "goals", "line": 0.5, "over": 2.5, "under": None},
            {"player": "Jack Hughes", "stat": "first_goals", "line": 0.5, "over": 10.25, "under": None},
        ])

    def test_a_row_missing_a_column_places_each_price_by_its_column_index(self):
        # no anytime price: the first two prices are first and last, not anytime and first
        scorer = market("Goal scorer", ["Anytime", "First", "Last"], [("Ryker Evans", [{"handicap": 0.0, "selections": [
            {"name": "Ryker Evans (Seattle Kraken)", "price": 120.0, "columnIndex": 1},
            {"name": "Ryker Evans (Seattle Kraken)", "price": 120.0, "columnIndex": 2}]}])])
        ladder = market("2+ Assists", ["2+ Assists", "3+ Assists"], [("Ryker Evans", [{"handicap": 0.0, "selections": [
            {"name": "Ryker Evans (Seattle Kraken)", "price": 175.0, "columnIndex": 1}]}])])
        props, _ = betano.parse(self.payload(scorer, ladder), nhl)
        self.assertEqual(props, [
            {"player": "Ryker Evans", "line": 0.5, "under": None, "stat": "first_goals", "over": 120.0},
            {"player": "Ryker Evans", "stat": "assists", "under": None, "line": 2.5, "over": 175.0},
        ])

    def test_a_yes_table_is_a_one_way_rung_at_half_below_the_count(self):
        yes = market("Powerplay Points", ["Yes"], [("Brady Tkachuk",
            [{"handicap": 0, "selections": [{"name": "Yes", "price": 2.9}]}])])
        milestone = market("To Score 2+ Goals", ["2+ Goals", "3+ Goals"], [("Tim Stutzle", [
            {"handicap": 2, "selections": [{"name": "Tim Stutzle 2+ Goals", "price": 6.0}]},
            {"handicap": 3, "selections": [{"name": "Tim Stutzle 3+ Goals", "price": 15.0}]}])])
        props, _ = betano.parse(self.payload(yes, milestone), nhl)
        self.assertEqual(props, [
            {"player": "Brady Tkachuk", "stat": "ppp", "line": 0.5, "over": 2.9, "under": None},
            {"player": "Tim Stutzle", "stat": "goals", "line": 1.5, "over": 6.0, "under": None},
            {"player": "Tim Stutzle", "stat": "goals", "line": 2.5, "over": 15.0, "under": None},
        ])

    def test_a_rungs_count_comes_from_its_name_or_titles_not_its_handicap(self):
        # as Betano sends them: handicap 0 on every rung, the count in the column title, the
        # selection's name or the table's title
        player = lambda name, *prices: [{"handicap": 0.0, "selections": [{"name": name, "handicap": 0.0, "price": p}
                                                                        for p in prices]}]
        points = market("2+ Points", ["2+ Points", "3+ Points"], [("Cole Caufield", player("Cole Caufield (MTL)", 2.95, 6.7))])
        goals = market("To Score 2+ Goals", [None], [("Cole Caufield", player("Cole Caufield (MTL)", 5.8))])
        shots = market("Shots on Goal", ["Over"], [("Cole Caufield", [{"handicap": 3.5, "selections": [
            {"name": "Over 3.5", "handicap": 3.5, "price": 2.08}]}])])
        props, _ = betano.parse(self.payload(points, goals, shots), nhl)
        self.assertEqual([(p["stat"], p["line"], p["over"]) for p in props],
                         [("points", 1.5, 2.95), ("points", 2.5, 6.7), ("goals", 1.5, 5.8), ("shots", 3.5, 2.08)])
        yards = market("Receiving Yards", [None, None], [("Jaxon Smith-Njigba", [{"handicap": 0.0, "selections": [
            {"name": "50+", "handicap": 0.0, "price": 1.17}, {"name": "100+", "handicap": 0.0, "price": 2.22}]}])])
        props, _ = betano.parse(self.payload(yards), nfl)
        self.assertEqual([(p["stat"], p["line"]) for p in props], [("rec_yds", 49.5), ("rec_yds", 99.5)])

    def test_a_yes_no_table_is_two_way_at_half(self):
        table = market("Double-Double", ["Yes", "No"], [("Cade Cunningham", [{"handicap": 0.0, "selections": [
            {"name": "Yes", "price": 1.91}, {"name": "No", "price": 1.83}]}])])
        from props.leagues import nba
        props, _ = betano.parse(self.payload(table), nba)
        self.assertEqual(props, [{"player": "Cade Cunningham", "stat": "double_double", "line": 0.5, "over": 1.91, "under": 1.83}])

    def test_the_walks_ladder_is_the_pitchers(self):
        from props.leagues import mlb
        table = market("Walks", [None], [("Blake Snell", [{"handicap": 0.0, "selections": [{"name": "1+", "price": 1.1}]}])])
        props, _ = betano.parse(self.payload(table), mlb)
        self.assertEqual([(p["stat"], p["line"]) for p in props], [("walks_allowed", 0.5)])

    def test_a_hat_trick_is_three_goals(self):
        from props.leagues import soccer
        table = market("Multi-Goalscorer", ["2+ Goals", "Hat-trick"], [("Kylian Mbappe", [{"handicap": 0.0, "selections": [
            {"name": "Kylian Mbappe (Real Madrid)", "price": 4.55}, {"name": "Kylian Mbappe (Real Madrid)", "price": 15.5}]}])])
        props, _ = betano.parse(self.payload(table), soccer)
        self.assertEqual([(p["stat"], p["line"], p["over"]) for p in props], [("goals", 1.5, 4.55), ("goals", 2.5, 15.5)])

    def test_all_fumbles_are_not_fumbles_lost(self):
        table = market("Fumbles", [None], [("Sam Darnold", [{"handicap": 0.0, "selections": [{"name": "1+", "price": 2.27}]}])])
        props, skipped = betano.parse(self.payload(table), nfl)
        self.assertEqual((props, dict(skipped)), ([], {"Fumbles": 1}))

    def test_parts_of_a_game_and_unmapped_tables_are_skipped(self):
        half = market("Passing Yards - First Half", ["Over", "Under"], [("Josh Allen", [over_under(150.5, 1.87, 1.93)])])
        unmapped = market("Sacks", ["Over", "Under"], [("Von Miller", [over_under(0.5, 4.0, 1.2)])])
        props, skipped = betano.parse(self.payload(half, unmapped), nhl)
        self.assertEqual(props, [])
        self.assertEqual(dict(skipped), {"Sacks": 1})


class FetchTest(unittest.TestCase):
    def test_a_refusal_is_an_error_the_build_moves_past(self):
        class Splash:   # Betano's splash screen answers a client it doesn't let in with 403
            status_code = 403

            def raise_for_status(self):
                raise betano.creq.exceptions.HTTPError("HTTP Error 403: ")

        with unittest.mock.patch.object(betano.creq, "get", return_value=Splash()):
            with self.assertRaises(ValueError):   # what the build's cache catches, unlike curl_cffi's own errors
                betano.fetch(nhl)


if __name__ == "__main__":
    unittest.main()
