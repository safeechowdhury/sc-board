import unittest

import polars as pl

from props import metrics, site
from props.leagues import nhl

from .helpers import game_logs


GROUPS = {position: group for position, (_, _, group) in nhl.POSITIONS.items()}


class TallyTest(unittest.TestCase):
    def test_counts_overs_and_unders(self):
        self.assertEqual(metrics.tally([0, 1, 2, 3, 4], 1.5), [3, 2, 5])

    def test_value_on_the_line_is_a_push(self):
        self.assertEqual(metrics.tally([1, 2, 3], 2), [1, 1, 3])

    def test_no_games(self):
        self.assertEqual(metrics.tally([], 0.5), [0, 0, 0])


class PropRowTest(unittest.TestCase):
    def setUp(self):
        self.games = {   # newest first
            "shots": [4, 0, 3, 5, 1, 2, 6],
            "saves": [None] * 7,
            "opponent": ["WPG", "DET", "WPG", "DET", "WPG", "DET", "WPG"],
            "season": [20262027] * 3 + [20252026] * 4,
            "game_type": [2] * 7,
        }

    def test_windows(self):
        row = metrics.prop_row(self.games, "shots", 2.5, "WPG", 20262027)
        self.assertEqual(row["l5"], [3, 2, 5])
        self.assertEqual(row["l10"], [4, 3, 7])          # only seven games exist
        self.assertEqual(row["season"], [2, 1, 3])       # 4, 0, 3
        self.assertEqual(row["h2h"], [3, 1, 4])          # vs WPG: 4, 3, 1, 6
        self.assertEqual(row["avg"], 3.0)

    def test_chart_reads_oldest_to_newest(self):
        row = metrics.prop_row(self.games, "shots", 2.5, "WPG", 20262027)
        self.assertEqual(row["last"], [6, 2, 1, 5, 3, 0, 4])

    def test_chart_keeps_only_the_last_ten(self):
        log = {"shots": list(range(12)), "opponent": ["WPG"] * 12, "season": [20262027] * 12, "game_type": [2] * 12}
        self.assertEqual(metrics.prop_row(log, "shots", 2.5, "WPG", 20262027)["last"], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0])

    def test_playoff_games_count_only_when_asked(self):
        self.games["game_type"] = [2, 2, 2, 3, 3, 2, 2]             # the 5 and the 1 were playoff games
        row = metrics.prop_row(self.games, "shots", 2.5, "WPG", 20262027)
        self.assertEqual((row["l5"], row["l10"], row["h2h"]), ([3, 2, 5], [3, 2, 5], [3, 0, 3]))
        self.assertEqual(row["last"], [6, 2, 3, 0, 4])
        row = metrics.prop_row(self.games, "shots", 2.5, "WPG", 20262027, playoffs=True)
        self.assertEqual((row["l10"], row["last"]), ([4, 3, 7], [6, 2, 1, 5, 3, 0, 4]))

    def test_stat_the_player_never_recorded(self):
        row = metrics.prop_row(self.games, "saves", 24.5, "WPG", 20262027)
        self.assertEqual(row["l10"], [0, 0, 0])
        self.assertIsNone(row["avg"])
        self.assertEqual(row["last"], [])


class TypicalLineTest(unittest.TestCase):
    def test_half_point_above_the_average_rounded_down(self):
        self.assertEqual(metrics.typical_line([3, 4, 3, 4]), 3.5)       # 3.5 a game
        self.assertEqual(metrics.typical_line([2, 3, 3, 3]), 2.5)       # 2.75 a game
        self.assertEqual(metrics.typical_line([26, 27, 30]), 27.5)

    def test_never_below_a_half(self):
        self.assertEqual(metrics.typical_line([0, 0, 1]), 0.5)
        self.assertEqual(metrics.typical_line([-1, -2]), 0.5)           # plus/minus



class RestTest(unittest.TestCase):
    def test_days_off_between_team_games(self):
        df = metrics.with_rest(game_logs([
            {"game_id": 1, "date": "2026-10-01", "player_id": 1, "team": "BOS", "opponent": "WPG"},
            {"game_id": 2, "date": "2026-10-02", "player_id": 1, "team": "BOS", "opponent": "DET"},
            {"game_id": 3, "date": "2026-10-05", "player_id": 1, "team": "BOS", "opponent": "NYR"},
            {"game_id": 4, "date": "2026-10-20", "player_id": 1, "team": "BOS", "opponent": "NYR"},
        ])).sort("date")
        # first game has no prior game; then a back-to-back, two days off, and a capped long break
        self.assertEqual(df["rest"].to_list(), [3, 0, 2, 3])

    def test_rest_follows_the_team_not_the_player(self):
        df = metrics.with_rest(game_logs([
            {"game_id": 1, "date": "2026-10-01", "player_id": 1, "team": "BOS", "opponent": "WPG"},
            {"game_id": 2, "date": "2026-10-02", "player_id": 2, "team": "BOS", "opponent": "DET"},
        ])).sort("date")
        self.assertEqual(df["rest"].to_list(), [3, 0])   # player 2 sat out game 1; the team still played


class SamplesTest(unittest.TestCase):
    def test_goalie_relief_appearances_are_dropped(self):
        df = game_logs([
            {"game_id": 1, "date": "2026-10-01", "player_id": 1, "position": "G", "started": True, "saves": 30},
            {"game_id": 2, "date": "2026-10-02", "player_id": 1, "position": "G", "started": False, "saves": 4},
            {"game_id": 2, "date": "2026-10-02", "player_id": 2, "position": "D", "shots": 2},
        ])
        kept = nhl.samples(df)
        self.assertEqual(kept["game_id"].to_list(), [1, 2])
        self.assertEqual(kept["player_id"].to_list(), [1, 2])


class TeammatesTest(unittest.TestCase):
    def setUp(self):
        # Player 1 plays all five BOS games. Player 2 misses game 3, then leaves for WPG.
        rows = [{"game_id": g, "date": f"2026-10-0{g}", "player_id": 1, "name": "Stayed", "team": "BOS",
                 "opponent": "DET"} for g in range(1, 6)]
        rows += [{"game_id": g, "date": f"2026-10-0{g}", "player_id": 2, "name": "Left", "team": team,
                  "opponent": "DET", "position": "R"} for g, team in [(1, "BOS"), (2, "BOS"), (4, "BOS"), (5, "WPG")]]
        df = game_logs(rows)
        self.logs = df.sort("date", "game_id", descending=True)
        self.index = site._player_index(nhl, df)

    def test_runs_alternate_together_and_apart_from_the_newest_game(self):
        self.assertEqual(metrics.runs([0, 1, 2, 5]), [3, 2, 1])
        self.assertEqual(metrics.runs([2, 3]), [0, 2, 2])
        self.assertEqual(metrics.runs([]), [0])

    def test_a_teammate_counts_only_for_games_on_the_same_team(self):
        mates = metrics.teammates(self.logs, self.logs, self.index, [1, 2])
        # newest first: apart in game 5 (he had left), together in 4, apart in 3, together in 2 and 1
        self.assertEqual(mates[1], [[2, "Left", "R", [0, 1, 1, 1, 2]]])
        self.assertEqual(mates[2], [[1, "Stayed", "C", [0, 1, 3]]])

    def test_only_the_players_asked_for(self):
        self.assertEqual(list(metrics.teammates(self.logs, self.logs, self.index, [2])), [2])

    def test_presence_comes_from_the_lineups_not_the_teammates_own_log(self):
        # player 2 counts as playing in game 3 for his teammates, though it is not in his own log
        there = game_logs([{"game_id": 3, "date": "2026-10-03", "player_id": 2, "name": "Left", "team": "BOS",
                            "opponent": "DET", "position": "R"}])
        mates = metrics.teammates(self.logs, pl.concat([self.logs, there]), self.index, [1, 2])
        self.assertEqual(mates[1], [[2, "Left", "R", [0, 1, 4]]])
        self.assertEqual(mates[2], [[1, "Stayed", "C", [0, 1, 3]]])        # his own games are unchanged


class OpenQuestionTest(unittest.TestCase):
    def test_a_line_the_player_never_clears_is_settled(self):
        self.assertFalse(metrics.is_open_question([0] * 20, 0.5))
        self.assertFalse(metrics.is_open_question([0] * 18 + [1, 1], 0.5))     # 10% over

    def test_a_line_the_player_always_clears_is_settled(self):
        self.assertFalse(metrics.is_open_question([3] * 20, 0.5))

    def test_either_side_at_fifteen_percent_keeps_it_open(self):
        self.assertTrue(metrics.is_open_question([0] * 17 + [1] * 3, 0.5))
        self.assertTrue(metrics.is_open_question([2, 3, 4, 1], 2.5))

    def test_no_games(self):
        self.assertFalse(metrics.is_open_question([], 0.5))


class DvpTest(unittest.TestCase):
    def setUp(self):
        # BOS centers shoot 4 a game against WPG and 2 against DET; a DET center shoots 1 against BOS.
        rows = []
        for game_id, team, opponent, shots in [(1, "BOS", "WPG", 4), (2, "BOS", "DET", 2), (2, "DET", "BOS", 1),
                                               (3, "BOS", "WPG", 4), (4, "BOS", "DET", 2)]:
            rows.append({"game_id": game_id, "date": f"2026-10-0{game_id}", "player_id": hash(team) % 1000,
                         "team": team, "opponent": opponent, "shots": shots})
        self.table = metrics.dvp(game_logs(rows), 20262027, GROUPS, site._dvp_stats(nhl)).filter(
            (pl.col("stat") == "shots") & (pl.col("group") == "C"))

    def row(self, team):
        return self.table.filter(pl.col("team") == team).row(0, named=True)

    def test_allowed_is_what_opponents_did_against_the_team(self):
        self.assertEqual(self.row("WPG")["allowed"], 4)
        self.assertEqual(self.row("DET")["allowed"], 2)

    def test_delta_is_relative_to_the_opponents_own_average(self):
        self.assertEqual(self.row("WPG")["delta"], 1)    # BOS centers average 3, got 4
        self.assertEqual(self.row("DET")["delta"], -1)
        self.assertEqual(self.row("BOS")["delta"], 0)    # DET's only game, so exactly their average

    def test_rank_one_is_the_stingiest(self):
        ranks = {team: self.row(team)["rank"] for team in ("DET", "BOS", "WPG")}
        self.assertEqual(ranks, {"DET": 1, "BOS": 2, "WPG": 3})

    def test_last_season_does_not_count(self):
        table = metrics.dvp(game_logs([
            {"game_id": 1, "date": "2026-04-10", "season": 20252026, "player_id": 1, "team": "BOS", "opponent": "WPG", "shots": 9},
            {"game_id": 2, "date": "2026-10-01", "player_id": 1, "team": "BOS", "opponent": "DET", "shots": 2},
        ]), 20262027, GROUPS, site._dvp_stats(nhl))
        self.assertEqual(table["team"].unique().to_list(), ["DET"])

    def test_wingers_share_a_group_and_goalie_stats_stay_with_goalies(self):
        table = metrics.dvp(game_logs([
            {"game_id": 1, "date": "2026-10-01", "player_id": 1, "team": "BOS", "opponent": "WPG", "position": "L", "shots": 2},
            {"game_id": 1, "date": "2026-10-01", "player_id": 2, "team": "BOS", "opponent": "WPG", "position": "R", "shots": 3},
            {"game_id": 1, "date": "2026-10-01", "player_id": 3, "team": "BOS", "opponent": "WPG", "position": "G", "saves": 25},
        ]), 20262027, GROUPS, site._dvp_stats(nhl))
        wings = table.filter((pl.col("stat") == "shots") & (pl.col("group") == "W")).row(0, named=True)
        self.assertEqual(wings["allowed"], 5)
        self.assertEqual(set(table.filter(pl.col("group") == "G")["stat"]), {"saves", "goals_against", "shots_against"})
        self.assertNotIn("saves", set(table.filter(pl.col("group") == "W")["stat"]))


if __name__ == "__main__":
    unittest.main()
