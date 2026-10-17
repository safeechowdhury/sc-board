import datetime as dt
import unittest
import unittest.mock

import httpx

from props import bovada
from props.leagues import mlb, nfl, nhl


def market(name, outcomes, key="GAME-PROP"):
    return {"name": name, "key": key, "outcomes": [
        {"name": n, "type": t, "line": line, "odds": odds} for n, t, line, odds in outcomes]}


def over_under(name, line, over, under):
    return market(name, [("Over", "O", line, over), ("Under", "U", line, under)], key="2W-OU")


class ParseTest(unittest.TestCase):
    def test_a_players_over_under_carries_its_line(self):
        props, skipped = bovada.parse({"markets": [
            over_under("Total Receptions - Alvin Kamara (NOR)", "3.5", "1.87", "1.87"),
            over_under("Total Passing & Rushing Yards O/U - Michael Penix Jr. (ATL)", "232.5", "1.909091", "1.833333"),
        ]}, nfl)
        self.assertEqual(props, [
            {"player": "Alvin Kamara", "stat": "receptions", "line": 3.5, "over": 1.87, "under": 1.87},
            {"player": "Michael Penix Jr.", "stat": "pass_rush_yds", "line": 232.5, "over": 1.909, "under": 1.833},
        ])
        self.assertFalse(skipped)

    def test_an_over_with_no_under_is_one_way(self):
        props, _ = bovada.parse({"markets": [
            market("Total Doubles - Andrew Benintendi (CWS)", [("Over", "O", "0.5", "7.500")], key="2W-OU"),
        ]}, mlb)
        self.assertEqual(props, [{"player": "Andrew Benintendi", "stat": "doubles", "line": 0.5, "over": 7.5, "under": None}])

    def test_a_players_ladder_rungs_are_overs_half_a_point_below(self):
        props, _ = bovada.parse({"markets": [
            market("Assists Milestones - Anthony Cirelli (TB)",
                   [("To Record 1+ Assists", "X", None, "3.150"), ("To Record 2+ Assists", "X", None, "15.00")]),
        ]}, nhl)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"], p["under"]) for p in props],
                         [("Anthony Cirelli", "assists", 0.5, 3.15, None), ("Anthony Cirelli", "assists", 1.5, 15.0, None)])

    def test_a_field_market_is_one_rung_with_a_runner_per_player(self):
        props, _ = bovada.parse({"markets": [
            market("Player to record 2+ Hits", [("Jose Ramirez (CLE)", "X", None, "3.800")]),
            market("Player to record a Hit", [("Jose Ramirez (CLE)", "X", None, "1.400")]),   # "a" is one
        ]}, mlb)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"]) for p in props],
                         [("Jose Ramirez", "hits", 1.5, 3.8), ("Jose Ramirez", "hits", 0.5, 1.4)])

    def test_soccers_markets_are_the_whole_of_regulation_time(self):
        from props.leagues import soccer
        event = {"displayGroups": [{"markets": [
            {"description": "Shots On Target - Aaron Hickey (BRE)", "key": "GAME-PROP-12", "status": "O",
             "period": {"description": "Regulation Time"},
             "outcomes": [{"description": "1+ Shots on Target", "type": "X", "status": "O", "price": {"decimal": "3.500"}}]},
            {"description": "Shots On Target - Aaron Hickey (BRE)", "key": "GAME-PROP-12", "status": "O",
             "period": {"description": "First Half"},
             "outcomes": [{"description": "1+ Shots on Target", "type": "X", "status": "O", "price": {"decimal": "8.00"}}]},
        ]}]}
        props, _ = bovada.parse({"markets": bovada._compact(event)}, soccer)
        self.assertEqual(props, [{"player": "Aaron Hickey", "stat": "shots_on_target", "line": 0.5, "over": 3.5, "under": None}])

    def test_unmapped_markets_are_counted_by_name(self):
        props, skipped = bovada.parse({"markets": [
            market("Longest Reception - Chris Olave (NOR)", [("Over", "O", "24.5", "1.87"), ("Under", "U", "24.5", "1.87")]),
            market("Player to Record a 20+ Yard Reception", [("Chris Olave (NOR)", "X", None, "1.60")]),
            market("Anytime 2nd Half TD Scorer", [("Chris Olave (NOR)", "X", None, "3.50")]),   # part of a game: skipped quietly
        ]}, nfl)
        self.assertEqual(props, [])
        self.assertEqual(dict(skipped), {"Longest Reception": 1, "Player to Record a N+ Yard Reception": 1})


class FetchTest(unittest.TestCase):
    def test_reads_every_games_markets_from_the_league_listing(self):
        start = lambda hours: int((dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc) + dt.timedelta(hours=hours)).timestamp() * 1000)
        groups = [{"markets": [
            {"description": "Total Bases - Chase DeLauter (CLE)", "key": "2W-OU", "status": "O", "period": {"description": "Game"},
             "outcomes": [{"description": "Over", "type": "O", "status": "O", "price": {"handicap": "1.5", "decimal": "2.550"}}]},
            {"description": "Total Bases - Chase DeLauter (CLE)", "key": "2W-OU", "status": "O", "period": {"description": "1st Inning"},
             "outcomes": []},
            {"description": "Moneyline", "key": "2W-12", "status": "O", "period": {"description": "Game"},
             "outcomes": [{"description": "Cleveland Guardians", "type": "H", "status": "O", "price": {"decimal": "1.6"}}]},
        ]}]
        seen = []

        class Http:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, params):
                seen.append((url.split("/description/", 1)[1], params))
                data = [{"events": [{"startTime": start(5), "live": False, "displayGroups": groups},
                                    {"startTime": start(-1), "live": True, "displayGroups": groups},
                                    {"startTime": start(24 * 9), "live": False, "displayGroups": groups}]}]
                return type("R", (), {"status_code": 200, "raise_for_status": lambda self: None, "json": lambda self: data})()

        with unittest.mock.patch.object(bovada.httpx, "Client", Http):
            payload = bovada.fetch(mlb, now=dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc))
        # one request, with no market filter so it carries every market; not the game under way, not one past the slate
        self.assertEqual(seen, [("baseball/mlb", {"lang": "en", "preMatchOnly": "true", "eventsLimit": 100})])
        self.assertEqual(payload, {"markets": [{"name": "Total Bases - Chase DeLauter (CLE)", "key": "2W-OU",
                                                "outcomes": [{"name": "Over", "type": "O", "line": "1.5", "odds": "2.550"}]}]})

if __name__ == "__main__":
    unittest.main()
