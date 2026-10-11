import datetime as dt
import unittest
from unittest import mock

import polars as pl

from props.leagues import nfl

from .helpers import game_logs

D = dt.date


def stat_row(**over) -> dict:
    row = {"game_id": "2026_03_CLE_TB", "player_id": "00-1", "player_display_name": "Baker Mayfield",
           "position": "QB", "team": "TB", "headshot_url": "https://img/mayfield",
           **{source: 0 for source in nfl.STAT_COLUMNS}}
    return {**row, **over}


def snap_row(pfr: str, team: str, offense: int, share: float, game: str = "2026_03_CLE_TB") -> dict:
    return {"game_id": game, "pfr_player_id": pfr, "player": "x", "position": "x", "team": team,
            "offense_snaps": float(offense), "offense_pct": share}


PLAYERS = pl.DataFrame([
    {"gsis_id": "00-1", "pfr_id": "MayfBa00", "display_name": "Baker Mayfield", "position": "QB", "headshot": "https://img/mayfield"},
    {"gsis_id": "00-2", "pfr_id": "JeudJe00", "display_name": "Jerry Jeudy", "position": "WR", "headshot": "https://img/jeudy"},
    {"gsis_id": "00-3", "pfr_id": "McLaCh00", "display_name": "Chase McLaughlin", "position": "K", "headshot": None},
    {"gsis_id": "00-4", "pfr_id": "NacuPu00", "display_name": "Puka Nacua", "position": "WR", "headshot": None},
    {"gsis_id": "00-5", "pfr_id": "WirfTr00", "display_name": "Tristan Wirfs", "position": "T", "headshot": None},
])
SCHEDULE = pl.DataFrame([
    {"game_id": "2026_03_CLE_TB", "season": 2026, "date": D(2026, 9, 20), "start": "2026-09-20T17:00:00Z",
     "game_type": 2, "away": "CLE", "home": "TB", "away_qb": "00-8", "home_qb": "00-1", "final": True},
    {"game_id": "2026_20_LAR_PHI", "season": 2026, "date": D(2027, 1, 24), "start": "2027-01-24T20:00:00Z",
     "game_type": 3, "away": "LAR", "home": "PHI", "away_qb": None, "home_qb": None, "final": True},
], schema_overrides={"season": pl.Int32, "game_type": pl.Int8})


class AssembleTest(unittest.TestCase):
    def setUp(self):
        stats = pl.DataFrame([
            stat_row(attempts=30, completions=20, passing_yards=250, passing_tds=2, carries=3, rushing_yards=12, rushing_tds=1),
            stat_row(player_id="00-3", player_display_name="Chase McLaughlin", position="K", fg_made=2, pat_made=3),
            stat_row(player_id="00-4", player_display_name="Puka Nacua", position="WR", team="LA", game_id="2026_20_LAR_PHI",
                     targets=9, receptions=7, receiving_yards=101, receiving_tds=1),
            stat_row(player_id="00-9", player_display_name="A Linebacker", position="LB"),
        ])
        snaps = pl.DataFrame([
            snap_row("MayfBa00", "TB", 60, 1.0),
            snap_row("JeudJe00", "CLE", 36, 0.51),       # played half the game, never targeted
            snap_row("McLaCh00", "TB", 0, 0.0),
            snap_row("WirfTr00", "TB", 60, 1.0),         # a tackle: not a position the board tracks
            snap_row("NacuPu00", "LA", 55, 0.9, "2026_20_LAR_PHI"),
        ])
        self.df = nfl.assemble(stats, snaps, PLAYERS, SCHEDULE)

    def player(self, name):
        return self.df.filter(pl.col("name") == name).row(0, named=True)

    def test_matches_the_stored_schema_and_tracked_positions(self):
        self.assertEqual(dict(self.df.schema), nfl.SCHEMA)
        self.assertEqual(sorted(self.df["name"]), ["Baker Mayfield", "Chase McLaughlin", "Jerry Jeudy", "Puka Nacua"])

    def test_stat_row_gets_its_game_and_snaps(self):
        qb = self.player("Baker Mayfield")
        self.assertEqual((qb["date"], qb["season"], qb["game_type"], qb["team"], qb["opponent"], qb["home"]),
                         (D(2026, 9, 20), 2026, 2, "TB", "CLE", True))
        self.assertEqual((qb["pass_att"], qb["pass_cmp"], qb["pass_yds"], qb["pass_tds"], qb["snaps"]), (30, 20, 250, 2, 60))

    def test_combined_stats(self):
        qb = self.player("Baker Mayfield")
        self.assertEqual((qb["pass_rush_yds"], qb["rush_rec_tds"], qb["rush_rec_yds"]), (262, 1, 12))
        self.assertEqual(self.player("Chase McLaughlin")["kicking_points"], 9)

    def test_a_player_who_took_snaps_without_a_stat_gets_a_zero_game(self):
        jeudy = self.player("Jerry Jeudy")
        self.assertEqual((jeudy["player_id"], jeudy["position"], jeudy["team"], jeudy["opponent"], jeudy["home"]),
                         ("00-2", "WR", "CLE", "TB", False))
        self.assertEqual((jeudy["snaps"], jeudy["targets"], jeudy["receptions"], jeudy["rec_yds"], jeudy["rush_rec_tds"]),
                         (36, 0, 0, 0, 0))
        self.assertEqual(jeudy["headshot"], "https://img/jeudy")

    def test_started_marks_the_quarterback_the_schedule_lists(self):
        self.assertTrue(self.player("Baker Mayfield")["started"])
        self.assertFalse(self.player("Jerry Jeudy")["started"])
        self.assertIsNone(self.player("Puka Nacua")["started"])           # the schedule names no one for that game

    def test_kicker_snaps_are_blank_not_zero(self):
        self.assertIsNone(self.player("Chase McLaughlin")["snaps"])

    def test_rams_code_and_playoff_game_type(self):
        puka = self.player("Puka Nacua")
        self.assertEqual((puka["team"], puka["opponent"], puka["home"], puka["game_type"]), ("LAR", "PHI", False, 3))


class SamplesTest(unittest.TestCase):
    def rows(self, **over):
        return game_logs([{"game_id": "g", "date": "2026-09-20", "season": 2026, "player_id": "00-1", **over}], nfl)

    def kept(self, **over) -> bool:
        return nfl.samples(self.rows(**over)).height == 1

    def test_any_offensive_snap_counts_for_a_skill_player(self):
        self.assertTrue(self.kept(position="WR", snaps=2, snap_pct=0.03))

    def test_special_teams_only_does_not(self):
        self.assertFalse(self.kept(position="WR", snaps=0, snap_pct=0.0))

    def test_quarterback_needs_half_the_snaps(self):
        self.assertTrue(self.kept(position="QB", snaps=40, snap_pct=0.6, pass_att=22))
        self.assertFalse(self.kept(position="QB", snaps=6, snap_pct=0.09, pass_att=3))

    def test_a_starter_who_left_hurt_still_played_with_his_teammates(self):
        hurt = dict(position="QB", snaps=5, snap_pct=0.08, pass_att=2, started=True)
        self.assertFalse(self.kept(**hurt))                               # not a game for his own props
        self.assertEqual(nfl.lineups(self.rows(**hurt)).height, 1)

    def test_a_mop_up_series_is_not_playing_with_them(self):
        self.assertEqual(nfl.lineups(self.rows(position="QB", snaps=6, snap_pct=0.09, pass_att=3, started=False)).height, 0)
        self.assertEqual(nfl.lineups(self.rows(position="QB", snaps=6, snap_pct=0.09, pass_att=3)).height, 0)

    def test_a_listed_starter_who_never_took_a_snap_did_not_play(self):
        self.assertEqual(nfl.lineups(self.rows(position="QB", snaps=0, snap_pct=0.0, started=True)).height, 0)

    def test_the_reliever_who_took_most_of_the_snaps_played_too(self):
        self.assertEqual(nfl.lineups(self.rows(position="QB", snaps=58, snap_pct=0.92, pass_att=30, started=False)).height, 1)

    def test_without_snap_counts_the_stat_row_stands(self):
        self.assertTrue(self.kept(position="K"))
        self.assertTrue(self.kept(position="QB", pass_att=31))
        self.assertFalse(self.kept(position="QB", pass_att=2))


class CalendarTest(unittest.TestCase):
    def test_season_runs_into_the_new_year(self):
        self.assertEqual(nfl.season_id(D(2026, 10, 2)), 2026)
        self.assertEqual(nfl.season_id(D(2027, 2, 7)), 2026)
        self.assertEqual(nfl.season_id(D(2027, 8, 20)), 2027)
        self.assertEqual((nfl.previous_season(2026), nfl.season_label(2026)), (2025, "2026"))

    def test_schedule_lists_the_week_ahead(self):
        games = pl.DataFrame([
            {"date": D(2026, 10, 1), "start": "2026-10-02T00:15:00Z", "away": "PIT", "home": "CLE", "final": True},
            {"date": D(2026, 10, 4), "start": "2026-10-04T17:00:00Z", "away": "LAR", "home": "PHI", "final": False},
            {"date": D(2026, 10, 8), "start": "2026-10-09T00:15:00Z", "away": "TB", "home": "DAL", "final": False},
            {"date": D(2026, 10, 11), "start": "2026-10-11T17:00:00Z", "away": "PHI", "home": "JAX", "final": False},
        ])
        with mock.patch.object(nfl, "games", return_value=games):
            listed = nfl.schedule(D(2026, 10, 2))
        self.assertEqual([(g["date"], g["away"], g["home"], g["state"]) for g in listed],
                         [("2026-10-04", "LAR", "PHI", "upcoming"), ("2026-10-08", "TB", "DAL", "upcoming")])

    def test_kickoff_is_converted_from_eastern_to_utc(self):
        raw = pl.DataFrame([{"game_id": "2026_04_ATL_NO", "season": 2026, "game_type": "REG", "gameday": "2026-10-05",
                             "gametime": "20:15", "away_team": "ATL", "home_team": "NO", "home_score": None,
                             "away_qb_id": "00-6", "home_qb_id": None},
                            {"game_id": "2026_19_LA_SEA", "season": 2026, "game_type": "DIV", "gameday": "2027-01-17",
                             "gametime": "16:30", "away_team": "LA", "home_team": "SEA", "home_score": 24,
                             "away_qb_id": "00-7", "home_qb_id": "00-8"}],
                           schema_overrides={"home_score": pl.Int32})
        with mock.patch.object(nfl, "_download", return_value=raw):
            games = nfl.games(http=object()).rows(named=True)
        self.assertEqual((games[0]["start"], games[0]["date"], games[0]["game_type"], games[0]["final"]),
                         ("2026-10-06T00:15:00Z", D(2026, 10, 5), 2, False))
        self.assertEqual((games[1]["start"], games[1]["away"], games[1]["game_type"], games[1]["final"]),
                         ("2027-01-17T21:30:00Z", "LAR", 3, True))
        self.assertEqual((games[0]["away_qb"], games[0]["home_qb"], games[1]["home_qb"]), ("00-6", None, "00-8"))


if __name__ == "__main__":
    unittest.main()
