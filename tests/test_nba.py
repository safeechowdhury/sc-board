import datetime as dt
import unittest
from unittest import mock

from props.leagues import nba

KEYS = ["minutes", "points", "fieldGoalsMade-fieldGoalsAttempted",
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "freeThrowsMade-freeThrowsAttempted",
        "rebounds", "assists", "turnovers", "steals", "blocks", "offensiveRebounds", "defensiveRebounds",
        "fouls", "plusMinus"]


def athlete(pid, name, stats, starter=True, dnp=False, position="F"):
    return {"starter": starter, "didNotPlay": dnp, "stats": stats,
            "athlete": {"id": pid, "displayName": name, "position": {"abbreviation": position}}}


def scoreboard(*events):
    return {"events": [
        {"id": eid, "date": "2026-04-15T23:30Z", "season": {"year": 2026, "type": kind},
         "status": {"type": {"state": state}},
         "competitions": [{"competitors": [{"homeAway": "home", "team": {"abbreviation": home}},
                                           {"homeAway": "away", "team": {"abbreviation": away}}]}]}
        for eid, away, home, kind, state in events]}


SUMMARY = {"boxscore": {"players": [
    {"team": {"abbreviation": "GS"}, "statistics": [{"keys": KEYS, "athletes": [
        athlete("1", "Stephen Curry", ["34", "31", "10-21", "6-13", "5-5", "5", "10", "3", "2", "0", "0", "5", "2", "+8"],
                position="G"),
        athlete("2", "Bench Guy", [], starter=False, dnp=True),
        athlete("3", "Garbage Time", ["0", "0", "0-0", "0-0", "0-0", "0", "0", "0", "0", "0", "0", "0", "0", "0"],
                starter=False),
    ]}]},
    {"team": {"abbreviation": "SA"}, "statistics": [{"keys": KEYS, "athletes": [
        athlete("4", "Victor Wembanyama", ["36", "28", "11-19", "2-6", "4-5", "14", "3", "4", "1", "6", "3", "11", "3", "-8"],
                position="C"),
    ]}]},
]}}


class PlayerGamesTest(unittest.TestCase):
    def games(self, events):
        def get(http, path, params=None):
            return scoreboard(*events) if path == "scoreboard" else SUMMARY
        with mock.patch.object(nba, "_get", side_effect=get):
            return nba.player_games(dt.date(2026, 4, 15), dt.date(2026, 4, 15), http=object())

    def test_box_score_rows_with_combos_and_team_codes(self):
        df = self.games([("e1", "SA", "GS", 2, "post")])
        self.assertEqual(df["name"].to_list(), ["Stephen Curry", "Victor Wembanyama"])   # no DNP, no 0-minute cameo
        curry = df.row(0, named=True)
        self.assertEqual((curry["team"], curry["opponent"], curry["home"], curry["season"], curry["game_type"]),
                         ("GSW", "SAS", True, 20252026, 2))
        self.assertEqual((curry["points"], curry["threes"], curry["threes_att"], curry["fga"], curry["plus_minus"]),
                         (31, 6, 13, 21, 8))
        self.assertEqual((curry["pts_rebs_asts"], curry["double_double"], curry["triple_double"]), (46, 1, 0))
        wemby = df.row(1, named=True)
        self.assertEqual((wemby["blks_stls"], wemby["double_double"], wemby["plus_minus"]), (7, 1, -8))

    def test_play_in_counts_as_playoffs_and_preseason_and_unfinished_games_are_skipped(self):
        df = self.games([("e1", "SA", "GS", 5, "post"), ("e2", "SA", "GS", 1, "post"), ("e3", "SA", "GS", 2, "in")])
        self.assertEqual(df["game_id"].unique().to_list(), ["e1"])
        self.assertEqual(df["game_type"].unique().to_list(), [3])

    def test_exhibitions_against_non_nba_teams_are_left_off(self):
        def get(http, path, params=None):
            return scoreboard(("e1", "LON", "POR", 1, "pre"), ("e2", "SA", "GS", 1, "pre"))
        with mock.patch.object(nba, "_get", side_effect=get):
            games = nba.schedule(dt.date(2026, 10, 10), http=object())
        self.assertEqual([(g["away"], g["home"], g["state"]) for g in games], [("SAS", "GSW", "upcoming")])


class SeasonTest(unittest.TestCase):
    def test_season_ids_and_labels(self):
        self.assertEqual(nba.season_id(dt.date(2026, 10, 21)), 20262027)
        self.assertEqual(nba.season_id(dt.date(2026, 6, 13)), 20252026)
        self.assertEqual((nba.previous_season(20262027), nba.season_label(20262027)), (20252026, "26/27"))


if __name__ == "__main__":
    unittest.main()
