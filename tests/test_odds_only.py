import contextlib
import datetime as dt
import types
import unittest
from unittest import mock

from props import books, matchups, snapshots
from props.leagues import cfb, soccer

from .helpers import underdog_payload
from .test_store_and_site import TempStore

UTC = dt.timezone.utc


class MatchupsTest(unittest.TestCase):
    def test_a_teams_distinctive_words(self):
        self.assertEqual(matchups.words("Penn State Nittany Lions @ Michigan Wolverines"),
                         {"penn", "nittany", "lions", "michigan", "wolverines"})
        self.assertEqual(matchups.words("Real Betis Seville vs FC Barcelona"), {"betis", "seville", "barcelona"})

    def test_a_book_game_is_on_the_board_when_it_starts_with_one_and_shares_a_word(self):
        board = types.SimpleNamespace(BOARD_GAMES=[(dt.datetime(2026, 10, 17, 19, 30, tzinfo=UTC),
                                                    matchups.words("Penn State Nittany Lions @ Michigan Wolverines"))])
        self.assertTrue(matchups.on_board(board, "2026-10-17T19:30:00.000Z", "Penn State @ Michigan"))
        self.assertTrue(matchups.on_board(board, 1792265400000, "Michigan Wolverines - Penn State Nittany Lions"))
        self.assertFalse(matchups.on_board(board, "2026-10-17T19:30:00Z", "Ohio State @ Indiana"))   # same kickoff
        self.assertFalse(matchups.on_board(board, "2026-10-18T19:30:00Z", "Penn State @ Michigan"))  # another day

    def test_a_league_that_names_no_board_games_lets_every_game_through(self):
        self.assertTrue(matchups.on_board(types.SimpleNamespace(), "2026-10-17T19:30:00Z", "Anyone @ Anywhere"))


class CompetitionsTest(unittest.TestCase):
    def test_a_book_is_asked_once_per_competition_it_carries_and_the_answers_merged(self):
        asked = []

        def fetch(league, until=None):
            asked.append(league.COMPETITION)
            if league.COMPETITION == "La Liga":
                raise ValueError("Betano league page has no events embedded")
            return {"markets": [league.COMPETITION]}

        with mock.patch.object(books.betano, "fetch", fetch):
            answer = books.fetch(books.betano, soccer)
        carried = [c["COMPETITION"] for c in soccer.COMPETITIONS if c.get("BETANO_PATH")]
        self.assertEqual(asked, carried)
        self.assertEqual(answer["markets"], [c for c in carried if c != "La Liga"])
        self.assertEqual(len(answer["errors"]), 1)
        self.assertIn("La Liga", answer["errors"][0])

    def test_competitions_a_book_is_asked_for_alike_are_asked_once(self):
        asked = []
        with mock.patch.object(books.mise, "fetch", lambda league, **when: asked.append(league.MISE_LEAGUE) or {"markets": [1]}):
            answer = books.fetch(books.mise, soccer)   # every competition's tags in one listing
        self.assertEqual((asked, answer), (["125,25277,143,139,134,130,599"], {"markets": [1]}))
        asked.clear()   # FanDuel's page is the same for all, but each competition's games are picked by its id
        with mock.patch.object(books.fanduel, "fetch", lambda league, **when: asked.append(league.FANDUEL_COMPETITION) or {}):
            books.fetch(books.fanduel, soccer)
        self.assertEqual(asked, [c["FANDUEL_COMPETITION"] for c in soccer.COMPETITIONS])

    def test_a_book_without_the_league_is_not_asked(self):
        self.assertFalse(books.carries(books.novig, cfb))     # no college player props
        self.assertFalse(books.carries(books.kalshi, cfb))
        self.assertTrue(books.carries(books.polymarket, cfb))   # its anytime touchdowns
        self.assertTrue(books.carries(books.novig, soccer))   # the Premier League and MLS
        self.assertTrue(books.carries(books.draftkings, soccer))
        self.assertTrue(books.carries(books.pinnacle, cfb))


class DraftKingsSoccerTest(unittest.TestCase):
    def test_ladders_and_fields_are_read_one_way(self):
        sel = lambda market, name, player, odds, milestone=None: {
            "market": market, "name": name, "player": player, "side": "", "line": None, "odds": odds,
            "main": False, "milestone": milestone}
        props, skipped = books.draftkings.parse({"subcategories": [
            {"name": "Player Shots on Target", "selections": [sel(1, "Cole Palmer Shots on Target", "Cole Palmer", 1.5, 1),
                                                              sel(1, "Cole Palmer Shots on Target", "Cole Palmer", 3.4, 2)]},
            {"name": "Goalscorer", "selections": [sel(2, "Anytime Goalscorer", "Erling Braut Haaland", 1.6),
                                                  sel(3, "2+ Goals", "Erling Braut Haaland", 3.35),
                                                  sel(4, "1st Goalscorer", "Erling Braut Haaland", 4.5)]},
            {"name": "To Receive a Card", "selections": [sel(5, "Player to Receive a Card", "Tim Iroegbunam", 2.25)]},
        ]}, soccer)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"], p["under"]) for p in props], [
            ("Cole Palmer", "shots_on_target", 0.5, 1.5, None), ("Cole Palmer", "shots_on_target", 1.5, 3.4, None),
            ("Erling Braut Haaland", "goals", 0.5, 1.6, None), ("Erling Braut Haaland", "goals", 1.5, 3.35, None),
            ("Tim Iroegbunam", "cards", 0.5, 2.25, None)])
        self.assertEqual(skipped, {})   # the first scorer is passed over quietly

    def test_a_competition_with_none_of_the_boards_games_is_not_read_further(self):
        asked = []

        def get(http, league, path):
            asked.append(path)
            return {"events": [{"id": 9, "name": "Everton vs Chelsea", "status": "NOT_STARTED",
                                "startEventDate": "2026-10-17T11:30:00.0000000Z"}],
                    "categories": [{"id": 1, "name": "Player Shots"}],
                    "subcategories": [{"id": 2, "categoryId": 1, "name": "Player Shots"}]}

        part = books.competitions(soccer)[0]
        with mock.patch.object(books.draftkings, "_get", get), \
                mock.patch.object(books.draftkings, "_client", lambda: contextlib.nullcontext(None)):
            part.BOARD_GAMES = [(dt.datetime(2026, 10, 18, 14, tzinfo=UTC), matchups.words("Arsenal vs Fulham"))]
            self.assertEqual(books.draftkings.fetch(part), {"subcategories": []})
            part.BOARD_GAMES = [(dt.datetime(2026, 10, 17, 11, 30, tzinfo=UTC), matchups.words("Everton vs Chelsea"))]
            books.draftkings.fetch(part)
        self.assertEqual(asked, ["", "", "/categories/1/subcategories/2"])


class PricesTest(unittest.TestCase):
    def test_a_team_code_the_board_doesnt_use_leaves_the_name_to_decide(self):
        rows = [{"id": None, "name": "Nacho Ferri", "stat": "goals", "team": "FEY", "line": 0.5}]
        prop = {"player": "Nacho Ferri", "team": "FEYN", "stat": "goals", "line": 0.5, "over": 3.0, "under": 1.4}
        self.assertEqual(books.prices([prop], rows), ({"Nacho Ferri|goals": [0.5, 3.0, 1.4]}, 0))
        known = {**prop, "team": "COM"}   # a code the board uses, but not his: someone else
        rows.append({"id": None, "name": "Someone Else", "stat": "goals", "team": "COM", "line": 0.5})
        self.assertEqual(books.prices([known], rows), ({}, 1))

    def test_an_anytime_stat_takes_only_a_price_at_its_line(self):
        rows = [{"id": None, "name": "Terry McLaurin", "stat": "rush_rec_tds", "team": "WAS", "line": 0.5}]
        two_plus = {"player": "Terry McLaurin", "stat": "rush_rec_tds", "line": 1.5, "over": 16.667, "under": 1.02}
        self.assertEqual(books.prices([two_plus], rows), ({"Terry McLaurin|rush_rec_tds": [1.5, 16.667, 1.02]}, 0))
        self.assertEqual(books.prices([two_plus], rows, {"rush_rec_tds"}), ({}, 0))   # 2+ TDs isn't the anytime price
        anytime = {**two_plus, "line": 0.5, "over": 3.0, "under": 1.4}
        self.assertEqual(books.prices([two_plus, anytime], rows, {"rush_rec_tds"}),
                         ({"Terry McLaurin|rush_rec_tds": [0.5, 3.0, 1.4, [[1.5, 16.667, 1.02]]]}, 0))


def soccer_lines() -> dict:
    """Underdog's soccer: one game, Roma at home to Real Madrid, titled home team first."""
    payload = underdog_payload([
        {"name": "Kylian Mbappe", "team": "WPG", "stat": "period_1_2_goals", "value": 0.5, "sport": "FIFA",
         "options": [("higher", 1.72, 1.0), ("lower", 2.1, 1.0)]},
        {"name": "Paulo Dybala", "team": "BOS", "stat": "period_1_2_shots_on_target", "value": 0.5, "sport": "FIFA",
         "options": [("higher", 1.87, 1.0)]},
        {"name": "Paulo Dybala", "team": "BOS", "stat": "period_1_2_first_goal_scorer", "value": 0.5, "sport": "FIFA",
         "options": [("higher", 7.0, 1.0)]},
    ])
    payload["games"][0].update(title="ROM vs RMA", full_team_names_title="AS Roma vs Real Madrid",
                               home_team_id="bos", away_team_id="wpg")
    return payload


class OddsBoardTest(TempStore):
    """A board with no game logs: Underdog's lines, priced by the books that carry the league."""
    def setUp(self):
        super().setUp()
        self.now = dt.datetime(2026, 10, 2, 16, tzinfo=UTC)
        self.lines = soccer_lines()
        self.schedule = []
        self.kambi = {"offers": [{"criterion": "To Score", "outcomes": [
            {"player": "Kylian Mbappe", "type": "OT_YES", "line": 1000, "odds": 1660, "status": "OPEN"}]}]}
        self.novig = {"markets": [{"type": "SHOTS_ON_TARGET", "player": "Paulo Dybala", "strike": 0.5,
                                   "over": 0.55, "under": 0.48}]}

    def test_rows_are_underdogs_lines_keyed_by_name_with_the_books_prices(self):
        data = self.build(league=soccer, record=True)
        self.assertTrue(data["oddsOnly"])
        self.assertEqual([g["key"] for g in data["games"]], ["2026-10-02|RMA@ROM"])
        self.assertEqual([(r["id"], r["name"], r["team"], r["opp"], r["home"], r["stat"]) for r in data["rows"]],
                         [(None, "Kylian Mbappe", "RMA", "ROM", False, "goals"),
                          (None, "Paulo Dybala", "ROM", "RMA", True, "shots_on_target")])
        prices = {b["key"]: b["prices"] for b in data["books"]}
        self.assertEqual(set(prices), {book.KEY for book in books.BOOKS if books.carries(book, soccer)})
        self.assertEqual(prices["kambi"], {"Kylian Mbappe|goals": [0.5, 1.66, None]})
        # Novig asks each competition it has; every one answers with the same game here
        self.assertEqual(prices["novig"]["Paulo Dybala|shots_on_target"][0], 0.5)
        self.assertEqual(data["fair"]["confident"], [["novig"], ["kalshi"], ["polymarket"], ["pinnacle", "draftkings", "fanduel"]])
        kept = snapshots.load(soccer)
        self.assertEqual(sorted(set(kept["player_id"])), ["Kylian Mbappe", "Paulo Dybala"])

    def test_the_books_asked_game_by_game_are_told_the_boards_games(self):
        seen = []
        self.build(league=soccer, betano_fetch=lambda league: (seen.append(league.BOARD_GAMES), {"markets": []})[1])
        (start, words), = seen[0]
        self.assertEqual(start, dt.datetime(2026, 10, 3, tzinfo=UTC))
        self.assertEqual(words, {"roma", "madrid"})

    def listing(self):
        """Kambi lists Roma v Real Madrid (Underdog's game) and Chelsea at Everton, which Underdog doesn't
        have, in their competitions, and a Premier League game four days out."""
        games = [{"id": 1, "start": "2026-10-03T00:00:00Z", "home": "AS Roma", "away": "Real Madrid", "comp": "Serie A"},
                 {"id": 2, "start": "2026-10-03T14:00:00Z", "home": "Everton", "away": "Chelsea", "comp": "Premier League"},
                 {"id": 3, "start": "2026-10-06T19:00:00Z", "home": "Tottenham", "away": "Coventry City", "comp": "Premier League"}]
        return lambda league: [g for g in games if g["comp"] == league.COMPETITION]

    def test_every_listed_game_is_on_the_board_in_its_competition(self):
        data = self.build(league=soccer, kambi_games=self.listing())
        self.assertEqual([(g["key"], g["away"], g["home"], g["comp"], g["lines"]) for g in data["games"]], [
            ("2026-10-02|RMA@ROM", "Real Madrid", "AS Roma", "Serie A", 2),   # Underdog's game, named as Kambi does
            ("2026-10-03|Chelsea@Everton", "Chelsea", "Everton", "Premier League", 0),
            ("2026-10-06|Coventry City@Tottenham", "Coventry City", "Tottenham", "Premier League", 0)])
        self.assertEqual(data["comps"][:3], ["Premier League", "Champions League", "Europa League"])

    def test_a_game_underdog_skips_gets_rows_at_the_markets_line(self):
        game = ["2026-10-03T14:00:00Z", "Everton - Chelsea"]
        self.kambi = {"offers": [
            {"criterion": "To Score", "game": game, "outcomes": [
                {"player": "Cole Palmer", "type": "OT_YES", "line": 1000, "odds": 2950, "status": "OPEN"},
                {"player": "Pedro Neto", "type": "OT_YES", "line": 1000, "odds": 5000, "status": "OPEN"},
                {"player": "Michael Keane", "type": "OT_YES", "line": 1000, "odds": 9000, "status": "OPEN"}]}]}
        self.novig = {"markets": [
            {"type": "PLAYER_GOALS", "player": "Cole Palmer", "strike": 0.5, "over": 0.335, "under": 0.7, "game": game},
            {"type": "PLAYER_GOALS", "player": "Pedro Lomba Neto", "strike": 0.5, "over": 0.19, "under": None, "game": game}]}
        data = self.build(league=soccer, kambi_games=self.listing())
        market = [(r["name"], r["stat"], r["line"], r["game"], r["matchup"]) for r in data["rows"] if not r["ud"]]
        # Keane has Kambi's price only: one book makes no row
        self.assertEqual(sorted(market), [
            ("Cole Palmer", "goals", 0.5, "2026-10-03|Chelsea@Everton", "Chelsea @ Everton"),
            ("Pedro Neto", "goals", 0.5, "2026-10-03|Chelsea@Everton", "Chelsea @ Everton")])
        prices = {b["key"]: b["prices"] for b in data["books"]}
        self.assertEqual(prices["novig"]["Pedro Neto|goals"][:3], [0.5, 5.263, None])   # Novig's full name is his
        self.assertEqual(prices["kambi"]["Cole Palmer|goals"], [0.5, 2.95, None])

    def test_the_books_asked_game_by_game_ask_for_the_next_two_days_only(self):
        asked = {}
        def keep(name, answer):
            return lambda league: (asked.setdefault(name, []).append(league.BOARD_GAMES), answer)[1]
        self.build(league=soccer, kambi_games=self.listing(), betano_fetch=keep("betano", {"markets": []}),
                   kambi_fetch=keep("kambi", {"offers": []}))
        starts = lambda name: sorted({start for games in asked[name] for start, _ in games})
        self.assertEqual(starts("betano"), [dt.datetime(2026, 10, 3, tzinfo=UTC), dt.datetime(2026, 10, 3, 14, tzinfo=UTC)])
        self.assertEqual(starts("kambi")[-1], dt.datetime(2026, 10, 6, 19, tzinfo=UTC))   # a request for ten games

    def test_no_lines_no_requests(self):
        self.lines = {"games": [], "players": [], "appearances": [], "over_under_lines": []}
        asked = []
        data = self.build(league=soccer, kambi_fetch=lambda league: asked.append(league) or self.kambi)
        self.assertEqual((data["rows"], asked), ([], []))


if __name__ == "__main__":
    unittest.main()
