import unittest

from props import market, matchups


class LineTest(unittest.TestCase):
    """The market's line for a player's stat: {line: {book: (over, under)}}."""

    def test_the_line_most_sharp_books_post_both_sides_at(self):
        prices = {0.5: {"draftkings": (1.3, 3.2), "fanduel": (1.32, None)},
                  1.5: {"novig": (2.4, 1.6), "pinnacle": (2.35, 1.62), "betano": (2.3, 1.55)}}
        self.assertEqual(market._line(prices, {"novig", "pinnacle", "draftkings"}), 1.5)

    def test_with_no_two_way_price_the_rung_nearest_even_money(self):
        prices = {0.5: {"draftkings": (1.3, None), "fanduel": (1.32, None), "kambi": (1.28, None)},
                  1.5: {"draftkings": (2.4, None), "fanduel": (2.5, None), "kambi": (2.3, None)},
                  2.5: {"draftkings": (5.0, None), "fanduel": (5.5, None)}}
        self.assertEqual(market._line(prices, set()), 1.5)

    def test_a_rung_few_books_price_is_passed_over(self):
        prices = {0.5: {"draftkings": (1.6, None), "fanduel": (1.6, None), "kambi": (1.55, None), "mise": (1.5, None)},
                  1.5: {"betano": (2.0, None)}}
        self.assertEqual(market._line(prices, set()), 0.5)


class MergeTest(unittest.TestCase):
    def test_underdogs_game_is_the_listed_game_sharing_the_most_words(self):
        start = "2026-10-17T16:00:00Z"
        listed = [{"start": start, "date": "2026-10-17", "away": "(25) Northwestern Wildcats", "home": "Michigan State Spartans"},
                  {"start": start, "date": "2026-10-17", "away": "Kansas Jayhawks", "home": "Kansas State Wildcats"}]
        ud = [{"key": "2026-10-17|KU@KSU", "date": "2026-10-17", "start": start, "away": "KU", "home": "KSU", "lines": 9,
               "words": matchups.words("Kansas Jayhawks @ Kansas State Wildcats")}]
        games = market.merge_games(listed, ud)
        self.assertEqual([(g["key"], g["lines"]) for g in games],
                         [("2026-10-17|(25) Northwestern Wildcats@Michigan State Spartans", 0), ("2026-10-17|KU@KSU", 9)])

    def test_a_books_game_is_the_one_sharing_the_most_words(self):
        start = "2026-10-17T16:00:00Z"
        find = market.GameFinder([{"key": "nw", "start": start, "away": "Northwestern Wildcats", "home": "Michigan State Spartans"},
                                  {"key": "ks", "start": start, "away": "Kansas Jayhawks", "home": "Kansas State Wildcats"}])
        self.assertEqual(find([start, "Kansas @ Kansas State Wildcats"])["key"], "ks")
        self.assertIsNone(find([start, "Wildcats"]))   # a tie: no telling which


class ShortNameTest(unittest.TestCase):
    def test_a_game_takes_the_shortest_books_names_and_keeps_its_ranking_apart(self):
        start = "2026-10-17T16:00:00Z"
        games = [{"key": "2026-10-17|MSST@LSU", "start": start, "away": "(16) Mississippi State Bulldogs",
                  "home": "(22) LSU Tigers", "lines": 9},
                 {"key": "2026-10-17|UVA@SMU", "start": start, "away": "Virginia Cavaliers",
                  "home": "(24) Southern Methodist University Mustangs", "lines": 4},
                 {"key": "2026-10-17|Kansas Jayhawks@Kansas State Wildcats", "start": start,
                  "away": "Kansas Jayhawks", "home": "Kansas State Wildcats", "lines": 0}]
        find = market.GameFinder(games)
        answers = [("bovada", [{"player": "x", "game": [start, "Kansas @ Kansas State (#19)"]}]),
                   ("draftkings", [{"player": "y", "game": [start, "Mississippi State @ LSU"]}])]
        market.short_names(games, answers, find)
        self.assertEqual([(g["away"], g["home"], g.get("awayRank"), g.get("homeRank")) for g in games], [
            ("Mississippi State", "LSU", 16, 22),
            ("UVA", "SMU", None, 24),   # no book's name for it: Underdog's codes
            ("Kansas", "Kansas State", None, None)])
        self.assertEqual(games[0]["names"], "(16) Mississippi State Bulldogs (22) LSU Tigers")


class AliasTest(unittest.TestCase):
    def finder(self):
        return market.GameFinder([{"key": "g", "start": "2026-10-17T11:30:00Z", "away": "Chelsea", "home": "Everton"}])

    def test_a_name_spelled_out_in_full_is_the_shorter_one_in_its_game(self):
        game = ["2026-10-17T11:30:00Z", "Everton - Chelsea"]
        props = [[{"player": "João Pedro", "game": game}, {"player": "Pedro Neto", "game": game}],
                 [{"player": "João Pedro Junqueira de Jesus", "game": game}, {"player": "Pedro Lomba Neto", "game": game}]]
        self.assertEqual(market.aliases([], props, self.finder()),
                         {"João Pedro Junqueira de Jesus": "João Pedro", "Pedro Lomba Neto": "Pedro Neto"})

    def test_one_word_is_never_enough(self):
        game = ["2026-10-17T11:30:00Z", "Everton - Chelsea"]
        props = [[{"player": "Estevao", "game": game}, {"player": "Estevao Willian", "game": game}]]
        self.assertEqual(market.aliases([], props, self.finder()), {})


if __name__ == "__main__":
    unittest.main()
