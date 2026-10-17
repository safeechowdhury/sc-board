import datetime as dt
import unittest
import unittest.mock

from props import mise
from props.leagues import mlb, nfl, nhl


def market(name, code, outcomes, line=None, status="ACTIVE"):
    return {"name": name, "code": code, "line": line, "status": status,
            "outcomes": [{"name": n, "status": "ACTIVE", "odds": odds} for n, odds in outcomes]}


class ParseTest(unittest.TestCase):
    def test_two_way_markets_carry_their_line(self):
        props, skipped = mise.parse({"markets": [
            market("Moritz Seider Total Shots on Target Over/Under 2.5", "TOTAL_SHOTS_ON_TARGET_OVER/UNDER_PLAYER_INC_OT",
                   [("Over", 2.25), ("Under", 1.56)], line=2.5),
            market("Mark Scheifele Total Points 0.5", "TOTAL_POINTS_OVER_UNDER_PLAYER", [("Under", 2.5), ("Over", 1.5)], line=0.5),
        ]}, nhl)
        self.assertEqual(props, [
            {"player": "Moritz Seider", "stat": "shots", "line": 2.5, "over": 2.25, "under": 1.56},
            {"player": "Mark Scheifele", "stat": "points", "line": 0.5, "over": 1.5, "under": 2.5},
        ])
        self.assertFalse(skipped)

    def test_goalie_saves_are_read_by_name_whatever_their_code(self):
        props, skipped = mise.parse({"markets": [
            market("Linus Ullmark Total Saves", "ROLLING_TOTAL_GOALS_OVER/UNDER_INC_OT_AND_PENS_WITH_ALT",
                   [("Over", 1.95), ("Under", 1.74)], line=25.5),
            market("Nico Daws Total Saves", "", [("Over", 1.74), ("Under", 1.95)], line=22.5),
        ]}, nhl)
        self.assertEqual(props, [
            {"player": "Linus Ullmark", "stat": "saves", "line": 25.5, "over": 1.95, "under": 1.74},
            {"player": "Nico Daws", "stat": "saves", "line": 22.5, "over": 1.74, "under": 1.95},
        ])
        self.assertFalse(skipped)

    def test_a_ladders_rungs_are_overs_half_a_point_below(self):
        props, _ = mise.parse({"markets": [
            market("Xander Bogaerts Total Bases (sin. = 1, dou. = 2, tri. = 3, hr = 4)", "PLAYER_UNDEFINED_TOTAL_BASES",
                   [("2+", 2.7), ("1+", 1.51)]),
            market("Fernando Tatis Jr. Total Doubles", "PLAYER_UNDEFINED_TOTAL_DOUBLES", [("1+", 5.0)]),
        ]}, mlb)
        self.assertEqual(props, [
            {"player": "Xander Bogaerts", "stat": "total_bases", "line": 1.5, "over": 2.7, "under": None},
            {"player": "Xander Bogaerts", "stat": "total_bases", "line": 0.5, "over": 1.51, "under": None},
            {"player": "Fernando Tatis Jr.", "stat": "doubles", "line": 0.5, "over": 5.0, "under": None},
        ])

    def test_a_field_market_names_a_player_per_runner(self):
        props, _ = mise.parse({"markets": [
            market("Player 3+ Goals (Inc OT)", "PLAYER_TOTAL_GOALS", [("Kyle Connor", 67.0), ("Alex Debrincat", 41.0)]),
        ]}, nhl)
        self.assertEqual([(p["player"], p["line"], p["over"], p["under"]) for p in props],
                         [("Kyle Connor", 2.5, 67.0, None), ("Alex Debrincat", 2.5, 41.0, None)])
        first, _ = mise.parse({"markets": [
            market("First Goalscorer", "GOALSCORER_FIRST", [("Kyle Connor", 11.0), ("Dylan DeMelo", 91.0)]),
            market("1st Period First Goalscorer", "GOALSCORER_FIRST_1ST_PERIOD", [("Kyle Connor", 12.0)]),
        ]}, nhl)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"]) for p in first],
                         [("Kyle Connor", "first_goals", 0.5, 11.0), ("Dylan DeMelo", "first_goals", 0.5, 91.0)])

    def test_soccers_scorer_markets(self):
        from props.leagues import soccer
        props, _ = mise.parse({"markets": [
            market("Player to score", "GOALSCORER_WITH_NO_GOAL_ANYTIME", [("Igor Thiago", 2.4)]),
            market("Player to Score 2+", "GOALSCORER_TWO_OR_MORE", [("Igor Thiago", 7.25)]),
        ]}, soccer)
        self.assertEqual([(p["stat"], p["line"], p["over"]) for p in props], [("goals", 0.5, 2.4), ("goals", 1.5, 7.25)])

    def test_periods_closed_markets_and_suspended_prices_are_left_out(self):
        suspended = market("Tyler Warren Total Receptions 5.5", "TOTAL_RECEPTIONS_OVER_UNDER_PLAYER",
                           [("Over", 1.72), ("Under", 2.05)], line=5.5)
        suspended["outcomes"][1]["status"] = "SUSPENDED"
        props, skipped = mise.parse({"markets": [
            market("Laquon Treadwell 1st Half Total Touchdowns scored", "PLAYER_UNDEFINED_TOTAL_TOUCHDOWNS_1ST_HALF", [("1+", 7.75)]),
            market("Tyler Warren Total touchdowns scored", "PLAYER_UNDEFINED_TOTAL_TOUCHDOWNS", [("1+", 2.5)], status="SUSPENDED"),
            market("Arden Key Total Sacks", "PLAYER_UNDEFINED_TOTAL_SACKS", [("1+", 2.7)]),
            suspended,
        ]}, nfl)
        self.assertEqual(props, [{"player": "Tyler Warren", "stat": "receptions", "line": 5.5, "over": 1.72, "under": None}])
        self.assertEqual(dict(skipped), {"PLAYER_UNDEFINED_TOTAL_SACKS": 1})   # quarters and halves aren't worth listing


class FetchTest(unittest.TestCase):
    def test_lists_the_leagues_games_then_reads_their_player_markets(self):
        class Http:
            def __init__(self, *a, **k): self.seen = []
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, params):
                self.seen.append((url.rsplit("/", 1)[1], params))
                data = ({"events": [{"id": 1, "started": False}, {"id": 2, "started": True}]} if url.endswith("event-list")
                        else {"events": [{"id": 1, "markets": [
                            {"name": "Ty France Total Hits", "groupCode": "PLAYER_UNDEFINED_TOTAL_HITS", "status": "ACTIVE",
                             "outcomes": [{"name": "1+", "status": "ACTIVE", "prices": [{"decimal": 1.54}]}]},
                            {"name": "Money Line", "groupCode": "MONEY_LINE", "status": "ACTIVE", "outcomes": []}]}]})
                return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"data": data}})()

        http = Http()
        with unittest.mock.patch.object(mise.httpx, "Client", return_value=http):
            payload = mise.fetch(mlb, now=dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(payload, {"markets": [{"name": "Ty France Total Hits", "code": "PLAYER_UNDEFINED_TOTAL_HITS", "line": None,
                                                "status": "ACTIVE", "outcomes": [{"name": "1+", "status": "ACTIVE", "odds": 1.54}]}]})
        (listed, first), (detail, second) = http.seen
        self.assertEqual((listed, first["drilldownTagIds"], first["startTimeTo"]), ("event-list", "597", "2026-10-06T12:00:00Z"))
        self.assertEqual((detail, second["eventIds"]), ("events-by-ids", "1"))   # a game already under way is skipped


if __name__ == "__main__":
    unittest.main()
