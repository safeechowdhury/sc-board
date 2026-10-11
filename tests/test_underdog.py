import unittest
from unittest import mock

import polars as pl

from props import underdog
from props.leagues import nfl, nhl

from .helpers import underdog_payload

BOTH = [("higher", 2.0, 1.07), ("lower", 1.66, 0.89)]


class ParseTest(unittest.TestCase):
    def test_line_with_both_sides(self):
        lines, skipped = underdog.parse(underdog_payload([
            {"name": "David Pastrnak", "team": "BOS", "stat": "shots", "value": 3.5, "options": BOTH},
        ]), nhl)
        self.assertEqual(lines, [{
            "player": "David Pastrnak", "team": "BOS", "opponent": "WPG", "home": False, "position": "C",
            "stat": "shots", "line": 3.5,
            "over": {"odds": 2.0, "mult": 1.07}, "under": {"odds": 1.66, "mult": 0.89},
            "start": "2026-10-03T00:00:00Z",
        }])
        self.assertFalse(skipped)

    def test_home_player_and_stat_renames(self):
        lines, _ = underdog.parse(underdog_payload([
            {"name": "Mark Scheifele", "team": "WPG", "stat": "power_play_points", "value": 0.5, "options": BOTH},
            {"name": "Mark Scheifele", "team": "WPG", "stat": "faceoffs_won", "value": 9.5, "options": BOTH},
        ]), nhl)
        self.assertEqual([(l["team"], l["opponent"], l["home"], l["stat"]) for l in lines],
                         [("WPG", "BOS", True, "ppp"), ("WPG", "BOS", True, "faceoff_wins")])

    def test_one_sided_line_has_no_under(self):
        lines, _ = underdog.parse(underdog_payload([
            {"name": "David Pastrnak", "team": "BOS", "stat": "period_first_goal_scorer", "value": 0.5,
             "options": [("higher", 9.63, 5.15)]},
        ]), nhl)
        self.assertEqual(lines[0]["stat"], "first_goals")
        self.assertIsNone(lines[0]["under"])

    def test_skips_other_sports_suspended_lines_and_untracked_stats(self):
        lines, skipped = underdog.parse(underdog_payload([
            {"name": "Some Pitcher", "team": "BOS", "stat": "shots", "value": 1.5, "options": BOTH, "sport": "MLB"},
            {"name": "David Pastrnak", "team": "BOS", "stat": "shots", "value": 3.5, "options": BOTH, "status": "suspended"},
            {"name": "David Pastrnak", "team": "BOS", "stat": "fantasy_points", "value": 9.5, "options": BOTH},
        ]), nhl)
        self.assertEqual(lines, [])
        self.assertEqual(dict(skipped), {"Fantasy Points": 1})

    def test_team_abbreviations_are_normalized(self):
        payload = underdog_payload([{"name": "Jack Hughes", "team": "nj", "stat": "points", "value": 0.5, "options": BOTH}])
        payload["games"][0].update(title="NJ @ LA", away_team_id="nj", home_team_id="la")
        lines, _ = underdog.parse(payload, nhl)
        self.assertEqual((lines[0]["team"], lines[0]["opponent"]), ("NJD", "LAK"))


    def test_missing_odds_keep_the_multiplier(self):
        payload = underdog_payload([{"name": "Jack Hughes", "team": "BOS", "stat": "points", "value": 0.5,
                                     "options": [("higher", 1.87, 1.0)]}])
        del payload["over_under_lines"][0]["options"][0]["odds"]
        lines, _ = underdog.parse(payload, nhl)
        self.assertEqual(lines[0]["over"], {"odds": None, "mult": 1.0})


    def test_each_league_reads_only_its_own_sport_and_stats(self):
        payload = underdog_payload([
            {"name": "Patrick Mahomes", "team": "BOS", "stat": "passing_yds", "value": 236.5, "options": BOTH, "sport": "NFL"},
            {"name": "David Pastrnak", "team": "BOS", "stat": "shots", "value": 3.5, "options": BOTH},
        ])
        football, _ = underdog.parse(payload, nfl)
        self.assertEqual([(l["player"], l["stat"], l["line"]) for l in football], [("Patrick Mahomes", "pass_yds", 236.5)])
        hockey, _ = underdog.parse(payload, nhl)
        self.assertEqual([l["player"] for l in hockey], ["David Pastrnak"])


class FixedLineTest(unittest.TestCase):
    def test_a_td_line_at_1_5_is_two_tds_and_others_stay_tds(self):
        lines, _ = underdog.parse(underdog_payload([
            {"name": "Puka Nacua", "team": "BOS", "stat": "rush_rec_tds", "value": value, "sport": "NFL",
             "options": [("higher", 2.0, 1.0)]} for value in (0.5, 1.5, 2.5)
        ]), nfl)
        self.assertEqual([(line["stat"], line["line"]) for line in lines],
                         [("rush_rec_tds", 0.5), ("two_tds", 1.5), ("rush_rec_tds", 2.5)])

    def test_leagues_without_fixed_lines_are_untouched(self):
        self.assertEqual(underdog.fixed_stat(nhl, "goals", 1.5), "goals")


class SportsTest(unittest.TestCase):
    def test_every_sport_with_lines_most_first_named_as_underdog_names_it(self):
        payload = underdog_payload([
            {"name": "A One", "team": "BOS", "stat": "goals", "value": 0.5, "sport": "NHL", "options": BOTH},
            {"name": "B Two", "team": "BOS", "stat": "goals", "value": 0.5, "sport": "FIFA", "options": BOTH},
            {"name": "C Three", "team": "BOS", "stat": "goals", "value": 0.5, "sport": "FIFA", "options": BOTH},
            {"name": "D Four", "team": "BOS", "stat": "goals", "value": 0.5, "sport": "MLB", "status": "suspended",
             "options": BOTH},
        ])
        payload["games"][0].update(sport_id="FIFA", sport_name="Soccer")
        with mock.patch.object(underdog, "fetch", return_value=payload) as fetch:
            self.assertEqual(underdog.sports(), [["FIFA", "Soccer", 2], ["NHL", "NHL", 1]])
        fetch.assert_called_once_with("")   # no sport_id: every sport at once


class MatchTest(unittest.TestCase):
    players = pl.DataFrame({
        "player_id": [1, 2, 3, 4, 5, 6],
        "name": ["Tim Stützle", "Sebastian Aho", "Sebastian Aho", "Mitchell Marner", "Anthony-John (AJ) Greer", "J.T. Miller"],
        "team": ["OTT", "CAR", "PIT", "VGK", "FLA", "NYR"],
    })

    def match(self, name, team):
        line = {"player": name, "team": team}
        unmatched = underdog.match_players([line], self.players)
        return line["player_id"], unmatched

    def test_accents_and_punctuation(self):
        self.assertEqual(self.match("Tim Stutzle", "OTT"), (1, []))
        self.assertEqual(self.match("JT Miller", "NYR"), (6, []))

    def test_shared_name_is_split_by_team(self):
        self.assertEqual(self.match("Sebastian Aho", "PIT")[0], 3)
        self.assertEqual(self.match("Sebastian Aho", "CAR")[0], 2)

    def test_exact_name_survives_a_trade(self):
        self.assertEqual(self.match("Tim Stutzle", "BOS")[0], 1)

    def test_short_first_name_falls_back_to_surname_and_team(self):
        self.assertEqual(self.match("Mitch Marner", "VGK")[0], 4)
        self.assertIsNone(self.match("Mitch Marner", "TOR")[0])   # surname alone is not enough

    def test_teammate_with_the_same_surname_is_a_different_player(self):
        self.assertEqual(self.match("Mark Marner", "VGK"), (None, ["Mark Marner"]))
        self.assertIsNone(self.match("Tom Stutzle", "OTT")[0])

    def test_nickname_from_the_end_of_the_name(self):
        players = pl.DataFrame({"player_id": [7], "name": ["Andrew Ogletree"], "team": ["IND"]})
        line = {"player": "Drew Ogletree", "team": "IND"}
        self.assertEqual((underdog.match_players([line], players), line["player_id"]), ([], 7))

    def test_given_name_swap(self):
        players = pl.DataFrame({"player_id": [8], "name": ["Tommy Novak"], "team": ["PIT"]})
        line = {"player": "Thomas Novak", "team": "PIT"}
        self.assertEqual((underdog.match_players([line], players), line["player_id"]), ([], 8))
        self.assertEqual(underdog.norm_name("Tom Novak"), underdog.norm_name("Thomas Novak"))

    def test_nhl_nickname_in_parentheses(self):
        self.assertEqual(self.match("A.J. Greer", "ANA")[0], 5)

    def test_unknown_player_is_reported(self):
        self.assertEqual(self.match("Viggo Bjorck", "WPG"), (None, ["Viggo Bjorck"]))


if __name__ == "__main__":
    unittest.main()
