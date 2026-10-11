import datetime as dt
import unittest

import polars as pl

from props import pinnacle
from props.leagues import mlb

from .helpers import game_logs, pinnacle_payload

D = dt.date


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeApi:
    """Stands in for httpx.Client: answers each Stats API path from a dict."""

    def __init__(self, paths):
        self.paths = paths
        self.calls = []

    def get(self, url, params=None):
        path = url.removeprefix(mlb.API)
        self.calls.append((path, params))
        return FakeResponse(self.paths[path])


def batter(pid, name, order, positions, **stats):
    return {"person": {"id": pid, "fullName": name}, "battingOrder": order,
            "position": {"abbreviation": positions[-1]}, "allPositions": [{"abbreviation": p} for p in positions],
            "stats": {"batting": {"plateAppearances": 4, "atBats": 4, "hits": 0, "doubles": 0, "triples": 0,
                                  "homeRuns": 0, "runs": 0, "rbi": 0, "baseOnBalls": 0, "strikeOuts": 0,
                                  "stolenBases": 0, "totalBases": 0, **stats}, "pitching": {}}}


def pitcher(pid, name, started, **stats):
    return {"person": {"id": pid, "fullName": name}, "position": {"abbreviation": "P"},
            "allPositions": [{"abbreviation": "P"}],
            "stats": {"batting": {}, "pitching": {"gamesStarted": int(started), "outs": 0, "strikeOuts": 0, "hits": 0,
                                                  "earnedRuns": 0, "baseOnBalls": 0, "numberOfPitches": 0,
                                                  "battersFaced": 0, **stats}}}


GAME = {"gamePk": 849844, "officialDate": "2026-10-01", "season": "2026", "gameType": "F",
        "status": {"codedGameState": "F"}}
BOX = {"teams": {
    "away": {"team": {"abbreviation": "PHI"}, "batters": [607208, 669016, 681082], "pitchers": [605400, 621111], "players": {
        "ID607208": batter(607208, "Trea Turner", "100", ["SS"], hits=2, doubles=1, homeRuns=1, runs=1, rbi=2, totalBases=7),
        "ID669016": batter(669016, "Brandon Marsh", "701", ["PH", "LF"], plateAppearances=2, atBats=1, baseOnBalls=1),
        "ID681082": batter(681082, "Bryson Stott", "800", ["2B", "P"]),
        "ID605400": pitcher(605400, "Aaron Nola", True, outs=5, strikeOuts=1, hits=2, earnedRuns=3, baseOnBalls=2,
                            numberOfPitches=38, battersFaced=9, losses=1),
        "ID621111": pitcher(621111, "A Reliever", False, outs=3),
        "ID999999": pitcher(999999, "Unused Bullpen Arm", False),
    }},
    "home": {"team": {"abbreviation": "ATL"}, "batters": [660670], "pitchers": [660670], "players": {
        "ID660670": {**batter(660670, "Two Way", "300", ["DH"], hits=1, totalBases=1),
                     "stats": {"batting": batter(0, "", "", ["DH"], hits=1, totalBases=1)["stats"]["batting"],
                               "pitching": pitcher(0, "", True, outs=18, strikeOuts=9, wins=1)["stats"]["pitching"]}},
    }},
}}
# Stott pitched a mop-up inning too
BOX["teams"]["away"]["pitchers"].append(681082)
BOX["teams"]["away"]["players"]["ID681082"]["stats"]["pitching"] = pitcher(0, "", False, outs=3)["stats"]["pitching"]


class PlayerGamesTest(unittest.TestCase):
    def setUp(self):
        api = FakeApi({
            "/schedule": {"dates": [{"games": [
                GAME,
                {**GAME, "gamePk": 1, "status": {"codedGameState": "D"}},    # postponed
                {**GAME, "gamePk": 2, "gameType": "S"},                       # spring training
            ]}]},
            "/sports/1/players": {"people": [{"id": 660670, "primaryPosition": {"abbreviation": "TWP"}}]},
            f"/game/{GAME['gamePk']}/boxscore": BOX,
        })
        self.df = mlb.player_games(D(2026, 10, 1), D(2026, 10, 1), http=api)
        self.calls = api.calls

    def player(self, name):
        return self.df.filter(pl.col("name") == name).row(0, named=True)

    def test_matches_the_stored_schema_and_skips_unplayed_games(self):
        self.assertEqual(dict(self.df.schema), mlb.SCHEMA)
        self.assertEqual(self.df["game_id"].unique().to_list(), [849844])
        self.assertNotIn("Unused Bullpen Arm", self.df["name"].to_list())
        self.assertIn("fields", self.calls[-1][1])                   # boxscores are trimmed

    def test_batting_line(self):
        turner = self.player("Trea Turner")
        self.assertEqual((turner["date"], turner["season"], turner["game_type"], turner["team"], turner["opponent"],
                          turner["home"], turner["position"], turner["order"]),
                         (D(2026, 10, 1), 2026, 3, "PHI", "ATL", False, "SS", 1))
        self.assertEqual((turner["hits"], turner["singles"], turner["home_runs"], turner["total_bases"],
                          turner["hits_runs_rbis"]), (2, 0, 1, 7, 5))
        self.assertIsNone(turner["outs"])

    def test_pitching_line(self):
        nola = self.player("Aaron Nola")
        self.assertEqual((nola["position"], nola["started"], nola["order"], nola["decision"]), ("P", True, None, "L"))
        self.assertEqual((nola["outs"], nola["strikeouts"], nola["hits_allowed"], nola["earned_runs"],
                          nola["walks_allowed"], nola["pitches"]), (5, 1, 2, 3, 2, 38))
        self.assertIsNone(nola["hits"])

    def test_a_substitute_is_filed_under_the_position_he_took(self):
        marsh = self.player("Brandon Marsh")
        self.assertEqual((marsh["position"], marsh["order"], marsh["walks"]), ("LF", None, 1))

    def test_a_position_player_who_pitched_stays_a_hitter(self):
        stott = self.player("Bryson Stott")
        self.assertEqual((stott["position"], stott["order"], stott["started"], stott["outs"]), ("2B", 8, False, 3))

    def test_a_two_way_player_keeps_one_position(self):
        two = self.player("Two Way")
        self.assertEqual((two["position"], two["order"], two["started"], two["hits"], two["strikeouts"], two["decision"]),
                         ("TWP", 3, True, 1, 9, "W"))

    def test_samples_are_starts(self):
        kept = set(mlb.samples(self.df)["name"])
        self.assertEqual(kept, {"Trea Turner", "Aaron Nola", "Bryson Stott", "Two Way"})


class CalendarTest(unittest.TestCase):
    def test_season_is_the_calendar_year_and_winter_belongs_to_the_last(self):
        self.assertEqual(mlb.season_id(D(2026, 10, 3)), 2026)
        self.assertEqual(mlb.season_id(D(2026, 3, 18)), 2026)
        self.assertEqual(mlb.season_id(D(2027, 1, 15)), 2026)
        self.assertEqual((mlb.previous_season(2026), mlb.season_label(2026)), (2025, "2026"))

    def test_schedule_leaves_out_postponed_games(self):
        def game(pk, state, coded, start):
            return {"gamePk": pk, "officialDate": "2026-10-03", "gameDate": start, "gameType": "D",
                    "status": {"abstractGameState": state, "codedGameState": coded},
                    "teams": {"away": {"team": {"abbreviation": "ATL", "id": 144}},
                              "home": {"team": {"abbreviation": "LAD", "id": 119}}}}
        api = FakeApi({"/schedule": {"dates": [{"games": [
            game(1, "Preview", "S", "2026-10-03T20:00:00Z"),
            game(2, "Final", "D", "2026-10-03T23:00:00Z"),
            game(3, "Live", "I", "2026-10-03T17:00:00Z"),
        ]}]}})
        listed = mlb.schedule(D(2026, 10, 3), http=api)
        self.assertEqual([(g["game_id"], g["away"], g["home"], g["state"]) for g in listed],
                         [(1, "ATL", "LAD", "upcoming"), (3, "ATL", "LAD", "live")])


class RostersTest(unittest.TestCase):
    def test_posted_lineup_or_active_hitters_plus_the_probable_starter(self):
        api = FakeApi({
            "/schedule": {"dates": [{"games": [{
                "gameType": "D", "lineups": {"homePlayers": [{"id": 11}, {"id": 12}]},
                "teams": {"away": {"team": {"abbreviation": "ATL", "id": 144}, "probablePitcher": {"id": 50}},
                          "home": {"team": {"abbreviation": "LAD", "id": 119}, "probablePitcher": {"id": 60}}},
            }]}]},
            "/teams/144/roster": {"roster": [
                {"person": {"id": 21}, "position": {"abbreviation": "1B"}},
                {"person": {"id": 22}, "position": {"abbreviation": "TWP"}},
                {"person": {"id": 51}, "position": {"abbreviation": "P"}},
            ]},
        })
        rosters = mlb.rosters(["ATL", "LAD"], D(2026, 10, 3), http=api)
        self.assertEqual(rosters, {"ATL": [21, 22, 50], "LAD": [11, 12, 60]})


class SamplesTest(unittest.TestCase):
    def kept(self, **over) -> bool:
        rows = game_logs([{"game_id": 1, "date": "2026-09-20", "season": 2026, "player_id": 1, **over}], mlb)
        return mlb.samples(rows).height == 1

    def test_starters_count_and_pinch_hitters_and_relievers_do_not(self):
        self.assertTrue(self.kept(position="SS", order=1))
        self.assertFalse(self.kept(position="PH"))
        self.assertTrue(self.kept(position="P", started=True))
        self.assertFalse(self.kept(position="P", started=False))


class PinnacleTest(unittest.TestCase):
    def test_the_words_after_total_name_the_prop(self):
        payload = pinnacle_payload([("Aaron Judge", "Bases", 1.5, 110, -140),
                                    ("Jacob Misiorowski", "Runs", 2.5, -105, -125)])
        payload["matchups"][1]["special"]["description"] = "Jacob Misiorowski Total Earned Runs"
        props, skipped = pinnacle.parse(payload, mlb)
        self.assertEqual([(p["player"], p["stat"]) for p in props],
                         [("Aaron Judge", "total_bases"), ("Jacob Misiorowski", "earned_runs")])
        self.assertFalse(skipped)


if __name__ == "__main__":
    unittest.main()
