import datetime as dt
import re
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from props.leagues import nhl
from props.slate import board_date, fetch_board_date, slate_date

D = dt.date


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": self.data}


class FakeStats:
    """Stands in for httpx.Client: answers each stats report from a dict."""

    def __init__(self, reports):
        self.reports = reports
        self.calls = []

    def get(self, url, params=None):
        report = url.removeprefix(nhl.STATS_URL + "/")
        self.calls.append((report, params["cayenneExp"]))
        return FakeResponse(self.reports.get(report, []))


IDS = {"gameId": 2026020009, "gameDate": "2026-10-01", "teamAbbrev": "NJD", "opponentTeamAbbrev": "PHI", "homeRoad": "H"}
REPORTS = {
    "skater/summary": [{**IDS, "playerId": 10, "skaterFullName": "Jack Hughes", "positionCode": "C",
                        "timeOnIcePerGame": 1204.0, "goals": 1, "assists": 1, "points": 2, "shots": 5,
                        "plusMinus": 1, "penaltyMinutes": 0, "ppGoals": 0, "ppPoints": 1}],
    "skater/realtime": [{"gameId": 2026020009, "playerId": 10, "hits": 1, "blockedShots": 0, "giveaways": 2,
                         "takeaways": 1, "missedShots": 2, "totalShotAttempts": 9, "firstGoals": 1}],
    "skater/faceoffwins": [{"gameId": 2026020009, "playerId": 10, "totalFaceoffWins": 7, "totalFaceoffLosses": 6}],
    "skater/timeonice": [{"gameId": 2026020009, "playerId": 10, "shifts": 22, "ppTimeOnIce": 188}],
    "goalie/summary": [{**IDS, "playerId": 20, "goalieFullName": "Jake Allen", "timeOnIce": 3803, "saves": 23,
                        "shotsAgainst": 25, "goalsAgainst": 2, "gamesStarted": 1, "wins": 1, "losses": 0, "otLosses": 0},
                       {**IDS, "gameId": 2025030111, "gameDate": "2026-04-20", "homeRoad": "R", "playerId": 21,
                        "goalieFullName": "Relief Goalie", "timeOnIce": 600, "saves": 4, "shotsAgainst": 4,
                        "goalsAgainst": 0, "gamesStarted": 0, "wins": 0, "losses": 0, "otLosses": 0}],
}


class PlayerGamesTest(unittest.TestCase):
    def setUp(self):
        self.df = nhl.player_games(D(2026, 4, 20), D(2026, 10, 1), http=FakeStats(REPORTS))

    def player(self, pid):
        return self.df.filter(pl.col("player_id") == pid).row(0, named=True)

    def test_matches_the_stored_schema(self):
        self.assertEqual(dict(self.df.schema), nhl.SCHEMA)
        self.assertEqual(self.df.height, 3)

    def test_skater_reports_are_joined(self):
        hughes = self.player(10)
        self.assertEqual((hughes["name"], hughes["team"], hughes["opponent"], hughes["home"]),
                         ("Jack Hughes", "NJD", "PHI", True))
        self.assertEqual((hughes["shots"], hughes["ppp"], hughes["first_goals"], hughes["faceoff_wins"],
                          hughes["shot_attempts"], hughes["shifts"], hughes["toi"], hughes["pp_toi"]),
                         (5, 1, 1, 7, 9, 22, 1204, 188))
        self.assertIsNone(hughes["saves"])

    def test_goalie_rows(self):
        allen = self.player(20)
        self.assertEqual((allen["position"], allen["saves"], allen["goals_against"], allen["started"], allen["decision"]),
                         ("G", 23, 2, True, "W"))
        relief = self.player(21)
        self.assertEqual((relief["started"], relief["decision"], relief["home"]), (False, None, False))

    def test_season_and_game_type_come_from_the_game_id(self):
        self.assertEqual((self.player(10)["season"], self.player(10)["game_type"]), (20262027, 2))
        self.assertEqual((self.player(21)["season"], self.player(21)["game_type"]), (20252026, 3))

    def test_no_games_returns_an_empty_frame_in_schema(self):
        df = nhl.player_games(D(2026, 8, 1), D(2026, 8, 5), http=FakeStats({}))
        self.assertEqual((df.height, dict(df.schema)), (0, nhl.SCHEMA))


class ReportTest(unittest.TestCase):
    def test_truncated_response_is_split_and_refetched(self):
        class Capped:
            def __init__(self):
                self.ranges = []

            def get(self, url, params=None):
                window = tuple(re.findall(r"\d{4}-\d\d-\d\d", params["cayenneExp"]))
                self.ranges.append(window)
                # the full fortnight overflows; each half fits
                return FakeResponse([{}] * (3 if window == ("2026-10-01", "2026-10-14") else 1))

        http = Capped()
        with mock.patch.object(nhl, "ROW_CAP", 3):
            rows = nhl._report(http, "skater/summary", D(2026, 10, 1), D(2026, 10, 14))
        self.assertEqual(len(rows), 2)
        self.assertEqual(http.ranges, [("2026-10-01", "2026-10-14"), ("2026-10-01", "2026-10-07"), ("2026-10-08", "2026-10-14")])

    def test_only_regular_season_and_playoffs_are_requested(self):
        http = FakeStats({})
        nhl._report(http, "goalie/summary", D(2026, 10, 1), D(2026, 10, 1))
        self.assertEqual(http.calls, [("goalie/summary",
            'gameDate>="2026-10-01" and gameDate<="2026-10-01 23:59:59" and gameTypeId in (2,3)')])


class DatesTest(unittest.TestCase):
    def test_windows_cover_the_range_without_overlap(self):
        windows = list(nhl._windows(D(2026, 10, 1), D(2026, 10, 31)))
        self.assertEqual(windows, [(D(2026, 10, 1), D(2026, 10, 14)), (D(2026, 10, 15), D(2026, 10, 28)),
                                   (D(2026, 10, 29), D(2026, 10, 31))])
        self.assertEqual(list(nhl._windows(D(2026, 10, 2), D(2026, 10, 1))), [])

    def test_season_id(self):
        self.assertEqual(nhl.season_id(D(2026, 10, 2)), 20262027)
        self.assertEqual(nhl.season_id(D(2027, 6, 10)), 20262027)
        self.assertEqual(nhl.season_id(D(2026, 9, 1)), 20262027)

    def test_slate_rolls_over_at_5am_eastern(self):
        eastern = ZoneInfo("America/New_York")
        self.assertEqual(slate_date(dt.datetime(2026, 10, 3, 1, 30, tzinfo=eastern)), D(2026, 10, 2))
        self.assertEqual(slate_date(dt.datetime(2026, 10, 3, 5, 0, tzinfo=eastern)), D(2026, 10, 3))
        self.assertEqual(slate_date(dt.datetime(2026, 10, 3, 6, 0, tzinfo=dt.timezone.utc)), D(2026, 10, 2))   # 2 AM Eastern

    def test_slate_rolls_over_early_once_every_game_is_final(self):
        eastern = ZoneInfo("America/New_York")
        night, evening = dt.datetime(2026, 10, 3, 1, 30, tzinfo=eastern), dt.datetime(2026, 10, 2, 23, 30, tzinfo=eastern)
        game = lambda date, state: {"date": date, "state": state}
        done = [game("2026-10-02", "final"), game("2026-10-02", "final"), game("2026-10-03", "upcoming")]
        self.assertEqual(board_date(done, night), D(2026, 10, 3))
        self.assertEqual(board_date(done[:1] + [game("2026-10-02", "live")], night), D(2026, 10, 2))
        self.assertEqual(board_date([game("2026-10-04", "upcoming")], night), D(2026, 10, 3))   # no games last night
        self.assertEqual(board_date([], night), D(2026, 10, 2))                    # no schedule: wait for 5 AM
        self.assertEqual(board_date(done, evening), D(2026, 10, 2))                # never ahead of the calendar

    def test_schedule_is_only_asked_after_midnight_and_a_failure_waits(self):
        eastern = ZoneInfo("America/New_York")
        asked = []
        def schedule(day):
            asked.append(day)
            raise httpx.ConnectError("down")
        self.assertEqual(fetch_board_date(schedule, dt.datetime(2026, 10, 3, 1, 30, tzinfo=eastern)), D(2026, 10, 2))
        self.assertEqual(fetch_board_date(schedule, dt.datetime(2026, 10, 3, 9, 0, tzinfo=eastern)), D(2026, 10, 3))
        self.assertEqual(asked, [D(2026, 10, 2)])


if __name__ == "__main__":
    unittest.main()
